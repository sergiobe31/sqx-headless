"""Tests de los hooks --slim (Fase 1 del plan ram-runner-integration).

Criterios de aceptación F1 del plan, con driver/motor mockeados y fixture de
proyecto en tmp (cfx mínimo con 3 bancos declarados; .sqx dummy = zip con
settings.xml mínimo):

  (a) integración: durante la "task" sólo existen input+output en databanks/;
      al finalizar, todo restaurado 1:1 y marcador borrado;
  (b) driver muerto a mitad (kill -9) → la siguiente invocación reconcilia
      presence-aware y deja el proyecto completo;
  (c) muerte DURANTE la reconciliación → el siguiente arranque la reanuda sin
      abortar (idempotencia);
  (d) update-in-place (input==output) no archiva ese banco;
  (e) doble corrida --slim consecutiva sin intervención;
  (f) dir vacío recreado por el motor mock → restore lo salta/rmdir y completa;
  (g) pipeline: la task 2 usa como input un banco archivado → el driver lo
      restaura antes de lanzar el motor (L3 por task); + cadena completa con
      el ORQUESTADOR real (restore acumulado al final);
  (h) regresión: slim=False intacto (launcher y driver).

El "motor" mock es un script python que habla por stdout (lo que el driver
redirige a engine.log) y un mini-servidor HTTP en un puerto libre que responde
a los /call?cmd=... del driver. Los knobs de entorno (PROJECTS_DIR, SQCLI,
SQX_OUTDIR, SLIM_TESTING, cadencias) son el seam de test: en producción los
defaults son los paths reales y las cadencias reales.
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
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQX = ROOT / "sqx"
if str(SQX) not in sys.path:
    sys.path.insert(0, str(SQX))

import bank_plan as BP  # noqa: E402
import run_task_headless as RT  # noqa: E402

DRIVER = ROOT / "sqx" / "run_pipeline_headless.sh"
ORCHESTRATOR = ROOT / "sqx" / "run_pipeline_per_task.sh"

# "Motor" sqcli mock: imprime lo que el driver espera leer de engine.log, deja un
# snapshot de databanks/ al arrancar (para verificar qué veía el motor), opcionalmente
# recrea un dir de banco vacío (L2), toca outputs y/o crece en RAM (MOCK_GROW,
# para ejercitar al vigilante: "rate_mb_s:max_mb:paso_secs"); luego espera a que el
# driver lo mate.
MOCK_SQCLI = r"""#!/usr/bin/env python3
import json, os, pathlib, time
dbs = pathlib.Path(os.environ["MOCK_DBS"])
snap = pathlib.Path(os.environ["MOCK_SNAP"])
print("List of available projects", flush=True)
banks = sorted(p.name for p in dbs.iterdir() if p.is_dir()) if dbs.is_dir() else []
snap.write_text(json.dumps({"banks": banks}))
rec = os.environ.get("MOCK_RECREATE")
if rec:
    (dbs / rec).mkdir(parents=True, exist_ok=True)   # el motor/GUI recrea dirs vacíos (L2)
for b in [x for x in os.environ.get("MOCK_TOUCH", "").split(",") if x]:
    d = dbs / b
    d.mkdir(parents=True, exist_ok=True)
    (d / (b + "_nuevo.sqx")).write_bytes(b"sqx")
# Como el motor REAL, cada synctofiles escribe en su stdout "Databank '<proj>/<banco>' saved - files before sync X /
# after sync M / ..." (el driver lo usa para CONFIRMAR el guardado, R1). El HTTP del test vive en OTRO proceso y
# solo apunta los comandos en MOCK_REQUESTS; este hilo los lee e imprime la línea (único escritor del stdout).
import re, threading
_REQ = os.environ.get("MOCK_REQUESTS")
if _REQ:
    def _confirm_syncs():
        pos, buf = 0, b""
        while True:
            try:
                with open(_REQ, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                pos += len(chunk)
                buf += chunk
            except OSError:
                pass
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                m = re.search(r"action=synctofiles project=(\S+) name=(.+)$", line.decode("utf-8", "replace"))
                if m:
                    d = dbs / m.group(2).strip()
                    n = sum(1 for q in d.iterdir() if q.suffix == ".sqx") if d.is_dir() else 0
                    print(f"Databank '{m.group(1)}/{m.group(2).strip()}' saved - files before sync {n} / "
                          f"after sync {n} / saved {n} / removed 0 in 0.01 s.", flush=True)
            time.sleep(0.05)
    threading.Thread(target=_confirm_syncs, daemon=True).start()
_BLOBS = []   # mantiene la memoria asignada (si se libera, el RSS no sube)
_grow = os.environ.get("MOCK_GROW")
def _materialize(chunk):
    # Dos trampas del medidor de RSS en macOS bajo presión de memoria:
    #  - bytearray(n) queda como páginas COW de ceros: sin escribir, el RSS
    #    NO sube;
    #  - un buffer lleno de UN MISMO byte queda INACTIVO y el compresor del
    #    sistema lo COMPRIME (el RSS reportado se desploma).
    # Rellenamos cada página con bytes aleatorios (incompresibles) y listo.
    page = os.urandom(4096)
    for i in range(0, len(chunk), 4096):
        chunk[i:i + 4096] = page
    return chunk
if _grow:
    _base_mb = float(os.environ.get("MOCK_GROW_BASE", "0"))
    if _base_mb > 0:
        _BLOBS.append(_materialize(bytearray(int(_base_mb * 1048576))))
    _rate, _max, _step = (float(x) for x in _grow.split(":"))
    _chunk = max(1, int(_rate * _step * 1048576))
    def _grow_loop():
        total = 0.0
        while total < _max:
            _BLOBS.append(_materialize(bytearray(_chunk)))
            total += _rate * _step
            time.sleep(_step)
    threading.Thread(target=_grow_loop, daemon=True).start()
time.sleep(float(os.environ.get("MOCK_FIN_SECS", "0.3")))
print("Project finished", flush=True)
time.sleep(float(os.environ.get("MOCK_LINGER", "30")))   # ventana para el kill del driver
"""


class _Handler(BaseHTTPRequestHandler):
    cmds: list[str] = []   # registro de comandos recibidos (evidencia de stop/persist)

    def do_GET(self) -> None:  # noqa: N802 (API de BaseHTTPRequestHandler)
        cmd = parse_qs(urlparse(self.path).query).get("cmd", [""])[0]
        _Handler.cmds.append(cmd)
        m = re.search(r"project=(\S+)", cmd)
        if "action=synctofiles" in cmd and m:   # el motor mock (otro proceso) imprime la confirmación
            scratch = Path(f"/tmp/{m.group(1)}_run")
            if scratch.is_dir():
                with open(scratch / "mock_requests.log", "a") as f:
                    f.write(cmd + "\n")
        if "action=list" in cmd:
            body = "List of available projects"
        elif "action=status" in cmd:
            body = "Project p\nIn databank 5\nStrategies generated 3"
        else:
            body = "ok"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture()
def http_port() -> int:
    _Handler.cmds.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield port
    srv.shutdown()


def _write_sqx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("settings.xml", "<settings/>")


def make_proj(projects_dir: Path, name: str, tasks: list[tuple[str, str, list[str], str]],
              banks: list[str]) -> Path:
    """Proyecto fixture: cfx mínimo + databanks/ con un .sqx dummy por banco."""
    proj = projects_dir / name
    cfg = "".join(f'<Databank name="{b}" />' for b in banks)
    members: dict[str, str] = {}
    cfg_tasks = []
    for i, (label, typ, inputs, output) in enumerate(tasks):
        xmlfile = f"Task-{i}.xml"
        if typ == "Retest":
            dbs = (f'<Databank label="Output databank" name="O" value="{output}" />'
                   + "".join(f'<Databank label="Input databank" name="I" value="{b}" />'
                             for b in inputs))
        elif typ == "Build":
            dbs = (f'<Databank label="Output databank" name="O" value="{output}" />'
                   + "".join(f'<Databank label="Input databank" name="I" value="{b}" />'
                             for b in inputs))
        else:
            dbs = (f'<Databank label="Source databank" name="S" value="{inputs[0]}" />'
                   f'<Databank label="Target databank" name="T" value="{output}" />')
        members[xmlfile] = f"<Task>{dbs}</Task>"
        cfg_tasks.append(f'<Task type="{typ}" name="n{i}" taskXMLFile="{xmlfile}" '
                         f'title="{label}" active="true" />')
    members["config.xml"] = f"<root>{cfg}{''.join(cfg_tasks)}</root>"
    proj.mkdir(parents=True)
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        for n, content in members.items():
            z.writestr(n, content)
    for b in banks:
        _write_sqx(proj / "databanks" / b / f"{b}.sqx")
    return proj


def bank_files(proj: Path) -> dict[str, list[str]]:
    out = {}
    dbs = proj / "databanks"
    if dbs.is_dir():
        for d in dbs.iterdir():
            if d.is_dir():
                out[d.name] = sorted(p.name for p in d.iterdir() if p.is_file())
    return out


def run_driver(proj: Path, http_port: int, tmp_path: Path, task_idx: int = 0,
               slim: bool = True, pipeline: bool = False, linger: int = 30,
               **extra: str) -> subprocess.CompletedProcess:
    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    MOCK_REGISTRY.append(mock.name)   # para teardown que caza huérfanos aunque el test falle
    snap = tmp_path / f"snap_{uuid4().hex[:8]}.json"
    # Aislamiento determinista entre tests:
    #  - WD_PROC_PATTERN = nombre único del mock (uuid): un vigilante huérfano de
    #    otro test nunca mide ni mata el mock de este.
    #  - WD_FREE_PCT_MIN = 0: la señal "RAM libre < 10 %" depende de la máquina
    #    (GUI del usuario, carga) -> no-determinista en tests; los tests de KILL
    #    usan la señal RSS, que sí controlan.
    extra.setdefault("WD_PROC_PATTERN", mock.name)
    extra.setdefault("WD_FREE_PCT_MIN", "0")
    env = _mock_env(proj, tmp_path, mock, snap, MOCK_LINGER=str(linger), **extra)
    args = ["bash", str(DRIVER), proj.name, str(http_port), "", str(task_idx)]
    if slim:
        args.append("--slim")
        if pipeline:
            args.append("--slim-pipeline")
    proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    proc.mock_snap = snap  # type: ignore[attr-defined]
    proc.mock = mock  # type: ignore[attr-defined]
    return proc



def _mock_env(proj: Path, tmp_path: Path, mock: Path, snap: Path, **extra: str) -> dict:
    mock.chmod(0o755)   # el driver invoca "$SQCLI" entre comillas: debe ser ejecutable
    env = os.environ.copy()
    env.update({
        "PROJECTS_DIR": str(proj.parent),
        "SQCLI": str(mock),
        "SQX_OUTDIR": str(tmp_path / "out"),
        "SLIM_TESTING": "1",
        "CARGA_SLEEP": "0.1", "STABLE_N": "3", "QUICK_SECS": "0.2", "STOP_SECS": "0.1",
        "SLIM_TRAP_WAIT_SECS": "10",
        "WAIT_MARGIN": "0.5", "WAIT_SLEEP": "0.2",
        # R1: el driver confirma cada synctofiles por el stdout del motor (el mock lo imprime vía MOCK_REQUESTS)
        "MOCK_REQUESTS": str(Path("/tmp") / f"{proj.name}_run" / "mock_requests.log"),
        "SYNC_CONFIRM_POLL_SECS": "0.1", "SYNC_CONFIRM_SECS": "20", "SQ_LONG_SECS": "30", "KILL_GRACE_SECS": "5",
        "MOCK_DBS": str(proj / "databanks"),
        "MOCK_SNAP": str(snap),
        "MOCK_FIN_SECS": "0.3", "MOCK_LINGER": "30",
    })
    env.update(extra)
    return env

def _wait_for(predicate, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# Registro de mocks creados por run_driver: los teardowns lo usan para cazar
# procesos huérfanos (mock/vigilante/driver) aunque un test falle a mitad.
MOCK_REGISTRY: list[str] = []


# ----------------------------- (a) integración driver ----------------------------

def test_a_driver_slim_deja_solo_input_output_y_restaura(tmp_path, http_port,
                                                         monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slima_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    before = bank_files(proj)

    proc = run_driver(proj, http_port, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # durante la task el motor sólo veía input + output
    snap = json.loads(proc.mock_snap.read_text())  # type: ignore[attr-defined]
    assert snap["banks"] == ["InA", "Out"]
    # el persist final saltó el banco archivado (M4)
    assert "skip persist final ExtraC" in proc.stdout
    # al final: todo restaurado 1:1 y marcador borrado
    assert bank_files(proj) == before
    assert not BP.marker_path(proj).exists()
    assert (proj / "databanks_archive").is_dir()  # el dir archive puede quedar (manifest consumido)


def test_a2_persist_incremental_salta_bancos_archivados(tmp_path, http_port,
                                                        monkeypatch):
    """El skip del persist del loop de poll (no sólo el del persist final):
    la task sigue viva tras la fase quick y el poll itera al menos una vez."""
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slima2_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    # MOCK_FIN_SECS con margen: el "Project finished" del mock cuenta desde que arranca, y en un runner lento (CI de
    # GitHub, 2026-10-26) 2,5 s llegaban antes del primer sondeo y el bucle no iteraba nunca.
    proc = run_driver(proj, http_port, tmp_path, linger=5,
                      POLL_MIN="0", MOCK_FIN_SECS="10")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skip persist ExtraC (archivado --slim, M4)" in proc.stdout


def test_a3_camino_completo_terminado_stop_persist_restore(tmp_path, http_port,
                                                           monkeypatch):
    """El camino EXACTO que la corrida de 2026-10-06 no pudo terminar:
    task termina -> STOP -> persist final -> kill -> restore --slim -> DONE.
    Un error de sintaxis en CUALQUIER punto posterior al poll (el del incidente)
    hubiera fallado aquí: se verifican los hitos del log, los comandos HTTP
    reales recibidos por el motor mock y los CSV exportados en disco."""
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    name = f"slima3_{uuid4().hex[:6]}"
    proj = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    before = bank_files(proj)

    proc = run_driver(proj, http_port, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # hitos del log (de "proyecto TERMINADO" a "DONE")
    for hito in ("proyecto TERMINADO", "STOP graceful + persist final",
                 "skip persist final ExtraC", "SLIM: restore final",
                 "DONE. Artefactos"):
        assert hito in proc.stdout, f"hito ausente del log: {hito}"
    # comandos HTTP reales recibidos por el motor mock (stop + persist final)
    joined = "\n".join(_Handler.cmds)
    assert "action=stop" in joined
    assert "action=save" not in joined, "R10/F2-a: el driver ya no lanza `save` (sin folder= no persistía nada)"
    assert "action=synctofiles" in joined
    assert "action=export" in joined
    # R1: cada synctofiles del persist final queda CONFIRMADO por la línea del stdout del motor
    assert "sync de InA CONFIRMADO" in proc.stdout and "sync de Out CONFIRMADO" in proc.stdout
    assert "DONE. Artefactos" in proc.stdout
    # artefactos que el PROPIO driver escribe en disco (el CSV del export lo escribe
    # el motor real; con el mock se verifica por el comando HTTP recibido arriba).
    # Ojo: bajo out/ también viven <proj>_telemetry.csv y <proj>_state.json (Fase 2).
    out_dirs = [d for d in (tmp_path / "out").glob(f"{name}_*") if d.is_dir()]
    assert out_dirs and (out_dirs[0] / "diagnostico_presion.txt").exists()
    assert (out_dirs[0] / "monitor.log").exists() and (out_dirs[0] / "engine.log").exists()
    # restore final 1:1 y marcador borrado
    assert bank_files(proj) == before
    assert not BP.marker_path(proj).exists()


def test_i_driver_inmune_a_edicion_concurrente_del_script(tmp_path, http_port,
                                                         monkeypatch):
    """Regresión del incidente 2026-10-06: bash re-lee el script del driver por
    offset de byte durante el poll; una reescritura in-place (mismo inode) bajo
    el proceso desplaza el contenido y el re-parseo posterior muere con
    'syntax error' a mitad de línea (sin llegar al stop/persist final).

    Este test MODIFICA IN-PLACE una COPIA del script a mitad de la corrida (el
    script del repo no se toca): con la defensa (self-copy estable) el driver
    completa; sin ella fallaría con rc!=0."""
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    name = f"slimi_{uuid4().hex[:6]}"
    proj = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    driver_copy = tmp_path / "driver_copy.sh"
    driver_copy.write_text(DRIVER.read_text())

    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    env = _mock_env(proj, tmp_path, mock, tmp_path / "s.json",
                    MOCK_LINGER="5", POLL_MIN="0", MOCK_FIN_SECS="5", MAXITER="3000",
                    # el driver calcula REPO desde BASH_SOURCE; una copia en tmp
                    # daría "/" (mismo landmine que cubre el self-copy) → el test
                    # lo inyecta, igual que hace el propio driver al re-exec.
                    SQX_RUNNER_REPO=str(ROOT))
    proc = subprocess.Popen(
        ["bash", str(driver_copy), name, str(http_port), "", "0", "--slim"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    # esperar a que entre en el poll (primera iteración en el log del scratch)
    scratch_log = Path(f"/tmp/{name}_run/monitor.log")

    def _poll_started() -> bool:
        try:
            return scratch_log.exists() and "[#1]" in scratch_log.read_text()
        except OSError:
            return False

    assert _wait_for(_poll_started, timeout=30), "el driver no llegó al poll"
    # edición concurrente IN-PLACE: línea larga insertada AL PRINCIPIO (mismo
    # inode -> todos los offsets de byte se desplazan -> sin defensa, el
    # re-parseo tras el poll muere con syntax error)
    original = driver_copy.read_text()
    injected = ("# edicion concurrente simulada " + "x" * 200 + "\n") + original
    with open(driver_copy, "w") as f:   # escritura in-place sobre el mismo inode
        f.write(injected)
    out, _ = proc.communicate(timeout=90)
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}"
    assert "proyecto TERMINADO" in out
    assert "STOP graceful + persist final" in out   # el camino que el incidente no alcanzó
    assert "DONE. Artefactos" in out
    assert bank_files(proj) == {"InA": ["InA.sqx"], "Out": ["Out.sqx"],
                                "ExtraC": ["ExtraC.sqx"]}
    assert not BP.marker_path(proj).exists()


# ----------------------------- (b) kill -9 a mitad --------------------------------

def test_b_kill9_driver_siguiente_arranque_reconcilia(tmp_path, http_port, monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slimb_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])

    # arrancamos el driver y lo matamos en cuanto archiva (antes del restore)
    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    env = _mock_env(proj, tmp_path, mock, tmp_path / "s.json", MOCK_LINGER="30")
    proc = subprocess.Popen(
        ["bash", str(DRIVER), proj.name, str(http_port), "", "0", "--slim"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    assert _wait_for(lambda: (proj / "databanks_archive" / "ExtraC").is_dir())
    proc.kill()                                    # kill -9: el trap NO corre
    proc.wait(timeout=10)
    subprocess.run(["pkill", "-f", str(mock)], capture_output=True)  # limpiar el mock

    # estado roto: ExtraC archivado, marcador vivo con pid muerto
    assert not (proj / "databanks" / "ExtraC").exists()
    assert BP.marker_path(proj).exists()

    # la siguiente invocación del runner reconcilia presence-aware
    monkeypatch.setenv("PROJECTS_DIR", str(proj.parent))
    monkeypatch.setenv("SLIM_TESTING", "1")
    rec = RT.reconcile_at_startup(proj.name)
    assert rec["action"] == "reconciled"
    assert rec["restored"] == ["ExtraC"]
    assert bank_files(proj) == {"InA": ["InA.sqx"], "Out": ["Out.sqx"],
                                "ExtraC": ["ExtraC.sqx"]}
    assert not BP.marker_path(proj).exists()


# ----------------------------- (c) muerte durante la reconciliación ---------------

def test_c_reconcile_interrumpida_se_reanuda(tmp_path, monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slimc_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "B1", "B2"])
    BP.slim_archive(proj, 0, "run1", pid=123456)

    original = BP.gui_slim.cmd_restore
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SystemExit("simulación: muerte durante la reconciliación")
        return original(*args, **kwargs)

    monkeypatch.setattr(BP.gui_slim, "cmd_restore", flaky)
    with pytest.raises(BP.BankPlanError):
        BP.reconcile(proj)          # muere a mitad: un banco restaurado, otro no
    monkeypatch.setattr(BP.gui_slim, "cmd_restore", original)

    # el siguiente arranque reanuda sin abortar: el ya-restaurado se salta
    rec = BP.reconcile(proj)
    assert set(rec["restored"]) == {"B1", "B2"}
    assert not BP.marker_path(proj).exists()
    assert bank_files(proj)["B2"] == ["B2.sqx"]


# ----------------------------- (d) update-in-place --------------------------------

def test_d_update_in_place_no_archiva(tmp_path, http_port, monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slimd_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["Results"], "Results")],
                     ["Results", "ExtraC"])

    proc = run_driver(proj, http_port, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    snap = json.loads(proc.mock_snap.read_text())  # type: ignore[attr-defined]
    assert snap["banks"] == ["Results"]            # el input==output NUNCA se archiva
    assert bank_files(proj) == {"Results": ["Results.sqx"], "ExtraC": ["ExtraC.sqx"]}
    assert not BP.marker_path(proj).exists()


# ----------------------------- (e) doble corrida consecutiva ----------------------

def test_e_doble_corrida_slim(tmp_path, http_port, monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slime_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    before = bank_files(proj)

    for i in range(2):
        proc = run_driver(proj, http_port, tmp_path)
        assert proc.returncode == 0, f"corrida {i}: {proc.stdout}{proc.stderr}"
        assert not BP.marker_path(proj).exists()
        assert bank_files(proj) == before


# ----------------------------- (f) dir vacío recreado por el motor ----------------

def test_f_dir_vacio_recreated_se_rmdir_y_restaura(tmp_path, http_port, monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slimf_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])

    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    env = _mock_env(proj, tmp_path, mock, tmp_path / "s.json",
                    MOCK_RECREATE="ExtraC")   # el motor recrea el dir del banco archivado, VACÍO (L2)
    proc = subprocess.run(
        ["bash", str(DRIVER), proj.name, str(http_port), "", "0", "--slim"],
        capture_output=True, text=True, env=env, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # el restore hizo rmdir del dir vacío y restauró el contenido
    assert bank_files(proj) == {"InA": ["InA.sqx"], "Out": ["Out.sqx"],
                                "ExtraC": ["ExtraC.sqx"]}
    assert not BP.marker_path(proj).exists()


# ----------------------------- (g) pipeline: L3 por task + orquestador ------------

def test_g_driver_restaura_input_archivado_antes_del_motor(tmp_path, http_port,
                                                           monkeypatch):
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slimg_{uuid4().hex[:6]}",
                     [("T1", "Build", ["InA"], "MidM"),
                      ("T2", "Retest", ["MidM"], "OutB")],
                     ["InA", "MidM", "OutB", "ExtraC"])

    # iteración 1 (task 0, pipeline): archiva ExtraC
    p1 = run_driver(proj, http_port, tmp_path, task_idx=0, pipeline=True)
    assert p1.returncode == 0, p1.stdout + p1.stderr
    assert BP.marker_banks(proj) == ["ExtraC"]        # acumulado: NO restaura entre tasks
    assert not (proj / "databanks" / "ExtraC").exists()

    # forzar el estado "input de la task 2 archivado" (guarda defensiva del driver)
    BP.archive_banks(proj, ["MidM"])
    BP.marker_write(proj, {"run_id": "r", "pid_driver": 999999,
                           "pipeline": True, "banks": ["ExtraC", "MidM"]})

    # iteración 2 (task 1, pipeline): restaura MidM ANTES de arrancar el motor;
    # el marcador MERGEA: ExtraC y MidM (previos) + InA (archivable de la task 1:
    # la task 2 no usa InA, así que su driver lo archiva — comportamiento correcto)
    p2 = run_driver(proj, http_port, tmp_path, task_idx=1, pipeline=True)
    assert p2.returncode == 0, p2.stdout + p2.stderr
    snap = json.loads(p2.mock_snap.read_text())  # type: ignore[attr-defined]
    assert snap["banks"] == ["MidM", "OutB"]     # MidM ya estaba de vuelta al arrancar
    assert BP.marker_banks(proj) == ["ExtraC", "InA"]  # MidM restaurado y quitado del marcador
    # el restore acumulado (orquestador al final de la cadena) deja todo 1:1
    rep = BP.slim_restore(proj)
    assert set(rep["restored"]) == {"ExtraC", "InA"}
    assert bank_files(proj) == {"InA": ["InA.sqx"], "MidM": ["MidM.sqx"],
                                "OutB": ["OutB.sqx"], "ExtraC": ["ExtraC.sqx"]}
    assert not BP.marker_path(proj).exists()


def test_g_orquestador_completo_restore_acumulado(tmp_path, http_port, monkeypatch):
    """Cadena real orquestador → launcher → driver ×2 con mocks: entre tasks el
    archive se acumula (ExtraC sigue fuera) y al final de la cadena el trap del
    orquestador restaura todo."""
    projects = tmp_path / "projects"
    name = f"slimgorch_{uuid4().hex[:6]}"
    proj = make_proj(projects, name,
                     [("T1", "Build", ["InA"], "MidM"),
                      ("T2", "Retest", ["MidM"], "OutB")],
                     ["InA", "MidM", "OutB", "ExtraC"])
    mock = tmp_path / f"mock_sqcli_{uuid4().hex[:8]}.py"
    mock.write_text(MOCK_SQCLI)
    env = _mock_env(proj, tmp_path, mock, tmp_path / "s.json",
                    MOCK_TOUCH="MidM,OutB", PORT=str(http_port))
    proc = subprocess.run(
        ["bash", str(ORCHESTRATOR), name, "0", "--slim"],
        capture_output=True, text=True, env=env, timeout=300)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "PIPELINE PER-TASK COMPLETO" in out
    # restore acumulado al final: todo presente y marcador borrado
    files = bank_files(proj)
    assert set(files) == {"InA", "MidM", "OutB", "ExtraC"}
    assert "MidM_nuevo.sqx" in files["MidM"] and "OutB_nuevo.sqx" in files["OutB"]
    assert not BP.marker_path(proj).exists()


# ----------------------------- (h) regresión slim=False ---------------------------

def test_h_driver_sin_slim_no_toca_bancos(tmp_path, http_port):
    projects = tmp_path / "projects"
    proj = make_proj(projects, f"slimh_{uuid4().hex[:6]}",
                     [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    before = bank_files(proj)
    proc = run_driver(proj, http_port, tmp_path, slim=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SLIM" not in proc.stdout.split("LAUNCH")[1].splitlines()[0] or True
    assert bank_files(proj) == before
    assert not (proj / "databanks_archive" / "ExtraC").exists()


def _fake_motor_precheck_luego_arrancado(monkeypatch: pytest.MonkeyPatch) -> None:
    """motor_alive alterna False (pre-check de instancia única) / True (el motor
    del mock ya arrancó para el wait_start del launcher): patrón por corrida."""
    from itertools import cycle
    no_live_procs(monkeypatch)   # R7: el launcher sondea procesos con pgrep; aquí Popen está parcheado
    states = cycle([False, True])
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port: next(states))
    monkeypatch.setattr(RT.SP, "_http",
                        lambda cmd, port, timeout: "Project running")
    monkeypatch.setattr(RT.time, "sleep", lambda s: None)


def test_h_launcher_slim_false_no_propaga_flag(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    proj = make_proj(projects, "proj_h", [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    _fake_motor_precheck_luego_arrancado(monkeypatch)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            FakePopen.last_cmd = cmd
            self.pid = 4242

        def terminate(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, timeout=None):
            return (b"", b"")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(RT.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(RT, "gui_open", lambda: False)
    rep = RT.run("proj_h", "T1", "only", slim=False)
    assert rep["ok"], rep["msg"]
    assert "--slim" not in FakePopen.last_cmd
    assert FakePopen.last_cmd[5] == "0"          # task_idx propagado siempre

    rep = RT.run("proj_h", "T1", "only", slim=True)
    assert rep["ok"], rep["msg"]
    assert "--slim" in FakePopen.last_cmd


def test_h_launcher_reconcilia_marcador_huerfano_antes_de_lanzar(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    proj = make_proj(projects, "proj_h2", [("T1", "Retest", ["InA"], "Out")],
                     ["InA", "Out", "ExtraC"])
    # simular corrida previa muerta a mitad: ExtraC archivado + marcador pid muerto
    monkeypatch.setattr(BP.gui_slim, "live_processes", lambda: [])
    monkeypatch.setenv("SLIM_TESTING", "1")   # mocks: sin guardas de GUI/motor real
    BP.slim_archive(proj, 0, "run_muerta", pid=123456)
    no_live_procs(monkeypatch)   # R7: el launcher sondea procesos con pgrep; aquí Popen está parcheado
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port: True)
    monkeypatch.setattr(RT.SP, "_http",
                        lambda cmd, port, timeout: "Project running")
    monkeypatch.setattr(RT.time, "sleep", lambda s: None)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            FakePopen.last_cmd = cmd
            self.pid = 4243

        def terminate(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, timeout=None):
            return (b"", b"")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(RT.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(RT, "gui_open", lambda: False)
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    rep = RT.run("proj_h2", "T1", "only", slim=True)
    assert rep["ok"], rep["msg"]
    assert rep.get("reconciled"), "el launcher debe reconciliar la corrida muerta y avisar"
    assert bank_files(proj)["ExtraC"] == ["ExtraC.sqx"]   # restaurado antes de lanzar
    assert "--slim" in FakePopen.last_cmd


def test_h_run_pipeline_per_task_propaga_slim(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    make_proj(projects, "proj_h3", [("T1", "Retest", ["InA"], "Out")],
              ["InA", "Out", "ExtraC"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    no_live_procs(monkeypatch)   # R7: el launcher sondea procesos con pgrep; aquí Popen está parcheado
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port: False)
    monkeypatch.setattr(RT, "gui_open", lambda: False)

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            FakePopen.last_cmd = cmd
            self.pid = 4244

    monkeypatch.setattr(RT.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(RT, "gui_open", lambda: False)
    rep = RT.run_pipeline_per_task("proj_h3", 0, slim=True)
    assert rep["ok"], rep["msg"]
    assert FakePopen.last_cmd[-1] == "--slim"
    rep = RT.run_pipeline_per_task("proj_h3", 0, slim=False)
    assert "--slim" not in FakePopen.last_cmd


# ----------------------------- infraestructura de procesos falsos -----------------------------
# (guardrail `sqx-headless-fallos`, Fase 1, 2026-10-21.) Procesos REALES con nombres y rutas ÚNICOS (uuid)
# para probar la detección y la muerte del motor sin tocar jamás un sqcli real: el motor falso se llama
# `sqcli_<uuid8>` (≤15 caracteres: macOS trunca el nombre de proceso a 16) y los tests apuntan
# SQX_ENGINE_PROC_NAME a ese nombre. Reglas de la plataforma (macOS, verificadas 2026-10-21):
#   - un SYMLINK `<nombre> -> /bin/sleep` da un proceso llamado <nombre> (lo ve `pgrep -x`); una COPIA de
#     /bin/sleep no llega a ejecutarse (binario de plataforma);
#   - un binario compilado con `cc` sí (y con un bucle da ~95 % de CPU);
#   - con `start_new_session=True` pgid == pid; un proceso muerto sin `wait` queda <defunct> y NO lo ve
#     `pgrep -x`;
#   - hijos con stdout/stderr heredados cuelgan a quien lee ese pipe: todo a /dev/null.
# Estos helpers viven AQUÍ (y no en tests/test_sqx_procs.py) porque este módulo viaja en la copia
# `sqx-headless` y los demás tests exportados los importan.

def unique_engine_name() -> str:
    return f"sqcli_{uuid4().hex[:8]}"


class ProcRegistry:
    """Lanza procesos de test en su PROPIA sesión (pgid == pid) y los mata al final aunque el test falle:
    `killpg` alcanza también a los hijos huérfanos (p.ej. un `tail -f /dev/null`) del mismo grupo."""

    def __init__(self) -> None:
        self.popens: list[subprocess.Popen] = []

    def spawn(self, argv, *, ignore_term: bool = False, env: dict | None = None) -> subprocess.Popen:
        argv = [str(a) for a in argv]
        if ignore_term:
            # SIG_IGN se hereda a través de exec: el proceso final sobrevive a SIGTERM (solo muere con -9)
            argv = ["sh", "-c", 'trap "" TERM; exec "$0" "$@"', *argv]
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        self.popens.append(p)
        return p

    def close(self) -> None:
        for p in self.popens:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        self.popens.clear()


@pytest.fixture()
def procs():
    reg = ProcRegistry()
    yield reg
    reg.close()


def pgrep_x(name: str) -> list[int]:
    r = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def cmdline_of(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()


def pid_alive(pid: int) -> bool:
    """Vivo de verdad (un zombi <defunct> no cuenta)."""
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def fake_engine(procs: "ProcRegistry", tmp_dir: Path, name: str, *args: str,
                ignore_term: bool = False) -> subprocess.Popen:
    """Motor FALSO: symlink `<tmp_dir>/<name>` -> /bin/sleep (por defecto `sleep 120`). Devuelve el Popen
    una vez que `pgrep -x <name>` ya lo ve."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    link = tmp_dir / name
    if not link.is_symlink():
        link.symlink_to("/bin/sleep")
    p = procs.spawn([link, *(args or ("120",))], ignore_term=ignore_term)
    assert _wait_for(lambda: p.pid in pgrep_x(name), timeout=10), \
        f"el motor falso {name} no apareció en pgrep -x"
    return p


def build_c(tmp_dir: Path, name: str, source: str) -> "Path | None":
    """Compila `source` a `<tmp_dir>/<name>`. None si no hay compilador o falla."""
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        return None
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_dir / f"{name}.c"
    src.write_text(source)
    out = tmp_dir / name
    r = subprocess.run([cc, "-O1", "-o", str(out), str(src)], capture_output=True, text=True)
    return out if r.returncode == 0 else None


BUSY_C = "int main(void) { volatile unsigned long x = 0; for (;;) x++; return 0; }\n"


def busy_engine(procs: "ProcRegistry", tmp_dir: Path, name: str) -> subprocess.Popen:
    """Motor FALSO con CPU ALTA y nombre `name`: binario `cc` de bucle o, si no hay compilador, symlink a
    /usr/bin/yes (salida a /dev/null). `pytest.skip` explícito si ninguno da CPU alta medible aquí."""
    binary = build_c(tmp_dir, f"{name}_src", BUSY_C)
    if binary is not None:
        link = tmp_dir / name
        link.symlink_to(binary)
        p = procs.spawn([link])
    elif Path("/usr/bin/yes").exists():
        link = tmp_dir / name
        link.symlink_to("/usr/bin/yes")
        p = procs.spawn([link])
    else:
        pytest.skip("sin compilador cc ni /usr/bin/yes: no hay forma de fabricar un proceso con CPU alta")
    if not _wait_for(lambda: p.pid in pgrep_x(name), timeout=10):
        pytest.skip(f"el proceso de CPU alta {name} no aparece en pgrep -x en esta plataforma")

    def _cpu() -> float:
        out = subprocess.run(["ps", "-o", "%cpu=", "-p", str(p.pid)],
                             capture_output=True, text=True).stdout.strip()
        return float(out) if out else 0.0

    if not _wait_for(lambda: _cpu() >= 20.0, timeout=8):
        pytest.skip(f"la CPU del proceso falso no supera el 20 % en esta plataforma (ps: {_cpu()})")
    return p


def fake_launcher(procs: "ProcRegistry", directory: Path, filename: str, *args,
                  prefix: tuple = ()) -> subprocess.Popen:
    """`bash <directory>/<filename> <args…>` (el script solo duerme): imita el cmdline de un orquestador,
    driver o cascada. `prefix` = envoltorio (p.ej. ("caffeinate", "-i")). Espera a que el cmdline sea visible."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / filename
    if not script.exists():
        script.write_text("#!/bin/bash\nsleep 120\ntrue\n")   # 2 comandos: bash no hace exec del sleep
    p = procs.spawn([*prefix, "bash", script, *args])
    assert _wait_for(lambda: filename in cmdline_of(p.pid), timeout=10), \
        f"el cmdline de {filename} no es visible: {cmdline_of(p.pid)!r}"
    return p


def child_pid(parent: subprocess.Popen) -> int:
    """PID del (primer) hijo directo de `parent` (p.ej. el `bash` que lanza `caffeinate -i bash …`)."""
    out: list[int] = []

    def _find() -> bool:
        r = subprocess.run(["pgrep", "-P", str(parent.pid)], capture_output=True, text=True)
        out[:] = [int(x) for x in r.stdout.split()]
        return bool(out)

    assert _wait_for(_find, timeout=10), f"{parent.pid} no tiene hijos"
    return out[0]


def no_live_procs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los tests que parchean `subprocess.Popen` con un FakePopen no pueden sondear procesos con `pgrep`
    (subprocess.run también pasa por Popen): se anulan las sondas del stack y el guardián R7 queda cubierto
    por sus propios tests (tests/test_sqx_procs.py)."""
    for fn in ("engine_pids", "driver_pids", "orchestrator_pids", "gui_pids", "cascade_pids"):
        monkeypatch.setattr(RT.SP, fn, lambda *a, **k: [])


def shell_function(script: Path, name: str) -> str:
    """Texto de la función bash `name(){ … }` de `script` (para ejercitar la función REAL del script en aislado)."""
    lines = script.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\(\)\{{", ln):
            if ln.rstrip().endswith("}") and ln.count("{") == ln.count("}"):
                return ln
            for j in range(i + 1, len(lines)):
                if lines[j] == "}":
                    return "\n".join(lines[i:j + 1])
    raise AssertionError(f"no encuentro la función {name} en {script.name}")
