#!/usr/bin/env python3
"""Inspección DETERMINISTA del "What to build" de las Build tasks de un proyecto SQX.

Responde por chat/CLI a "¿cómo está configurado el Build X del proyecto Y?" sin abrir
la GUI: lee el XML de cada Build task dentro del `project.cfx` y evalúa los knobs del
"What to build" definidos en `builder_registry.py` (fuente única concepto→XML, con sus
gates: un knob INERTE — p.ej. un rango de SL cuyo toggle está OFF — no se reporta como
config activa).

Qué reporta por Build task (agrupado):
  - Motor: generationType (genético/random) + hiperparámetros genéticos activos.
  - StopCondition: condición de parada del build (time-limit con días/horas/minutos,
    o passed-count con N estrategias) — pseudo-knobs `stop.*`, inertes fuera de su tipo.
  - Complejidad: nº de condiciones de entrada/salida, periodos min/max.
  - Lados y simetría: long/short/both + simetría de entrada y de salida.
  - SL/PT: toggles requeridos y rangos activos (pips / ATR / RRR).
  - Ranking: fitness usado para rankear + máx. estrategias a guardar.

Con ≥2 Build tasks, imprime además un DIFF: solo los knobs cuyo valor difiere entre
tasks (lo típico que quieres saber al comparar "Build TF" vs "Build MR").

Uso:
  python sqx/inspect_build_tasks.py <proyecto>            # todas las Build tasks + diff
  python sqx/inspect_build_tasks.py <proyecto> 0 2        # solo las tasks #0 y #2 (orden config.xml)
  python sqx/inspect_build_tasks.py --json <proyecto>     # salida máquina (para pipelines)

Importable:
  from inspect_build_tasks import inspect_build_tasks
  tasks = inspect_build_tasks("NASDAQ_4H")   # [{label, xml, knobs: {concept: (value, active)}}]

El proyecto se resuelve como en builder_registry: fixture versionada en sqx/bases/<proj>/
si existe; si no, el proyecto VIVO de SQX (user/projects).
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402  KNOBS (What to build) + resolve_base
import run_task_headless as RT  # noqa: E402  tasks_of (lista ordenada del config.xml)

# Prefijos de concepto que forman la pestaña "What to build" (+ Ranking, que vive en ella)
WHAT_TO_BUILD = ("genetic.", "complexity.", "sides.", "sl.", "pt.", "ranking.")

_SECTIONS = [
    ("Motor", ("genetic.",)),
    ("StopCondition", ("stop.",)),
    ("Complejidad", ("complexity.",)),
    ("Lados y simetría", ("sides.",)),
    ("Stop Loss", ("sl.",)),
    ("Profit Target", ("pt.",)),
    ("Ranking", ("ranking.",)),
]


def _tasks_of(cfx: Path) -> list[dict]:
    """Tasks del config.xml (delega en el parseo canónico de bank_plan, que acepta
    un path dado; antes duplicaba el código con RT._attr, eliminado al mover la
    lectura del .cfx a bank_plan en la Fase 0 del plan ram-runner)."""
    from bank_plan import _tasks_of_cfx
    return _tasks_of_cfx(cfx)


def build_tasks(proj: str) -> list[dict]:
    """Tasks de tipo Build del proyecto, en orden de ejecución (del config.xml)."""
    cfx = REG.resolve_base(proj) / "project.cfx"
    if not cfx.exists():
        raise SystemExit(f"No existe {cfx} (¿proyecto '{proj}' correcto?)")
    return [t for t in _tasks_of(cfx) if t["type"] == "Build"]


def inspect_task(proj: str, xml_name: str) -> dict:
    """Evalúa los knobs del What-to-build sobre UNA Build task.

    Devuelve {concept: (value, active)} donde active=False significa knob INERTE
    (sus gates no pasan: el valor existe en el XML pero SQX lo ignora).

    Añade pseudo-knobs `stop.*` con la StopCondition del genético (no es un knob del
    registry — vive en <StopCondition type=...> del task XML): stop.type siempre;
    stop.hours/minutes/days solo si type=time-limit; stop.passedStrategies solo si
    type=passed-count (misma lógica que GATE_STOP_PASSED del registry: fuera de su
    tipo, el atributo es INERTE y no se reporta).
    """
    cfx = REG.resolve_base(proj) / "project.cfx"
    with zipfile.ZipFile(cfx) as z:
        if xml_name not in z.namelist():
            raise SystemExit(f"{xml_name} no está en {cfx}")
        root = ET.fromstring(z.read(xml_name).decode("utf-8", "surrogateescape"))
    knobs = {}
    for k in REG.KNOBS:
        if not k.concept.startswith(WHAT_TO_BUILD):
            continue
        knobs[k.concept] = (k.read(root), k.active(root))
    sc = REG.stop_condition(root)
    if sc is not None:
        stype = sc.get("type")
        knobs["stop.type"] = (stype, True)
        if stype == "time-limit":
            for unit in ("days", "hours", "minutes"):
                knobs[f"stop.{unit}"] = (sc.get(unit) or "0", True)
        elif stype == "passed-count":
            knobs["stop.passedStrategies"] = (sc.get("passedStrategies"), True)
    return knobs


def inspect_build_tasks(proj: str, indices: list[int] | None = None) -> list[dict]:
    """Inspección completa: [{idx, label, xml, knobs}] para las Build tasks pedidas."""
    tasks = build_tasks(proj)
    sel = [(i, t) for i, t in enumerate(tasks) if indices is None or i in indices]
    out = []
    for i, t in sel:
        out.append({"idx": i, "label": t["label"], "xml": t["xml"],
                    "knobs": inspect_task(proj, t["xml"])})
    return out


def diff_tasks(inspected: list[dict]) -> dict:
    """{concept: {label: value}} solo de los knobs ACTIVOS que difieren entre tasks."""
    if len(inspected) < 2:
        return {}
    concepts = set().union(*(t["knobs"] for t in inspected))
    diff = {}
    for c in sorted(concepts):
        vals = {}
        actives = []
        for t in inspected:
            v, act = t["knobs"].get(c, (None, False))
            vals[t["label"]] = v
            actives.append(act)
        if any(actives) and len(set(map(repr, vals.values()))) > 1:
            diff[c] = vals
    return diff


def summary_for_monitor(proj: str, indices: list[int] | None = None) -> list[dict]:
    """Resumen compacto de Build tasks para mostrar en el monitor.

    Cada fila contiene: Build, Entradas, Salidas, Periodos, Lados, Simetría, Stop, SL/PT.
    Lee el `.cfx` directamente, así refleja los cambios guardados al cerrar SQX.
    """
    def v(knobs, concept, default="—"):
        # inspect_build_tasks devuelve (value, active); el CLI --json usa {"value":..., "active":...}
        val = knobs.get(concept, (None, False))
        if isinstance(val, dict):
            return val.get("value") or default
        return val[0] or default

    def rng(knobs, min_c, max_c):
        lo, hi = v(knobs, min_c, "?"), v(knobs, max_c, "?")
        return lo if lo == hi else f"{lo}-{hi}"

    def sym(knobs):
        entry = v(knobs, "sides.entrySymmetry") == "true"
        exit_ = v(knobs, "sides.exitSymmetry") == "true"
        if entry and exit_:
            return "entrada + salida"
        if entry:
            return "entrada"
        if exit_:
            return "salida"
        return "no"

    def stop(knobs):
        stype = v(knobs, "stop.type")
        if stype == "time-limit":
            d = v(knobs, "stop.days", "0")
            h = v(knobs, "stop.hours", "0")
            m = v(knobs, "stop.minutes", "0")
            parts = [f"{x}{u}" for x, u in ((d, "d"), (h, "h"), (m, "m")) if x != "0"]
            return f"⏱ {''.join(parts) or '0m'}"
        if stype == "passed-count":
            return f"🎯 {v(knobs, 'stop.passedStrategies', '?')}"
        return stype or "—"

    def slpt(knobs):
        sl_on = v(knobs, "sl.required") == "true"
        pt_on = v(knobs, "pt.required") == "true"
        parts = []
        if sl_on:
            if v(knobs, "sl.atr") == "true":
                parts.append(
                    f"SL ATR{v(knobs, 'sl.atr.periodMin', '?')}×"
                    f"{rng(knobs, 'sl.atr.multMin', 'sl.atr.multMax')}"
                )
            elif v(knobs, "sl.fixedPips") == "true":
                parts.append("SL pips")
            elif v(knobs, "sl.percent") == "true":
                parts.append("SL %")
            else:
                parts.append("SL")
        if pt_on:
            if v(knobs, "pt.atr") == "true":
                parts.append("PT ATR")
            elif v(knobs, "pt.fixedPips") == "true":
                parts.append("PT pips")
            elif v(knobs, "pt.limitRRR") == "true":
                parts.append("PT RRR")
            else:
                parts.append("PT")
        return ", ".join(parts) or "—"

    rows = []
    for t in inspect_build_tasks(proj, indices):
        k = t["knobs"]
        rows.append({
            "Build": t["label"],
            "Entradas": rng(k, "complexity.minConditions", "complexity.maxConditions"),
            "Salidas": rng(k, "complexity.minExitConditions", "complexity.maxExitConditions"),
            "Periodos": rng(k, "complexity.minPeriod", "complexity.maxPeriod"),
            "Lados": v(k, "sides.type"),
            "Simetría": sym(k),
            "Stop": stop(k),
            "SL / PT": slpt(k),
        })
    return rows


# ----------------------------- presentación ---------------------------------

def _fmt(v) -> str:
    if v is None:
        return "—"
    if v in ("true", "false"):
        return "ON" if v == "true" else "off"
    return str(v)


def print_report(proj: str, inspected: list[dict]) -> None:
    print(f"\n{'=' * 78}\nWHAT TO BUILD · {proj}\n{'=' * 78}")
    for t in inspected:
        print(f"\n■ Task #{t['idx']}: {t['label']}  ({t['xml']})")
        for sec, prefixes in _SECTIONS:
            rows = []
            for concept, (val, act) in t["knobs"].items():
                if not concept.startswith(prefixes) or not act:
                    continue
                short = concept.split(".", 1)[1]
                rows.append(f"    {short:24} {_fmt(val)}")
            if rows:
                print(f"  {sec}:")
                print("\n".join(rows))
    diff = diff_tasks(inspected)
    if diff:
        print(f"\n{'-' * 78}\nDIFF entre tasks (solo knobs activos que difieren):")
        labels = [t["label"] for t in inspected]
        print(f"  {'knob':34} " + "  |  ".join(f"{l[:22]:22}" for l in labels))
        for c, vals in diff.items():
            print(f"  {c:34} " + "  |  ".join(f"{_fmt(vals[l]):22}" for l in labels))
    print()


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Inspección determinista del What-to-build de las Build tasks.")
    ap.add_argument("proj", help="proyecto SQX (o fixture de sqx/bases/)")
    ap.add_argument("indices", nargs="*", type=int,
                    help="índices de Build task (orden config.xml); por defecto todas")
    ap.add_argument("--json", action="store_true", help="salida máquina")
    args = ap.parse_args()
    inspected = inspect_build_tasks(args.proj, args.indices or None)
    if args.json:
        print(json.dumps(
            [{**t, "knobs": {c: {"value": v, "active": a} for c, (v, a) in t["knobs"].items()}}
             for t in inspected], indent=2))
    else:
        print_report(args.proj, inspected)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
