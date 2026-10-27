#!/usr/bin/env python3
"""Plan de databanks por task del runner headless (Fase 0 del plan
`ram-runner-integration-plan.md`, aprobado 2026-10-05).

Puro y **fail-loud**: dado un proyecto y un índice de task devuelve qué databanks
se conservan en `databanks/`, cuál es el output y cuáles se pueden archivar a
`databanks_archive/` durante esa task (modo `--slim`: el motor carga en RAM TODOS
los bancos declarados aunque la task no los use, ~0,9 GB/1.000 estrategias).

Reglas del parser type-aware (evidencia: .cfx real de intraday_nasdaq_future,
verificado 2026-10-06):
  - Build    → 0..N tags `Input databank` (p.ej. `Initial population`,
               `Strategies to improve`); todos se conservan. Output = tag
               `Output databank` (exactamente 1).
  - Retest   → exactamente 1 `Input databank`; 0 o ≥2 → abortar. Output igual.
  - Filtering → 0 tags `Input databank` (su input es el flujo del pipeline):
               input resuelto = tag `Source databank` (en el .cfx real ES el
               output de la task del pipeline que le corresponde, no
               necesariamente la anterior inmediata: las 3 Builds van seguidas
               y luego las 3 Filterings). Si no hubiera tag `Source`, fallback
               a la regla del plan: output de la task activa anterior en el
               orden del config.xml. Output = tag `Target databank`.
  - Todo input/output resuelto debe estar declarado en config.xml; si no →
    abortar (banco no declarado = invisible para el motor, landmine L1).
  - input==output (update-in-place): el banco se preserva vía output y se
    excluye de la lista de inputs (aparece una sola vez en el keep-set).
  - Regex tolerante al orden de atributos (no copiar el patrón de orden fijo de
    builder_registry.py:58).

También es el helper de marcador del modo `--slim` (Fase 1): el DRIVER
(run_pipeline_headless.sh / run_pipeline_per_task.sh) es el dueño único del
archive/marcador/restore y este módulo centraliza el plan y las operaciones
atómicas (tmp+rename) sobre `databanks_archive/.runner_session.json`, para que el
bash no parsee JSON a mano.

Uso (CLI):
  python sqx/bank_plan.py <proj> --list                    # tasks (orden = ejecución)
  python sqx/bank_plan.py <proj> --task-idx N              # plan JSON de la task (F0)
  python sqx/bank_plan.py <proj> --task-idx N --archive-list   # bancos a archivar (driver, ∩ present)
  python sqx/bank_plan.py <proj> --task-idx N --restore-list   # inputs de la task (driver, L3)
  python sqx/bank_plan.py <proj> --slim-archive --task-idx N --run-id R --pid P [--pipeline]
  python sqx/bank_plan.py <proj> --slim-restore-inputs --task-idx N
  python sqx/bank_plan.py <proj> --slim-restore
  python sqx/bank_plan.py <proj> --reconcile               # reconcilia si el pid del marcador murió
  python sqx/bank_plan.py <proj> --marker-read | --marker-banks | --marker-clear
<proj> = nombre bajo PROJECTS o ruta directa a la carpeta del proyecto.
Exit codes: 0 ok (incl. reconcile none/reconciled), 1 error, 3 --reconcile con
corrida viva (no toca nada).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402  PROJECTS
import gui_slim  # noqa: E402  cmd_archive / cmd_restore (guardas reutilizadas)

MARKER_NAME = ".runner_session.json"

_DB_TAG = re.compile(r"<Databank\b[^>]*>")
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


# Banco auxiliar del genético que los Build de la GUI declaran como 2.º "Output databank".
AUX_BUILD_OUTPUT = "Last generation"


class BankPlanError(RuntimeError):
    """Fallo de plan/archive/restore: fail-loud, nunca un plan vacío silencioso."""


# ----------------------------- lectura del proyecto ---------------------------

def resolve_projdir(proj: str) -> Path:
    """Ruta directa a la carpeta del proyecto o nombre bajo PROJECTS."""
    p = Path(proj).expanduser()
    if p.is_dir():
        return p
    q = REG.PROJECTS / proj
    if q.is_dir():
        return q
    raise BankPlanError(f"no encuentro la carpeta del proyecto '{proj}'")


def _read_cfg(cfx: Path) -> str:
    try:
        with zipfile.ZipFile(cfx) as z:
            return z.read("config.xml").decode("utf-8", "replace")
    except Exception as e:
        raise BankPlanError(f"no puedo leer {cfx}: {e}")


def _attr(tag: str, key: str) -> str | None:
    m = re.search(key + r'="([^"]*)"', tag)
    return m.group(1) if m else None


def _tasks_of_cfx(cfx: Path) -> list[dict]:
    """Tasks ordenadas de un .cfx: [{xml, label, type, active}] (orden = ejecución).
    Misma semántica que run_task_headless.tasks_of(), que delega aquí."""
    out: list[dict] = []
    with zipfile.ZipFile(cfx) as z:
        cfg = z.read("config.xml").decode("utf-8", "surrogateescape")
    for tag in re.findall(r"<Task\b[^>]*>", cfg):
        out.append({
            "xml": _attr(tag, "taskXMLFile"),
            "label": _attr(tag, "title") or _attr(tag, "name") or _attr(tag, "taskXMLFile"),
            "type": _attr(tag, "type"),
            "active": (_attr(tag, "active") or "true") == "true",
        })
    return out


def tasks_of_at(projdir: Path) -> list[dict]:
    """Tasks ordenadas del config.xml del proyecto (orden = ejecución)."""
    try:
        return _tasks_of_cfx(projdir / "project.cfx")
    except zipfile.BadZipFile as e:
        raise BankPlanError(f"{projdir}/project.cfx no es un .cfx válido: {e}")


def declared_banks(cfg: str) -> list[str]:
    """Bancos declarados en config.xml, en orden de aparición."""
    out = []
    for m in re.finditer(r"<Databank\b[^>]*>", cfg):
        name = dict(_ATTR.findall(m.group(0))).get("name")
        if name and name not in out:
            out.append(name)
    return out


def _databank_refs(task_xml: str) -> list[dict]:
    """Tags <Databank> del XML de la task como dicts de atributos (orden tolerante)."""
    return [dict(_ATTR.findall(m.group(0))) for m in _DB_TAG.finditer(task_xml)]


def _vals(refs: list[dict], *labels: str) -> list[str]:
    lab = set(labels)
    return [r["value"] for r in refs
            if r.get("label") in lab and r.get("value") is not None]


def _task_xmls(projdir: Path, tasks: list[dict]) -> dict[str, str]:
    cfx = projdir / "project.cfx"
    try:
        z = zipfile.ZipFile(cfx)
    except Exception as e:
        raise BankPlanError(f"no puedo abrir {cfx}: {e}")
    with z:
        names = set(z.namelist())
        out = {}
        for t in tasks:
            xf = t["xml"]
            if not xf or xf not in names:
                raise BankPlanError(f"task '{t['label']}': {xf} no está en el .cfx")
            out[xf] = z.read(xf).decode("utf-8", "replace")
    return out


def resolve_ios(projdir: Path) -> tuple[list[dict], dict[str, list[str]], dict[str, list[str]], list[str]]:
    """Resuelve y valida inputs/output de TODAS las tasks en una pasada.

    Devuelve (tasks, inputs_por_xml, output_por_xml, declared). Fail-loud:
    output sin resolver, Retest sin exactamente 1 input, banco no declarado
    (L1) o Source de Filtering inconsistente con el output de la activa anterior.
    """
    tasks = tasks_of_at(projdir)
    if not tasks:
        raise BankPlanError(f"{projdir}: sin tasks en config.xml")
    declared = declared_banks(_read_cfg(projdir / "project.cfx"))
    xmls = _task_xmls(projdir, tasks)
    inputs_by: dict[str, list[str]] = {}
    output_by: dict[str, str] = {}
    last_active_output: str | None = None
    for t in tasks:
        refs = _databank_refs(xmls[t["xml"]])
        typ = t["type"]
        if typ in ("Build", "Retest"):
            inputs = _vals(refs, "Input databank")
            outs = _vals(refs, "Output databank")
            if typ == "Build" and len(outs) == 2 and AUX_BUILD_OUTPUT in outs:
                # Builds de la plantilla de la GUI (2026-10-12): 2.º Output = el banco
                # auxiliar del genético. El principal es el otro; el auxiliar se
                # conserva siempre (va al keep-set como input), nunca se archiva.
                outs = [o for o in outs if o != AUX_BUILD_OUTPUT]
                inputs = inputs + [AUX_BUILD_OUTPUT]
            if len(outs) != 1:
                raise BankPlanError(
                    f"task '{t['label']}' ({typ}): {len(outs)} tags 'Output databank' (esperado 1)")
            output = outs[0]
            if typ == "Retest" and len(inputs) != 1:
                raise BankPlanError(
                    f"Retest '{t['label']}': {len(inputs)} Input databanks (esperado exactamente 1)")
        elif typ == "Filtering":
            sources = _vals(refs, "Source databank")
            targets = _vals(refs, "Target databank")
            if len(targets) != 1:
                raise BankPlanError(
                    f"Filtering '{t['label']}': {len(targets)} tags 'Target databank' (esperado 1)")
            output = targets[0]
            if len(sources) > 1:
                raise BankPlanError(
                    f"Filtering '{t['label']}': {len(sources)} tags 'Source databank' (esperado 0..1)")
            if sources:
                inputs = [sources[0]]
            else:
                # fallback (plan F0): input = output de la task activa anterior
                inputs = [] if last_active_output is None else [last_active_output]
        else:
            raise BankPlanError(f"task '{t['label']}': tipo '{typ}' no soportado")
        # update-in-place: preservado vía output; aparece una sola vez en el keep-set
        inputs = [b for b in inputs if b != output]
        for b in inputs + [output]:
            if b not in declared:
                raise BankPlanError(
                    f"task '{t['label']}': banco '{b}' no declarado en config.xml (L1)")
        inputs_by[t["xml"]] = inputs
        output_by[t["xml"]] = output
        if t["active"]:
            last_active_output = output
    return tasks, inputs_by, output_by, declared


# ----------------------------- presencia en disco -----------------------------

def _has_files(d: Path) -> bool:
    return d.is_dir() and any(p.is_file() for p in d.rglob("*"))


def present_banks(projdir: Path) -> list[str]:
    """Bancos con ≥1 fichero en databanks/ ahora (un dir vacío recreado por el
    motor/GUI (L2) NO cuenta como presente)."""
    dbs = projdir / "databanks"
    if not dbs.is_dir():
        return []
    return sorted(p.name for p in dbs.iterdir() if p.is_dir() and _has_files(p))


# ----------------------------- plan (Fase 0) ----------------------------------

def build_plan_at(projdir: Path, task_idx: int) -> dict:
    """Plan F0 de UNA task aislada (la usa el test del golden y el CLI --task-idx)."""
    tasks, inputs_by, output_by, declared = resolve_ios(projdir)
    if not 0 <= task_idx < len(tasks):
        raise BankPlanError(f"task_idx {task_idx} fuera de rango (0..{len(tasks) - 1})")
    t = tasks[task_idx]
    inputs = inputs_by[t["xml"]]
    output = output_by[t["xml"]]
    keep = set(inputs) | {output}
    return {
        "task": t["label"],
        "type": t["type"],
        "task_idx": task_idx,
        "inputs": inputs,
        "output": output,
        "declared": declared,
        "archive": [b for b in declared if b not in keep],
        "present": present_banks(projdir),
    }


def build_plan(proj: str, task_idx: int) -> dict:
    return build_plan_at(resolve_projdir(proj), task_idx)


def driver_plan_at(projdir: Path, first_idx: int) -> dict:
    """Plan del DRIVER para la corrida que arranca en la task `first_idx`.

    archive = declared − union(inputs de tasks first_idx..fin) − union(outputs de
    tasks first_idx..fin). El plan literal era '− output de la task actual', pero
    excluir SOLO ese output archivaría los outputs de las tasks futuras que no sean
    input de nadie (p.ej. MR_Long/MR_Short en la iteración 0 de la cadena nasdaq):
    un output archivado mientras su task lo regenera hace que el persist final lo
    salte (M4) y que H11 aborte con el trabajo perdido. Excluir todos los outputs
    restantes es la lectura coherente con L5 ('el restore nunca toca el output').
    Desviación mínima registrada en el handoff de la Fase 1.

    Computa sobre tasks first_idx..fin SIN filtrar por active: el .cfx puede estar
    mutado por el launcher (carrera restore inmediato del .cfx) y el plan debe ser
    determinista.
    """
    tasks, inputs_by, output_by, declared = resolve_ios(projdir)
    if not 0 <= first_idx < len(tasks):
        raise BankPlanError(f"task_idx {first_idx} fuera de rango (0..{len(tasks) - 1})")
    keep: set[str] = set()
    for t in tasks[first_idx:]:
        keep.update(inputs_by[t["xml"]])
        keep.update([output_by[t["xml"]]])
    first = tasks[first_idx]
    return {
        "task": first["label"],
        "task_idx": first_idx,
        "declared": declared,
        "archive": [b for b in declared if b not in keep],
        "restore": inputs_by[first["xml"]],
        "present": present_banks(projdir),
    }


def driver_plan(proj: str, first_idx: int) -> dict:
    return driver_plan_at(resolve_projdir(proj), first_idx)


# ----------------------------- marcador de sesión -----------------------------

def marker_path(projdir: Path) -> Path:
    return projdir / "databanks_archive" / MARKER_NAME


def marker_read(projdir: Path) -> dict | None:
    m = marker_path(projdir)
    if not m.is_file():
        return None
    try:
        return json.loads(m.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BankPlanError(f"marcador corrupto {m}: {e}")


def marker_write(projdir: Path, data: dict) -> None:
    """Escritura atómica (tmp + os.replace en el mismo dir)."""
    arch = projdir / "databanks_archive"
    arch.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=arch, prefix=".runner_session.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, marker_path(projdir))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def marker_clear(projdir: Path) -> None:
    m = marker_path(projdir)
    if m.is_file():
        m.unlink()


def marker_banks(projdir: Path) -> list[str]:
    m = marker_read(projdir)
    if not m:
        return []
    return [b for b in (m.get("banks") or []) if isinstance(b, str)]


def marker_pid_alive(marker: dict) -> bool:
    pid = marker.get("pid_driver")
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


# ----------------------------- archive / restore ------------------------------

def archive_banks(projdir: Path, banks: list[str]) -> list[str]:
    """Archiva (gui_slim) los bancos de la lista que están presentes; rollback de
    lo movido si gui_slim falla a medias. Devuelve los realmente archivados."""
    todo = [b for b in dict.fromkeys(banks) if _has_files(projdir / "databanks" / b)]
    if not todo:
        return []
    try:
        gui_slim.cmd_archive(projdir, todo)
    except SystemExit as e:
        moved = [b for b in todo
                 if not _has_files(projdir / "databanks" / b)
                 and _has_files(projdir / "databanks_archive" / b)]
        if moved:
            try:
                gui_slim.cmd_restore(projdir, moved)
            except SystemExit:
                pass
        raise BankPlanError(f"archive falló (gui_slim: {e}) — rollback de {moved}")
    return [b for b in todo if not _has_files(projdir / "databanks" / b)]


def restore_banks(projdir: Path, banks: list[str]) -> dict:
    """Restore presence-aware e idempotente, banco a banco.

    - ya presente (ficheros en databanks/<b>) → skip;
    - dir vacío recreado por el motor/GUI en destino → rmdir (L2) y restaurar;
    - nada que restaurar (ni src ni dst con ficheros) → skip.

    Fail-loud si gui_slim rechaza la verificación 1:1 del manifiesto.
    """
    restored, skipped = [], []
    for b in dict.fromkeys(banks):
        src = projdir / "databanks_archive" / b
        dst = projdir / "databanks" / b
        if _has_files(dst) or not _has_files(src):
            skipped.append(b)
            continue
        if dst.exists():
            if not any(dst.iterdir()):
                dst.rmdir()
            else:
                skipped.append(b)
                continue
        try:
            gui_slim.cmd_restore(projdir, [b])
        except SystemExit as e:
            raise BankPlanError(f"restore de '{b}' falló (gui_slim: {e})")
        restored.append(b)
    return {"restored": restored, "skipped": skipped}


def reconcile(projdir: Path) -> dict:
    """Reconciliación presence-aware (B2): restaura lo del marcador y lo borra."""
    m = marker_read(projdir)
    if m is None:
        return {"restored": [], "skipped": [], "cleared": False}
    rep = restore_banks(projdir, m.get("banks") or [])
    marker_clear(projdir)
    return {**rep, "cleared": True}


def reconcile_if_dead(projdir: Path) -> dict:
    """Para el arranque de una corrida nueva: reconcilia SOLO si el pid del driver
    registrado ya no vive (la corrida anterior murió a mitad). Si vive, no toca
    nada (action='live') — el pre-check de instancia única abortará después."""
    m = marker_read(projdir)
    if m is None:
        return {"action": "none"}
    if marker_pid_alive(m):
        return {"action": "live", "run_id": m.get("run_id")}
    return {"action": "reconciled", **reconcile(projdir)}


# ----------------------------- ciclo --slim (Fase 1) --------------------------

def slim_archive(projdir: Path, task_idx: int, run_id: str, pid: int,
                 pipeline: bool = False) -> list[str]:
    """Archive pre-motor por task (punto 1 del driver): escribe el marcador
    ANTES de mover nada (un kill -9 entre medias deja marcador + estado
    coherentes para la reconciliación), archiva archive ∩ present y deja el
    marcador con los bancos realmente archivados.

    MERGE sobre el marcador existente: en pipeline cada driver de task reescribe
    run_id/pid, pero los `banks` se acumulan — si cada iteración pisara la lista,
    el restore acumulado del orquestador solo vería lo del ÚLTIMO driver y los
    bancos de iteraciones anteriores quedarían atrapados (problema (c) que el
    diseño v3 corrige haciendo dueño al driver por iteración).
    """
    plan = driver_plan_at(projdir, task_idx)
    todo = [b for b in plan["archive"] if _has_files(projdir / "databanks" / b)]
    prev = marker_read(projdir) or {}
    prev_banks = [b for b in (prev.get("banks") or []) if isinstance(b, str)]
    data = {
        "run_id": run_id,
        "pid_driver": pid,
        "started": prev.get("started") or dt.datetime.now().isoformat(timespec="seconds"),
        "pipeline": bool(pipeline) or bool(prev.get("pipeline")),
        "banks": sorted(set(prev_banks) | set(todo)),
    }
    marker_write(projdir, data)
    done = archive_banks(projdir, todo)
    merged = sorted(set(prev_banks) | set(done))
    if merged != data["banks"]:
        marker_write(projdir, {**data, "banks": merged})
    return done


def slim_restore_inputs(projdir: Path, task_idx: int) -> dict:
    """Restaura los inputs de la task que sigan archivados (L3 por task) y los
    quita del marcador. No-op sin marcador."""
    m = marker_read(projdir)
    if m is None:
        return {"restored": [], "skipped": []}
    plan = driver_plan_at(projdir, task_idx)
    todo = [b for b in plan["restore"] if b in set(m.get("banks") or [])]
    rep = restore_banks(projdir, todo)
    remaining = [b for b in (m.get("banks") or []) if b not in set(rep["restored"])]
    marker_write(projdir, {**m, "banks": remaining})
    return rep


def slim_restore(projdir: Path) -> dict:
    """Restore final (task única o acumulado de cadena): restaura lo que quede
    archivado en el marcador y lo borra. No-op sin marcador."""
    m = marker_read(projdir)
    if m is None:
        return {"restored": [], "skipped": [], "cleared": False}
    rep = restore_banks(projdir, m.get("banks") or [])
    marker_clear(projdir)
    return {**rep, "cleared": True}


# ----------------------------- CLI ---------------------------------------------

def _print_plan(plan: dict) -> None:
    print(json.dumps(plan, indent=2, ensure_ascii=False))


def _print_tasks(projdir: Path) -> None:
    tasks, inputs_by, output_by, _ = resolve_ios(projdir)
    print(f"Tasks de {projdir.name} (orden = ejecución):")
    for i, t in enumerate(tasks):
        flag = "" if t["active"] else "  [active=false]"
        ins = ",".join(inputs_by[t["xml"]]) or "-"
        print(f"  {i:2}. {t['label']:36} type={t['type']:10} in={ins:26} out={output_by[t['xml']]}{flag}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("proj", help="nombre bajo PROJECTS o ruta directa a la carpeta del proyecto")
    ap.add_argument("--list", action="store_true", help="lista las tasks con input/output y sale")
    ap.add_argument("--task-idx", type=int, help="índice 0-based de la task (orden de ejecución)")
    ap.add_argument("--archive-list", action="store_true",
                    help="con --task-idx: bancos a archivar (plan del driver ∩ present), uno por línea")
    ap.add_argument("--restore-list", action="store_true",
                    help="con --task-idx: inputs de la task (a restaurar si están archivados)")
    ap.add_argument("--slim-archive", action="store_true", help="archive pre-motor + marcador atómico")
    ap.add_argument("--slim-restore-inputs", action="store_true",
                    help="restaura los inputs de la task que sigan archivados (L3 por task)")
    ap.add_argument("--slim-restore", action="store_true", help="restore presence-aware final + clear")
    ap.add_argument("--reconcile", action="store_true",
                    help="si el pid del marcador murió, reconcilia; exit 3 = corrida viva")
    ap.add_argument("--marker-read", action="store_true")
    ap.add_argument("--marker-banks", action="store_true", help="bancos del marcador, uno por línea")
    ap.add_argument("--marker-clear", action="store_true")
    ap.add_argument("--run-id", help="id de corrida para el marcador (--slim-archive)")
    ap.add_argument("--pid", type=int, help="pid del driver (--slim-archive)")
    ap.add_argument("--pipeline", action="store_true", help="marcador de corrida en pipeline (per-task)")
    args = ap.parse_args(argv)

    try:
        projdir = resolve_projdir(args.proj)

        if args.list:
            _print_tasks(projdir)
            return 0
        if args.archive_list or args.restore_list:
            if args.task_idx is None:
                ap.error("--archive-list/--restore-list requieren --task-idx")
            plan = driver_plan_at(projdir, args.task_idx)
            for b in (plan["restore"] if args.restore_list else
                      [x for x in plan["archive"]
                       if _has_files(projdir / "databanks" / x)]):
                print(b)
            return 0
        if args.task_idx is not None and not (args.slim_archive or args.slim_restore_inputs):
            _print_plan(build_plan_at(projdir, args.task_idx))
            return 0
        if args.slim_archive:
            if args.task_idx is None:
                ap.error("--slim-archive requiere --task-idx")
            done = slim_archive(projdir, args.task_idx, args.run_id or "manual",
                                args.pid or os.getpid(), pipeline=args.pipeline)
            for b in done:
                print(b)
            return 0
        if args.slim_restore_inputs:
            if args.task_idx is None:
                ap.error("--slim-restore-inputs requiere --task-idx")
            _print_plan(slim_restore_inputs(projdir, args.task_idx))
            return 0
        if args.slim_restore:
            _print_plan(slim_restore(projdir))
            return 0
        if args.reconcile:
            rep = reconcile_if_dead(projdir)
            _print_plan(rep)
            return 3 if rep["action"] == "live" else 0
        if args.marker_read:
            m = marker_read(projdir)
            if m is None:
                return 1
            _print_plan(m)
            return 0
        if args.marker_banks:
            for b in marker_banks(projdir):
                print(b)
            return 0
        if args.marker_clear:
            marker_clear(projdir)
            return 0
        ap.error("ninguna acción (usa --list, --task-idx, --slim-*, --reconcile o --marker-*)")
    except BankPlanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
