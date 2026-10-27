#!/usr/bin/env python3
"""Máquina de estados del runner SQX headless (Fase 2 del plan
`ram-runner-integration-plan.md`, aprobado 2026-10-05).

Estado por corrida en `sqx/output/<proj>_state.json` (con el seam SQX_OUTDIR, en
tmp en tests):

    {"project": ..., "updated": ...,
     "tasks": {"<idx>:<label>": {"status": "running|done|failed",
                                 "cause": null|"ram_kill"|"engine_died"|"output_empty",
                                 "pid": <pid del escritor>,
                                 "started": "ISO", "ended": "ISO"|null,
                                 "peak_rss_mb": <int>}}}

Reglas del diseño adjudicado (v3):
  - UN SOLO ESCRITOR por corrida: el driver (task única) o el orquestador
    (pipeline per-task). Este módulo solo garantiza escritura atómica
    (tmp + os.replace) y merge; la exclusión mútua es por construcción.
  - El VIGILANTE no escribe aquí: escribe `<proj>_kill.flag` (atómico) y el
    escritor lo recoge al detectar la muerte del motor.
  - Reconciliación de stale: task en `running` cuyo `pid` ya no vive →
    `failed/engine_died` (una corrida anterior murió a mitad).

Uso (CLI, lo invocan los scripts bash):
  python sqx/run_state.py <state.json> set --task KEY --status S
      [--cause C] [--pid N] [--started ISO] [--ended ISO] [--peak-rss-mb N]
  python sqx/run_state.py <state.json> get [--task KEY]
  python sqx/run_state.py <state.json> reconcile
  python sqx/run_state.py <state.json> peak-from-csv <telemetry.csv> <task_key>
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

VALID_STATUS = ("pending", "running", "done", "failed")
# user_stop: el usuario pidió parar (Stop & persist → bandera .pipeline_stop_request); la task persistió y
# el pipeline se detuvo. sync_unconfirmed: el sync FINAL no se pudo confirmar en el stdout del motor y el
# driver dejó el motor VIVO a propósito (guardrail sqx-headless-fallos, R1/R6, 2026-10-21).
VALID_CAUSES = (None, "ram_kill", "engine_died", "output_empty", "user_stop", "sync_unconfirmed")


class StateError(RuntimeError):
    """Estado inválido: fail-loud, nunca un state.json silenciosamente mal."""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def state_read(path: Path) -> dict:
    if not path.is_file():
        return {"tasks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise StateError(f"state corrupto {path}: {e}")
    data.setdefault("tasks", {})
    return data


def state_write(path: Path, data: dict) -> None:
    """Escritura atómica (tmp + os.replace en el mismo dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_task(path: Path, key: str, status: str, cause: str | None = None,
             pid: int | None = None, started: str | None = None,
             ended: str | None = None, peak_rss_mb: int | None = None) -> dict:
    """Merge de la transición de UNA task. `started` se fija solo al entrar en
    running si la task no tenía; `ended` solo en transiciones finales."""
    if status not in VALID_STATUS:
        raise StateError(f"status '{status}' no válido ({VALID_STATUS})")
    if cause not in VALID_CAUSES:
        raise StateError(f"cause '{cause}' no válida ({VALID_CAUSES})")
    data = state_read(path)
    task = data["tasks"].get(key) or {}
    prev_status = task.get("status")
    task["status"] = status
    task["cause"] = cause
    if pid is not None:
        task["pid"] = pid
    if started is not None:
        task["started"] = started
    elif status == "running" and "started" not in task:
        task["started"] = _now()
    if ended is not None:
        task["ended"] = ended
    elif status in ("done", "failed"):
        task["ended"] = _now()
    if status == "running" and prev_status != "running":
        task["ended"] = None          # re-arranque de la misma task
    if peak_rss_mb is not None:
        task["peak_rss_mb"] = int(peak_rss_mb)
    data["tasks"][key] = task
    data["updated"] = _now()
    state_write(path, data)
    return task


def reconcile_stale(path: Path) -> list[str]:
    """Task en running cuyo pid ya no vive -> failed/engine_died. Devuelve las
    keys reconciliadas (vacio = nada que hacer). Idempotente."""
    data = state_read(path)
    reconciled = []
    for key, task in data["tasks"].items():
        if task.get("status") != "running":
            continue
        pid = task.get("pid")
        alive = True
        try:
            os.kill(int(pid), 0)
        except (TypeError, ValueError, ProcessLookupError):
            alive = False
        except PermissionError:
            alive = True
        if not alive:
            task["status"] = "failed"
            task["cause"] = "engine_died"
            task["ended"] = _now()
            reconciled.append(key)
    if reconciled:
        data["updated"] = _now()
        state_write(path, data)
    return reconciled


def peak_from_csv(csv_path: Path, task_key: str) -> int:
    """Máximo rss_mb de las filas de la task en el CSV de telemetría (0 si no hay)."""
    if not csv_path.is_file():
        return 0
    peak = 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("task") != task_key:
                continue
            try:
                peak = max(peak, int(float(row.get("rss_mb") or 0)))
            except ValueError:
                continue
    return peak


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("state_file", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set", help="transición de una task (merge atómico)")
    p_set.add_argument("--task", required=True)
    p_set.add_argument("--status", required=True, choices=VALID_STATUS)
    p_set.add_argument("--cause", choices=[c for c in VALID_CAUSES if c])
    p_set.add_argument("--pid", type=int)
    p_set.add_argument("--started")
    p_set.add_argument("--ended")
    p_set.add_argument("--peak-rss-mb", type=int)

    p_get = sub.add_parser("get", help="devuelve el state completo o de una task")
    p_get.add_argument("--task")

    sub.add_parser("reconcile", help="running con pid muerto -> failed/engine_died")

    p_peak = sub.add_parser("peak-from-csv", help="max rss_mb de una task en el CSV")
    p_peak.add_argument("csv", type=Path)
    p_peak.add_argument("task_key")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "set":
            task = set_task(args.state_file, args.task, args.status, cause=args.cause,
                            pid=args.pid, started=args.started, ended=args.ended,
                            peak_rss_mb=args.peak_rss_mb)
            print(json.dumps(task, indent=2, ensure_ascii=False))
        elif args.cmd == "get":
            data = state_read(args.state_file)
            out = data["tasks"].get(args.task) if args.task else data
            print(json.dumps(out, indent=2, ensure_ascii=False))
        elif args.cmd == "reconcile":
            print(json.dumps({"reconciled": reconcile_stale(args.state_file)},
                             indent=2, ensure_ascii=False))
        else:  # peak-from-csv
            print(peak_from_csv(args.csv, args.task_key))
        return 0
    except StateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
