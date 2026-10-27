"""Tests de la Fase 1 del guardrail `sqx-headless-fallos` (2026-10-21): PROCESOS — R5, R7, R2, R3, R8 (+ A6/A7).

Criterios de aceptación del plan (§3 Fase 1) -> tests de este fichero (y de los módulos que se citan):

  F1-a (R5)  el motor se detecta por NOMBRE EXACTO de proceso ..... test_f1a_*
  F1-b (R5)  guardián estático: sin patrones anchos en los 9 ficheros test_f1b_*
  F1-c (R7)  orquestador / driver / cascada por regex ANCLADO ...... test_f1c_*
  F1-d (R7)  monitor: estado, botón "Ejecutar" real y rechazo de RT  test_f1d_*
             (+ tests/sqx/test_sqx_monitor.py: orchestrator_status con state.json sintético)
  F1-e (R2)  camino directo sin HTTP: zombi / saturado / --force-kill; tails solo del grupo
             ...................................................... test_f1e_*
             (+ tests/test_stop_and_persist_idle.py: camino CON HTTP, barrera de carga, A3, A6)
  F1-f (R3)  barrera del orquestador por PROPIEDAD ................. test_f1f_*
  F1-g (R8)  parseo de argumentos del orquestador ................. test_f1g_*
  F1-h       `bash -n` de los 4 .sh (la regresión es la suite del stack) test_f1h_*
  F1-i (A7)  un solo pipeline por proyecto ......................... test_f1i_*

Fase 2 (R6 y revisión de código C1-C7): los tests A6 pasan de "no actúo" a MODO PIPELINE (bandera de parada +
delegación en el driver) y se añaden `test_c1_*` … `test_c7_*` (pgrep roto = fail-closed en la barrera, wait_driver
y A7; `pkill -g` con el pgid vacío; LAST_DRIVER_PID entero). El e2e de la parada está en tests/test_pipeline_stop.py.

Todo con procesos REALES de nombre y ruta ÚNICOS (uuid): el motor falso se llama `sqcli_<uuid8>` y se apunta
SQX_ENGINE_PROC_NAME a él; los orquestadores/drivers falsos son `bash <fichero>` con el nombre de fichero que
casa el regex. NUNCA se toca un sqcli real: lo único que se mata son los PIDs/grupos que crea cada test
(fixture `procs`: teardown con killpg aunque el test falle). Infraestructura en tests/test_slim_hooks.py.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "sqx"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(ROOT / "tests") not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx
    sys.path.append(str(ROOT / "tests"))

import builder_registry as REG  # noqa: E402
import run_task_headless as RT  # noqa: E402
import stop_and_persist as SP  # noqa: E402
from test_slim_hooks import (  # noqa: E402  infraestructura compartida de procesos falsos
    _wait_for, busy_engine, cmdline_of, fake_engine, fake_launcher, make_proj,
    pgrep_x, pid_alive, procs, unique_engine_name,
)

SQX_DIR = ROOT / "sqx"
ORCH = SQX_DIR / "run_pipeline_per_task.sh"
#: `run_production_night.sh` no viaja a la copia compartible `sqx-headless` (scripts/export_sqx_headless/manifest.yaml):
#: allí se omite; cualquier OTRO fichero que falte sigue haciendo fallar los guardianes.
NOT_IN_COPY = {"run_production_night.sh"}


def _present(names):
    return [f for f in names if f not in NOT_IN_COPY or (SQX_DIR / f).exists()]


FOUR_SH = _present(["run_pipeline_headless.sh", "run_pipeline_per_task.sh", "run_cascade_projects.sh",
                    "run_production_night.sh"])
PHASE1_SOURCES = ["stop_and_persist.py", "run_task_headless.py", "retry_project.py", "gui_slim.py",
                  "sqx_monitor.py", *FOUR_SH]


# ----------------------------- helpers -----------------------------------------------

def proj_name(prefix: str = "fx") -> str:
    """Nombre de proyecto ÚNICO (el regex de orquestador/driver discrimina por proyecto)."""
    return f"{prefix}{uuid4().hex[:6]}"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _nunca_el_sqcli_real(monkeypatch, tmp_path):
    """Seguridad de plataforma: SI un test llegara a lanzar un driver de verdad (un fallo del código bajo
    prueba), `$SQCLI` apunta a un fichero inexistente y `SQX_APP` a una app inexistente: jamás se arranca el
    motor REAL de SQX de esta máquina. (Provenencia, 2026-10-21: durante el desarrollo de esta fase un test
    de puerto con el listener mal hecho dejó pasar la barrera y el driver lanzado arrancó el sqcli real
    unos minutos, hasta que se le hizo kill -9.)"""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))


@pytest.fixture()
def engine_env(monkeypatch):
    """Nombre único de motor falso para ESTE test: el stack ignora cualquier sqcli real."""
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)
    return name


@pytest.fixture()
def fast_probes(monkeypatch):
    """Sondeos y gracias cortos para el camino directo de stop_and_persist."""
    monkeypatch.setattr(SP, "CPU_SAMPLES", 2)
    monkeypatch.setattr(SP, "CPU_INTERVAL", 0.3)
    monkeypatch.setattr(SP, "KILL_GRACE_SECS", 2.0)


@pytest.fixture()
def pipeline_leftovers():
    """Registra proyectos (nombre ÚNICO) cuyos restos se limpian al terminar aunque el test falle: la
    auto-copia del orquestador y el scratch del driver en /tmp, y cualquier orquestador/driver/lanzador que
    haya quedado vivo (su grupo de procesos entero)."""
    projs: list[str] = []
    yield projs
    for p in projs:
        pgids = {SP.engine_owner_pgid(x) for x in {*SP.driver_pids(p), *SP.orchestrator_pids(p)}}
        lanzadores = subprocess.run(["pgrep", "-f", rf"run_task_headless\.py {SP._ere_escape(p)} "],
                                    capture_output=True, text=True).stdout.split()
        for x in lanzadores:
            try:
                os.kill(int(x), signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pg in pgids - {0, 1, os.getpgrp()}:
            try:
                os.killpg(pg, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pattern in (f"{p}_pertask_*", f"{p}_driver_*", f"{p}_task*.log"):
            for f in Path("/tmp").glob(pattern):
                f.unlink(missing_ok=True)
        shutil.rmtree(f"/tmp/{p}_run", ignore_errors=True)


FAST_BARRIER = {"BARRIER_TESTING": "1", "BARRIER_OWN_GRACE_SECS": "1", "BARRIER_FOREIGN_SECS": "2",
                "BARRIER_TERM_SECS": "1", "BARRIER_POLL_SECS": "0.2", "PORT_WAIT_SECS": "2"}


def orch_run(tmp_path: Path, proj: str, *args, engine: str, port: int | None = None,
             extra: dict | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    """Ejecuta el orquestador REAL contra el fixture `tmp_path/projects` con el entorno de tests."""
    env = os.environ.copy()
    env.update({"PROJECTS_DIR": str(tmp_path / "projects"), "SQX_OUTDIR": str(tmp_path / "out"),
                "SLIM_TESTING": "1", "SQX_ENGINE_PROC_NAME": engine,
                "PORT": str(port or free_port()), "WAIT_MARGIN": "0.2", "WAIT_SLEEP": "0.2"})
    env.update(extra or {})
    return subprocess.run(["bash", str(ORCH), proj, *map(str, args)], env=env,
                          capture_output=True, text=True, timeout=timeout)


def make_pipeline_proj(tmp_path: Path, proj: str, n_tasks: int = 2, orphan_bak: bool = False) -> Path:
    """Fixture de proyecto con `n_tasks` Retest. `orphan_bak` deja un .cfx.pre_run_task.bak huérfano: el
    lanzador de la 1.ª task falla al instante (H8), así el test acaba justo tras la barrera sin lanzar motores."""
    tasks = [(f"T{i + 1}", "Retest", ["InA"], f"Out{i}") for i in range(n_tasks)]
    d = make_proj(tmp_path / "projects", proj, tasks, ["InA", *(f"Out{i}" for i in range(n_tasks))])
    if orphan_bak:
        (d / "project.cfx.pre_run_task.bak").write_bytes((d / "project.cfx").read_bytes())
    return d


# =========================================================================================
# F1-a (R5) — el motor se detecta por nombre EXACTO de proceso
# =========================================================================================

def test_f1a_symlink_a_sleep_llamado_como_el_motor_esta_en_engine_pids(tmp_path, procs, engine_env):
    fake = fake_engine(procs, tmp_path / "bin", engine_env)
    assert fake.pid in SP.engine_pids()


def test_f1a_texto_de_cmdline_no_es_un_motor(tmp_path, procs, engine_env):
    """Un shell cuyo cmdline CONTIENE el texto `/Resources/sqcli` no es un motor (incidente 2026-10-13: la
    barrera del orquestador le hizo `kill -9` a ese shell y el driver abortaba 'ya hay un sqcli')."""
    bait_shell = procs.spawn(["bash", "-c", "sleep 60; true # /Resources/sqcli"])
    bait_argv = procs.spawn([sys.executable, "-c", "import time; time.sleep(60)",
                             "/Applications/Falso.app/Contents/Resources/sqcli"])
    real = fake_engine(procs, tmp_path / "bin", engine_env)
    # el cebo es de verdad un cebo: el patrón ANCHO antiguo SÍ los casaba (si no, el test no probaría nada)
    assert _wait_for(lambda: all("Resources/sqcli" in cmdline_of(b.pid) for b in (bait_shell, bait_argv)))
    old = subprocess.run(["pgrep", "-f", "/Resources/sqcli"], capture_output=True, text=True).stdout.split()
    assert {str(bait_shell.pid), str(bait_argv.pid)} <= set(old)
    # con el nombre exacto: solo el motor
    pids = SP.engine_pids()
    assert real.pid in pids
    assert bait_shell.pid not in pids and bait_argv.pid not in pids


def test_f1a_nombre_literal_sqcli_sin_override(tmp_path, procs, monkeypatch):
    """El nombre LITERAL `sqcli` (el de producción, sin override). Aserción por PERTENENCIA y no por
    igualdad: la máquina puede tener ya otro proceso llamado sqcli (un motor real, o un resto sin morir);
    el test solo crea y mata el suyo.

    PLAN-DESVIO: el brief pedía `skip` si ya hay un sqcli vivo. Aquí NO se salta: en el Mac de desarrollo hay
    un proceso residual llamado `sqcli` (estado UE, de un experimento, imposible de matar) que haría saltar
    el test para siempre; con la aserción por pertenencia no hay riesgo (solo se toca el PID propio)."""
    monkeypatch.delenv("SQX_ENGINE_PROC_NAME", raising=False)
    assert SP.engine_proc_name() == "sqcli"
    fake = fake_engine(procs, tmp_path / "bin", "sqcli")
    assert fake.pid in SP.engine_pids()


def test_f1a_zombi_no_cuenta_como_motor(tmp_path, procs, engine_env):
    """Un motor muerto sin `wait` (<defunct>) no lo ve `pgrep -x` ni cuenta como vivo para kill_engine."""
    fake = fake_engine(procs, tmp_path / "bin", engine_env)
    os.kill(fake.pid, signal.SIGKILL)          # muerto, sin wait: queda <defunct>
    assert _wait_for(lambda: fake.pid not in SP.engine_pids(), timeout=5)
    assert SP._pid_alive(fake.pid) is False
    assert SP.kill_engine([fake.pid], grace=1) is True


# =========================================================================================
# F1-b (R5) — guardián estático
# =========================================================================================

def test_f1b_guardian_estatico_sin_patrones_anchos_de_motor():
    """Ni `pgrep -f`/`pkill -f` con el texto del bundle del motor, ni `pkill` global de `tail -f /dev/null`
    (solo se permite `pkill -g <pgid>` / `pgrep -g`, acotado al grupo del motor) en los 9 ficheros de la fase."""
    bad: list[str] = []
    for rel in PHASE1_SOURCES:
        for n, line in enumerate((SQX_DIR / rel).read_text(encoding="utf-8").splitlines(), 1):
            if "Resources/sqcli" in line:
                bad.append(f"{rel}:{n}: patrón ancho del motor: {line.strip()}")
            if "pkill" in line and "tail -f /dev/null" in line and not re.search(r"(^|[\s\"'])-g\b", line):
                bad.append(f"{rel}:{n}: pkill global de tail -f /dev/null: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_f1b_los_sh_detectan_el_motor_por_nombre_exacto():
    for rel in FOUR_SH:
        txt = (SQX_DIR / rel).read_text(encoding="utf-8")
        assert 'pgrep -x "$ENGINE_PROC"' in txt, f"{rel}: falta la detección por nombre exacto"
        assert 'ENGINE_PROC="${SQX_ENGINE_PROC_NAME:-sqcli}"' in txt, f"{rel}: sin override de tests"
    # el driver conserva `pgrep -f "$WD_PROC_PATTERN"` SOLO si el pattern está definido (tests)
    drv = (SQX_DIR / "run_pipeline_headless.sh").read_text(encoding="utf-8")
    assert "WD_PROC_PATTERN=${WD_PROC_PATTERN:-}" in drv
    # wait_driver ya no usa el patrón ancho "<proj>_driver_"
    assert 'pgrep -f "${PROJ}_driver_"' not in (SQX_DIR / "run_pipeline_per_task.sh").read_text(encoding="utf-8")


# =========================================================================================
# F1-c (R7) — orquestador / driver / cascada por regex ANCLADO
# =========================================================================================

def test_f1c_orchestrator_pids_formas_reales_y_falsos_positivos(tmp_path, procs, pipeline_leftovers):
    p = proj_name()
    pipeline_leftovers.append(p)                            # borra lo que dejemos en /tmp
    orig = fake_launcher(procs, tmp_path / "d1", "run_pipeline_per_task.sh", p, 0)
    copia = fake_launcher(procs, tmp_path / "d2", f"{p}_pertask_AbC123", p, 0, "--slim")
    # la auto-copia REAL vive en /tmp (mktemp /tmp/<proj>_pertask_XXXXXX) y el monitor deja su log en /tmp
    copia_tmp = fake_launcher(procs, Path("/tmp"), f"{p}_pertask_Zy9Xw8", p, 0, "--slim")
    log = Path("/tmp") / f"{p}_pertask_1792585136.log"      # `tail -f` de ese log NO es un orquestador
    log.write_text("x")
    tail = procs.spawn(["tail", "-f", log])
    otro = fake_launcher(procs, tmp_path / "d3", f"{p}2_pertask_AbC123", f"{p}2", 0)     # prefijo común
    otro_orig = fake_launcher(procs, tmp_path / "d4", "run_pipeline_per_task.sh", f"{p}2", 0)
    drv = fake_launcher(procs, tmp_path / "d5", f"{p}_driver_AbC123", p, 5050, "", 0)    # un driver no es un orquestador
    assert _wait_for(lambda: "tail" in cmdline_of(tail.pid))

    mine = SP.orchestrator_pids(p)
    assert orig.pid in mine and copia.pid in mine and copia_tmp.pid in mine
    for falso in (tail, otro, otro_orig, drv):
        assert falso.pid not in mine
    # el proyecto de prefijo común ve SOLO los suyos
    other = SP.orchestrator_pids(f"{p}2")
    assert otro.pid in other and otro_orig.pid in other
    assert orig.pid not in other and copia.pid not in other
    # sin proyecto: cualquiera (pertenencia: en la máquina puede haber otros orquestadores)
    anyp = SP.orchestrator_pids()
    assert {orig.pid, copia.pid, otro.pid, otro_orig.pid} <= set(anyp)
    assert tail.pid not in anyp and drv.pid not in anyp


def test_f1c_caffeinate_devuelve_el_bash_y_no_el_helper(tmp_path, procs):
    """`caffeinate -i bash <script> <proy>` deja en la tabla de procesos un helper `caffeinate -i bash …` con el
    cmdline COMPLETO del script (en macOS 26 es hijo del bash): el patrón ancho de antes lo tomaba por el
    orquestador y por eso el monitor solo lo veía "vivo" mientras el helper seguía ahí (O6). El regex anclado
    devuelve el `bash`, nunca el helper."""
    if not shutil.which("caffeinate"):
        pytest.skip("sin `caffeinate` (solo macOS): el caso del monitor 2026-10-21 no se puede reproducir")
    p = proj_name()
    raiz = fake_launcher(procs, tmp_path / "d", "run_pipeline_per_task.sh", p, 0, prefix=("caffeinate", "-i"))

    def arbol() -> list[int]:
        hijos = subprocess.run(["pgrep", "-P", str(raiz.pid)], capture_output=True, text=True).stdout.split()
        return [raiz.pid, *map(int, hijos)]

    assert _wait_for(lambda: any(cmdline_of(x).startswith("caffeinate") for x in arbol())
                     and any(cmdline_of(x).startswith("bash ") for x in arbol()), timeout=10), \
        [cmdline_of(x) for x in arbol()]
    helpers = [x for x in arbol() if cmdline_of(x).startswith("caffeinate")]
    bashes = [x for x in arbol() if cmdline_of(x).startswith("bash ")]
    # sanity: el patrón ancho antiguo SÍ casaba con el helper
    viejo = subprocess.run(["pgrep", "-f", f"run_pipeline_per_task.sh {p}"], capture_output=True,
                           text=True).stdout.split()
    assert {str(h) for h in helpers} <= set(viejo)
    assert _wait_for(lambda: all(b in SP.orchestrator_pids(p) for b in bashes), timeout=10)
    for h in helpers:
        assert h not in SP.orchestrator_pids(p) and h not in SP.orchestrator_pids()


def test_f1c_driver_pids_formas_reales_y_falsos_positivos(tmp_path, procs, pipeline_leftovers):
    p = proj_name()
    pipeline_leftovers.append(p)                            # borra lo que dejemos en /tmp
    orig = fake_launcher(procs, tmp_path / "d1", "run_pipeline_headless.sh", p, 5050, "", 0)
    copia = fake_launcher(procs, tmp_path / "d2", f"{p}_driver_AbC123", p, 5050, "", 0, "--slim")
    copia_tmp = fake_launcher(procs, Path("/tmp"), f"{p}_driver_Zy9Xw8", p, 5050, "", 0)   # auto-copia REAL en /tmp
    log = tmp_path / f"{p}_driver_notes.log"
    log.write_text("x")
    tail = procs.spawn(["tail", "-f", log])
    otro = fake_launcher(procs, tmp_path / "d3", f"{p}2_driver_AbC123", f"{p}2", 5050, "", 0)
    orq = fake_launcher(procs, tmp_path / "d4", f"{p}_pertask_AbC123", p, 0)             # un orquestador no es un driver
    assert _wait_for(lambda: "tail" in cmdline_of(tail.pid))

    mine = SP.driver_pids(p)
    assert orig.pid in mine and copia.pid in mine and copia_tmp.pid in mine
    for falso in (tail, otro, orq):
        assert falso.pid not in mine
    assert otro.pid in SP.driver_pids(f"{p}2") and orig.pid not in SP.driver_pids(f"{p}2")
    anyp = SP.driver_pids()
    assert {orig.pid, copia.pid, otro.pid} <= set(anyp)
    assert tail.pid not in anyp and orq.pid not in anyp


def test_f1c_cascade_pids(tmp_path, procs):
    p = proj_name()
    casc = fake_launcher(procs, tmp_path / "d1", "run_cascade_projects.sh", 0, p)
    log = tmp_path / "cascada_x.log"
    log.write_text("x")
    tail = procs.spawn(["tail", "-f", log])
    orq = fake_launcher(procs, tmp_path / "d2", "run_pipeline_per_task.sh", p, 0)
    assert _wait_for(lambda: "tail" in cmdline_of(tail.pid))
    pids = SP.cascade_pids()
    assert casc.pid in pids
    assert tail.pid not in pids and orq.pid not in pids


def test_f1c_regex_del_shell_igual_al_de_python_y_funciona_con_pgrep(tmp_path, procs):
    """MM-H14 (d): el regex del DRIVER tal como lo construye `wait_driver` en shell (PROJ_ERE / ORCH_RE /
    DRIVER_RE del orquestador, evaluadas en bash) es idéntico al de Python y detecta de verdad."""
    src = ORCH.read_text(encoding="utf-8")
    lines = [re.search(rf"^{v}=.*$", src, re.M) for v in ("PROJ_ERE", "ORCH_RE", "DRIVER_RE")]
    assert all(lines), "no encuentro PROJ_ERE/ORCH_RE/DRIVER_RE en el orquestador"
    snippet = "\n".join(['PROJ="$1"', *(m.group(0) for m in lines),
                         'printf "%s\\n%s\\n" "$ORCH_RE" "$DRIVER_RE"'])
    for proj in ("p", "dax40_4h", "mi.proj", "a+b", "x(y)", "q[1]", "Real OOS", "w$z"):
        out = subprocess.run(["bash", "-c", snippet, "_", proj], capture_output=True, text=True).stdout
        orch_re, drv_re = out.splitlines()
        assert orch_re == SP.orchestrator_re(proj), proj
        assert drv_re == SP.driver_re(proj), proj
    # y detecta procesos reales (lo que usa wait_driver)
    p = proj_name()
    a = fake_launcher(procs, tmp_path / "d1", f"{p}_driver_AbC123", p, 5050, "", 0)
    b = fake_launcher(procs, tmp_path / "d2", "run_pipeline_headless.sh", p, 5050, "", 0)
    log = tmp_path / f"{p}_driver_AbC123.log"
    log.write_text("x")
    tail = procs.spawn(["tail", "-f", log])
    assert _wait_for(lambda: "tail" in cmdline_of(tail.pid))
    out = subprocess.run(["bash", "-c", snippet, "_", p], capture_output=True, text=True).stdout
    drv_re = out.splitlines()[1]
    found = {int(x) for x in subprocess.run(["pgrep", "-f", drv_re], capture_output=True,
                                            text=True).stdout.split()}
    assert {a.pid, b.pid} <= found and tail.pid not in found
    assert found == set(SP.driver_pids(p))


def test_f1c_proyecto_con_metacaracteres_no_inyecta_regex(tmp_path, procs):
    """Un proyecto `a.c` no debe casar con `abc` (el punto se escapa)."""
    real = f"x{uuid4().hex[:5]}.c"
    falso = real.replace(".", "X")
    a = fake_launcher(procs, tmp_path / "d1", f"{real}_driver_AbC123", real, 5050, "", 0)
    b = fake_launcher(procs, tmp_path / "d2", f"{falso}_driver_AbC123", falso, 5050, "", 0)
    assert a.pid in SP.driver_pids(real) and b.pid not in SP.driver_pids(real)
    assert b.pid in SP.driver_pids(falso)


# ----------------------------- pre_gui_check (global, solo lectura) -----------------------------

@pytest.fixture()
def quiet_probes(monkeypatch):
    """Anula las sondas globales de procesos (en la máquina puede haber GUI, orquestadores o motores reales)."""
    for fn in ("engine_pids", "gui_pids", "orchestrator_pids", "driver_pids", "cascade_pids"):
        monkeypatch.setattr(SP, fn, lambda *a, **k: [])


def test_pre_gui_check_limpio_es_seguro_y_cuenta_sqx(tmp_path, monkeypatch, quiet_probes):
    projects = tmp_path / "projects"
    p = proj_name()
    d = make_proj(projects, p, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    rep = SP.pre_gui_check(p)
    assert rep["safe"] is True and rep["reasons"] == []
    assert rep["databanks"] == {"InA": 1, "Out": 1}
    # databanks_archive/ vacío (manifest consumido) no es un motivo
    (d / "databanks_archive").mkdir()
    assert SP.pre_gui_check(p)["safe"] is True


@pytest.mark.parametrize("probe,needle", [("engine_pids", "sqcli"), ("gui_pids", "GUI"),
                                          ("orchestrator_pids", "orquestador"),
                                          ("driver_pids", "driver"), ("cascade_pids", "cascada")])
def test_pre_gui_check_cada_proceso_vivo_es_un_motivo(tmp_path, monkeypatch, quiet_probes, probe, needle):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(SP, probe, lambda *a, **k: [4321])
    rep = SP.pre_gui_check("cualquier_proyecto")      # GLOBAL: no hace falta que sea del proyecto
    assert rep["safe"] is False
    assert any(needle in r and "4321" in r for r in rep["reasons"]), rep["reasons"]


def test_pre_gui_check_marcador_slim_bancos_archivados_y_bak(tmp_path, monkeypatch, quiet_probes):
    projects = tmp_path / "projects"
    p = proj_name()
    d = make_proj(projects, p, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    arch = d / "databanks_archive"
    (arch / "ExtraC").mkdir(parents=True)
    (arch / ".runner_session.json").write_text("{}")
    (d / "project.cfx.pre_run_task.bak").write_bytes(b"x")
    rep = SP.pre_gui_check(p)
    joined = " | ".join(rep["reasons"])
    assert rep["safe"] is False
    assert "--slim" in joined and "ExtraC" in joined and "pre_run_task.bak" in joined


def test_sondas_de_procesos_fallan_en_cerrado(tmp_path, monkeypatch):
    """Si `pgrep` falla (regex rechazado, binario ausente) NO sabemos qué hay vivo: las compuertas de seguridad
    bloquean en vez de dar luz verde (fail-closed); el estado del monitor, que solo informa, no rompe."""
    with pytest.raises(SP.ProcProbeError):
        SP._pgrep("-f", "(sin_cerrar")                          # pgrep rc 2: regex inválido
    monkeypatch.setattr(SP, "_pgrep", lambda *a, **k: (_ for _ in ()).throw(SP.ProcProbeError("pgrep roto (test)")))
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("SLIM_TESTING", "1")
    rep = SP.pre_gui_check("x")
    assert rep["safe"] is False and any("no pude comprobar" in r for r in rep["reasons"])
    motivo = RT.launch_block_reason("x")
    assert motivo and "No pude comprobar" in motivo and "no lanzo por seguridad" in motivo
    assert RT.run("x", "T", "only")["ok"] is False


def test_pre_gui_check_ve_procesos_reales(tmp_path, procs, monkeypatch, engine_env):
    """Con procesos reales (motor falso + cascada falsa), sin parchear las sondas de motor/cascada."""
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(SP, "gui_pids", lambda: [])
    fake = fake_engine(procs, tmp_path / "bin", engine_env)
    casc = fake_launcher(procs, tmp_path / "c", "run_cascade_projects.sh", 0, proj_name())
    rep = SP.pre_gui_check("sin_proyecto")
    joined = " | ".join(rep["reasons"])
    assert rep["safe"] is False
    assert str(fake.pid) in joined and str(casc.pid) in joined


# =========================================================================================
# F1-d (R7) — monitor: botón "Ejecutar" real y rechazo en RT.run / RT.run_pipeline_per_task
# =========================================================================================

def _ejecutar(at):
    botones = [b for b in at.sidebar.button if "Ejecutar" in b.label]
    return botones[0] if botones else None


def test_f1d_boton_ejecutar_real_deshabilitado_con_orquestador_driver_y_sqcli(tmp_path, procs, monkeypatch):
    """Cableado REAL (AppTest sobre sqx/sqx_monitor.py): el botón "Ejecutar" del sidebar queda deshabilitado
    con un orquestador, con un driver o con un sqcli vivos, y habilitado cuando no hay nada."""
    from streamlit.testing.v1 import AppTest
    try:  # otros módulos de test parchean st.stop a nivel global: usar el real
        from streamlit.commands.execution_control import stop as real_stop
        import streamlit
        monkeypatch.setattr(streamlit, "stop", real_stop)
    except ImportError:
        pass
    projects = tmp_path / "projects"
    p = proj_name("mon")
    make_proj(projects, p, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setattr(REG, "PROJECTS", projects)
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path / "out"))
    monkeypatch.delenv("SLIM_TESTING", raising=False)      # el chequeo de sqcli vivo debe actuar
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)

    def _sin_motor(*a, **k):
        raise OSError("sin motor en :5050 (test)")

    monkeypatch.setattr(urllib.request, "urlopen", _sin_motor)   # motor_alive() -> False: sección visible

    at = AppTest.from_file(str(SQX_DIR / "sqx_monitor.py"), default_timeout=90)
    at.run()
    btn = _ejecutar(at)
    assert btn is not None, "no se pintó el botón Ejecutar (¿falló el script antes?): %s" % list(at.exception)
    assert btn.disabled is False                                    # nada vivo: habilitado

    orq = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", p, 0)
    at.run()
    assert _ejecutar(at).disabled is True
    assert any("orquestador" in w.value for w in at.sidebar.warning), [w.value for w in at.sidebar.warning]
    os.killpg(orq.pid, signal.SIGKILL)
    orq.wait(timeout=5)

    drv = fake_launcher(procs, tmp_path / "d", f"{p}_driver_AbC123", p, 5050, "", 0)
    at.run()
    assert _ejecutar(at).disabled is True
    assert any("driver" in w.value for w in at.sidebar.warning), [w.value for w in at.sidebar.warning]
    os.killpg(drv.pid, signal.SIGKILL)
    drv.wait(timeout=5)

    fake_engine(procs, tmp_path / "bin", name)
    at.run()
    assert _ejecutar(at).disabled is True
    assert any("sqcli vivo" in w.value for w in at.sidebar.warning), [w.value for w in at.sidebar.warning]


@pytest.fixture()
def rt_proyecto(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    p = proj_name("rt")
    make_proj(projects, p, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    monkeypatch.setenv("SLIM_TESTING", "1")
    monkeypatch.delenv("RUNNER_PIPELINE", raising=False)
    return p, projects / p


def test_f1d_rt_run_y_run_pipeline_per_task_rechazan_con_orquestador_falso(tmp_path, procs, rt_proyecto):
    p, projdir = rt_proyecto
    orq = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", p, 0)
    antes = sorted(x.name for x in projdir.iterdir())
    rep = RT.run(p, "T1", "only")
    assert rep["ok"] is False and "orquestador" in rep["msg"] and str(orq.pid) in rep["msg"]
    rep = RT.run_pipeline_per_task(p, 0)
    assert rep["ok"] is False and "orquestador" in rep["msg"] and str(orq.pid) in rep["msg"]
    # se rechaza ANTES de tocar nada: ni .bak ni .cfx mutado, ni logs de lanzamiento
    assert sorted(x.name for x in projdir.iterdir()) == antes
    assert not list(Path("/tmp").glob(f"{p}_task*.log")) and not list(Path("/tmp").glob(f"{p}_pertask_*.log"))


def test_f1d_rt_rechaza_con_driver_falso_y_en_pipeline_solo_el_driver_bloquea(tmp_path, procs, rt_proyecto):
    p, _ = rt_proyecto
    drv = fake_launcher(procs, tmp_path / "d", f"{p}_driver_AbC123", p, 5050, "", 0)
    for rep in (RT.run(p, "T1", "only"), RT.run_pipeline_per_task(p, 0)):
        assert rep["ok"] is False and "driver" in rep["msg"] and str(drv.pid) in rep["msg"]
    # dentro del pipeline (RUNNER_PIPELINE=1: lo exporta el orquestador) el orquestador es el llamador legítimo
    orq = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", p, 0)
    assert RT.launch_block_reason(p, in_pipeline=True) is not None       # el driver sigue bloqueando
    os.killpg(drv.pid, signal.SIGKILL)
    drv.wait(timeout=5)
    assert _wait_for(lambda: not SP.driver_pids(p), timeout=5)
    assert RT.launch_block_reason(p, in_pipeline=True) is None            # solo el orquestador: se permite
    assert "orquestador" in RT.launch_block_reason(p, in_pipeline=False)
    assert orq.pid in SP.orchestrator_pids(p)


def test_f1d_sqcli_vivo_bloquea_salvo_slim_testing(tmp_path, procs, rt_proyecto, monkeypatch, engine_env):
    p, _ = rt_proyecto
    fake = fake_engine(procs, tmp_path / "bin", engine_env)
    # SLIM_TESTING=1 (mocks): un sqcli ajeno de la máquina no cuenta
    assert RT.launch_block_reason(p) is None
    monkeypatch.delenv("SLIM_TESTING")
    motivo = RT.launch_block_reason(p)
    assert motivo and "sqcli vivo" in motivo and str(fake.pid) in motivo


def test_f1d_projects_dir_delega_en_stop_and_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "x"))
    assert RT._projects_dir() == SP.projects_dir() == tmp_path / "x"
    monkeypatch.setenv("PROJECTS_DIR", "")                       # vacío = no definido (igual que paths.sh)
    monkeypatch.setattr(REG, "PROJECTS", tmp_path / "reg")
    assert RT._projects_dir() == SP.projects_dir() == tmp_path / "reg"
    monkeypatch.delenv("PROJECTS_DIR")
    assert SP.projects_dir() == tmp_path / "reg"


# =========================================================================================
# F1-e (R2) — camino directo de stop_and_persist.run SIN HTTP
# =========================================================================================

def test_f1e_cpu_baja_es_zombi_y_se_mata(tmp_path, procs, engine_env, fast_probes):
    eng = fake_engine(procs, tmp_path / "bin", engine_env)               # sleep: CPU ~0 %
    rep = SP.run(proj_name(), port=free_port())
    assert rep["zombi_killed"] is True and rep["nothing_to_stop"] is True
    assert rep["engine_cpu"] < SP.CPU_BUSY_PCT
    assert _wait_for(lambda: not pid_alive(eng.pid), timeout=10)


def test_f1e_cpu_alta_no_se_mata_y_force_kill_si(tmp_path, procs, engine_env, fast_probes):
    eng = busy_engine(procs, tmp_path / "bin", engine_env)               # CPU alta medible (o skip explícito)
    p = proj_name()
    rep = SP.run(p, port=free_port())
    assert rep["engine_busy"] is True and rep["nothing_to_stop"] is False
    assert rep["warnings"] and "saturado" in rep["warnings"][0] and "--force-kill" in rep["warnings"][0]
    assert not rep.get("zombi_killed") and rep["motor_killed"] is False
    assert pid_alive(eng.pid), "un motor con CPU alta NO se mata sin --force-kill"
    # --force-kill sí lo mata, y lo dice
    rep2 = SP.run(p, port=free_port(), force_kill=True)
    assert rep2["zombi_killed"] is True
    assert any("--force-kill" in w for w in rep2["warnings"])
    assert _wait_for(lambda: not pid_alive(eng.pid), timeout=10)


def test_f1e_sin_proceso_no_hay_nada_que_parar(tmp_path, engine_env, fast_probes):
    rep = SP.run(proj_name(), port=free_port())
    assert rep["nothing_to_stop"] is True and not rep.get("zombi_killed") and not rep["warnings"]


def test_f1e_kill_engine_tail_del_mismo_grupo_muere_el_de_otro_grupo_sobrevive(tmp_path, procs, engine_env):
    """R2: `kill_engine` solo mata los `tail -f /dev/null` del grupo de procesos del motor (O8)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / engine_env).symlink_to("/bin/sleep")
    # `tail -f /dev/null | <motor> &` en su propia sesión: bash, tail y motor comparten pgid == pid del bash
    grupo = procs.spawn(["bash", "-c", f"tail -f /dev/null | '{bindir / engine_env}' 120 & sleep 120"])
    assert _wait_for(lambda: len(pgrep_x(engine_env)) == 1, timeout=10)
    motor = pgrep_x(engine_env)[0]
    assert SP.engine_owner_pgid(motor) == grupo.pid

    def tails_del_grupo() -> list[int]:
        r = subprocess.run(["pgrep", "-g", str(grupo.pid), "-f", "^tail -f /dev/null$"],
                           capture_output=True, text=True)
        return [int(x) for x in r.stdout.split()]

    assert _wait_for(lambda: len(tails_del_grupo()) == 1, timeout=10)
    ajeno = procs.spawn(["tail", "-f", "/dev/null"])                      # OTRO grupo (otra sesión)
    assert _wait_for(lambda: "tail" in cmdline_of(ajeno.pid))
    assert SP.engine_owner_pgid(ajeno.pid) != grupo.pid

    assert SP.kill_engine([motor], grace=1) is True
    assert _wait_for(lambda: not tails_del_grupo(), timeout=10), "el tail del grupo del motor debe morir"
    assert pid_alive(ajeno.pid), "un tail -f /dev/null de OTRO grupo NO se toca"


def test_f1e_kill_engine_remata_con_kill9_si_ignora_term(tmp_path, procs, engine_env):
    eng = fake_engine(procs, tmp_path / "bin", engine_env, ignore_term=True)
    assert SP.kill_engine([eng.pid], grace=1) is True
    assert _wait_for(lambda: not pid_alive(eng.pid), timeout=10)


def test_f1e_kill_engine_vacio_o_inexistente_es_true():
    assert SP.kill_engine([], grace=1) is True
    assert SP.kill_engine([2 ** 22 + 12345], grace=1) is True             # PID que no existe


def test_f1e_engine_cpu_mide_y_es_cero_si_no_existe(tmp_path, procs, engine_env):
    eng = fake_engine(procs, tmp_path / "bin", engine_env)
    assert SP.engine_cpu(eng.pid, samples=2, interval=0.1) < SP.CPU_BUSY_PCT
    assert SP.engine_cpu(2 ** 22 + 12345, samples=2, interval=0.0) == 0.0


# ----------------------------- A6: pipeline vivo -> modo pipeline (Fase 2, R6) -----------------------------
# (En la Fase 1 estos tests comprobaban "no actúo"; la Fase 2 lo sustituye por la delegación: bandera de parada +
# una entrega de `stop` por driver + espera. Aquí con procesos REALES falsos; la mecánica fina, en
# tests/test_stop_and_persist_idle.py y el e2e con orquestador/driver reales en tests/test_pipeline_stop.py.)

@pytest.fixture()
def projects_fixture(tmp_path, monkeypatch):
    """PROJECTS_DIR con el directorio del proyecto creado (la bandera se escribe en <proyecto>/)."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    return projects


def test_a6_run_con_driver_del_proyecto_vivo_entra_en_modo_pipeline_y_no_mata_el_motor(
        tmp_path, procs, engine_env, fast_probes, projects_fixture):
    p = proj_name()
    (projects_fixture / p).mkdir()
    eng = fake_engine(procs, tmp_path / "bin", engine_env)
    drv = fake_launcher(procs, tmp_path / "d", f"{p}_driver_AbC123", p, 5050, "", 0)
    rep = SP.run(p, port=free_port(), wait_secs=2)
    assert rep["pipeline_alive"] is True and drv.pid in rep["pipeline_pids"]["driver"]
    assert rep["pipeline_stopping"] is True and rep["pipeline_stopped"] is False and rep["stopped_by"] is None
    assert rep["motor_killed"] is False and not rep.get("zombi_killed")
    assert pid_alive(eng.pid), "con un driver del proyecto vivo no se mata el motor (compite con el driver)"
    assert "parando: el pipeline saldrá al terminar de guardar la task en curso" in rep["warnings"][0]
    assert not (projects_fixture / p / ".pipeline_stop_request").exists(), "sin orquestador vivo la bandera se borra"
    assert pid_alive(drv.pid), "Stop & persist nunca mata al driver: persiste él"


def test_a6_run_con_orquestador_del_proyecto_vivo_deja_la_bandera_y_no_toca_el_motor(
        tmp_path, procs, engine_env, fast_probes, projects_fixture):
    p = proj_name()
    (projects_fixture / p).mkdir()
    eng = fake_engine(procs, tmp_path / "bin", engine_env)
    orq = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", p, 0)
    rep = SP.run(p, port=free_port(), wait_secs=2)
    assert rep["pipeline_alive"] is True and orq.pid in rep["pipeline_pids"]["orchestrator"]
    assert rep["pipeline_stopping"] is True and pid_alive(eng.pid) and pid_alive(orq.pid)
    flag = projects_fixture / p / ".pipeline_stop_request"
    assert flag.exists() and "origin=stop_and_persist" in flag.read_text(), "con orquestador vivo la bandera se queda"
    # un orquestador de OTRO proyecto no impide parar este (camino directo: zombi sin HTTP y CPU baja)
    rep = SP.run(proj_name(), port=free_port())
    assert not rep.get("pipeline_alive") and rep["zombi_killed"] is True


def test_a6_driver_que_aparece_justo_antes_de_matar_pasa_a_modo_pipeline_y_no_se_mata(
        tmp_path, procs, engine_env, fast_probes, monkeypatch, projects_fixture):
    """Re-comprobación 'justo antes de actuar': el driver aparece DESPUÉS del primer chequeo."""
    p = proj_name()
    (projects_fixture / p).mkdir()
    eng = fake_engine(procs, tmp_path / "bin", engine_env)
    real_alive = SP.motor_alive
    lanzado: list = []

    def motor_alive_y_aparece_driver(port=5050):
        if not lanzado:   # carrera de lanzamiento: el driver nace entre el primer chequeo y la decisión de matar
            lanzado.append(fake_launcher(procs, tmp_path / "d", f"{p}_driver_AbC123", p, 5050, "", 0))
        return real_alive(port)

    monkeypatch.setattr(SP, "motor_alive", motor_alive_y_aparece_driver)
    rep = SP.run(p, port=free_port(), wait_secs=2)
    assert rep["pipeline_alive"] is True and lanzado[0].pid in rep["pipeline_pids"]["driver"]
    assert pid_alive(eng.pid) and not rep.get("zombi_killed")


# =========================================================================================
# C1-C7 (revisión de código de la Fase 1, deliberación CR1) — `pgrep` roto = fail-closed, etc.
# =========================================================================================

REAL_PGREP = shutil.which("pgrep") or "/usr/bin/pgrep"


def fake_pgrep_dir(tmp_path: Path, *, fail_when: str, rc: int = 2) -> str:
    """Directorio con un `pgrep` que FALLA (rc, p. ej. 2 = regex rechazado) cuando alguno de sus argumentos es
    `fail_when` (`-x` = detección del motor por nombre, `-f` = patrones de cmdline) y, si no, delega en el real.
    Se antepone al PATH del script bajo prueba: simula un `pgrep` roto sin tocar el del sistema."""
    d = tmp_path / f"fakebin_{uuid4().hex[:6]}"
    d.mkdir()
    sh = d / "pgrep"
    sh.write_text(f'#!/bin/bash\nfor a in "$@"; do [ "$a" = "{fail_when}" ] && exit {rc}; done\nexec {REAL_PGREP} "$@"\n')
    sh.chmod(0o755)
    return str(d)


def test_c1_la_barrera_aborta_si_pgrep_falla_en_vez_de_dar_luz_verde(tmp_path, engine_env, pipeline_leftovers):
    """C1: `pgrep -x` con rc 2 (no 'sin coincidencias') dejaba la lista vacía y la barrera lanzaba un motor nuevo
    encima de uno posiblemente vivo. Ahora ABORT (fail-closed)."""
    p = proj_name("c1")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p)
    r = orch_run(tmp_path, p, 0, engine=engine_env,
                 extra={**FAST_BARRIER, "PATH": f"{fake_pgrep_dir(tmp_path, fail_when='-x')}:{os.environ['PATH']}"})
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ABORT: pgrep falló (rc=2) comprobando si hay un sqcli vivo" in out and "sin saberlo no lanzo ninguna task" in out
    assert "lanzando motor fresco" not in out


def test_c1_wait_driver_con_pgrep_roto_no_da_el_driver_por_muerto_sigue_esperando_y_avisa(tmp_path):
    """C1: antes `while pgrep -f DRIVER_RE; do sleep …; done` salía al primer fallo de pgrep y el orquestador
    clasificaba la task como 'no terminó limpio' con el driver y el motor aún vivos. Función REAL del script,
    con un pgrep falso que falla 3 veces, luego 'driver vivo' y por fin 'ya no hay'."""
    from test_slim_hooks import shell_function
    d = tmp_path / "fb"
    d.mkdir()
    counter = tmp_path / "pgrep_calls"
    sh = d / "pgrep"
    sh.write_text(f'#!/bin/bash\nn=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"\n'
                  'case $n in 1|2|3) exit 2 ;; 4) echo 12345; exit 0 ;; *) exit 1 ;; esac\n')
    sh.chmod(0o755)
    fns = "\n".join(shell_function(ORCH, n) for n in ("isleep", "wait_driver"))
    script = (f'ts(){{ date +%H:%M:%S; }}\nWAIT_MARGIN=0; WAIT_SLEEP=0.05; DRIVER_RE=x; CURRENT_IDX=7; WAIT_SLEEP_PID=""\n'
              f'{fns}\nwait_driver\necho FIN')
    r = subprocess.run(["bash", "-c", script], env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}"},
                       capture_output=True, text=True, timeout=30)
    assert "FIN" in r.stdout, r.stdout + r.stderr
    assert r.stdout.count("pgrep falló (rc=2)") == 1, "avisa UNA vez por racha de fallos"
    assert "NO lo doy por muerto" in r.stdout and "task 7" in r.stdout
    assert counter.read_text().strip() == "5", "siguió esperando durante los fallos y mientras el driver vivía"


def test_c2_a7_aborta_si_pgrep_falla_y_no_toca_la_bandera(tmp_path, engine_env, pipeline_leftovers):
    """C2: `others_alive` con pgrep roto devolvía 'nadie vivo' y el orquestador arrancaba (y borraba la bandera de
    una parada en curso). Ahora ABORT sin tocar nada."""
    p = proj_name("c2")
    pipeline_leftovers.append(p)
    d = make_pipeline_proj(tmp_path, p)
    flag = d / ".pipeline_stop_request"
    flag.write_text("ts=x\norigin=test\n")
    r = orch_run(tmp_path, p, 0, engine=engine_env,
                 extra={"PATH": f"{fake_pgrep_dir(tmp_path, fail_when='-f')}:{os.environ['PATH']}"})
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ABORT: no pude comprobar si ya hay un pipeline vivo" in out and "No toco nada" in out
    assert "PIPELINE PER-TASK" not in out and flag.exists()
    assert not list(Path("/tmp").glob(f"{p}_pertask_*")), "la auto-copia se borra al abortar"


def _fake_pkill_dir(tmp_path: Path) -> tuple[str, Path]:
    d = tmp_path / f"fakepkill_{uuid4().hex[:6]}"
    d.mkdir()
    log = d / "pkill_calls"
    sh = d / "pkill"
    sh.write_text(f'#!/bin/bash\necho "$@" >> "{log}"\nexit 1\n')
    sh.chmod(0o755)
    return str(d), log


def test_c6_la_barrera_no_llama_a_pkill_g_con_el_pgid_vacio(tmp_path, procs, engine_env):
    """C6: `pkill -g ""` apuntaría a un grupo equivocado. Con LAST_DRIVER_PID vacío no se invoca; con él, sí, acotado
    a su grupo. (Función REAL `barrier_kill_own` con un `pkill` falso que registra sus llamadas.)"""
    from test_slim_hooks import shell_function
    fn = shell_function(ORCH, "barrier_kill_own")
    d, log = _fake_pkill_dir(tmp_path)
    motor = fake_engine(procs, tmp_path / "bin", engine_env)

    def run_fn(last_pid: str, target: int) -> str:
        script = (f'ENGINE_PROC="{engine_env}"; LAST_DRIVER_PID="{last_pid}"; BARRIER_OWN_GRACE_SECS=0; '
                  f'BARRIER_TERM_SECS=2\nts(){{ date +%H:%M:%S; }}\n{fn}\nbarrier_kill_own {target}\necho FIN')
        return subprocess.run(["bash", "-c", script], env={**os.environ, "PATH": f"{d}:{os.environ['PATH']}"},
                              capture_output=True, text=True, timeout=30).stdout

    assert "FIN" in run_fn("", motor.pid)
    assert _wait_for(lambda: not pid_alive(motor.pid), timeout=10), "el motor propio sí muere (TERM)"
    assert not log.exists(), "con LAST_DRIVER_PID vacío NO se llama a pkill"
    motor2 = fake_engine(procs, tmp_path / "bin", engine_env)
    assert "FIN" in run_fn("4242", motor2.pid)
    assert log.exists() and "-g 4242" in log.read_text() and "^tail -f /dev/null$" in log.read_text()


@pytest.mark.parametrize("valor,valido", [("", True), ("4242", True), ("2", True), ("abc", False), ("12a", False),
                                          ("0", False), ("1", False), ("-5", False), ("4 2", False)])
def test_c7_last_driver_pid_debe_ser_un_entero_mayor_que_1(valor, valido):
    from test_slim_hooks import shell_function
    fn = shell_function(ORCH, "sanitize_last_driver_pid")
    script = f'ts(){{ date +%H:%M:%S; }}\nLAST_DRIVER_PID="{valor}"\n{fn}\nsanitize_last_driver_pid\nprintf "[%s]" "$LAST_DRIVER_PID"'
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    if valido:
        assert out == f"[{valor}]" and "no es un PID válido" not in out
    else:
        assert out.endswith("[]") and "no es un PID válido" in out


def test_c7_un_pgid_sembrado_invalido_deja_el_motor_como_ajeno_y_no_se_mata(tmp_path, procs, engine_env,
                                                                             pipeline_leftovers):
    """E2E: BARRIER_TEST_OWN_PGID='abc' (y 0/1) ⇒ aviso y NINGÚN motor es propio: la barrera aborta por el motor
    'ajeno' sin matarlo (antes el valor basura habría llegado a `pkill -g`)."""
    for basura in ("abc", "0"):
        p = proj_name("c7")
        pipeline_leftovers.append(p)
        make_pipeline_proj(tmp_path, p)
        ajeno = fake_engine(procs, tmp_path / "bin", engine_env)
        r = orch_run(tmp_path, p, 0, engine=engine_env,
                     extra={**FAST_BARRIER, "BARRIER_TEST_OWN_PGID": basura, "BARRIER_FOREIGN_SECS": "1"})
        out = r.stdout + r.stderr
        assert r.returncode == 1 and "no es un PID válido" in out and "NO es de esta cadena" in out, out
        assert pid_alive(ajeno.pid), "con un pgid basura NUNCA se mata un motor"
        os.killpg(ajeno.pid, signal.SIGKILL)
        ajeno.wait(timeout=5)


# =========================================================================================
# F1-f (R3) — barrera del orquestador por PROPIEDAD (orquestador REAL, tiempos cortos por env)
# =========================================================================================

def test_f1f_motor_ajeno_aborta_sin_matarlo(tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("bf")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p)
    ajeno = fake_engine(procs, tmp_path / "bin", engine_env)              # otro grupo: pgid != LAST_DRIVER_PID
    r = orch_run(tmp_path, p, 0, engine=engine_env, extra=FAST_BARRIER)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ABORT" in out and "NO es de esta cadena" in out and "NO lo mato" in out
    assert f"pid={ajeno.pid}" in out and f"pgid={ajeno.pid}" in out
    assert "stop_and_persist.py" in out and p in out                       # instrucciones de reanudación
    assert pid_alive(ajeno.pid), "un motor ajeno NUNCA se mata"
    assert "lanzando motor fresco" not in out                              # abortó antes de lanzar la task 0


def test_f1f_motor_propio_se_remata_y_la_cadena_continua(tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("bp")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, orphan_bak=True)       # el lanzamiento falla al instante tras la barrera
    propio = fake_engine(procs, tmp_path / "bin", engine_env)              # pgid == su pid (nueva sesión)
    r = orch_run(tmp_path, p, 0, engine=engine_env,
                 extra={**FAST_BARRIER, "BARRIER_TEST_OWN_PGID": str(propio.pid)})
    out = r.stdout + r.stderr
    assert "el motor propio" in out and f"pgid {propio.pid}" in out and "→ TERM" in out, out
    assert _wait_for(lambda: not pid_alive(propio.pid), timeout=10), "el motor PROPIO debe morir en la barrera"
    # pasó la barrera: llegó a intentar lanzar la task 0 (y falló por el .bak huérfano, como se diseñó)
    assert "TASK 0: T1" in out and "no se pudo lanzar la task 0" in out
    assert "ABORT: hay un motor sqcli que NO es de esta cadena" not in out


def test_f1f_motor_propio_que_ignora_term_recibe_kill9(tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("bk")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, orphan_bak=True)
    propio = fake_engine(procs, tmp_path / "bin", engine_env, ignore_term=True)
    r = orch_run(tmp_path, p, 0, engine=engine_env,
                 extra={**FAST_BARRIER, "BARRIER_TEST_OWN_PGID": str(propio.pid)})
    out = r.stdout + r.stderr
    assert "→ TERM" in out and "kill -9" in out and str(propio.pid) in out, out
    assert _wait_for(lambda: not pid_alive(propio.pid), timeout=10)
    assert "no se pudo lanzar la task 0" in out


def test_f1f_la_barrera_remata_tambien_el_tail_del_grupo_propio_y_respeta_los_de_otros(
        tmp_path, procs, engine_env, pipeline_leftovers):
    """Al rematar un motor PROPIO la barrera también mata el `tail -f /dev/null` de SU grupo (la otra mitad
    del `tail | sqcli` del driver), nunca los de otros grupos (E5: antes `pkill -f` global)."""
    p = proj_name("bq")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, orphan_bak=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / engine_env).symlink_to("/bin/sleep")
    grupo = procs.spawn(["bash", "-c", f"tail -f /dev/null | '{bindir / engine_env}' 120 & sleep 120"])
    assert _wait_for(lambda: len(pgrep_x(engine_env)) == 1, timeout=10)
    motor = pgrep_x(engine_env)[0]

    def tails_del_grupo() -> list[int]:
        r = subprocess.run(["pgrep", "-g", str(grupo.pid), "-f", "^tail -f /dev/null$"],
                           capture_output=True, text=True)
        return [int(x) for x in r.stdout.split()]

    assert _wait_for(lambda: len(tails_del_grupo()) == 1, timeout=10)
    otro_tail = procs.spawn(["tail", "-f", "/dev/null"])                     # OTRO grupo
    assert _wait_for(lambda: "tail" in cmdline_of(otro_tail.pid))
    r = orch_run(tmp_path, p, 0, engine=engine_env,
                 extra={**FAST_BARRIER, "BARRIER_TEST_OWN_PGID": str(grupo.pid)})
    out = r.stdout + r.stderr
    assert "el motor propio" in out and str(motor) in out, out
    assert _wait_for(lambda: not pid_alive(motor), timeout=10)
    assert _wait_for(lambda: not tails_del_grupo(), timeout=10), "el tail del grupo propio debe morir con el motor"
    assert pid_alive(otro_tail.pid), "un tail -f /dev/null de OTRO grupo NO se toca"


def test_f1f_motor_propio_y_ajeno_a_la_vez_solo_cae_el_propio_y_aborta_por_el_ajeno(
        tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("bm")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p)
    propio = fake_engine(procs, tmp_path / "bin", engine_env)
    ajeno = fake_engine(procs, tmp_path / "bin", engine_env)
    r = orch_run(tmp_path, p, 0, engine=engine_env,
                 extra={**FAST_BARRIER, "BARRIER_TEST_OWN_PGID": str(propio.pid),
                        "BARRIER_FOREIGN_SECS": "3"})
    out = r.stdout + r.stderr
    assert r.returncode == 1 and f"pid={ajeno.pid}" in out, out
    assert pid_alive(ajeno.pid) and f"pid={propio.pid}" not in out
    assert _wait_for(lambda: not pid_alive(propio.pid), timeout=10)


def test_f1f_puerto_ocupado_aborta_tras_el_tope_con_el_duenio(tmp_path, engine_env, pipeline_leftovers):
    p = proj_name("bt")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def _acepta() -> None:   # un listener que no acepta llena el backlog y el 2.º `nc -z` se queda colgado
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=_acepta, daemon=True).start()
    try:
        r = orch_run(tmp_path, p, 0, engine=engine_env, port=port, extra=FAST_BARRIER)
    finally:
        srv.close()
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ABORT" in out and f"el puerto {port} sigue ocupado" in out and "2s" in out
    assert str(os.getpid()) in out, "el mensaje debe nombrar al DUEÑO del puerto (lsof): aquí el proceso de pytest"
    assert "lanzando motor fresco" not in out


def test_f1f_sin_barrier_testing_la_barrera_no_actua_con_slim_testing(tmp_path, procs, engine_env,
                                                                      pipeline_leftovers):
    """Activación: con SLIM_TESTING=1 y SIN BARRIER_TESTING la barrera NO actúa (los tests existentes con
    mocks no cambian): un motor ajeno vivo no la hace abortar y el orquestador sigue hasta el lanzamiento."""
    p = proj_name("bn")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, orphan_bak=True)
    ajeno = fake_engine(procs, tmp_path / "bin", engine_env)
    extra = {k: v for k, v in FAST_BARRIER.items() if k != "BARRIER_TESTING"}
    r = orch_run(tmp_path, p, 0, engine=engine_env, extra=extra)
    out = r.stdout + r.stderr
    assert "NO es de esta cadena" not in out and "esperando a que muera sqcli" not in out, out
    assert "no se pudo lanzar la task 0" in out
    assert pid_alive(ajeno.pid)


def test_f1f_la_salida_de_run_task_headless_trae_el_pid_del_driver_y_el_sed_lo_extrae(
        tmp_path, monkeypatch, capsys, pipeline_leftovers):
    """LAST_DRIVER_PID: el orquestador lo lee de la salida REAL del lanzador ("PID driver N") con la línea
    `sed` REAL del script (extraída del fichero)."""
    from types import SimpleNamespace
    projects = tmp_path / "projects"
    p = proj_name("pd")
    pipeline_leftovers.append(p)        # RT.run deja su log de lanzamiento en /tmp/<proy>_task00_<epoch>.log
    make_proj(projects, p, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    monkeypatch.setenv("SLIM_TESTING", "1")
    monkeypatch.delenv("RUNNER_PIPELINE", raising=False)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 424242

        def terminate(self):
            pass

    monkeypatch.setattr(RT, "subprocess", SimpleNamespace(Popen=FakePopen, STDOUT=subprocess.STDOUT,
                                                          run=subprocess.run))
    monkeypatch.setattr(RT, "time", SimpleNamespace(sleep=lambda s: None, time=time.time))
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port: True)
    monkeypatch.setattr(RT.SP, "_http", lambda cmd, port, timeout: "Project running")
    monkeypatch.setattr(RT, "gui_open", lambda: False)
    monkeypatch.setattr(sys, "argv", ["run_task_headless.py", p, "--task", "T1", "--mode", "only"])
    assert RT._cli() == 0
    salida = capsys.readouterr().out
    assert "PID driver 424242" in salida
    linea = re.search(r"^\s*LAST_DRIVER_PID=\$\(printf.*$", ORCH.read_text(encoding="utf-8"), re.M)
    assert linea, "no encuentro la asignación de LAST_DRIVER_PID en el orquestador"
    r = subprocess.run(["bash", "-c", linea.group(0).strip() + '\nprintf "%s" "$LAST_DRIVER_PID"'],
                       env={**os.environ, "LAUNCH_OUT": salida}, capture_output=True, text=True)
    assert r.stdout == "424242", (r.stdout, r.stderr)
    # sin esa frase (lanzamiento fallido): vacío, nunca el PID de la task anterior
    r = subprocess.run(["bash", "-c", "LAST_DRIVER_PID=999\n" + linea.group(0).strip()
                        + '\nprintf "%s" "$LAST_DRIVER_PID"'],
                       env={**os.environ, "LAUNCH_OUT": "⚠ no se pudo spawn"}, capture_output=True, text=True)
    assert r.stdout == ""


# ----------------------------- cascada y noche de producción: nombre exacto, sin matar -----------------

def _cascade_env(tmp_path: Path, engine: str, port: int, **extra) -> dict:
    env = os.environ.copy()
    env.update({"PROJECTS_DIR": str(tmp_path / "projects"), "SQX_OUTDIR": str(tmp_path / "out"),
                "SLIM_TESTING": "1", "SQX_ENGINE_PROC_NAME": engine, "PORT": str(port),
                "PORT_WAIT_SECS": "2", "BARRIER_POLL_SECS": "0.2", "WAIT_MARGIN": "0.2", "WAIT_SLEEP": "0.2"})
    env.update(extra)
    return env


def test_cascada_puerto_ocupado_aborta_con_el_duenio_y_no_mata_nada(tmp_path, procs, engine_env):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def _acepta() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.close()

    threading.Thread(target=_acepta, daemon=True).start()
    ajeno = procs.spawn(["tail", "-f", "/dev/null"])             # un proceso cualquiera que la cascada NO debe tocar
    try:
        r = subprocess.run(["bash", str(SQX_DIR / "run_cascade_projects.sh"), "0", proj_name("ca")],
                           env=_cascade_env(tmp_path, engine_env, port), capture_output=True, text=True,
                           timeout=60)
    finally:
        srv.close()
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ABORT de la cascada" in out and f"puerto {port} sigue ocupado" in out and "no se mata nada" in out
    assert str(os.getpid()) in out, "debe nombrar al dueño del puerto (lsof)"
    assert "lanzando" not in out, "abortó antes de lanzar el primer proyecto"
    assert pid_alive(ajeno.pid)
    # el log de la cascada va a SQX_OUTDIR (no a sqx/output del repo desde los tests)
    logs = list((tmp_path / "out").glob("cascada_*.log"))
    assert len(logs) == 1 and "ABORT de la cascada" in logs[0].read_text(encoding="utf-8")


def test_cascada_espera_al_motor_por_nombre_sin_matarlo_y_sigue_cuando_muere(tmp_path, procs, engine_env):
    ajeno = fake_engine(procs, tmp_path / "bin", engine_env)
    p = proj_name("cw")                                           # proyecto inexistente: el orquestador aborta al instante
    log = tmp_path / "cascada.out"
    with open(log, "w") as lf:
        proc = subprocess.Popen(["bash", str(SQX_DIR / "run_cascade_projects.sh"), "0", p],
                                env=_cascade_env(tmp_path, engine_env, free_port()), stdin=subprocess.DEVNULL,
                                stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    procs.popens.append(proc)           # teardown: killpg del grupo de la cascada aunque el test falle
    assert _wait_for(lambda: "esperando a que muera sqcli previo" in log.read_text(encoding="utf-8"), timeout=15)
    time.sleep(1.5)
    assert pid_alive(ajeno.pid), "la cascada NUNCA mata el motor (ni tras esperar: el tope es solo del puerto)"
    assert proc.poll() is None, "mientras haya motor la cascada espera"
    os.killpg(ajeno.pid, signal.SIGKILL)                           # el "ajeno" muere por su cuenta
    ajeno.wait(timeout=5)
    assert proc.wait(timeout=60) == 0
    txt = log.read_text(encoding="utf-8")
    assert f"lanzando {p}" in txt and "CASACADA TERMINADA" in txt
    assert "sin tasks activas" in txt                              # lo dijo el orquestador hijo (proyecto inexistente)


@pytest.mark.skipif(not (SQX_DIR / "run_production_night.sh").exists(),
                    reason="run_production_night.sh no viaja a la copia sqx-headless")
def test_noche_de_produccion_espera_al_motor_por_nombre_sin_matarlo(tmp_path, procs, engine_env):
    """Misma detección por nombre exacto en run_production_night.sh (sin tope: solo espera y no mata)."""
    ajeno = fake_engine(procs, tmp_path / "bin", engine_env)
    log = tmp_path / "noche.out"
    with open(log, "w") as lf:
        proc = subprocess.Popen(["bash", str(SQX_DIR / "run_production_night.sh"), proj_name("nn")],
                                env=_cascade_env(tmp_path, engine_env, free_port()), stdin=subprocess.DEVNULL,
                                stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    procs.popens.append(proc)
    assert _wait_for(lambda: "esperando a que muera sqcli previo" in log.read_text(encoding="utf-8"), timeout=15)
    time.sleep(1.0)
    assert pid_alive(ajeno.pid) and proc.poll() is None
    assert "lanzando" not in log.read_text(encoding="utf-8")


# =========================================================================================
# F1-g (R8) — parseo de argumentos del orquestador
# =========================================================================================

BANNER = re.compile(r"PIPELINE PER-TASK \S+ \(from_task=(\d+), (\d+) tasks activas, slim=(\d)\)")


@pytest.mark.parametrize("extra,desde,slim", [
    (["--slim"], 0, 1),           # `P --slim`: antes tomaba "--slim" como índice
    (["2", "--slim"], 2, 1),
    (["--slim", "2"], 2, 1),      # orden libre
    (["3"], 3, 0),
    ([], 0, 0),
])
def test_f1g_parseo_valido(tmp_path, procs, engine_env, pipeline_leftovers, extra, desde, slim):
    """El banner del orquestador REAL trae `from_task=` y `slim=`; la barrera aborta justo después (motor
    ajeno vivo) para no lanzar nada."""
    p = proj_name("pa")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, n_tasks=3)
    fake_engine(procs, tmp_path / "bin", engine_env)
    r = orch_run(tmp_path, p, *extra, engine=engine_env,
                 extra={**FAST_BARRIER, "BARRIER_FOREIGN_SECS": "1"})
    out = r.stdout + r.stderr
    m = BANNER.search(out)
    assert m, out
    assert (int(m.group(1)), int(m.group(3))) == (desde, slim), out
    assert r.returncode == 1 and "NO es de esta cadena" in out


@pytest.mark.parametrize("args,needle", [
    (["foo"], "arg desconocido"),
    (["1", "2"], "índice repetido"),
    (["--slim", "x"], "arg desconocido"),
    (["-1"], "arg desconocido"),
    ([""], "arg desconocido"),
    (["--slim", "--slim", "1", "2"], "índice repetido"),
])
def test_f1g_argumentos_invalidos_uso_y_exit_2(tmp_path, engine_env, args, needle):
    p = proj_name("pi")
    r = orch_run(tmp_path, p, *args, engine=engine_env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert needle in r.stderr and "uso:" in r.stderr
    assert not list(Path("/tmp").glob(f"{p}_pertask_*")), "el parseo falla ANTES de la auto-copia"


def test_f1g_proyecto_que_parece_flag_es_uso(tmp_path, engine_env):
    r = orch_run(tmp_path, "--slim", engine=engine_env)
    assert r.returncode == 2 and "uso:" in r.stderr


# =========================================================================================
# F1-h — `bash -n` de los 4 .sh (la regresión completa es la suite del stack, con y sin SQX)
# =========================================================================================

@pytest.mark.parametrize("script", FOUR_SH)
def test_f1h_bash_n(script):
    r = subprocess.run(["bash", "-n", str(SQX_DIR / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# =========================================================================================
# F1-i (A7) — un solo pipeline por proyecto
# =========================================================================================

def test_f1i_segundo_orquestador_del_mismo_proyecto_aborta(tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("ia")
    pipeline_leftovers.append(p)
    d = make_pipeline_proj(tmp_path, p, orphan_bak=True)
    bak = d / "project.cfx.pre_run_task.bak"
    primero = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", p, 0)
    cfx_antes = (d / "project.cfx").read_bytes()
    r = orch_run(tmp_path, p, 0, engine=engine_env)
    out = r.stdout + r.stderr
    assert r.returncode == 1, out
    assert "ABORT" in out and "ya hay un pipeline vivo" in out and str(primero.pid) in out
    assert "No toco nada" in out
    # no tocó NADA: el .bak (de otra corrida, para el caso) sigue ahí y el .cfx intacto; no hubo reconcile/lanzamiento
    assert bak.exists() and (d / "project.cfx").read_bytes() == cfx_antes
    assert "PIPELINE PER-TASK" not in out and "lanzando motor fresco" not in out
    assert not list(Path("/tmp").glob(f"{p}_pertask_*")), "la auto-copia se borra al abortar"


def test_f1i_driver_vivo_del_mismo_proyecto_tambien_aborta(tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("id")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p)
    drv = fake_launcher(procs, tmp_path / "d", f"{p}_driver_AbC123", p, 5050, "", 0)
    r = orch_run(tmp_path, p, 0, engine=engine_env)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "ya hay un pipeline vivo" in out and f"driver: {drv.pid}" in out, out


def test_f1i_otro_proyecto_vivo_no_impide_arrancar(tmp_path, procs, engine_env, pipeline_leftovers):
    p = proj_name("io")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, orphan_bak=True)
    fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", f"{p}2", 0)       # prefijo común
    fake_launcher(procs, tmp_path / "d", f"{proj_name('zz')}_driver_AbC123", "otro", 5050, "", 0)
    r = orch_run(tmp_path, p, 0, engine=engine_env)
    out = r.stdout + r.stderr
    assert "ya hay un pipeline vivo" not in out, out
    assert "TASK 0: T1" in out                                   # pasó la guarda y llegó a lanzar


def test_f1i_el_propio_orquestador_y_sus_subshells_no_cuentan(tmp_path, engine_env, pipeline_leftovers):
    """Sin nadie más vivo el orquestador arranca (no se detecta a sí mismo ni a sus subshells)."""
    p = proj_name("is")
    pipeline_leftovers.append(p)
    make_pipeline_proj(tmp_path, p, orphan_bak=True)
    r = orch_run(tmp_path, p, 0, engine=engine_env)
    out = r.stdout + r.stderr
    assert "ya hay un pipeline vivo" not in out and "PIPELINE PER-TASK" in out, out


# ----------------------------- revisión de código de la Fase 2 (2026-10-25) -----------------------------

@pytest.mark.parametrize("script_name, wait_fn", [("run_pipeline_headless.sh", "wait_free_trap"),
                                                  ("run_pipeline_per_task.sh", "wait_free_orch")])
@pytest.mark.parametrize("pgrep_rc, libre", [(2, False), (1, True), (0, False)])
def test_d3_esperas_y_restore_slim_son_fail_closed_con_pgrep_roto(tmp_path, script_name, wait_fn, pgrep_rc, libre):
    """D3: `wait_free_trap`/`wait_free_orch` (y la comprobación previa al restore --slim) daban un `pgrep` ROTO (rc 2)
    por "no hay motor" y restauraban bancos con el motor quizá vivo. Funciones REALES de los scripts, con un `pgrep`
    falso: rc 2 (roto) y rc 0 (motor vivo) = NO libre; solo rc 1 ("ninguno") = libre."""
    from test_slim_hooks import shell_function
    script = SQX_DIR / script_name
    fns = "\n".join(shell_function(script, n) for n in ("no_engine_alive", wait_fn))
    d = tmp_path / "fb"
    d.mkdir()
    sh = d / "pgrep"
    sh.write_text(f"#!/bin/bash\n{'echo 4242' if pgrep_rc == 0 else ':'}\nexit {pgrep_rc}\n")
    sh.chmod(0o755)
    body = (f"ENGINE_PROC=sqcli_d3_{uuid4().hex[:6]}; SLIM_TRAP_WAIT_SECS=5\n{fns}\nsleep(){{ :; }}\n"
            "if no_engine_alive; then echo LIBRE; else echo NO-LIBRE; fi\n"
            f"{wait_fn}; echo \"WAIT=$?\"")
    env = {k: v for k, v in os.environ.items() if k != "SLIM_TESTING"}
    env["PATH"] = f"{d}:{env['PATH']}"
    r = subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True, timeout=30)
    out = r.stdout.split()
    assert out == (["LIBRE", "WAIT=0"] if libre else ["NO-LIBRE", "WAIT=1"]), r.stdout + r.stderr


def test_d3_ningun_script_da_un_pgrep_roto_por_motor_ausente():
    """D3 (guardián estático): ni `! pgrep -x …` ni `pgrep -x … || return 0` en los .sh del stack: la única
    pregunta "¿no queda motor?" es `no_engine_alive` (solo rc 1 = libre)."""
    for name in ("run_pipeline_headless.sh", "run_pipeline_per_task.sh", "run_cascade_projects.sh"):
        txt = (SQX_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"!\s*pgrep -x", txt), name
        assert not re.search(r'pgrep -x "\$ENGINE_PROC" >/dev/null(?: 2>&1)? \|\|', txt), name
