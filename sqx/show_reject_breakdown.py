#!/usr/bin/env python3
"""Lee el desglose 'Rejected/Accepted details' que SQX vuelca al TASK FINISHED.

Ese desglose = la MISMA tabla 'Reason to dismiss / Count / % of all' que la GUI enseña al pinchar
Rejected → [Detailed]. SQX la persiste en  user/projects/<proj>/log/global_log_<ts>.log  — pero
SOLO al terminar la task. Si matas el motor a media (o lo paras a lo bruto), el log queda VACÍO
(0 líneas) y el desglose se pierde. Por eso esto se mira "al final" (verificado 2026-06-30, doc §9.2).

Uso:
  python sqx/show_reject_breakdown.py                  # dashboard: TODOS los proyectos con log
  python sqx/show_reject_breakdown.py tf_eurusd_h1     # desglose completo de uno o varios
  python sqx/show_reject_breakdown.py tf_eurusd_h1 tf_xauusd_h4
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402  (PROJECTS = user/projects de SQX)

# Una línea de detalle:  "<motivo (puede llevar comas)>, Count: N, % of all: X %"
DETAIL_RE = re.compile(r"^(?P<reason>.*?),\s*Count:\s*(?P<count>\d+),\s*% of all:\s*(?P<pct>[\d.]+)\s*%\s*$")
SUMMARY_RE = re.compile(r"Strategies generated:\s*(\d+).*?Accepted:\s*(\d+),\s*Rejected:\s*(\d+)")
# ⚠ El NOMBRE del fichero global_log_<ts> está en UTC; los timestamps DE DENTRO (TASK STARTED/FINISHED) en
# hora LOCAL. Fíate de estos, no del nombre, para saber a qué corrida pertenece (mina cazada 2026-06-30).
STARTED_RE = re.compile(r"TASK STARTED at\s*(.+)")
FINISHED_RE = re.compile(r"TASK FINISHED at\s*(.+)")


def stage_of(reason: str) -> str:
    """Heurística: a qué gate del pipeline pertenece el motivo → para ver DÓNDE mueren las estrategias."""
    r = reason.lower()
    if any(k in r for k in ("higher precision", "monte carlo", "montecarlo", "walk forward", "additional market")):
        return "CROSS-CHECK"
    if "initial population" in r:
        return "SEEDING"
    if "automatic filter" in r:
        return "AUTO-FILTER"
    if "global filter" in r or "ranking" in r:
        return "RANKING"
    if any(k in r for k in ("too similar", "replaced", "in databank")):
        return "DEDUP/OK"
    return "?"


def latest_log(proj: str):
    d = REG.PROJECTS / proj / "log"
    logs = sorted(d.glob("global_log_*.log"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []
    return logs[-1] if logs else None


def parse(text: str):
    finished = "TASK FINISHED" in text
    summ = SUMMARY_RE.search(text)
    started = STARTED_RE.search(text)
    fin = FINISHED_RE.search(text)
    blocks = {"Rejected": [], "Accepted": []}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Rejected details:"):
            cur = "Rejected"; continue
        if s.startswith("Accepted details:"):
            cur = "Accepted"; continue
        if cur and not s:          # línea en blanco cierra el bloque
            cur = None; continue
        if cur:
            m = DETAIL_RE.match(s)
            if m:
                blocks[cur].append((m["reason"].strip(), int(m["count"]), float(m["pct"])))
    return finished, summ, blocks, started, fin


def print_breakdown(proj: str) -> None:
    log = latest_log(proj)
    print(f"\n{'=' * 86}\n{proj}")
    if log is None:
        print("  (sin carpeta log / sin global_log)"); return
    size = log.stat().st_size
    text = log.read_text("utf-8", "replace")
    print(f"  log: {log.name}  ({size} bytes)")
    if size == 0 or not text.strip():
        print("  ⚠ LOG VACÍO → el build NO llegó a TASK FINISHED (matado / parado a media).")
        print("    El desglose se vuelca SOLO al finalizar. Deja que llegue a su StopCondition,")
        print("    o párale con  -project action=stop  (graceful) ANTES de apagar el motor.")
        return
    finished, summ, blocks, started, fin = parse(text)
    if started:
        print(f"  TASK STARTED  {started.group(1).strip()}   (hora LOCAL; el nombre del fichero va en UTC)")
    if fin:
        print(f"  TASK FINISHED {fin.group(1).strip()}")
    if summ:
        gen, acc, rej = summ.groups()
        estado = "FINISHED" if finished else "¿en curso / parcial?"
        print(f"  generadas={gen}  aceptadas={acc}  rechazadas={rej}   [{estado}]")
    for which in ("Rejected", "Accepted"):
        rows = blocks[which]
        if not rows:
            continue
        print(f"\n  {which} details:")
        print(f"    {'%':>6}  {'count':>7}  {'etapa':<12} motivo")
        for reason, count, pct in sorted(rows, key=lambda x: -x[2]):
            print(f"    {pct:5.1f}%  {count:>7}  {stage_of(reason):<12} {reason}")


def dashboard() -> None:
    print(f"Proyectos en {REG.PROJECTS} con log de build:\n")
    print(f"{'proyecto':30} {'estado':<14} {'gen':>8} {'acc':>7} {'rej':>8}  log")
    print("-" * 100)
    any_found = False
    for d in sorted(p for p in REG.PROJECTS.iterdir() if p.is_dir()):
        log = latest_log(d.name)
        if log is None:
            continue
        any_found = True
        if log.stat().st_size == 0:
            print(f"{d.name:30} {'VACÍO':<14} {'-':>8} {'-':>7} {'-':>8}  {log.name}")
            continue
        finished, summ, _, _, _ = parse(log.read_text("utf-8", "replace"))
        est = "FINISHED" if finished else "parcial"
        g, a, r = summ.groups() if summ else ("?", "?", "?")
        print(f"{d.name:30} {est:<14} {g:>8} {a:>7} {r:>8}  {log.name}")
    if not any_found:
        print("(ningún proyecto tiene global_log)")
    print("\n→ desglose completo:  python sqx/show_reject_breakdown.py <proyecto> [<proyecto> ...]")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for proj in sys.argv[1:]:
            print_breakdown(proj)
    else:
        dashboard()