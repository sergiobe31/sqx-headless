#!/usr/bin/env python3
"""Loop de snapshots para el histórico del monitor SQX. Corre en background.

Toma una muestra del estado del motor + fitness del databank cada N segundos y la
appendea al JSON que lee el monitor (`~/.sqx_monitor/<proj>_snap.json`). Así el gráfico
de evolución se llena con resolución temporal fija sin depender de que el usuario clique.

Uso:
  python snapshot_loop.py [proyecto=Esqueleto] [intervalo_seg=180]
  (o el botón "Auto-muestrear" del sidebar del monitor, que lo lanza detached)

Es read-only sobre el motor (HTTP export del databank); no interfiere con el build.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqx_monitor as M  # noqa: E402

PROJ = sys.argv[1] if len(sys.argv) > 1 else "Esqueleto"
INTERVAL = int(sys.argv[2]) if len(sys.argv) > 2 else 180  # 3 min por defecto


def main():
    print(f"snapshot_loop: proyecto={PROJ} cada {INTERVAL}s (Ctrl+C para parar)", flush=True)
    while True:
        if not M.motor_alive():
            print(f"{time.strftime('%H:%M:%S')} motor inactivo, reintentando en {INTERVAL}s...", flush=True)
            time.sleep(INTERVAL)
            continue
        # Banks del config.xml ∪ disco (un bank en RAM sin sync a disco era invisible)
        disk = M.databanks_on_disk(PROJ)
        banks = list(dict.fromkeys([*M.SP.databanks_of(PROJ), *disk.keys()]))
        mc = M.motor_databank_counts(PROJ, banks)
        snap = M.take_snapshot(PROJ, mc)
        if snap:
            snaps = M.save_snapshot(PROJ, snap)
            rmin = (snap.get("running_secs") or 0) / 60
            print(f"{time.strftime('%H:%M:%S')} snap #{len(snaps)} · {rmin:.0f}min · "
                  f"bank={snap.get('active_bank')} · p50={snap.get('fit_median')} "
                  f"IQR=[{snap.get('fit_q1')},{snap.get('fit_q3')}] máx={snap.get('fit_max')} · "
                  f"in_db={snap.get('in_databank')} acc%={snap.get('accepted_pct')}", flush=True)
            if snap.get("fit_median") is None and M.LAST_FIT_ERROR:
                print(f"  ⚠ export fitness: {M.LAST_FIT_ERROR}", flush=True)
        else:
            print(f"{time.strftime('%H:%M:%S')} sin snap (¿proyecto no corre / motor en otra tarea?)", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nparado.", flush=True)
