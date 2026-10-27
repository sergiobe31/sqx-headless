"""Tests de la Fase 2 del plan ram-runner: vigilante compuesto + máquina de estados.

Criterios de aceptación F2 convertidos en tests (motor mock como en la Fase 1;
el mock puede CRECER en RAM vía MOCK_GROW="rate_mb_s:max_mb:paso_secs" para
ejercitar las señales del vigilante; los umbrales/sleeps se comprimen por env):

  (a) canary: corrida normal -> telemetría CSV completa (ts, task, pid, rss_mb,
      free_pct, swap_used_mb) y transiciones del state running -> done con peak;
  (b) kill simulado: el mock crece por encima de WD_KILL_RSS_MB bajo -> el
      vigilante mata (SIGTERM), kill.flag, state failed/ram_kill;
  (c) proceso por encima del umbral WARN pero bajo el KILL -> sobrevive: WARN en
      el log, state done;
  (c2) WARN por derivada > WD_DERIV_MB_MIN sostenida, gateada a post-carga y con
      RSS plano (crecimiento suave);
  (d) corrida real de aceptación sobre el proyecto nasdaq: la ejecuta el
      ORQUESTADOR (NO aquí: hay trabajo en curso sobre ese proyecto);
  (e) reconciliación de stale (run_state.py): running con pid muerto ->
      failed/engine_died, idempotente;
  (f) pipeline: el ORQUESTADOR es el único escritor del state (running -> done
      por task) y el driver no toca el state (RUNNER_PIPELINE=1).

Telemetría/state/kill.flag se escriben bajo SQX_OUTDIR (seam de test) -> tmp.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sqx"))
if str(ROOT / "tests") not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx (orden de colección)
    sys.path.append(str(ROOT / "tests"))

from test_slim_hooks import (  # noqa: E402  infra de la Fase 1 (mocks/driver)
    DRIVER, MOCK_REGISTRY, MOCK_SQCLI, ORCHESTRATOR, _Handler, _mock_env,
    _wait_for, build_c, http_port, make_proj, run_driver, unique_engine_name,
)
import bank_plan  # noqa: E402
import run_state as RS  # noqa: E402



def _monitor_log(name: str) -> str:
    p = Path(f"/tmp/{name}_run/monitor.log")
    return p.read_text() if p.exists() else ""



_TEST_PATTERNS: list[str] = []


@pytest.fixture(autouse=True)
def _kill_leftover_processes():
    """Aislamiento entre tests: si un test falla a mitad, su driver/vigilante/mock
    quedan huérfanos y podrían medirse o matarse cruzados en el siguiente. El
    teardown caza los patrones registrados (uuid único por mock) aunque el test
    haya fallado."""
    yield
    for pat in MOCK_REGISTRY + _TEST_PATTERNS:
        subprocess.run(["pkill", "-f", pat], capture_output=True)
    MOCK_REGISTRY.clear()
    _TEST_PATTERNS.clear()


def _paths(tmp_path: Path, name: str) -> dict:
    out = tmp_path / "out"
    return {
        "telemetry": out / f"{name}_telemetry.csv",
        "state": out / f"{name}_state.json",
        "killflag": out / f"{name}_kill.flag",
    }


def _telemetry_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _task(state: Path, key: str) -> dict:
    return json.loads(subprocess.run(
        [sys.executable, str(ROOT / "sqx" / "run_state.py"), str(state),
         "get", "--task", key],
        capture_output=True, text=True).stdout)


# ----------------------------- (a) canary ---------------------------------------

def test_f2a_canary_telemetria_y_transiciones(tmp_path, http_port, monkeypatch):
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    name = f"wdcan_{uuid4().hex[:6]}"
    proj = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])

    proc = run_driver(proj, http_port, tmp_path,
                      WD_SLEEP_SECS="1",
                      MOCK_FIN_SECS="8", POLL_MIN="0", MAXITER="3000")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    p = _paths(tmp_path, name)

    # telemetría permanente: header + filas con las 6 columnas rellenas (la 1ª
    # muestra puede registrar rss=0: el proceso del motor acaba de arrancar)
    rows = _telemetry_rows(p["telemetry"])
    assert len(rows) >= 2, "el vigilante debe haber muestreado al menos 2 veces"
    con_rss = 0
    for r in rows:
        assert r["task"] == "0:T1"
        if int(r["rss_mb"]) > 0:
            con_rss += 1
        assert r["free_pct"] not in ("", "NA"), "free_pct debe venir de memory_pressure"
        assert r["swap_used_mb"] not in ("", "NA"), "swap debe venir de sysctl"
    assert con_rss >= 2, "el RSS del motor debe registrarse en la telemetría"
    # state: running -> done con peak y timestamps
    task = _task(p["state"], "0:T1")
    assert task["status"] == "done"
    assert task["cause"] is None
    assert task["started"] and task["ended"]
    assert int(task["peak_rss_mb"]) > 0
    assert not p["killflag"].exists()


# ----------------------------- (b) kill simulado ----------------------------------

def test_f2b_kill_por_ram_mata_y_marca_state(tmp_path, http_port, monkeypatch):
    """El mock crece > WD_KILL_RSS_MB (60 MB) -> el vigilante mata (SIGTERM),
    kill.flag, el driver sale del poll y el state queda failed/ram_kill."""
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    name = f"wdkill_{uuid4().hex[:6]}"
    proj = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])

    proc = run_driver(proj, http_port, tmp_path, linger=5,
                      WD_SLEEP_SECS="1", WD_GRACE_SECS="2",
                      WD_WARN_RSS_MB="50", WD_KILL_RSS_MB="60",
                      MOCK_GROW="8:200:0.5", MOCK_FIN_SECS="999",
                      POLL_MIN="0", MAXITER="3000")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    p = _paths(tmp_path, name)

    log = _monitor_log(name)
    assert "WATCHDOG KILL" in log
    assert "KILL FLAG del vigilante" in proc.stdout
    task = _task(p["state"], "0:T1")
    assert task["status"] == "failed"
    assert task["cause"] == "ram_kill"
    assert int(task["peak_rss_mb"]) >= 60
    # el vigilante mató al mock (SIGTERM; el mock no resiste)
    assert subprocess.run(["pgrep", "-f", "mock_sqcli"],
                          capture_output=True).returncode != 0
    assert not p["killflag"].exists()   # en task única el driver lo recogió
    # la telemetría capturó el crecimiento
    rss = [int(r["rss_mb"]) for r in _telemetry_rows(p["telemetry"])]
    assert max(rss) >= 60


# ----------------------------- (c) WARN no mata ---------------------------------

def test_f2c_warn_no_mata_sobrevive(tmp_path, http_port, monkeypatch):
    """Mock ~70-100 MB: por encima del WARN (60 MB) pero muy por debajo del KILL
    -> WARN en el log y telemetría, la corrida completa y el state queda done."""
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    name = f"wdwarn_{uuid4().hex[:6]}"
    proj = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])

    proc = run_driver(proj, http_port, tmp_path,
                      WD_SLEEP_SECS="1", WD_GRACE_SECS="2",
                      WD_WARN_RSS_MB="60", WD_KILL_RSS_MB="100000",
                      MOCK_GROW="20:100:0.5", MOCK_FIN_SECS="6",
                      POLL_MIN="0", MAXITER="3000")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    p = _paths(tmp_path, name)

    log = _monitor_log(name)
    assert "WATCHDOG WARN" in log
    assert "WATCHDOG KILL" not in log
    task = _task(p["state"], "0:T1")
    assert task["status"] == "done"
    assert task["cause"] is None
    assert not p["killflag"].exists()


def test_f2c2_warn_por_derivada_gateada_post_carga(tmp_path, http_port, monkeypatch):
    """Derivada suave y sostenida (0.5 MB/s = 30 MB/min) con RSS plano ±5%,
    evaluada solo tras la barrera de carga -> WARN por derivada (RSS absoluto
    muy por debajo del umbral)."""
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    name = f"wdderiv_{uuid4().hex[:6]}"
    proj = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])

    proc = run_driver(proj, http_port, tmp_path,
                      WD_SLEEP_SECS="1", WD_GRACE_SECS="2",
                      WD_WARN_RSS_MB="999999", WD_KILL_RSS_MB="1000000",
                      WD_DERIV_MB_MIN="2", WD_HIST_SECS="8",
                      MOCK_GROW="0.5:40:1", MOCK_GROW_BASE="24", MOCK_FIN_SECS="25",
                      POLL_MIN="0", MAXITER="3000")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _monitor_log(name)
    if "WATCHDOG WARN: derivada" not in log:
        # Este test mide RSS REAL del mock. Bajo presión de memoria macOS comprime/pagina sus páginas y el RSS se
        # DESPLOMA (observado 2026-10-24 con swap en 1,9 GB y la GUI de SQX abierta: 37 MB → 7 MB): la señal de
        # "derivada sostenida y plana" no existe y el vigilante hace lo correcto al no avisar. Eso no es un fallo del
        # vigilante sino de la plataforma → skip explícito con el motivo (un vigilante roto NO colapsa el RSS).
        rss = [int(r["rss_mb"]) for r in _telemetry_rows(_paths(tmp_path, name)["telemetry"])]
        if len(rss) > 3 and max(rss[:-1]) >= 2 * max(rss[-1], 1):
            pytest.skip(f"el RSS del mock se desplomó por la presión de memoria de la máquina (rss_mb={rss}): "
                        "no se puede medir una derivada sostenida")
    assert "WATCHDOG WARN: derivada" in log
    assert "WATCHDOG KILL" not in log
    task = _task(_paths(tmp_path, name)["state"], "0:T1")
    assert task["status"] == "done"


# ----------------------------- (e) reconcile stale (run_state) ------------------

def test_f2e_reconcile_stale_pid_muerto(tmp_path):
    state = tmp_path / "state.json"
    RS.set_task(state, "0:T1", "running", pid=999999, started="2026-10-06T10:00:00")
    RS.set_task(state, "1:T2", "running", pid=os.getpid())   # vivo -> no tocar
    RS.set_task(state, "2:T3", "done")

    assert RS.reconcile_stale(state) == ["0:T1"]
    assert RS.reconcile_stale(state) == []          # idempotente
    tasks = json.loads(state.read_text())["tasks"]
    assert tasks["0:T1"]["status"] == "failed"
    assert tasks["0:T1"]["cause"] == "engine_died"
    assert tasks["0:T1"]["ended"]
    assert tasks["1:T2"]["status"] == "running"     # pid vivo intacto
    assert tasks["2:T3"]["status"] == "done"


def test_f2e_set_valida_status_y_cause(tmp_path):
    state = tmp_path / "state.json"
    with pytest.raises(RS.StateError):
        RS.set_task(state, "0:T1", "exploded")
    with pytest.raises(RS.StateError):
        RS.set_task(state, "0:T1", "failed", cause="otro")


# ----------------------------- (f) pipeline: orquestador escritor único ----------

def test_f2f_orquestador_escribe_state_por_task(tmp_path, http_port):
    """Pipeline real (orquestador -> launcher -> driver ×2): el state lo escribe
    el ORQUESTADOR (running -> done por task); los drivers no lo tocan."""
    projects = tmp_path / "projects"
    name = f"wdorch_{uuid4().hex[:6]}"
    proj = make_proj(projects, name,
                     [("T1", "Build", ["InA"], "MidM"),
                      ("T2", "Retest", ["MidM"], "OutB")],
                     ["InA", "MidM", "OutB", "ExtraC"])
    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    _TEST_PATTERNS.append(mock.name)   # teardown lo caza aunque el orquestador falle a mitad
    env = _mock_env(proj, tmp_path, mock, tmp_path / "s.json",
                    MOCK_TOUCH="MidM,OutB", PORT=str(http_port),
                    MOCK_FIN_SECS="6", POLL_MIN="0", MAXITER="3000",
                    WD_SLEEP_SECS="1", WD_PROC_PATTERN=mock.name,
                    WD_FREE_PCT_MIN="0")
    proc = subprocess.run(
        ["bash", str(ORCHESTRATOR), name, "0", "--slim"],
        capture_output=True, text=True, env=env, timeout=600)
    out = proc.stdout + proc.stderr
    if proc.returncode != 0 or "PIPELINE PER-TASK COMPLETO" not in out:
        Path(f"/tmp/{name}_orch_out.txt").write_text(out)
    assert proc.returncode == 0, out
    assert "PIPELINE PER-TASK COMPLETO" in out

    p = _paths(tmp_path, name)
    data = json.loads(p["state"].read_text())
    t1 = data["tasks"]["0:T1"]
    t2 = data["tasks"]["1:T2"]
    assert t1["status"] == "done" and t2["status"] == "done"
    assert t1["cause"] is None and t2["cause"] is None
    assert t1["started"] and t1["ended"] and t2["started"] and t2["ended"]
    assert int(t1["peak_rss_mb"]) > 0 and int(t2["peak_rss_mb"]) > 0
    # telemetría de ambas tasks (el vigilante vive en cada driver)
    tasks_vistas = {r["task"] for r in _telemetry_rows(p["telemetry"])}
    assert tasks_vistas == {"0:T1", "1:T2"}
    assert not p["killflag"].exists()


# ----------------------------- (g/h) driver huérfano de su lanzador ---------------------
# Camino per-task real: run_task_headless.py lanza el driver y SALE en cuanto la task arranca.
# Regresión 2026-10-21: con `driver_pid=$PPID` el vigilante vigilaba al lanzador (en un subshell
# `$PPID` es el padre del driver, no el driver) y se apagaba tras la 1.ª muestra; producción y
# las corridas smoke de sqx-headless dejaban 1 fila de telemetría por task. f2f no lo cazaba
# (le basta 1 fila por task).

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _launch_orphan_driver(proj: Path, http_port: int, tmp_path: Path, **extra: str) -> int:
    """Lanza el driver desde un padre que sale en el acto. Devuelve el PID del driver."""
    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    MOCK_REGISTRY.append(mock.name)
    _TEST_PATTERNS.append(f"{proj.name}_driver_")   # auto-copia del driver en /tmp (teardown)
    extra.setdefault("WD_PROC_PATTERN", mock.name)
    extra.setdefault("WD_FREE_PCT_MIN", "0")
    env = _mock_env(proj, tmp_path, mock, tmp_path / f"snap_{uuid4().hex[:8]}.json", **extra)
    launcher = subprocess.run(
        ["bash", "-c", 'bash "$0" "$1" "$2" "" 0 > "$3" 2>&1 & echo $!',
         str(DRIVER), proj.name, str(http_port), str(tmp_path / "driver_stdout.log")],
        env=env, capture_output=True, text=True, check=True, timeout=30)
    return int(launcher.stdout.strip())


def test_f2g_vigilante_sobrevive_a_su_lanzador(tmp_path, http_port, monkeypatch):
    """Con el lanzador ya muerto, el vigilante sigue midiendo y mata por RAM."""
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    name = f"wdorph_{uuid4().hex[:6]}"
    proj = make_proj(tmp_path / "projects", name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    pid = _launch_orphan_driver(proj, http_port, tmp_path, MOCK_LINGER="5",
                                WD_SLEEP_SECS="1", WD_GRACE_SECS="2",
                                WD_WARN_RSS_MB="50", WD_KILL_RSS_MB="60",
                                MOCK_GROW="8:200:0.5", MOCK_FIN_SECS="999",
                                POLL_MIN="0", MAXITER="3000")
    assert _wait_for(lambda: not _alive(pid), timeout=90), \
        "el driver no terminó: el vigilante no mató al motor que crecía"
    assert "WATCHDOG KILL" in _monitor_log(name)
    p = _paths(tmp_path, name)
    assert len(_telemetry_rows(p["telemetry"])) >= 2
    task = _task(p["state"], "0:T1")
    assert task["status"] == "failed"
    assert task["cause"] == "ram_kill"


def test_f2h_vigilante_se_apaga_si_matan_al_driver(tmp_path, http_port, monkeypatch):
    """kill -9 al driver no dispara su trap: el vigilante debe apagarse solo."""
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    name = f"wddead_{uuid4().hex[:6]}"
    proj = make_proj(tmp_path / "projects", name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    pid = _launch_orphan_driver(proj, http_port, tmp_path, MOCK_LINGER="5",
                                WD_SLEEP_SECS="1", WD_KILL_RSS_MB="100000",
                                MOCK_FIN_SECS="999", POLL_MIN="0", MAXITER="3000")
    tele = _paths(tmp_path, name)["telemetry"]
    assert _wait_for(lambda: tele.exists() and len(_telemetry_rows(tele)) >= 3, timeout=60), \
        "el vigilante no llegó a 3 muestras con el driver vivo"
    os.kill(pid, signal.SIGKILL)
    time.sleep(3)   # > WD_SLEEP_SECS: el vigilante ya vio que el driver no existe
    n = len(_telemetry_rows(tele))
    time.sleep(3)
    assert len(_telemetry_rows(tele)) == n, "el vigilante sigue muestreando con el driver muerto"


# ----------------------------- (i) vigilante en MODO PRODUCCIÓN: nombre exacto de proceso -----------------
# Guardrail `sqx-headless-fallos` (Fase 1, R5): en producción WD_PROC_PATTERN va VACÍO y el vigilante mide y
# mata con `pgrep -x sqcli` (antes `pgrep -f` con el texto del bundle). Todos los tests de arriba definen
# WD_PROC_PATTERN (apuntan al mock Python), así que este camino —el que protege las corridas reales— no
# estaba cubierto: aquí el motor falso es un binario compilado con el nombre `sqcli_<uuid>` (un script
# Python no puede tener nombre de proceso propio) y SQX_ENGINE_PROC_NAME apunta a él.

MOCK_ENGINE_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
static void nap(double s) {
    struct timespec ts;
    ts.tv_sec = (time_t)s;
    ts.tv_nsec = (long)((s - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
}
int main(void) {
    const char *e;
    double fin = (e = getenv("MOCK_FIN_SECS")) ? atof(e) : 0.3;
    double linger = (e = getenv("MOCK_LINGER")) ? atof(e) : 30;
    if ((e = getenv("MOCK_RSS_MB"))) {          /* RSS real: páginas con datos aleatorios (incompresibles) */
        size_t n = (size_t)(atof(e) * 1048576.0);
        volatile char *p = malloc(n);   /* volatile: sin él -O1 elimina la reserva y las escrituras */
        if (p) for (size_t i = 0; i < n; i++) p[i] = (char)rand();
    }
    printf("List of available projects\n"); fflush(stdout);
    nap(fin);
    printf("Project finished\n"); fflush(stdout);
    nap(linger);
    return 0;
}
"""


def test_f1_vigilante_con_pattern_vacio_mide_y_mata_por_nombre_exacto(tmp_path, http_port, monkeypatch,
                                                                      request):
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    binary = build_c(tmp_path / "c", "mock_engine", MOCK_ENGINE_C)
    if binary is None:
        pytest.skip("sin compilador cc: no hay forma de fabricar un motor falso con nombre de proceso propio")
    engine = unique_engine_name()
    link = tmp_path / "bin" / engine
    link.parent.mkdir()
    link.symlink_to(binary)
    name = f"wdnom_{uuid4().hex[:6]}"
    proj = make_proj(tmp_path / "projects", name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    _TEST_PATTERNS.extend([f"{name}_driver_", engine])
    request.addfinalizer(lambda: shutil.rmtree(f"/tmp/{name}_run", ignore_errors=True))   # scratch del driver
    env = _mock_env(proj, tmp_path, link, tmp_path / "snap.json",
                    SQX_ENGINE_PROC_NAME=engine,
                    WD_PROC_PATTERN="",                       # PRODUCCIÓN: vacío -> pgrep -x <nombre exacto>
                    WD_FREE_PCT_MIN="0",                      # la RAM libre de la máquina no debe matar
                    WD_SLEEP_SECS="1", WD_GRACE_SECS="2",
                    WD_WARN_RSS_MB="50", WD_KILL_RSS_MB="60",
                    MOCK_RSS_MB="100", MOCK_FIN_SECS="999", MOCK_LINGER="5",
                    POLL_MIN="0", MAXITER="3000")
    proc = subprocess.run(["bash", str(DRIVER), name, str(http_port), "", "0"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    sqpid = re.search(r"sqcli PID=(\d+)", proc.stdout)
    assert sqpid, proc.stdout
    log = _monitor_log(name)
    # mide: la telemetría registra el PID del motor falso (hallado por nombre exacto) con su RSS real
    rows = [r for r in _telemetry_rows(_paths(tmp_path, name)["telemetry"]) if r["pid"] == sqpid.group(1)]
    assert rows and max(int(r["rss_mb"]) for r in rows) >= 60, (rows, log)
    # mata: SIGTERM al motor por nombre exacto; kill.flag recogido por el driver; state failed/ram_kill
    assert "WATCHDOG KILL" in log and "KILL FLAG del vigilante" in proc.stdout
    task = _task(_paths(tmp_path, name)["state"], "0:T1")
    assert task["status"] == "failed" and task["cause"] == "ram_kill"
    assert subprocess.run(["pgrep", "-x", engine], capture_output=True).returncode != 0


# ----------------------------- (j) C3 (Fase 2): pgrep ROTO en el vigilante = fail-closed -----------------------
# Revisión de código de la Fase 1 (C3): `wd_pids` devolvía la lista vacía cuando `pgrep` fallaba con rc 2/3 (no
# "sin coincidencias", que es rc 1): el vigilante creía que no había motor y dejaba de proteger la RAM. Ahora
# `wd_pids` propaga el rc, la ronda no mide ni mata, y el vigilante registra y REINTENTA en la siguiente.

import stat  # noqa: E402

from test_slim_hooks import shell_function  # noqa: E402  extrae la función REAL del script
from test_slim_hooks import fake_engine, pid_alive, procs  # noqa: E402,F401

REAL_PGREP = shutil.which("pgrep") or "/usr/bin/pgrep"


def _fake_pgrep(dirpath: Path, body: str) -> str:
    dirpath.mkdir(parents=True, exist_ok=True)
    sh = dirpath / "pgrep"
    sh.write_text("#!/bin/bash\n" + body)
    sh.chmod(sh.stat().st_mode | stat.S_IXUSR)
    return str(dirpath)


def _run_fn(script: str, path_prefix: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)


def test_c3_wd_pids_distingue_sin_coincidencias_de_pgrep_roto(tmp_path, procs):
    fn = shell_function(DRIVER, "wd_pids")
    name = unique_engine_name()
    motor = fake_engine(procs, tmp_path / "bin", name)
    head = f'ENGINE_PROC="{name}"; WD_PROC_PATTERN=""\n{fn}\n'
    # motor vivo: lista su PID y rc 0
    r = _run_fn(head + 'wd_pids; echo "rc=$?"')
    assert r.stdout.split() == [str(motor.pid), f"rc=0"], r.stdout
    # nadie con ese nombre (pgrep rc 1): rc 0 y lista VACÍA (es normal)
    r = _run_fn(f'ENGINE_PROC="{unique_engine_name()}"; WD_PROC_PATTERN=""\n{fn}\nwd_pids; echo "rc=$?"')
    assert r.stdout.split() == ["rc=0"], r.stdout
    # pgrep ROTO (rc 2): rc 2 y sin lista: el llamador no debe fiarse
    roto = _fake_pgrep(tmp_path / "fb1", "exit 2\n")
    r = _run_fn(head + 'wd_pids; echo "rc=$?"', roto)
    assert r.stdout.split() == ["rc=2"], r.stdout
    # con WD_PROC_PATTERN (tests): usa `pgrep -f` y propaga igual el rc roto
    r = _run_fn(f'ENGINE_PROC=x; WD_PROC_PATTERN="loquesea"\n{fn}\nwd_pids; echo "rc=$?"', roto)
    assert r.stdout.split() == ["rc=2"], r.stdout


def test_c3_wd_kill_con_pgrep_roto_no_declara_el_kill_y_con_pgrep_sano_si(tmp_path, procs):
    """`wd_kill` (función REAL): con pgrep roto NO escribe el kill.flag (diría "ram_kill" sin haber matado nada) y
    devuelve rc 1 para que el vigilante reintente; con pgrep sano escribe el flag y mata al motor."""
    fns = "\n".join(shell_function(DRIVER, n) for n in ("wd_pids", "wd_kill"))
    name = unique_engine_name()
    motor = fake_engine(procs, tmp_path / "bin", name)
    flag = tmp_path / "k.flag"
    head = (f'ENGINE_PROC="{name}"; WD_PROC_PATTERN=""; KILLFLAG="{flag}"; LOG=/dev/null; WD_GRACE_SECS=0\n'
            f'ts(){{ date +%H:%M:%S; }}\n{fns}\n')
    roto = _fake_pgrep(tmp_path / "fb2", "exit 2\n")
    r = _run_fn(head + 'wd_kill "RSS de prueba" 1; echo "rc=$?"', roto)
    assert "rc=1" in r.stdout and "KILL ABORTADO" in r.stdout and "pgrep falló (rc=2)" in r.stdout, r.stdout
    assert not flag.exists() and pid_alive(motor.pid), "con pgrep roto no se declara ni se hace ningún kill"
    r = _run_fn(head + 'wd_kill "RSS de prueba" 1; echo "rc=$?"')
    assert "rc=0" in r.stdout, r.stdout
    assert flag.exists() and "cause=ram_kill" in flag.read_text()
    assert _wait_for(lambda: not pid_alive(motor.pid), timeout=10), "con pgrep sano el motor recibe su SIGTERM"


def test_c3_el_vigilante_registra_el_fallo_de_pgrep_y_reintenta_en_la_ronda_siguiente(tmp_path, http_port,
                                                                                     monkeypatch):
    """E2E: un pgrep que falla (rc 2) las 2 primeras veces que el vigilante lo llama. El vigilante lo REGISTRA
    ("WATCHDOG: pgrep falló") y reintenta: en rondas posteriores vuelve a medir al motor (telemetría con su PID)
    y el driver completa su task con normalidad."""
    monkeypatch.setattr(bank_plan.gui_slim, "live_processes", lambda: [])
    name = f"wdflaky_{uuid4().hex[:6]}"
    proj = make_proj(tmp_path / "projects", name, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out", "ExtraC"])
    counter = tmp_path / "pgrep_fallos"
    fb = _fake_pgrep(tmp_path / "fb3",
                     f'for a in "$@"; do\n  case "$a" in mock_sqcli_*)\n'
                     f'    n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"\n'
                     f'    [ "$n" -le 2 ] && exit 2 ;;\n  esac\ndone\nexec {REAL_PGREP} "$@"\n')
    proc = run_driver(proj, http_port, tmp_path, WD_SLEEP_SECS="1", MOCK_FIN_SECS="7", POLL_MIN="0", MAXITER="3000",
                      PATH=f"{fb}:{os.environ['PATH']}", SQX_ENGINE_PROC_NAME=unique_engine_name(),
                      SQX_APP=str(tmp_path / "NoSQX.app"))   # nunca el sqcli/SQX reales de la máquina
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _monitor_log(name)
    assert log.count("WATCHDOG: pgrep falló (rc=2)") == 2, log
    assert "esta ronda no mido ni mato" in log
    rows = _telemetry_rows(_paths(tmp_path, name)["telemetry"])
    con_pid = [r for r in rows if int(r["pid"]) > 0]
    assert con_pid, "tras los fallos el vigilante volvió a medir al motor (reintento)"
    assert int(counter.read_text().strip()) >= 3
    assert _task(_paths(tmp_path, name)["state"], "0:T1")["status"] == "done"


# ----------------------------- (k) causas nuevas del state (Fase 2: R6 y R1) -----------------------------

def test_k_valid_causes_incluye_user_stop_y_sync_unconfirmed(tmp_path):
    """`user_stop` (parada del usuario con la task persistida) y `sync_unconfirmed` (sync final sin confirmar:
    motor dejado vivo) son causas válidas del state, también desde la CLI que usan los .sh."""
    assert {"user_stop", "sync_unconfirmed"} <= set(RS.VALID_CAUSES)
    state = tmp_path / "state.json"
    t = RS.set_task(state, "0:T1", "failed", cause="user_stop")
    assert t["status"] == "failed" and t["cause"] == "user_stop"
    t = RS.set_task(state, "1:T2", "failed", cause="sync_unconfirmed")
    assert t["cause"] == "sync_unconfirmed"
    for cause in ("user_stop", "sync_unconfirmed"):
        r = subprocess.run([sys.executable, str(ROOT / "sqx" / "run_state.py"), str(state), "set", "--task", "2:T3",
                            "--status", "failed", "--cause", cause], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["cause"] == cause
    with pytest.raises(RS.StateError):
        RS.set_task(state, "3:T4", "failed", cause="otra_causa")
