"""Tests de la Fase 2 del guardrail `sqx-headless-fallos` (2026-10-21): GUARDADO Y PARADA — R1, R10, R6, R9 (+ C1-C7).

Criterios de aceptación del plan (§3 Fase 2) -> tests de este fichero (y de los módulos que se citan):

  F2-a (R10, R5)  guardián estático (sin `action=save` ni patrón ancho de motor) ........ test_f2a_*
  F2-b (R1)       sync CONFIRMADO por el stdout del motor; sin línea ⇒ motor vivo (+A1, A4) test_f2b_*
  F2-c (R1/R2)    persist_bank timeout largo, no matar con banco sin guardar, A3, A6 ... tests/test_stop_and_persist_idle.py
  F2-d (R6)       Stop & persist de un pipeline de 2 tasks (rc 3, user_stop, slim) ..... test_f2d_*
  F2-e (R6/A7)    bandera rancia al arrancar; otro orquestador vivo ⇒ no se toca ....... test_f2e_*
                  (+ tests/sqx/test_run_task_headless.py: lanzamiento suelto con RT.run)
  F2-f (R6/D2)    cascada: rc 3 ⇒ el segundo proyecto no se lanza, cascada rc 3 .......... test_f2f_*
  F2-g (R6)       RT.run con la bandera antes de mutar / antes del Popen ................ tests/sqx/test_run_task_headless.py
                  + variante A5 (bandera justo tras la 2.ª comprobación, e2e) ........... test_f2g_*
  F2-h (R6)       TERM al orquestador durante wait_driver ............................... test_f2h_*
  F2-i (R9)       mensajes "ya puedes abrir la GUI" veraces (driver, orquestador) ....... test_f2i_*
                  (+ CLI en tests/test_stop_and_persist_idle.py, monitor en tests/sqx/test_sqx_monitor.py)
  F2-j (R4)       persist_before_gui.py solo comprueba ................................... tests/test_persist_before_gui.py
  F2-k            regresión: `bash -n` de los 4 .sh ..................................... test_f2k_*
  C1-C7           revisión de código de la Fase 1 (pgrep roto = fail-closed, etc.) ........ test_c*_*

Todo con procesos REALES de nombre y ruta ÚNICOS (uuid) y un MOTOR FALSO con HTTP propio (`MOCK_HTTP_ENGINE`):
imprime en su stdout las mismas líneas que el motor real ("Databank '<proj>/<b>' saved - files before sync X /
after sync M …", "Project finished") y deja un registro de eventos con orden (SYNC_LINE antes que TERM, etc.).
SEGURIDAD: SQCLI y SQX_APP apuntan a rutas inexistentes (fixture autouse) o al mock; SQX_ENGINE_PROC_NAME es único
por test: JAMÁS se arranca, se ve ni se mata un sqcli real (en esta máquina puede haber un proceso residual
llamado `sqcli` imposible de matar: ningún test supone que no existe ninguno).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "sqx"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(ROOT / "tests") not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx
    sys.path.append(str(ROOT / "tests"))

import stop_and_persist as SP  # noqa: E402
from test_slim_hooks import (  # noqa: E402  infraestructura compartida de procesos falsos
    _wait_for, cmdline_of, fake_engine, fake_launcher, make_proj, pgrep_x, pid_alive, procs,
    shell_function, unique_engine_name,
)

SQX_DIR = ROOT / "sqx"
ORCH = SQX_DIR / "run_pipeline_per_task.sh"
DRIVER = SQX_DIR / "run_pipeline_headless.sh"
CASCADE = SQX_DIR / "run_cascade_projects.sh"
#: `run_production_night.sh` no viaja a la copia compartible `sqx-headless` (scripts/export_sqx_headless/manifest.yaml):
#: allí se omite; cualquier OTRO fichero que falte sigue haciendo fallar los guardianes.
NOT_IN_COPY = {"run_production_night.sh"}


def _present(names):
    return [f for f in names if f not in NOT_IN_COPY or (SQX_DIR / f).exists()]


FOUR_SH = _present(["run_pipeline_headless.sh", "run_pipeline_per_task.sh", "run_cascade_projects.sh",
                    "run_production_night.sh"])
#: los 10 ficheros del perímetro del guardrail (§1): ninguno puede enviar `save` ni detectar el motor por texto
PERIMETER = _present(["run_pipeline_headless.sh", "run_pipeline_per_task.sh", "run_cascade_projects.sh",
                      "run_production_night.sh", "stop_and_persist.py", "run_task_headless.py", "retry_project.py",
                      "gui_slim.py", "sqx_monitor.py", "persist_before_gui.py"])
REAL_PYTHON3 = shutil.which("python3") or sys.executable


# ----------------------------- fixtures y helpers -----------------------------------------

def proj_name(prefix: str = "ps") -> str:
    """Nombre de proyecto ÚNICO (los regex de orquestador/driver discriminan por proyecto)."""
    return f"{prefix}{uuid4().hex[:6]}"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _nunca_el_sqcli_real(monkeypatch, tmp_path):
    """Seguridad de plataforma: SI un test llegara a lanzar un driver de verdad sin motor falso, `$SQCLI` apunta a
    un fichero inexistente y `SQX_APP` a una app inexistente: jamás se arranca el motor REAL de SQX."""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))


@pytest.fixture(autouse=True)
def engine_env(monkeypatch):
    """Nombre único de motor falso para ESTE test (autouse: TODO test de este fichero corre con él): el stack
    ignora cualquier sqcli real, también los tests que no lo piden."""
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)
    return name


@pytest.fixture()
def leftovers(engine_env):
    """Limpia al terminar (aunque el test falle): motores falsos por su nombre ÚNICO, orquestadores/drivers/
    lanzadores de los proyectos registrados (su grupo de procesos entero) y sus restos en /tmp."""
    projs: list[str] = []
    yield projs
    subprocess.run(["pkill", "-9", "-x", engine_env], capture_output=True)
    for p in projs:
        pgids = set()
        for pid in {*SP.driver_pids(p), *SP.orchestrator_pids(p)}:
            pgids.add(SP.engine_owner_pgid(pid))
        for x in subprocess.run(["pgrep", "-f", rf"run_task_headless\.py {SP._ere_escape(p)} "],
                                capture_output=True, text=True).stdout.split():
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
    subprocess.run(["pkill", "-9", "-x", engine_env], capture_output=True)


# Motor FALSO con HTTP propio (stdlib). Imprime en su stdout lo mismo que el motor real y deja un registro de
# eventos con orden en MOCK_EVENTS. Variables (env):
#   MOCK_PORT, MOCK_PROJ, MOCK_DBS (databanks/ del proyecto), MOCK_EVENTS (fichero de eventos)
#   MOCK_FIN_SECS  fin natural del proyecto N s tras `start` (default 100000 = nunca; solo acaba con `stop`)
#   MOCK_STOP_SECS tiempo entre `stop` y "Project finished" (default 0.5)
#   MOCK_SYNC_SECS duración del synctofiles (default 0) · MOCK_SYNC_LINE 1 | 0 | after_finish (línea de confirmación)
#   MOCK_AFTER_SYNC fuerza el M de "after sync M" (default: nº de .sqx del banco en disco)
#   MOCK_IGNORE_TERM=1 ignora SIGTERM (solo muere con kill -9) · MOCK_BLIND_AFTER=N s tras `start`: HTTP ciego
#   MOCK_COUNT_BAD=1 el `count` no responde con "Records: N" · MOCK_SYNC_ERROR=1 el synctofiles responde "doesn't exist"
#   MOCK_COUNT_EXTRA=N el `count` da N estrategias MÁS que los .sqx en disco (el motor tiene algo sin guardar)
# Como el motor REAL (logs de sqx/output/ftmo_cfd_cartera_pmaster2_20261014/engine.log): cada synctofiles de un banco
# NO vacío escribe DOS líneas (una con el sufijo "Sync triggered by 'API request project/synchronizeDatabank …'" y
# otra con el prefijo "<job> : "); un banco VACÍO no escribe ninguna (solo responde "Synchronization started.").
# MOCK_SYNC_LINE=0 = un sync SIN NADA NUEVO que guardar (B144 tampoco escribe línea, verificado 2026-10-27, A8): con
# MOCK_COUNT_EXTRA el motor sí tiene algo que no llega a disco (sync de verdad sin confirmar).
MOCK_HTTP_ENGINE = r'''#!{python}
import os, pathlib, re, signal, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ["MOCK_PORT"]); PROJ = os.environ["MOCK_PROJ"]
DBS = pathlib.Path(os.environ["MOCK_DBS"]); EVENTS = os.environ["MOCK_EVENTS"]
FIN = float(os.environ.get("MOCK_FIN_SECS", "100000")); STOP_SECS = float(os.environ.get("MOCK_STOP_SECS", "0.5"))
SYNC_SECS = float(os.environ.get("MOCK_SYNC_SECS", "0")); SYNC_LINE = os.environ.get("MOCK_SYNC_LINE", "1")
COUNT_BAD = os.environ.get("MOCK_COUNT_BAD") == "1"; SYNC_ERROR = os.environ.get("MOCK_SYNC_ERROR") == "1"
AFTER = os.environ.get("MOCK_AFTER_SYNC"); IGNORE_TERM = os.environ.get("MOCK_IGNORE_TERM") == "1"
BLIND_AFTER = float(os.environ.get("MOCK_BLIND_AFTER", "-1")); COUNT_EXTRA = int(os.environ.get("MOCK_COUNT_EXTRA", "0"))
LOCK = threading.Lock()
ST = {"running": False, "finished": False, "by_stop": False, "t0": 0.0, "n": 0, "t_end": 0.0}

def ev(kind, detail=""):
    with LOCK:
        with open(EVENTS, "a") as f:
            f.write(f"{time.time():.3f} {kind} {detail}\n")

def emit(line):
    print(line, flush=True)

def finish(by_stop=False):
    with LOCK:
        if ST["finished"]:
            return
        ST["running"] = False; ST["finished"] = True; ST["by_stop"] = by_stop; ST["t_end"] = time.time()
    ev("FINISHED"); emit("Project finished")

def on_term(signum, frame):
    if IGNORE_TERM:
        ev("TERM_IGNORED"); return
    ev("TERM"); os._exit(0)

signal.signal(signal.SIGTERM, on_term)

def disk_count(bank):
    d = DBS / bank
    return sum(1 for q in d.iterdir() if q.suffix == ".sqx") if d.is_dir() else 0

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, text):
        try:
            body = text.encode()
            self.send_response(200); self.end_headers(); self.wfile.write(body)
        except Exception:
            pass   # el cliente (curl con timeout) ya se fue: el motor sigue su trabajo
    def do_GET(self):
        cmd = parse_qs(urlparse(self.path).query).get("cmd", [""])[0]
        if BLIND_AFTER >= 0 and ST["running"] and time.time() - ST["t0"] >= BLIND_AFTER:
            self.connection.close(); return          # HTTP ciego: cierra sin responder (curl: respuesta vacía)
        m = re.search(r"action=(\w+)", cmd); act = m.group(1) if m else ""
        bank = (re.search(r" name=(.+?)(?: file=|$)", cmd) or [None, ""])[1].strip()
        if "-project" in cmd and act == "list":
            return self.reply("List of available projects: " + PROJ)
        if "-project" in cmd and act == "status":
            # Fin (natural o por `stop`): como en B144 (verificado 2026-10-27), el status queda CONGELADO y SIN palabras
            # de parada: tras un `stop` no dice "stopped" ni "not running" (antes el mock lo decía y ocultaba A9).
            if ST["finished"]:
                return self.reply("Status of project %s\nStrategies generated  %d\nIn databank  5\nRunning time so far  %d ms.\n" % (PROJ, ST["n"], int((ST["t_end"] - ST["t0"]) * 1000) + 1))
            if not ST["running"]:
                return self.reply("Status of project %s\nStrategies generated  0\nIn databank  5\nRunning time so far  0 ms.\n" % PROJ)
            ST["n"] += 7
            return self.reply("Status of project %s\nStrategies generated  %d\nIn databank  5\nRunning time so far  %d ms.\n" % (PROJ, ST["n"], int((time.time() - ST["t0"]) * 1000) + 1))
        if "-project" in cmd and act in ("start", "startFromTask"):
            with LOCK:
                ST["running"] = True; ST["t0"] = time.time()
            ev("START"); threading.Timer(FIN, finish).start()
            return self.reply("Project started.")
        if "-project" in cmd and act == "stop":
            ev("STOP"); threading.Timer(STOP_SECS, finish, args=(True,)).start()
            # respuesta literal de B144 (verificada 2026-10-27), también con el proyecto ya parado
            return self.reply("%s Stopping project %s\n" % (time.strftime("%H:%M:%S"), PROJ) + "-" * 50
                              + "\nProject execution stopped.\n" + "-" * 50 + "\n")
        if "-databank" in cmd and act == "synctofiles":
            if SYNC_ERROR:
                ev("SYNC_ERROR", bank)
                return self.reply("Databank '%s' doesn't exist" % bank)
            ev("SYNC_START", bank)
            time.sleep(SYNC_SECS)
            n = disk_count(bank); mm = AFTER if AFTER is not None else str(n)
            if n > 0 and (SYNC_LINE == "1" or (SYNC_LINE == "after_finish" and ST["finished"])):
                core = "Databank '%s/%s' saved - files before sync %d / after sync %s / saved %d / removed 0 in %.2f s." % (PROJ, bank, n, mm, n, SYNC_SECS)
                emit(core + " Sync triggered by 'API request project/synchronizeDatabank %s/%s.'." % (PROJ, bank))
                emit("PM_job : " + core)
                ev("SYNC_LINE", bank)
            elif n == 0:
                ev("SYNC_EMPTY", bank)
            return self.reply("Synchronization started.")
        if "-databank" in cmd and act == "count":
            if COUNT_BAD:
                return self.reply("Error: count no disponible")
            return self.reply("Number of strategies\n--------------------\nRecords: %d\n" % (disk_count(bank) + COUNT_EXTRA))
        if "-databank" in cmd and act == "export":
            f = re.search(r" file=(\S+)", cmd)
            if f:
                pathlib.Path(f.group(1)).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(f.group(1)).write_text("Name;Fitness\ns1;1.0\n")
            ev("EXPORT", bank)
            return self.reply("Export done")
        if "-databank" in cmd and act == "save":
            ev("SAVE", bank)
            return self.reply("Error: Missing parameter 'folder'.")
        return self.reply("ok")

ev("PID", str(os.getpid()))
srv = ThreadingHTTPServer(("127.0.0.1", PORT), H); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
emit("List of available projects")
while True:
    time.sleep(1)
'''


def named_python() -> str:
    """Intérprete que conserva el nombre del enlace que lo lanza (para que `pgrep -x <engine_name>` lo vea).

    En los Python "framework" de macOS (python.org, Homebrew, actions/setup-python de la CI) `bin/python3.x` se
    relanza como `…/Resources/Python.app/Contents/MacOS/Python`, y el proceso pasa a llamarse `Python`: el motor
    falso desaparecía para la detección por nombre (CI de sqx-headless, 2026-10-26). El binario de Python.app no se
    relanza. Con conda u otro Python no framework, `sys.executable` ya sirve."""
    real = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    return str(real) if real.exists() else sys.executable


class HttpEngine:
    """Un motor falso con HTTP propio listo para que el driver/orquestador lo lance como `$SQCLI`.

    `sqcli` = script wrapper que hace `exec` de `<bin>/<engine_name> mock_engine.py`: el proceso resultante se llama
    EXACTAMENTE `engine_name` (symlink al python del test), así `pgrep -x`, `engine_pids()` y el vigilante lo ven
    como ven al motor real, y `$SQPID` del driver es su PID."""

    def __init__(self, tmp_path: Path, engine_name: str, proj: str, projdir: Path, port: int):
        d = tmp_path / "http_engine"
        d.mkdir(parents=True, exist_ok=True)
        link = d / engine_name
        link.symlink_to(named_python())
        script = d / "mock_engine.py"
        script.write_text(MOCK_HTTP_ENGINE.replace("{python}", sys.executable))
        wrapper = d / "sqcli_wrapper.sh"
        wrapper.write_text(f'#!/bin/bash\nexec "{link}" "{script}" "$@"\n')
        wrapper.chmod(0o755)
        self.sqcli = wrapper
        self.events_file = tmp_path / "engine_events.log"
        self.port = port
        self.env = {"SQCLI": str(wrapper), "MOCK_PORT": str(port), "MOCK_PROJ": proj,
                    "MOCK_DBS": str(projdir / "databanks"), "MOCK_EVENTS": str(self.events_file)}

    def events(self) -> list[tuple[str, str]]:
        if not self.events_file.exists():
            return []
        out = []
        for line in self.events_file.read_text().splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 2:
                out.append((parts[1], parts[2] if len(parts) > 2 else ""))
        return out

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events()]

    def pids(self) -> list[int]:
        return [int(d) for k, d in self.events() if k == "PID" and d.isdigit()]


FAST = {"CARGA_SLEEP": "0.1", "STABLE_N": "3", "QUICK_SECS": "0.2", "STOP_SECS": "0.1", "SLIM_TRAP_WAIT_SECS": "10",
        "SYNC_CONFIRM_POLL_SECS": "0.1", "SYNC_CONFIRM_SECS": "20", "SQ_LONG_SECS": "30", "KILL_GRACE_SECS": "5",
        "WD_FREE_PCT_MIN": "0", "WD_SLEEP_SECS": "1", "WAIT_MARGIN": "0.2", "WAIT_SLEEP": "0.2"}


def base_env(tmp_path: Path, projects: Path, engine_name: str, eng: HttpEngine, **extra: str) -> dict:
    env = os.environ.copy()
    env.update({"PROJECTS_DIR": str(projects), "SQX_OUTDIR": str(tmp_path / "out"), "SLIM_TESTING": "1",
                "SQX_ENGINE_PROC_NAME": engine_name, "PORT": str(eng.port), **FAST, **eng.env})
    env.update(extra)
    return env


def make_two_task_proj(tmp_path: Path, name: str, banks: tuple = ("InA", "MidM", "OutB", "ExtraC")) -> Path:
    return make_proj(tmp_path / "projects", name,
                     [("T1", "Build", ["InA"], "MidM"), ("T2", "Retest", ["MidM"], "OutB")], list(banks))


def make_one_task_proj(tmp_path: Path, name: str, banks: tuple = ("InA", "Out")) -> Path:
    return make_proj(tmp_path / "projects", name, [("T1", "Retest", ["InA"], "Out")], list(banks))


def run_driver_http(tmp_path: Path, proj: Path, eng: HttpEngine, engine_name: str, *flags: str,
                    timeout: int = 120, **extra: str) -> subprocess.CompletedProcess:
    """Driver REAL de task única (STATE_WRITER=driver) contra el motor falso con HTTP propio."""
    env = base_env(tmp_path, proj.parent, engine_name, eng, **extra)
    out = tmp_path / f"driver_{uuid4().hex[:6]}.out"
    # salida a FICHERO y no a un pipe: con el sync sin confirmar el driver deja VIVO el motor y su `tail -f
    # /dev/null` (stdin del motor), que heredan el pipe y colgarían a `communicate()` (en producción el log es un fichero)
    with open(out, "w") as lf:
        r = subprocess.run(["bash", str(DRIVER), proj.name, str(eng.port), "", "0", *flags], env=env,
                           stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout)
    r.stdout = out.read_text()
    r.stderr = ""
    return r


def spawn_orchestrator(tmp_path: Path, proj: Path, eng: HttpEngine, engine_name: str, *args: str,
                       path_prefix: str | None = None, **extra: str) -> tuple[subprocess.Popen, Path]:
    """Orquestador REAL en segundo plano (nueva sesión), salida a un fichero."""
    env = base_env(tmp_path, proj.parent, engine_name, eng, **extra)
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    out = tmp_path / f"orch_{uuid4().hex[:6]}.out"
    with open(out, "w") as lf:
        p = subprocess.Popen(["bash", str(ORCH), proj.name, *args], env=env, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    return p, out


def wait_running_in_wait_driver(eng: HttpEngine, out: Path, timeout: float = 120) -> None:
    """El proyecto arrancó (START en el motor) Y el lanzador ya salió ("PID driver N" en la salida del orquestador):
    el orquestador está dentro de `wait_driver`, la situación real de un Stop & persist. (Si el stop llega ANTES de
    que el lanzador confirme el arranque, este espera sus 90 s: lo cubre otro test.)"""
    assert _wait_for(lambda: "START" in eng.kinds(), timeout=timeout), out.read_text()
    assert _wait_for(lambda: "PID driver" in out.read_text(), timeout=timeout), out.read_text()


def state_of(tmp_path: Path, proj: str) -> dict:
    f = tmp_path / "out" / f"{proj}_state.json"
    return json.loads(f.read_text())["tasks"] if f.exists() else {}


def bank_files(proj: Path) -> dict[str, list[str]]:
    out = {}
    dbs = proj / "databanks"
    if dbs.is_dir():
        for d in dbs.iterdir():
            if d.is_dir():
                out[d.name] = sorted(p.name for p in d.iterdir() if p.is_file())
    return out


def stop_flag(proj: Path) -> Path:
    return proj / ".pipeline_stop_request"


def monitor_log(proj: str) -> str:
    f = Path(f"/tmp/{proj}_run/monitor.log")
    return f.read_text() if f.exists() else ""


# =========================================================================================
# F2-a (R10, R5) — guardián estático sobre los 10 ficheros del perímetro
# =========================================================================================

def test_f2a_guardian_sin_action_save_ni_patron_ancho_de_motor_en_los_10_ficheros():
    """R10: ningún fichero del perímetro envía `-databank action=save` (sin `folder=` responde "Missing parameter
    'folder'": nunca persistió nada). R5: ninguno detecta o mata el motor por texto de cmdline (ahora incluye
    `persist_before_gui.py`, que en la Fase 1 aún usaba `pgrep -f Contents/Resources/sqcli`)."""
    bad: list[str] = []
    for rel in PERIMETER:
        for n, line in enumerate((SQX_DIR / rel).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"action=save\b", line):
                bad.append(f"{rel}:{n}: action=save: {line.strip()}")
            if "Resources/sqcli" in line:
                bad.append(f"{rel}:{n}: patrón ancho del motor: {line.strip()}")
            if "pkill" in line and "tail -f /dev/null" in line and not re.search(r"(^|[\s\"'])-g\b", line):
                bad.append(f"{rel}:{n}: pkill global de tail -f /dev/null: {line.strip()}")
    assert not bad, "\n".join(bad)


def test_f2a_el_driver_ya_no_usa_pkill_global_del_motor():
    """R1/R3: el camino zombi del driver ya no hace `pkill -9 -x` (mataba también motores ajenos)."""
    drv = (SQX_DIR / "run_pipeline_headless.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in drv.splitlines() if not ln.lstrip().startswith("#"))   # comentarios aparte
    assert not re.search(r"pkill\s+-9\s+-x", code)
    assert "kill_own_engine" in code


def test_f2a_el_mock_no_recibe_save_ni_en_el_persist_incremental_ni_en_el_final(tmp_path, engine_env, leftovers):
    """Driver real con una task que dura varios sondeos (persist incremental) y termina por sí sola (persist
    final): el motor falso NO recibe ningún `save` y SÍ los synctofiles y el export."""
    p = proj_name()
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="4", POLL_SECS="1")
    assert r.returncode == 0, r.stdout + r.stderr
    kinds = eng.kinds()
    assert "SAVE" not in kinds, kinds
    assert kinds.count("SYNC_LINE") >= 4, "incremental (al menos 1 ronda × 2 bancos) + final (2 bancos)"
    assert "EXPORT" in kinds


# =========================================================================================
# F2-b (R1) — synctofiles CONFIRMADO por el stdout del motor; sin línea ⇒ el motor NO se mata
# =========================================================================================

def test_f2b_synctofiles_y_export_usan_el_curl_largo_y_no_hay_curl_de_120s_para_ellos():
    """R1: `sq_long` (SQ_LONG_SECS, 1800) para synctofiles y export; `sq()` (120 s) solo para consultas."""
    src = DRIVER.read_text(encoding="utf-8")
    assert re.search(r'^SQ_LONG_SECS=\$\{SQ_LONG_SECS:-1800\}', src, re.M)
    assert re.search(r'^sq_long\(\)\{ curl -s --max-time "\$SQ_LONG_SECS"', src, re.M)
    for n, ln in enumerate(src.splitlines(), 1):
        if ln.lstrip().startswith("#"):
            continue
        if "action=synctofiles" in ln or "action=export" in ln:
            assert "sq_long" in ln and not re.search(r"\bsq \"", ln), f"{n}: {ln.strip()}"
    # stop_and_persist: mismo timeout largo (inyectable)
    assert "SQ_LONG_SECS" in (SQX_DIR / "stop_and_persist.py").read_text(encoding="utf-8")


def test_f2b_el_driver_no_mata_el_motor_antes_de_la_linea_de_sync_aunque_el_curl_expire(
        tmp_path, engine_env, leftovers):
    """El synctofiles tarda 4 s y el curl largo se fija a 2 s: el HTTP da rc 28, pero el driver CONFIRMA por la
    línea que el motor escribe al acabar y solo entonces mata el motor: en el registro de eventos del motor
    TODAS las líneas de sync (finales) preceden al TERM (orden verificado)."""
    p = proj_name("sb")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_SYNC_SECS="4",
                        SQ_LONG_SECS="2", SYNC_CONFIRM_SECS="30", SYNC_CONFIRM_POLL_SECS="0.3")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "curl rc=28" in out, "el curl largo (2 s) debía expirar con el sync aún en marcha"
    assert "NO confirmado" not in out
    kinds = eng.kinds()
    assert kinds.count("SYNC_LINE") == 2 and "TERM" in kinds
    assert max(i for i, k in enumerate(kinds) if k == "SYNC_LINE") < kinds.index("TERM"), kinds
    assert "sync de InA CONFIRMADO" in out and "sync de Out CONFIRMADO" in out
    assert not pgrep_x(engine_env), "tras confirmar, el driver mata su motor"


def test_f2b_sin_linea_de_confirmacion_el_motor_sigue_vivo_y_la_task_falla_con_sync_unconfirmed(
        tmp_path, engine_env, leftovers):
    """Task suelta: el HTTP responde "Synchronization started." pero el motor nunca escribe `Databank … saved` y
    tiene estrategias que no están en disco (A8: motor ≠ disco): el driver NO mata el motor (ni lo mató nadie: ningún
    TERM), lo dice y deja state failed/sync_unconfirmed. A2/R9: con el motor vivo al terminar NO dice "Ya puedes
    abrir la GUI" y da el motivo."""
    p = proj_name("su")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_SYNC_LINE="0", MOCK_COUNT_EXTRA="3",
                        SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="60", SYNC_CONFIRM_POLL_SECS="0.3")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "sync de InA sin línea del motor y motor ≠ disco o sin count (disco 1 .sqx, motor 4)" in out
    assert "sync de InA NO confirmado: dejo el motor vivo" in out
    assert "SYNC FINAL NO CONFIRMADO" in out and "dejo el motor VIVO" in out
    kinds = eng.kinds()
    assert "TERM" not in kinds, "con el sync sin confirmar NADIE mata el motor"
    assert pgrep_x(engine_env) and all(pid_alive(x) for x in eng.pids())
    t = state_of(tmp_path, p)["0:T1"]
    assert t["status"] == "failed" and t["cause"] == "sync_unconfirmed"
    assert "Ya puedes abrir la GUI" not in out
    assert "NO abras la GUI todavía" in out and "sqcli vivo" in out and "sync final no se confirmó" in out
    assert not (tmp_path / "out" / f"{p}_sync_unconfirmed.flag").exists(), "task suelta: lo dice el state, no la señal"


def test_f2b_a1_task_suelta_con_slim_y_marcador_el_trap_exit_no_mata_el_motor(tmp_path, engine_env, leftovers):
    """A1: con el sync final sin confirmar (task suelta `--slim`, marcador presente) el trap EXIT NO mata $SQPID
    ni el $TAILPID aunque haya marcador, NO restaura los bancos apartados y el marcador se queda."""
    p = proj_name("sa")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p, banks=("InA", "Out", "ExtraC"))
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, "--slim", MOCK_FIN_SECS="0.5", MOCK_SYNC_LINE="0",
                        MOCK_COUNT_EXTRA="3", SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="60",
                        SYNC_CONFIRM_POLL_SECS="0.3")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert (proj / "databanks_archive" / ".runner_session.json").exists(), "el marcador se queda"
    assert (proj / "databanks_archive" / "ExtraC").is_dir() and not (proj / "databanks" / "ExtraC").exists()
    assert "TERM" not in eng.kinds() and pid_alive(eng.pids()[0])
    log = monitor_log(p)
    assert "trap EXIT: sync final NO confirmado" in log and "NO restauro" in out
    assert "mato motor y restauro bancos" not in log
    # R9/A2: con el marcador slim sin restaurar y el motor vivo, NO dice "Ya puedes abrir la GUI" y da los motivos
    assert "Ya puedes abrir la GUI" not in out
    assert "NO abras la GUI todavía" in out and "corrida --slim sin restaurar" in out and "sqcli vivo" in out
    # el tail -f /dev/null (stdin del motor) del driver sigue vivo: matarlo le daría EOF al motor
    tails = subprocess.run(["pgrep", "-f", "^tail -f /dev/null$"], capture_output=True, text=True).stdout.split()
    assert any(SP.engine_owner_pgid(int(t)) == SP.engine_owner_pgid(eng.pids()[0]) for t in tails)


def test_f2b_orquestador_aborta_con_sync_unconfirmed_y_deja_el_motor_vivo(tmp_path, engine_env, leftovers):
    """Pipeline: el driver escribe la señal `<proj>_sync_unconfirmed.flag` (como el kill.flag del vigilante) y el
    orquestador aborta (rc 1) con la causa en el state y el motor VIVO a propósito."""
    p = proj_name("so")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", MOCK_FIN_SECS="0.5", MOCK_SYNC_LINE="0",
                                   MOCK_COUNT_EXTRA="3", SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="60",
                                   SYNC_CONFIRM_POLL_SECS="0.3", POLL_SECS="1")
    assert orch.wait(timeout=120) == 1, out.read_text()
    txt = out.read_text()
    assert "ABORT: task 0 (T1)" in txt and "SIGUE VIVO a propósito" in txt and "stop_and_persist.py" in txt
    assert re.search(rf"reanuda: bash \S*run_pipeline_per_task\.sh {p} 0", txt)
    t = state_of(tmp_path, p)["0:T1"]
    assert t["status"] == "failed" and t["cause"] == "sync_unconfirmed"
    assert not (tmp_path / "out" / f"{p}_sync_unconfirmed.flag").exists(), "el orquestador consume la señal"
    assert "TERM" not in eng.kinds() and eng.pids() and pid_alive(eng.pids()[0])


def test_f2b_incremental_sin_confirmar_avisa_y_sigue_y_el_final_confirma(tmp_path, engine_env, leftovers):
    """Persist incremental sin línea de confirmación (el motor solo la escribe con el proyecto ya terminado) y con
    estrategias en el motor que no están en disco: avisa y sigue; el persist FINAL sí confirma (por la línea) y el
    motor se mata normalmente."""
    p = proj_name("si")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="5", MOCK_SYNC_LINE="after_finish",
                        MOCK_COUNT_EXTRA="2", POLL_SECS="1", SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="60",
                        SYNC_CONFIRM_POLL_SECS="0.2")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "sync incremental de InA NO confirmado (sigo; el persist final decide)" in out
    assert "sync de InA CONFIRMADO" in out
    assert "TERM" in eng.kinds() and not pgrep_x(engine_env)
    assert state_of(tmp_path, p)["0:T1"]["status"] == "done"


def test_f2b_a4_aviso_si_hay_menos_sqx_en_disco_que_el_after_sync_del_motor(tmp_path, engine_env, leftovers):
    """A4: ficheros contra ficheros — el `after sync M` de la línea del motor frente a los .sqx de databanks/<b>/.
    Aviso (no compuerta): el driver sigue y mata el motor igual."""
    p = proj_name("sw")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_AFTER_SYNC="129")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "⚠ verificación en disco: InA tiene 1 .sqx en disco pero el motor declaró 'after sync 129'" in out
    assert "aviso, no compuerta" in out and "TERM" in eng.kinds()


def test_f2b_a4_sin_aviso_si_el_disco_coincide(tmp_path, engine_env, leftovers):
    p = proj_name("sk")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5")
    assert "verificación en disco OK: InA 1 .sqx (motor: after sync 1)" in r.stdout
    assert "⚠ verificación en disco" not in r.stdout


def test_f2b_el_nombre_del_banco_es_exacto_en_la_confirmacion(tmp_path):
    """`'proj/Out'` con la comilla de cierre NO casa con `'proj/OutX'`: una línea de otro banco de prefijo común no
    confirma el sync (y `.` en el nombre no es comodín: grep -F)."""
    eng_log = tmp_path / "engine.log"
    eng_log.write_text("Databank 'p/OutX' saved - files before sync 3 / after sync 3 / saved 3 / removed 0 in 1.00 s.\n"
                       "Databank 'p/Out' saved - files before sync 2 / after sync 2 / saved 2 / removed 0 in 1.00 s.\n"
                       "Databank 'p/a.c' saved - files before sync 1 / after sync 1 / saved 1 / removed 0 in 1.00 s.\n")
    fn = shell_function(DRIVER, "saved_count")

    def count(proj: str, bank: str) -> str:
        return subprocess.run(["bash", "-c", f'PROJ="{proj}"; ENG="{eng_log}"\n{fn}\nsaved_count "{bank}"'],
                              capture_output=True, text=True).stdout.strip()

    assert count("p", "Out") == "1" and count("p", "OutX") == "1"
    assert count("p", "Ou") == "0" and count("p", "O") == "0"
    assert count("p", "a.c") == "1" and count("p", "abc") == "0"          # '.' literal, no comodín
    assert count("p", "NoExiste") == "0"
    assert count("q", "Out") == "0"                                        # otro proyecto
    assert subprocess.run(["bash", "-c", f'PROJ=p; ENG="{tmp_path}/no_existe.log"\n{fn}\nsaved_count Out'],
                          capture_output=True, text=True).stdout.strip() == "0"


def test_f2b_un_banco_vacio_no_exige_linea_de_confirmacion_ni_deja_el_motor_vivo(tmp_path, engine_env,
                                                                                  leftovers):
    """PLAN-DESVIO (ver `sync_bank`): el motor real NO escribe la línea `saved` para un banco VACÍO (en
    ftmo_cfd_cartera_pmaster2_20261014 `Results` y `Carteras_21_s3_g1000` están vacíos y no tienen ninguna). Con el
    diseño literal cada banco vacío dejaría el motor vivo (sync_unconfirmed) y haría esperar SYNC_CONFIRM_SECS en cada
    ronda. Con `count`=0 el banco se da por vacío al instante: la corrida es normal y el motor se mata."""
    p = proj_name("sx")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p, banks=("InA", "Out", "Vacio"))
    for f in (proj / "databanks" / "Vacio").glob("*.sqx"):
        f.unlink()
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    t0 = time.time()
    # SYNC_CONFIRM_SECS muy por encima de la duración normal de la corrida (que en la CI de GitHub, ~3 veces más lenta
    # que en local, llegó a 43 s): esperarlo se distingue sin ambigüedad de no esperarlo.
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="4", POLL_SECS="1", SYNC_CONFIRM_SECS="300")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert time.time() - t0 < 200, "no esperó SYNC_CONFIRM_SECS (300 s) por el banco vacío"
    assert "sync de Vacio: banco VACÍO (count=0)" in out
    assert "NO confirmado" not in out and "SYNC FINAL NO CONFIRMADO" not in out
    kinds = eng.kinds()
    assert "SYNC_EMPTY" in kinds and "TERM" in kinds, "el motor se mata con normalidad"
    assert state_of(tmp_path, p)["0:T1"]["status"] == "done"
    assert "Ya puedes abrir la GUI" in out


def test_f2b_una_respuesta_de_error_del_motor_no_espera_la_confirmacion_y_no_mata_el_motor(tmp_path, engine_env,
                                                                                           leftovers):
    """Un `synctofiles` con respuesta de error (rc 0, p. ej. un nombre de banco que el HTTP no resuelve) no es un sync
    en marcha: la línea no va a llegar y no se espera SYNC_CONFIRM_SECS (300 s por banco aquí). Sigue siendo "sin
    confirmar": el motor no se mata."""
    p = proj_name("se2")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    t0 = time.time()
    # mismo margen que el test del banco vacío: 300 s de espera frente a < 200 s de corrida incluso en la CI lenta
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_SYNC_ERROR="1",
                        SYNC_CONFIRM_SECS="300")
    assert r.returncode == 0, r.stdout + r.stderr
    assert time.time() - t0 < 200, "no esperó SYNC_CONFIRM_SECS tras la respuesta de error"
    assert "el motor respondió con un error: no se espera confirmación" in r.stdout
    assert "SYNC FINAL NO CONFIRMADO" in r.stdout and "TERM" not in eng.kinds()


def test_f2b_si_el_count_no_responde_no_se_asume_vacio_y_sigue_sin_confirmar(tmp_path, engine_env, leftovers):
    """Fail-closed: sin línea Y sin poder comprobar que el banco está vacío (count sin respuesta) NO se da por
    confirmado: el motor se queda vivo."""
    p = proj_name("sn")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p, banks=("InA", "Out"))
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_SYNC_LINE="0", MOCK_COUNT_BAD="1",
                        SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="60", SYNC_CONFIRM_POLL_SECS="0.3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VACÍO" not in r.stdout and "sin cambios" not in r.stdout and "SYNC FINAL NO CONFIRMADO" in r.stdout
    assert "motor ?" in r.stdout, "A8: sin count no se compara con el disco"
    assert "TERM" not in eng.kinds()


def test_a8_sync_sin_nada_nuevo_no_escribe_linea_y_se_da_por_guardado_sin_esperar(tmp_path, engine_env, leftovers):
    """A8 (aceptación real 2026-10-27): B144 no escribe línea si el sync no tiene nada nuevo que guardar. Con rc 0, sin
    línea tras el margen y motor = disco → "sin cambios": la task acaba bien y el motor se mata, SIN esperar
    SYNC_CONFIRM_SECS (antes: 30 min y un sync_unconfirmed falso con el motor vivo)."""
    p = proj_name("sc")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p, banks=("InA", "Out"))
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    t0 = time.time()
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_SYNC_LINE="0",
                        SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="300", SYNC_CONFIRM_POLL_SECS="0.2")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert time.time() - t0 < 120, "no espera SYNC_CONFIRM_SECS (300 s)"
    assert "sync de InA sin cambios: el motor no escribe línea si no hay nada que guardar; disco 1 = motor 1" in out
    assert "sync de Out sin cambios" in out
    assert "NO CONFIRMADO" not in out and "NO confirmado" not in out
    assert "TERM" in eng.kinds() and not pgrep_x(engine_env)
    assert state_of(tmp_path, p)["0:T1"]["status"] == "done"


def test_a8_curl_agotado_sin_linea_sigue_esperando_el_tope_y_no_se_da_por_guardado(tmp_path, engine_env, leftovers):
    """A8 solo vale con rc 0 (el sync ya respondió). Con el curl largo agotado (rc 28) el sync puede seguir en el motor:
    se espera la línea hasta SYNC_CONFIRM_SECS y, si no llega, sin confirmar — aunque motor = disco."""
    p = proj_name("st")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    t0 = time.time()
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", MOCK_SYNC_LINE="0", MOCK_SYNC_SECS="3",
                        SQ_LONG_SECS="1", SYNC_LINE_GRACE_SECS="1", SYNC_CONFIRM_SECS="6",
                        SYNC_CONFIRM_POLL_SECS="0.2")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "curl rc=28" in out and "sin cambios" not in out
    assert time.time() - t0 >= 6, "con rc 28 espera el tope entero"
    assert "SYNC FINAL NO CONFIRMADO" in out and "TERM" not in eng.kinds()


def test_f2b_camino_zombi_e2e_mata_solo_el_motor_propio_y_no_uno_ajeno_del_mismo_nombre(
        tmp_path, procs, engine_env, leftovers):
    """E2E del camino ZOMBI del driver (HTTP ciego con proceso vivo y CPU baja): antes `pkill -9 -x sqcli` al final
    mataba TODO motor de ese nombre, también uno ajeno de otra corrida. Ahora TERM → gracia → kill -9 solo del
    $SQPID/grupo propio: el motor ajeno (otra sesión, mismo nombre exacto) sobrevive."""
    p = proj_name("sz")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    ajeno = fake_engine(procs, tmp_path / "bin2", engine_env)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_BLIND_AFTER="1", POLL_SECS="1", KILL_GRACE_SECS="2")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "declarado ZOMBI" in out and "MOTOR ZOMBI" in out
    assert "persist final (zombie=1" in out
    kinds = eng.kinds()
    assert "SYNC_LINE" not in kinds[kinds.index("START"):], "en zombi no hay persist por HTTP"
    assert _wait_for(lambda: not pid_alive(eng.pids()[0]), timeout=10), "el motor propio sí se mata"
    assert pid_alive(ajeno.pid), "el motor AJENO del mismo nombre NO se toca (antes: pkill -9 -x global)"
    assert state_of(tmp_path, p)["0:T1"]["cause"] == "engine_died"
    assert "Ya puedes abrir la GUI" not in out and "sqcli vivo" in out


def test_f2b_kill_final_term_gracia_y_kill9_solo_al_motor_propio(tmp_path, procs, engine_env):
    """`kill_own_engine` del driver (función REAL): TERM → gracia → kill -9 a $SQPID; un motor AJENO del mismo
    nombre exacto (otra sesión) NO se toca (antes: `pkill -9 -x` global en el camino zombi)."""
    propio = fake_engine(procs, tmp_path / "bin", engine_env, ignore_term=True)     # ignora TERM → kill -9
    ajeno = fake_engine(procs, tmp_path / "bin", engine_env)                        # otro grupo: NO se toca
    fns = "\n".join(shell_function(DRIVER, n) for n in ("pid_alive", "own_engine_pids", "kill_own_engine"))
    script = (f'ENGINE_PROC="{engine_env}"; SQPID={propio.pid}; TAILPID=""; KILL_GRACE_SECS=1; LOG=/dev/null\n'
              f'ts(){{ date +%H:%M:%S; }}\n{fns}\nkill_own_engine\necho FIN')
    t0 = time.time()
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert "FIN" in r.stdout and "kill -9" in r.stdout, r.stdout + r.stderr
    assert time.time() - t0 >= 1.0, "respeta la gracia antes del kill -9"
    assert _wait_for(lambda: not pid_alive(propio.pid), timeout=10)
    assert pid_alive(ajeno.pid), "un motor ajeno del mismo nombre NO se mata"


# =========================================================================================
# F2-d (R6) — Stop & persist de un pipeline de 2 tasks
# =========================================================================================

@pytest.mark.parametrize("slim", [False, True], ids=["normal", "slim"])
def test_f2d_parada_del_pipeline_persiste_antes_de_matar_no_lanza_la_task_1_y_sale_rc3(
        tmp_path, engine_env, leftovers, monkeypatch, slim):
    p = proj_name("sd")
    leftovers.append(p)
    proj = make_two_task_proj(tmp_path, p)
    cfx_antes = (proj / "project.cfx").read_bytes()
    antes = bank_files(proj)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    monkeypatch.setenv("PROJECTS_DIR", str(proj.parent))          # SP.projects_dir() == el del orquestador
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", *(["--slim"] if slim else []),
                                   POLL_SECS="60")
    wait_running_in_wait_driver(eng, out)

    rep = SP.run(p, port=eng.port, wait_secs=120)

    assert orch.wait(timeout=60) == 3, out.read_text()
    txt = out.read_text()
    # reporte: parada completada por el PIPELINE (no por el camino directo)
    assert rep["pipeline_stopped"] is True and rep["pipeline_stopping"] is False
    # A9: el status de B144 tras el stop no lo refleja; la respuesta al stop sí → UNA sola orden de parada
    assert rep["stopped_by"] == "pipeline" and rep["stop_delivered"] is True and rep["stop_sent"] == 1
    assert isinstance(rep["safe_for_gui"], bool) and isinstance(rep["reasons"], list)
    assert rep["motor_killed"] is False and rep["banks_persisted"] == [], "Stop & persist NO persiste en modo pipeline"
    # el driver de la task 0 persistió (sync confirmado) ANTES de que el motor muriera (orden en el motor)
    kinds = eng.kinds()
    assert "STOP" in kinds and "TERM" in kinds and kinds.count("START") == 1
    assert kinds.index("STOP") < kinds.index("FINISHED") < kinds.index("TERM")
    final_syncs = [i for i, k in enumerate(kinds) if k == "SYNC_LINE" and i > kinds.index("FINISHED")]
    assert final_syncs and max(final_syncs) < kinds.index("TERM"), kinds
    assert "SAVE" not in kinds
    assert "sync de InA CONFIRMADO" in monitor_log(p)
    # la task 1 NO se lanza: un solo motor en toda la corrida
    assert len(eng.pids()) == 1 and "TASK 1" not in txt
    # state de la task 0 = failed/user_stop; la 1 ni existe
    st = state_of(tmp_path, p)
    assert st["0:T1"]["status"] == "failed" and st["0:T1"]["cause"] == "user_stop"
    assert "1:T2" not in st
    # mensaje, bandera, .cfx y slim restaurados
    assert "PARADA por el usuario en la task 0 (T1)" in txt
    assert re.search(rf"Reanuda: bash \S*run_pipeline_per_task\.sh {p} 0 \(repite la task\) — o 1 para darla por buena", txt)
    assert not stop_flag(proj).exists()
    assert (proj / "project.cfx").read_bytes() == cfx_antes and not (proj / "project.cfx.pre_run_task.bak").exists()
    assert bank_files(proj) == antes, "bancos 1:1 (con --slim: restaurados)"
    assert not (proj / "databanks_archive" / ".runner_session.json").exists()
    assert not pgrep_x(engine_env), "el driver mató su motor DESPUÉS de persistir"


def test_f2d_stop_and_persist_cli_parada_de_pipeline_rc0_y_veredicto_de_la_gui(
        tmp_path, engine_env, leftovers, monkeypatch):
    """Misma parada pero por la CLI real (`python sqx/stop_and_persist.py`): rc 0 y el veredicto sale de
    `pre_gui_check` (R9): nunca "Ya puedes abrir la GUI" con algo vivo."""
    p = proj_name("sc")
    leftovers.append(p)
    proj = make_two_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", POLL_SECS="60")
    wait_running_in_wait_driver(eng, out)
    env = {**os.environ, "PROJECTS_DIR": str(proj.parent), "SQX_ENGINE_PROC_NAME": engine_env}
    r = subprocess.run([sys.executable, str(SQX_DIR / "stop_and_persist.py"), p, str(eng.port), "--wait-secs", "120"],
                       env=env, capture_output=True, text=True, timeout=180)
    assert orch.wait(timeout=60) == 3
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"Pipeline de '{p}' parado" in r.stdout
    if "Ya puedes abrir la GUI" in r.stdout:                      # solo si pre_gui_check dio luz verde
        assert "NO abras la GUI" not in r.stdout
    else:                                                          # (la máquina de desarrollo puede tener la GUI abierta)
        assert "NO abras la GUI todavía" in r.stdout


# =========================================================================================
# F2-e (R6/A7) — bandera rancia al arrancar
# =========================================================================================

def test_f2e_bandera_rancia_se_borra_se_avisa_y_el_pipeline_corre_entero(tmp_path, engine_env, leftovers):
    p = proj_name("se")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    stop_flag(proj).write_text("ts=2026-01-01 00:00:00\norigin=resto de una parada anterior\n")
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", MOCK_FIN_SECS="8", POLL_SECS="1")
    assert orch.wait(timeout=120) == 0, out.read_text()
    txt = out.read_text()
    assert "bandera de parada rancia" in txt and "la borro" in txt
    assert "PARADA por el usuario" not in txt, "la bandera rancia NO detiene la corrida nueva"
    assert not stop_flag(proj).exists()
    assert state_of(tmp_path, p)["0:T1"]["status"] == "done"
    assert "PIPELINE PER-TASK COMPLETO" in txt


def test_f2e_a7_con_otro_orquestador_vivo_el_nuevo_aborta_y_la_bandera_no_se_toca(tmp_path, procs, engine_env,
                                                                                   leftovers):
    """A7: una parada en curso (bandera puesta) de un orquestador vivo NO la borra un orquestador nuevo: aborta
    sin tocar nada. Solo tras la comprobación A7 se borra una bandera rancia."""
    p = proj_name("sv")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    contenido = "ts=2026-10-21 12:00:00\norigin=stop_and_persist pid=1\n"
    stop_flag(proj).write_text(contenido)
    primero = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", p, 0)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    env = base_env(tmp_path, proj.parent, engine_env, eng)
    r = subprocess.run(["bash", str(ORCH), p, "0"], env=env, capture_output=True, text=True, timeout=60)
    txt = r.stdout + r.stderr
    assert r.returncode == 1 and "ya hay un pipeline vivo" in txt and str(primero.pid) in txt
    assert "rancia" not in txt, "el aborto A7 ocurre ANTES de mirar la bandera"
    assert stop_flag(proj).read_text() == contenido, "la bandera NO se toca"


# =========================================================================================
# F2-f (R6/D2) — cascada: rc 3 detiene la cadena entera
# =========================================================================================

def _fake_repo_with_stub_orchestrator(tmp_path: Path, rc_by_proj: dict[str, int]) -> tuple[Path, Path]:
    """Copia la cascada REAL a un repo falso y le pone un orquestador de pega que registra a quién lanzan y sale
    con el rc pedido: aísla la lógica de la cascada (rc 3 = parada del usuario)."""
    repo = tmp_path / "fake_repo"
    (repo / "sqx").mkdir(parents=True)
    shutil.copy(CASCADE, repo / "sqx" / "run_cascade_projects.sh")
    calls = tmp_path / "calls.txt"
    cases = "\n".join(f'  {k}) exit {v} ;;' for k, v in rc_by_proj.items())
    (repo / "sqx" / "run_pipeline_per_task.sh").write_text(
        f'#!/bin/bash\necho "$1 $2" >> "{calls}"\ncase "$1" in\n{cases}\n  *) exit 0 ;;\nesac\n')
    return repo / "sqx" / "run_cascade_projects.sh", calls


def _run_cascade(tmp_path: Path, script: Path, engine_name: str, *projs: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update({"SQX_OUTDIR": str(tmp_path / "out"), "SQX_ENGINE_PROC_NAME": engine_name, "PORT": str(free_port()),
                "PORT_WAIT_SECS": "2", "BARRIER_POLL_SECS": "0.2"})
    return subprocess.run(["bash", str(script), "0", *projs], env=env, capture_output=True, text=True, timeout=60)


def test_f2f_cascada_rc3_del_primero_no_lanza_el_segundo_y_sale_rc3(tmp_path, engine_env):
    script, calls = _fake_repo_with_stub_orchestrator(tmp_path, {"p1": 3, "p2": 0})
    r = _run_cascade(tmp_path, script, engine_env, "p1", "p2")
    txt = r.stdout + r.stderr
    assert r.returncode == 3, txt
    assert calls.read_text().split("\n")[:-1] == ["p1 0"], "el 2.º proyecto NO se lanza"
    assert "⏹ PARADA por el usuario: no sigo con los demás proyectos" in txt, txt
    assert "el orquestador de p1 salió con rc 3" in txt and "sin lanzar: p2" in txt
    assert "CASACADA TERMINADA" not in txt


def test_f2f_un_fallo_normal_rc1_no_detiene_la_cascada_solo_la_parada_rc3(tmp_path, engine_env):
    """D2 se limita a rc 3: un proyecto que FALLA (rc 1) se loguea y la cadena sigue como siempre."""
    script, calls = _fake_repo_with_stub_orchestrator(tmp_path, {"p1": 1, "p2": 0})
    r = _run_cascade(tmp_path, script, engine_env, "p1", "p2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls.read_text().split("\n")[:-1] == ["p1 0", "p2 0"]
    assert "p1 FALLÓ (rc=1)" in r.stdout and "p2 COMPLETADO" in r.stdout


# =========================================================================================
# F2-g (R6, A5) — bandera inyectada JUSTO tras la 2.ª comprobación del lanzador (e2e)
# =========================================================================================

def _python3_shim(tmp_path: Path) -> str:
    """Directorio con un `python3` que, SOLO para run_task_headless.py, inyecta la bandera de parada justo después
    de la 2.ª comprobación de `stop_requested` (antes del Popen); para todo lo demás exec al python3 real."""
    d = tmp_path / "shim"
    d.mkdir()
    sh = d / "python3"
    sh.write_text(f'''#!/bin/bash
REAL="{REAL_PYTHON3}"
if [ "${{1##*/}}" = "run_task_headless.py" ]; then
  shift
  exec "$REAL" -c '
import sys
sys.path.insert(0, sys.argv[1])
import run_task_headless as RT
n = {{"calls": 0}}
orig = RT.stop_requested
def inyectada(proj):
    r = orig(proj)
    n["calls"] += 1
    if n["calls"] == 2:          # A5: la bandera aparece JUSTO DESPUÉS de la 2.ª comprobación (antes del Popen)
        RT.SP.write_stop_flag(proj, origin="test A5")
    return r
RT.stop_requested = inyectada
sys.argv = ["run_task_headless.py"] + sys.argv[2:]
sys.exit(RT._cli())
' "{SQX_DIR}" "$@"
fi
exec "$REAL" "$@"
''')
    sh.chmod(0o755)
    return str(d)


def test_f2g_a5_bandera_justo_tras_la_2a_comprobacion_el_driver_nace_recibe_su_stop_y_no_se_lanza_otra(
        tmp_path, engine_env, leftovers, monkeypatch):
    """Ventana residual aceptada y compensada (A5): el driver nace con la bandera ya puesta (el lanzador no la volvió
    a mirar), Stop & persist le entrega SU `stop` en el bucle del modo pipeline, la task 0 queda `user_stop`, rc 3
    y la task 1 no se lanza."""
    p = proj_name("g5")
    leftovers.append(p)
    proj = make_two_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    monkeypatch.setenv("PROJECTS_DIR", str(proj.parent))
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", POLL_SECS="60",
                                   path_prefix=_python3_shim(tmp_path))
    wait_running_in_wait_driver(eng, out)
    assert stop_flag(proj).exists(), "la bandera se inyectó tras la 2.ª comprobación y antes del Popen"
    rep = SP.run(p, port=eng.port, wait_secs=120)
    assert orch.wait(timeout=60) == 3, out.read_text()
    txt = out.read_text()
    assert rep["pipeline_stopped"] and rep["stop_delivered"] and rep["stopped_by"] == "pipeline"
    st = state_of(tmp_path, p)
    assert st["0:T1"]["status"] == "failed" and st["0:T1"]["cause"] == "user_stop" and "1:T2" not in st
    assert len(eng.pids()) == 1, "ninguna task más"
    assert "PARADA por el usuario en la task 0" in txt and not stop_flag(proj).exists()
    kinds = eng.kinds()
    assert kinds.index("STOP") < kinds.index("FINISHED") < kinds.index("TERM")


# =========================================================================================
# F2-h (R6) — TERM al orquestador durante wait_driver
# =========================================================================================

def test_f2h_term_al_orquestador_durante_wait_driver_sale_enseguida_con_el_indice(tmp_path, engine_env, leftovers):
    """Con WAIT_SLEEP=30 un TERM esperaba hasta 30 s (el trap se difería hasta que acababa el `sleep`): ahora
    `sleep … & wait $!` y sale en <5 s imprimiendo `Reanuda: … <idx>`."""
    p = proj_name("sh")
    leftovers.append(p)
    proj = make_two_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", POLL_SECS="60", WAIT_SLEEP="30")
    wait_running_in_wait_driver(eng, out)
    time.sleep(1.0)                                            # ya está dentro de wait_driver (dormido 30 s)
    t0 = time.time()
    os.kill(orch.pid, signal.SIGTERM)
    assert orch.wait(timeout=20) == 143
    assert time.time() - t0 < 5, f"tardó {time.time() - t0:.1f} s en atender el TERM"
    txt = out.read_text()
    assert "interrumpido (TERM) en la task 0 (T1)" in txt
    assert re.search(rf"Reanuda: bash \S*run_pipeline_per_task\.sh {p} 0 \(cuando ese driver termine", txt)
    assert "TASK 1" not in txt
    # el driver de la task en curso SIGUE vivo (otra sesión) y persistirá solo: aquí lo limpia el teardown
    assert SP.driver_pids(p), "el driver sigue vivo tras el TERM al orquestador"


def test_f2h_int_tambien_imprime_el_indice_y_sale_130(tmp_path, engine_env, leftovers):
    p = proj_name("sj")
    leftovers.append(p)
    proj = make_two_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", POLL_SECS="60", WAIT_SLEEP="30")
    assert _wait_for(lambda: "PID driver" in out.read_text(), timeout=120), out.read_text()
    time.sleep(1.0)
    os.kill(orch.pid, signal.SIGINT)
    assert orch.wait(timeout=20) == 130
    assert "interrumpido (INT)" in out.read_text() and "Reanuda" in out.read_text()


# =========================================================================================
# F2-i (R9) — mensajes "ya puedes abrir la GUI" veraces
# =========================================================================================

def test_f2i_driver_en_pipeline_nunca_dice_ya_puedes_abrir_la_gui(tmp_path, engine_env, leftovers):
    p = proj_name("i1")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5", RUNNER_PIPELINE="1")
    out = r.stdout
    assert r.returncode == 0, out + r.stderr
    assert "DONE task 0:T1" in out and "el pipeline sigue: NO abras la GUI hasta \"PIPELINE PER-TASK COMPLETO\"" in out
    assert "Ya puedes abrir la GUI" not in out


def test_f2i_task_suelta_limpia_con_motor_muerto_dice_ya_puedes_abrir_la_gui(tmp_path, engine_env, leftovers):
    p = proj_name("i2")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5")
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"DONE\. Artefactos en \S+ — Ya puedes abrir la GUI\. ===", r.stdout)
    assert "NO abras la GUI" not in r.stdout
    assert state_of(tmp_path, p)["0:T1"]["status"] == "done"


def test_f2i_con_bak_pendiente_no_lo_dice_y_da_el_motivo(tmp_path, engine_env, leftovers):
    p = proj_name("i3")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    (proj / "project.cfx.pre_run_task.bak").write_bytes((proj / "project.cfx").read_bytes())
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Ya puedes abrir la GUI" not in r.stdout
    assert "NO abras la GUI todavía" in r.stdout and "project.cfx.pre_run_task.bak" in r.stdout


def test_f2i_a2_con_un_sqcli_vivo_al_terminar_no_lo_dice_y_da_el_motivo(tmp_path, procs, engine_env, leftovers):
    """A2: un motor AJENO (otra sesión, mismo nombre exacto) sigue vivo cuando acaba la task suelta: el driver no
    lo mata y NO dice que se pueda abrir la GUI."""
    p = proj_name("i4")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    ajeno = fake_engine(procs, tmp_path / "bin2", engine_env)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5")
    assert r.returncode == 0, r.stdout + r.stderr
    assert pid_alive(ajeno.pid), "el driver no mata un motor ajeno"
    assert "Ya puedes abrir la GUI" not in r.stdout
    assert "NO abras la GUI todavía" in r.stdout and f"hay un sqcli vivo (pids {ajeno.pid}" in r.stdout


def test_f2i_driver_de_task_suelta_con_bandera_dice_parada_del_usuario_y_state_user_stop(tmp_path, engine_env,
                                                                                          leftovers):
    p = proj_name("i5")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    stop_flag(proj).write_text("ts=x\norigin=test\n")
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    r = run_driver_http(tmp_path, proj, eng, engine_env, MOCK_FIN_SECS="0.5")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "task parada por el usuario" in r.stdout and "Task parada por el usuario." in r.stdout
    t = state_of(tmp_path, p)["0:T1"]
    assert t["status"] == "failed" and t["cause"] == "user_stop"


def _gui_ocupada() -> str | None:
    """Motivo por el que la MÁQUINA no permite el caso 'seguro' (pre_gui_check es global): GUI abierta, otro
    orquestador/driver/cascada reales vivos."""
    try:
        for label, probe in (("GUI de SQX abierta", SP.gui_pids), ("orquestador vivo", SP.orchestrator_pids),
                             ("driver vivo", SP.driver_pids), ("cascada viva", SP.cascade_pids)):
            if probe():
                return label
    except SP.ProcProbeError as e:
        return f"pgrep roto: {e}"
    return None


def _esperar_maquina_en_calma(timeout: float = 10.0) -> str | None:
    """Motivo por el que la máquina NO está en calma tras esperar `timeout` s (los restos del test anterior tardan
    unos ms en desaparecer de `pgrep`); None si lo está."""
    _wait_for(lambda: _gui_ocupada() is None, timeout=timeout)
    return _gui_ocupada()


@pytest.mark.parametrize("slim", [False, True], ids=["normal", "slim"])
def test_f2i_orquestador_completo_dice_ya_puedes_abrir_la_gui_solo_si_pre_gui_check_es_seguro(
        tmp_path, engine_env, leftovers, slim):
    """Caso SEGURO (necesita la máquina en calma: pre_gui_check es global, y la GUI de SQX del desarrollador abierta
    lo vuelve inseguro; entonces el caso se cubre en `..._el_mensaje_siempre_coincide_con_pre_gui_check`). Con
    `--slim` el restore acumulado se hace ANTES del veredicto (si no, el marcador lo haría siempre inseguro)."""
    motivo = _esperar_maquina_en_calma()
    if motivo:
        pytest.skip(f"la máquina no permite el caso seguro (pre_gui_check es global): {motivo}")
    p = proj_name("i6")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p, banks=("InA", "Out", "ExtraC"))
    antes = bank_files(proj)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", *(["--slim"] if slim else []),
                                   MOCK_FIN_SECS="8", POLL_SECS="1")
    assert orch.wait(timeout=120) == 0, out.read_text()
    txt = out.read_text()
    assert re.search(r"PIPELINE PER-TASK COMPLETO: \S+ — artefactos en \S+ — ya puedes abrir la GUI ===", txt), txt
    assert "NO abras la GUI" not in txt
    assert bank_files(proj) == antes


@pytest.mark.parametrize("slim", [False, True], ids=["normal", "slim"])
def test_f2i_orquestador_completo_el_mensaje_siempre_coincide_con_pre_gui_check(tmp_path, engine_env, leftovers,
                                                                                monkeypatch, slim):
    """Sin depender de que la máquina esté en calma (nunca se salta): el final del orquestador dice "ya puedes abrir
    la GUI" SI Y SOLO SI `pre_gui_check` sale seguro (R9). Con la GUI de SQX del desarrollador abierta lo correcto es
    NO decirlo y dar el motivo; en calma, decirlo. Con `--slim` los bancos quedan restaurados ANTES del veredicto."""
    p = proj_name("i8")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p, banks=("InA", "Out", "ExtraC"))
    antes = bank_files(proj)
    monkeypatch.setenv("PROJECTS_DIR", str(proj.parent))
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", *(["--slim"] if slim else []),
                                   MOCK_FIN_SECS="8", POLL_SECS="1")
    assert orch.wait(timeout=120) == 0, out.read_text()
    txt = out.read_text()
    final = [ln for ln in txt.splitlines() if "PIPELINE PER-TASK COMPLETO" in ln]
    assert len(final) == 1, txt
    dice = bool(re.search(r"— ya puedes abrir la GUI ===$", final[0]))
    chk = SP.pre_gui_check(p)                     # tras salir el orquestador: el mismo veredicto global
    assert dice == chk["safe"], (final[0], chk)
    if chk["safe"]:
        assert "NO abras la GUI" not in final[0]
    else:
        assert "NO abras la GUI todavía:" in final[0] and "ya puedes abrir la GUI" not in final[0]
        assert chk["reasons"], "un veredicto inseguro siempre trae motivos"
    assert bank_files(proj) == antes, "restaurado antes del veredicto (slim) / intacto (normal)"
    assert not (proj / "databanks_archive" / ".runner_session.json").exists()


def test_f2i_orquestador_completo_con_una_cascada_viva_no_dice_ya_puedes_abrir_la_gui(tmp_path, procs, engine_env,
                                                                                       leftovers):
    p = proj_name("i7")
    leftovers.append(p)
    proj = make_one_task_proj(tmp_path, p)
    cascada = fake_launcher(procs, tmp_path / "c", "run_cascade_projects.sh", 0, p)
    eng = HttpEngine(tmp_path, engine_env, p, proj, free_port())
    orch, out = spawn_orchestrator(tmp_path, proj, eng, engine_env, "0", MOCK_FIN_SECS="8", POLL_SECS="1")
    assert orch.wait(timeout=120) == 0, out.read_text()
    txt = out.read_text()
    assert "PIPELINE PER-TASK COMPLETO" in txt
    assert "ya puedes abrir la GUI" not in txt
    assert "NO abras la GUI todavía" in txt and "hay una cascada de proyectos viva" in txt and str(cascada.pid) in txt


# =========================================================================================
# F2-k — regresión: `bash -n` de los 4 .sh
# =========================================================================================

@pytest.mark.parametrize("script", FOUR_SH)
def test_f2k_bash_n(script):
    r = subprocess.run(["bash", "-n", str(SQX_DIR / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
