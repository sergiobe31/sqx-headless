#!/usr/bin/env python3
"""Stop + persistencia de TODOS los databanks + liberar el motor sqcli.

Determinismo honesto (debatido con deepseek-v4-pro):
  - DETERMINISTA para databanks SIN espacios (synctofiles por HTTP funciona).
  - BEST-EFFORT para databanks CON espacios (`Second OOS`, `Real OOS`, `MC - *`): el
    %20 HTTP trunca el nombre (§6.2) y la instancia única impide lanzar otro `sqcli -run`
    paralelo. Esos databanks se fían al auto-sync del motor. El reporte SIEMPRE mide el
    count real en disco, así ves si faltan. NOTA: esos nombres son CANÓNICOS del pipeline
    de Retest (los task-XML los referencian) → renombrarlos rompe referencias; el fix
    estructural es que el driver persista los con espacios vía `sqcli -run` antes del kill.

MODO PIPELINE (guardrail `sqx-headless-fallos`, Fase 2, R6): si hay orquestador o driver del proyecto
vivos, parar el motor "a pelo" haría que el orquestador diese la task por buena y lanzase la siguiente
(O3). En vez de eso esta primitiva DELEGA: escribe la bandera `<proyecto>/.pipeline_stop_request` (la
leen el orquestador antes de lanzar cada task y tras `wait_driver`, y el lanzador antes de tocar el
.cfx y justo antes del `Popen`), entrega UN `-project action=stop` por driver vivo (solo cuando el status
dice que el proyecto corre) y espera a que el pipeline salga SIN persistir ella misma: persiste el
driver (confirmando el sync por el stdout del motor) y el orquestador sale con rc 3 ("parada por el
usuario"). Reporte: `pipeline_stopped` / `pipeline_stopping` (tope `wait_secs` agotado: la bandera
garantiza que no se lanza nada más) / `stopped_by` ("pipeline" | "direct") / `safe_for_gui` + `reasons`.

Proceso, CAMINO DIRECTO (sin driver ni orquestador vivos; si uno aparece justo antes de actuar → modo
pipeline, A6):
  0. (ver MODO PIPELINE arriba)
  1. detectar motor sqcli (HTTP :5050); si no responde pero HAY proceso → mirar su CPU:
     <5 % = ZOMBI (colgado) → matarlo; ≥5 % = SATURADO trabajando → NO matarlo (reintenta o
     `--force-kill`). Sin proceso → "nada que parar".
  2. proyecto = arg
  3. -project action=stop (graceful) + poll status hasta "not running" (max 120s).
     Si no confirma: WARNING + persistencia bajo riesgo (build podría seguir corriendo).
  3b. BARRERA DE CARGA: el motor responde HTTP mientras carga databanks en segundo plano (lazy);
     un `synctofiles` sobre un banco a medio cargar BORRA del disco lo que el motor aún no tiene en
     RAM. Se espera a que "In databank" del status se estabilice (3 lecturas × 5 s, tope 300 s). Si
     el tope se agota → NI sincroniza NI mata (con `--force-kill` solo se salta la compuerta del
     kill; nunca se sincroniza a ciegas).
  4. persistir TODOS los databanks del config.xml del proyecto (sin asumir 'Results'), con timeout
     largo (`SQ_LONG_SECS`, 1800 s: un sync de bancos gordos tarda minutos y cortarlo trunca el banco).
  5. matar sqcli + SOLO los `tail -f /dev/null` de su grupo de procesos (TERM, luego -9 si
     resiste). Si un `synctofiles` o un `count` FALLÓ → NO se mata (salvo `--force-kill`): matar con
     un banco sin guardar lo pierde (R1).
  6. reportar {proj, stopped, databanks: {name: n_sqx_disco}, motor_killed, warnings, safe_for_gui,
     reasons}. "Ya puedes abrir la GUI" SOLO con `safe_for_gui` (= `pre_gui_check`, R9).

Por qué existe: parar un build y liberar el motor era folklore disperso (qué databank
recibe las estrategias — Results_TF/Results_MR, no 'Results'; synctofiles no 'save';
espacios; zombi). Esta primitiva lo fija. Ver docs/sqx_cli_commands.md §6.1/§6.2/§9.2.

Uso (CLI):       python sqx/stop_and_persist.py <proj> [port=5050] [--no-kill] [--force-kill] [--wait-secs N]
Importable:      from stop_and_persist import run; run("dax40_4h")

Landmines cubiertos: §6.1 (persist ANTES de matar — las estrategias están sólo en RAM),
§6.2 (encoding %20, '=' literal), §9.2 (stop async → poll hasta not-running).
ORDEN (validado): stop→persist→kill. Stop ANTES de persistir estabiliza el databank en
RAM (evita snapshots parciales del genético activo); persistir ANTES de matar no pierde
estrategias (§6.1). Evidencia: el stop preserva el databank en RAM (1500 en Results_TF
tras stop, sesión 2026-07-14).

PRIMITIVAS DE PROCESOS (guardrail `sqx-headless-fallos`, Fase 1, 2026-10-21): este módulo es la
fuente ÚNICA de "¿hay motor / orquestador / driver / cascada / GUI vivos?" (`engine_pids`,
`orchestrator_pids`, `driver_pids`, `cascade_pids`, `gui_pids`, `pre_gui_check`) y de "matar el
motor" (`kill_engine`). El motor se detecta SOLO por nombre exacto de proceso (`pgrep -x sqcli`,
override `SQX_ENGINE_PROC_NAME` para tests): un patrón de cmdline casaba con cualquier shell que
contuviera el texto y la barrera del orquestador le hacía `kill -9` (incidente 2026-10-13). Los .sh
repiten la misma lógica en shell (corren de /tmp y no pueden importar esto).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402  PROJECTS

try:  # ruta de instalación SQX: única fuente en sqx/paths.py
    from sqx.paths import SQCLI as _SQCLI
except ImportError:  # ejecutado como script (sys.path[0] = sqx/)
    from paths import SQCLI as _SQCLI

SQCLI = str(_SQCLI)
HTTP_TIMEOUT = 30


# ----------------------------- primitivas de procesos (R5) ----------------------

#: Nombre EXACTO del proceso del motor (`pgrep -x`). Override SOLO para tests (nombre único por test:
#: en la máquina puede haber un motor real que un test nunca debe ver ni tocar).
ENGINE_PROC_DEFAULT = "sqcli"
#: GUI de SQX: electron StrategyQuantX_ui / lanzador StrategyQuantX_nocheck / binario del bundle de
#: CUALQUIER versión instalada. Es un patrón de cmdline (la GUI no tiene un nombre de proceso estable);
#: NO incluye al motor, que se detecta aparte por nombre exacto.
GUI_PATTERN = ("StrategyQuantX_ui|StrategyQuantX_nocheck"
               "|StrategyQuantX[^/]*[.]app/Contents/MacOS")
#: Un motor con HTTP ciego y CPU media por debajo de esto (%) está colgado (zombi): se puede matar.
#: Por encima está saturado TRABAJANDO (landmine fitab 2026-09-11): no se mata salvo `--force-kill`.
CPU_BUSY_PCT = 5.0
CPU_SAMPLES = 3
CPU_INTERVAL = 2.0
#: Gracia entre SIGTERM y SIGKILL de `kill_engine` (SIGTERM no basta con zombis — landmine fitab).
KILL_GRACE_SECS = 10.0
#: Barrera de carga lazy del camino directo (mismos knobs que el driver: CARGA_SLEEP, STABLE_N).
LOAD_SLEEP_SECS = 5.0
LOAD_STABLE_N = 3
LOAD_WAIT_SECS = 300.0
#: Timeout largo (s) de `synctofiles` por HTTP (R1): el MISMO knob que el driver (`SQ_LONG_SECS`, 1800).
#: Con 30 s un sync de bancos gordos se cortaba a mitad; el motor lo seguía y el kill posterior lo truncaba.
SQ_LONG_SECS = 1800
#: Modo pipeline (R6): tope de espera a que el pipeline salga, cadencia del bucle y espera a ver aceptado un
#: `stop` (el motor tarda en dejar de "running").
PIPELINE_STOP_WAIT = 1800.0
STOP_POLL_SECS = 2.0
STOP_ACCEPT_SECS = 10.0
#: Bandera de parada del pipeline: `projects_dir()/<proj>/.pipeline_stop_request`.
STOP_FLAG_NAME = ".pipeline_stop_request"
#: Recursión máxima modo pipeline ↔ camino directo (A6 puede encadenarlos; nunca debería pasar de 2).
_MAX_DEPTH = 3


class ProcProbeError(RuntimeError):
    """`pgrep` no se pudo ejecutar o rechazó el patrón: no sabemos si hay procesos (fail-closed)."""


def projects_dir() -> Path:
    """Raíz de proyectos SQX: env PROJECTS_DIR si existe (la inyectan los drivers bash y los tests con
    fixtures en tmp); si no, PROJECTS real del registro. Misma semántica que el `PROJECTS_DIR` de
    `sqx/paths.sh` (vacío = no definido). Toda ruta de proyecto del stack pasa por aquí."""
    return Path(os.environ.get("PROJECTS_DIR") or str(REG.PROJECTS))


def stop_flag_path(proj: str) -> Path:
    """Bandera de parada del pipeline de `proj` (R6): su mera EXISTENCIA es la orden. La escribe
    `run` en modo pipeline; la leen el orquestador, el lanzador (`run_task_headless`) y el driver."""
    return projects_dir() / proj / STOP_FLAG_NAME


def stop_requested(proj: str) -> bool:
    return stop_flag_path(proj).exists()


def write_stop_flag(proj: str, origin: str = "stop_and_persist") -> Path | None:
    """Escribe la bandera (atómica: tmp + replace en el mismo dir). None si no se pudo (p. ej. el proyecto
    no existe: NO se crea el directorio, aparecería como proyecto vacío en la GUI de SQX)."""
    path = stop_flag_path(proj)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(f"ts={time.strftime('%F %T')}\norigin={origin} pid={os.getpid()}\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return path


def clear_stop_flag(proj: str) -> bool:
    """Borra la bandera. True si existía."""
    try:
        stop_flag_path(proj).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _pgrep(*args: str) -> list[int]:
    try:
        r = subprocess.run(["pgrep", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProcProbeError(f"pgrep {' '.join(args)}: {e}") from e
    if r.returncode == 0:
        return [int(x) for x in r.stdout.split() if x.isdigit()]
    if r.returncode == 1:  # sin coincidencias
        return []
    raise ProcProbeError(f"pgrep {' '.join(args)} rc={r.returncode}: {r.stderr.strip()}")


def _ps_field(pid: int, field: str) -> str:
    try:
        r = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip()


def _pid_alive(pid: int) -> bool:
    """Vivo de verdad: un zombi (defunct, hijo muerto sin `wait`) existe para `os.kill(pid, 0)` pero no
    cuenta; `pgrep -x` tampoco lo ve."""
    st = _ps_field(pid, "stat")
    return bool(st) and not st.startswith("Z")


def engine_proc_name() -> str:
    return os.environ.get("SQX_ENGINE_PROC_NAME") or ENGINE_PROC_DEFAULT


def engine_pids() -> list[int]:
    """PIDs de los motores vivos: `pgrep -x sqcli` (nombre EXACTO, nunca texto de cmdline)."""
    return _pgrep("-x", engine_proc_name())


def gui_pids() -> list[int]:
    return _pgrep("-f", GUI_PATTERN)


def engine_cpu(pid: int, samples: int | None = None, interval: float | None = None) -> float:
    """%CPU medio del proceso en `samples` lecturas de `ps -o %cpu=` separadas `interval` s.
    0.0 si el proceso no existe (ninguna lectura válida)."""
    n = CPU_SAMPLES if samples is None else samples
    dt = CPU_INTERVAL if interval is None else interval
    vals: list[float] = []
    for i in range(max(1, n)):
        if i:
            time.sleep(dt)
        try:
            vals.append(float(_ps_field(pid, "%cpu").replace(",", ".")))
        except ValueError:
            pass
    return sum(vals) / len(vals) if vals else 0.0


def engine_owner_pgid(pid: int) -> int:
    """Grupo de procesos del motor. El driver nace con `start_new_session=True` → su pgid = su PID y el
    `tail -f /dev/null | sqcli` hereda ese grupo: el pgid de un motor identifica a SU driver (0 = no sé)."""
    s = _ps_field(pid, "pgid")
    return int(s) if s.isdigit() else 0


def _ere_escape(text: str) -> str:
    """Escapa solo los metacaracteres de una ERE POSIX (`\\-`/`\\_` no están definidos en POSIX)."""
    return re.sub(r"([\\^$.|?*+()\[\]{}])", r"\\\1", text)


def _launcher_re(script_re: str, copy_tag: str, proj: str | None) -> str:
    """Regex ERE (`pgrep -f`) ANCLADA al inicio del cmdline de un `bash` que ejecuta `script_re` o su
    auto-copia estable `<proj>_<copy_tag>_XXXXXX` (mktemp: 6 alfanuméricos) con `proj` de 1.er argumento.

    Anclar al principio evita los falsos positivos del patrón ancho: `tail -f /tmp/<p>_pertask_<epoch>.log`,
    un `caffeinate -i bash …` (casa el `bash` hijo, no el caffeinate), otro proyecto de prefijo común
    (`p` vs `p2`) o un shell de vigilancia que solo contenga el texto. `proj=None` = cualquier proyecto.
    Limitación asumida: el script original con espacios en su ruta no casa (la auto-copia vive en /tmp y
    sí, así que solo se pierden los milisegundos previos al `exec`)."""
    if proj is None:
        name, arg = f"[^ /]+_{copy_tag}_[A-Za-z0-9]{{6}}", "[^ ]+"
    else:
        p = _ere_escape(proj)
        name, arg = f"{p}_{copy_tag}_[A-Za-z0-9]{{6}}", p
    return f"^[^ ]*bash ([^ ]*/)?({script_re}|{name}) {arg}( |$)"


def orchestrator_re(proj: str | None = None) -> str:
    return _launcher_re(r"run_pipeline_per_task\.sh", "pertask", proj)


def driver_re(proj: str | None = None) -> str:
    return _launcher_re(r"run_pipeline_headless\.sh", "driver", proj)


CASCADE_RE = r"^[^ ]*bash ([^ ]*/)?run_cascade_projects\.sh( |$)"


def orchestrator_pids(proj: str | None = None) -> list[int]:
    """Orquestadores `run_pipeline_per_task.sh` vivos (también la auto-copia /tmp/<p>_pertask_XXXXXX).
    Incluye subshells del propio orquestador (mismo cmdline): sirve para "¿hay alguno?"."""
    return _pgrep("-f", orchestrator_re(proj))


def driver_pids(proj: str | None = None) -> list[int]:
    """Drivers `run_pipeline_headless.sh` vivos (también la auto-copia /tmp/<p>_driver_XXXXXX y el
    subshell del vigilante, que comparte cmdline con el driver)."""
    return _pgrep("-f", driver_re(proj))


def cascade_pids() -> list[int]:
    return _pgrep("-f", CASCADE_RE)


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_tails(pgid: int) -> list[int]:
    """`tail -f /dev/null` del grupo `pgid` (la mitad del `tail | sqcli` del driver). NUNCA globales: otros
    `tail -f /dev/null` de la máquina son de otras corridas."""
    if pgid <= 1 or pgid == os.getpgrp():
        return []
    try:
        return _pgrep("-g", str(pgid), "-f", "^tail -f /dev/null$")
    except ProcProbeError:
        return []


def kill_engine(pids: list[int] | None = None, grace: float | None = None) -> bool:
    """Mata los motores `pids` (None = todos los `engine_pids()`): TERM → gracia → `kill -9`, y los
    `tail -f /dev/null` de SU MISMO grupo (R2). Devuelve True si ninguno de los PIDs objetivo sigue vivo.

    La gracia cuenta iteraciones de 0,2 s (no reloj de pared): con `time.sleep` parcheado en un test no
    se queda girando."""
    targets = [int(p) for p in (engine_pids() if pids is None else pids)]
    if not targets:
        return True
    g = KILL_GRACE_SECS if grace is None else grace
    pgids = {engine_owner_pgid(p) for p in targets} - {0}
    for p in targets:
        _signal(p, signal.SIGTERM)
    for _ in range(max(1, int(g / 0.2))):
        if not any(_pid_alive(p) for p in targets):
            break
        time.sleep(0.2)
    for p in [p for p in targets if _pid_alive(p)]:
        _signal(p, signal.SIGKILL)
    for pg in pgids:
        for t in _group_tails(pg):
            _signal(t, signal.SIGTERM)
    for _ in range(25):  # hasta ~5 s a que el SIGKILL haga efecto
        if not any(_pid_alive(p) for p in targets):
            return True
        time.sleep(0.2)
    return not any(_pid_alive(p) for p in targets)


def kill_motor() -> bool:
    """Compat: mata todos los motores (nombre exacto) y los tail de su grupo. Ver `kill_engine`."""
    return kill_engine()


def _ppid(pid: int) -> int:
    s = _ps_field(pid, "ppid")
    return int(s) if s.isdigit() else 0


def _descends_from(pid: int, roots: set[int], max_depth: int = 8) -> bool:
    """¿`pid` es uno de `roots` o desciende de alguno? Los `$(…)` y pipelines de un script bash son procesos
    hijos con el MISMO cmdline que el script: casan con el mismo regex que él."""
    cur = pid
    for _ in range(max_depth):
        if cur in roots:
            return True
        cur = _ppid(cur)
        if cur <= 1:
            return False
    return cur in roots


def pre_gui_check(proj: str, ignore_pids=()) -> dict:
    """¿Es seguro abrir la GUI de SQX ahora? Comprobación de SOLO LECTURA, GLOBAL (la instancia única
    es de toda la máquina, no de un proyecto).

    Devuelve {safe, reasons, databanks}. Inseguro si: algún motor `sqcli` vivo; la GUI ya abierta;
    algún orquestador, driver o cascada vivos de CUALQUIER proyecto; marcador `--slim`
    (`databanks_archive/.runner_session.json`) o bancos aún en `databanks_archive/`; o
    `project.cfx.pre_run_task.bak` pendiente (el .cfx puede tener tasks desactivadas).

    `ignore_pids` (PLAN-DESVIO menor: parámetro nuevo, el plan no lo prevé): el LLAMADOR (p. ej. el orquestador que
    imprime su mensaje final desde su propio proceso, que si no se vería a sí mismo como "pipeline vivo"): esos PIDs
    y sus descendientes no cuentan como "pipeline vivo"."""
    reasons: list[str] = []
    ign = {int(p) for p in ignore_pids}
    try:
        probes = (("hay un sqcli vivo", engine_pids),
                  ("la GUI de SQX ya está abierta", gui_pids),
                  ("hay un orquestador per-task vivo", orchestrator_pids),
                  ("hay un driver de pipeline vivo", driver_pids),
                  ("hay una cascada de proyectos viva", cascade_pids))
        for label, probe in probes:
            pids = probe()
            if ign:
                pids = [p for p in pids if not _descends_from(p, ign)]
            if pids:
                reasons.append(f"{label} (pids {', '.join(map(str, pids))})")
    except ProcProbeError as e:
        reasons.append(f"no pude comprobar los procesos vivos ({e})")
    projdir = projects_dir() / proj
    arch = projdir / "databanks_archive"
    if (arch / ".runner_session.json").exists():
        reasons.append("hay una corrida --slim sin restaurar (marcador "
                       "databanks_archive/.runner_session.json)")
    if arch.is_dir():
        archived = sorted(d.name for d in arch.iterdir() if d.is_dir())
        if archived:
            reasons.append("hay bancos archivados en databanks_archive/: " + ", ".join(archived))
    if (projdir / "project.cfx.pre_run_task.bak").exists():
        reasons.append("queda project.cfx.pre_run_task.bak (el .cfx puede tener tasks desactivadas "
                       "en silencio)")
    return {"safe": not reasons, "reasons": reasons, "databanks": count_on_disk(proj)}


# ----------------------------- helpers HTTP (motor) -----------------------------

def _http(cmd: str, port: int = 5050, timeout: int = HTTP_TIMEOUT) -> str:
    """GET :5050/call?cmd=... con espacios→%20 y '=' literal (landmine §9.2)."""
    enc = cmd.replace(" ", "%20")
    url = f"http://localhost:{port}/call?cmd={enc}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def motor_alive(port: int = 5050) -> bool:
    """¿El motor sqcli responde (HTTP :5050)?"""
    try:
        return "available projects" in _http("-project action=list", port, 6).lower()
    except Exception:
        return False


def _status(proj: str, port: int) -> str:
    try:
        return _http(f"-project action=status name={proj}", port, 20)
    except Exception:
        return ""


_LIST_ROW_RE = re.compile(r"^\s*(.+),\s*Records:\s*(\d+)\s*$")   # greedy: nombres con comas (CR5-H2)


def _motor_counts(proj: str, banks: list[str], port: int = 5050, timeout: float = 30) -> dict:
    """{bank: n_estrategias_en_RAM} vía HTTP. Base para decidir "¿merece la pena guardar este databank?".
    None para un bank si el motor no responde.

    A10 (aceptación real 2026-10-27): en B144 `count` NO funciona con nombres con espacios (el motor corta el
    nombre en el primer espacio: "Databank 'Last' doesn't exist") y un count fallido bloquea el kill. Primero
    `-databank action=list project=<p>`, que da "<banco>, Records: N" de TODOS los bancos (con o sin espacios)
    en una llamada; `count` queda de respaldo para los que no salgan en la lista. Si la lista no responde (timeout,
    conexión), tampoco se prueba `count` banco a banco: todos None sin sumar esperas (CR5-H1; el monitor pasa 6 s).
    """
    listed: dict[str, int] = {}
    try:
        for line in _http(f"-databank action=list project={proj}", port, timeout).splitlines():
            m = _LIST_ROW_RE.match(line)
            if m:
                listed[m.group(1)] = int(m.group(2))
    except Exception:
        return {b: None for b in banks}
    out: dict[str, int | None] = {}
    for b in banks:
        if b in listed:
            out[b] = listed[b]
            continue
        try:
            text = _http(f"-databank action=count project={proj} name={b}", port, min(timeout, 10))
            m = re.search(r"Records:\s*(\d+)", text)
            out[b] = int(m.group(1)) if m else None
        except Exception:
            out[b] = None
    return out


# ----------------------------- lectura del .cfx --------------------------------

def databanks_of(proj: str) -> list[str]:
    """Lista de databanks del config.xml del proyecto (orden de aparición)."""
    cfx = projects_dir() / proj / "project.cfx"
    if not cfx.exists():
        return []
    with zipfile.ZipFile(cfx) as z:
        cfg = z.read("config.xml").decode("utf-8", "replace")
    # preserva orden + únicos
    seen: list[str] = []
    for name in re.findall(r'<Databank\s+name="([^"]+)"', cfg):
        if name not in seen:
            seen.append(name)
    return seen


def count_on_disk(proj: str) -> dict:
    """{databank: n_sqx} contando .sqx en disco (verdad de suelo)."""
    d = projects_dir() / proj / "databanks"
    out: dict[str, int] = {}
    if not d.is_dir():
        return out
    for sub in sorted(d.iterdir()):
        if sub.is_dir():
            out[sub.name] = sum(1 for f in sub.iterdir() if f.suffix == ".sqx")
    return out


# ----------------------------- pasos del proceso -------------------------------

def stop_graceful(proj: str, port: int, max_wait: int = 120) -> bool:
    """-project action=stop + poll hasta 'not running'. Devuelve si quedó parado."""
    try:
        resp = _http(f"-project action=stop name={proj}", port, 30)
    except Exception:
        resp = ""
    # A9: el status de B144 tras el stop no lo refleja (solo contadores); la respuesta al stop sí.
    if re.search(r"execution stopped", resp or "", re.I):
        return True
    for _ in range(max_wait // 5):
        time.sleep(5)
        st = _status(proj, port)
        if re.search(r"not running|stopped|project execution stopped|finished", st, re.I):
            return True
        # Proyecto que nunca arrancó (B144, smoke 2026-10-09): el status solo trae contadores
        # a cero, sin ninguna de las palabras de arriba → antes esperaba los 120 s enteros.
        if re.search(r"Running time so far\s+0 ms", st):
            return True
    return False


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _in_databank(status: str) -> str | None:
    """Primer entero de la primera línea con "In databank" (misma lectura que el driver:
    `grep -m1 "In databank" | tr -d ',' | grep -oE '[0-9]+' | head -1`)."""
    for line in status.splitlines():
        if "In databank" in line:
            m = re.search(r"\d+", line.replace(",", ""))
            return m.group(0) if m else None
    return None


def wait_loaded(proj: str, port: int, sleep_s: float | None = None, stable_n: int | None = None,
                timeout_s: float | None = None) -> tuple[bool, str | None]:
    """Barrera de carga lazy: espera a que "In databank" del status se estabilice `stable_n` lecturas
    iguales seguidas (cada `sleep_s`), con tope `timeout_s`. Devuelve (estable, último valor).

    Knobs por env (tests): CARGA_SLEEP (5), STABLE_N (3) — los del driver — y LOAD_WAIT_SECS (300).
    El tope cuenta iteraciones Y reloj: con `time.sleep` parcheado no gira para siempre y con un status
    lento tampoco se pasa del tope real."""
    sleep_s = _env_num("CARGA_SLEEP", LOAD_SLEEP_SECS) if sleep_s is None else sleep_s
    stable_n = int(_env_num("STABLE_N", LOAD_STABLE_N)) if stable_n is None else stable_n
    timeout_s = _env_num("LOAD_WAIT_SECS", LOAD_WAIT_SECS) if timeout_s is None else timeout_s
    prev: str | None = None
    stable = 0
    waited = 0.0
    t0 = time.monotonic()
    while True:
        cur = _in_databank(_status(proj, port))
        if cur is not None and cur == prev:
            stable += 1
            if stable >= stable_n:
                return True, cur
        else:
            stable = 0
        prev = cur
        if waited >= timeout_s or time.monotonic() - t0 >= timeout_s:
            return False, cur
        time.sleep(sleep_s)
        waited += sleep_s


def persist_bank(proj: str, bank: str, port: int, timeout: float | None = None) -> bool:
    """synctofiles por HTTP con timeout LARGO (R1). Devuelve True si el motor NO devolvió error.

    `timeout` inyectable; por defecto `SQ_LONG_SECS` (env, 1800 s): con 30 s un sync de un banco gordo se
    cortaba a mitad (el motor seguía) y el kill posterior truncaba el banco (incidente 2.754/4.999).

    Para databanks con espacios el %20 puede truncar el nombre (§6.2) → el motor
    responde 'doesn't exist'. No es fatal: el count en disco real se reporta siempre.
    """
    t = int(_env_num("SQ_LONG_SECS", SQ_LONG_SECS)) if timeout is None else timeout
    try:
        text = _http(f"-databank action=synctofiles project={proj} name={bank}", port, t)
    except Exception:
        return False
    low = text.lower()
    return ("doesn't exist" not in low) and ("error" not in low) and ("syncing" in low or "synchronization" in low)


# ----------------------------- orquestación ------------------------------------

#: Mismas señales que usa `stop_graceful` para "parado": el status del motor NO siempre las dice (queda
#: congelado al acabar, smoke_p2 2026-07-19), por eso un status vacío (motor muerto) también cuenta.
_STOPPED_RE = re.compile(r"not running|stopped|project execution stopped|finished", re.I)
_NEVER_STARTED_RE = re.compile(r"Running time so far\s+0 ms")
#: "Parado" CLARO, para decidir si ENVIAR el stop: el proyecto no corre. Más estricto que `_STOPPED_RE` (que usa
#: `stop_graceful` y acepta también `stopped`/`finished` sueltos): un status EN MARCHA puede llevar esas palabras en
#: otro contexto y entonces el stop no se enviaría nunca. Para dar un stop por aceptado sí vale la regex laxa
#: (el comando ya se envió: lo peor que pasa es no reenviarlo).
_CLEARLY_STOPPED_RE = re.compile(r"not running|project execution stopped", re.I)


def _stopped_like(status: str) -> bool:
    """El status describe un proyecto parado, terminado o que nunca arrancó."""
    return bool(_STOPPED_RE.search(status) or _NEVER_STARTED_RE.search(status))


def _pipeline_alive(proj: str, report: dict) -> bool:
    """¿Hay un orquestador o un driver de `proj` vivos? Si sí anota `pipeline_alive`/`pipeline_pids` en el reporte.

    C4 (fail-closed): si `pgrep` FALLA (no es "sin coincidencias") no sabemos si hay pipeline: se anota
    `probe_failed` + el motivo en el reporte y se devuelve True —quien llame a ciegas con `if _pipeline_alive(...)`
    NO actúa—. Los llamadores que distinguen ("hay pipeline" vs "no pude comprobar") miran `report["probe_failed"]`
    antes. Sin saberlo, el camino directo competiría con un driver (parar el motor hace que el orquestador dé la
    task por buena y lance la siguiente)."""
    try:
        orch = orchestrator_pids(proj)
        drv = driver_pids(proj)
    except ProcProbeError as e:
        _probe_failed(report, f"si hay un pipeline vivo de '{proj}' ({e})")
        return True
    if not orch and not drv:
        return False
    report["pipeline_alive"] = True
    report["pipeline_pids"] = {"orchestrator": orch, "driver": drv}
    return True


def _probe_failed(report: dict, what: str) -> None:
    report["probe_failed"] = True
    msg = (f"no se pudo comprobar: no actúo — {what}. Sin saberlo, parar o matar el motor podría competir con un "
           "driver o destruir trabajo sin guardar. Reintenta; si `pgrep` está roto, revisa el sistema.")
    if msg not in report["warnings"]:
        report["warnings"].append(msg)


def _finish(report: dict, proj: str) -> dict:
    """Cierra el reporte: .sqx en disco y veredicto de la GUI. R9: ningún consumidor dice "ya puedes abrir la
    GUI" sin `safe_for_gui` (= `pre_gui_check`, global: motor, GUI, pipeline/cascada vivos, slim, .bak)."""
    chk = pre_gui_check(proj)
    report["databanks"] = chk["databanks"]
    report["safe_for_gui"] = chk["safe"]
    report["reasons"] = chk["reasons"]
    if report.get("stopped_by") is None and (report.get("stopped") or report.get("motor_killed")
                                             or report.get("zombi_killed")):
        report["stopped_by"] = "direct"
    return report


def _pid_exists(pid: int) -> bool:
    """¿Existe el proceso (zombis incluidos)? Sin `ps`: sirve para distinguir "ya no existe" de "falló `ps`"."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _has_ancestor_in(pid: int, pool: set[int], max_depth: int = 8, ppid: int | None = None) -> bool:
    cur = _ppid(pid) if ppid is None else ppid
    for _ in range(max_depth):
        if cur <= 1:
            return False
        if cur in pool:
            return True
        cur = _ppid(cur)
    return False


def _driver_roots(pids: list[int]) -> list[int]:
    """`driver_pids` también devuelve los subshells del driver (el vigilante de RAM y los `$(…)` comparten su
    cmdline). Driver "raíz" = el que no desciende de otro de la lista: la unidad a la que se entrega el stop.

    A11 (2026-10-27, `test_f2d` inestable con la máquina cargada): un `$(…)` que termina entre el `pgrep` y esta
    consulta da ppid 0 y contaba como driver raíz NUEVO, que recibía un segundo `stop`. Un pid que ya no existe
    no es destino de nada: se descarta. El ppid se lee una sola vez para no reabrir la misma carrera. Si el ppid
    sale 0 pero el proceso existe (falló `ps`, CR6-A1), cuenta como raíz: un stop de más es inocuo, uno de menos no."""
    pool = set(pids)
    roots = []
    for p in pids:
        pp = _ppid(p)
        if pp == 0 and not _pid_exists(p):    # ya no existe
            continue
        if not _has_ancestor_in(p, pool, ppid=pp):
            roots.append(p)
    return roots


def _deliver_stop(proj: str, port: int, report: dict) -> bool:
    """UNA entrega de `-project action=stop` al motor que responde por HTTP. True = aceptada: el status deja
    de decir que el proyecto corre, o el motor deja de responder (murió). No persiste nada: lo hace el driver.

    Refinamiento del §2.4 (PLAN-DESVIO, abajo): el plan envía el stop en cuanto el motor responde. Aquí solo si
    el status dice que el proyecto CORRE: un stop antes del START (el driver aún carga los databanks) o después
    del fin no hace nada, y darlo por "entregado" dejaría que el driver arrancase el proyecto justo después y la
    parada se perdiera. Con status "parado/sin arrancar" se reintenta en la vuelta siguiente."""
    # PLAN-DESVIO (menor): solo se envía el stop si el status dice que el proyecto CORRE (ver docstring).
    st = _status(proj, port)
    if not st or _NEVER_STARTED_RE.search(st) or _CLEARLY_STOPPED_RE.search(st):
        return False
    try:
        resp = _http(f"-project action=stop name={proj}", port, 30)
    except Exception:
        return False
    report["stop_sent"] = report.get("stop_sent", 0) + 1
    # A9 (aceptación real 2026-10-27): en B144 el status tras un `stop` es IDÉNTICO al de un proyecto en marcha (solo
    # contadores), así que no sirve para saber si se aceptó y el stop se reenviaba cada vuelta. La respuesta al propio
    # `stop` sí lo dice ("Stopping project …  Project execution stopped.").
    if re.search(r"execution stopped", resp or "", re.I):
        return True
    for _ in range(max(1, int(STOP_ACCEPT_SECS))):
        time.sleep(1)
        st = _status(proj, port)
        if not st or _stopped_like(st):
            return True
    return False


def _stop_pipeline(proj: str, port: int, report: dict, wait_secs: float, kill: bool, force_kill: bool,
                   depth: int) -> dict:
    """Modo pipeline (R6, §2.4): delegar la parada en el driver y el orquestador.

    1. escribe la bandera; 2. bucle hasta `wait_secs` mientras haya orquestador O driver vivos: UNA entrega
    efectiva de `stop` por driver (un driver que aparezca en la espera —carrera de lanzamiento— recibe el
    suyo) sin persistir desde aquí; 3. al salir: sin orquestador ni driver → pipeline parado (se borra la
    bandera); con orquestador vivo (tope agotado) la bandera se queda —abortará al llegar— y
    `pipeline_stopping`; 4. si queda un sqcli huérfano sin driver ni orquestador → camino directo."""
    report["pipeline_alive"] = True
    flag = write_stop_flag(proj)
    if flag is None:
        report["warnings"].append(
            f"no pude escribir la bandera de parada ({stop_flag_path(proj)}): sin ella el orquestador NO se "
            "detendrá tras la task en curso.")
    else:
        report["stop_flag"] = str(flag)

    delivered: set[int] = set()
    orch: list[int] = []
    drv: list[int] = []
    deadline = time.monotonic() + wait_secs
    for _ in range(max(1, int(wait_secs / STOP_POLL_SECS) + 1)):
        try:
            orch, drv = orchestrator_pids(proj), driver_pids(proj)
        except ProcProbeError:
            pass                          # sin información: sigo esperando; el sondeo final decide
        else:
            report["pipeline_pids"] = {"orchestrator": orch, "driver": drv}
            if not orch and not drv:
                break
            roots = _driver_roots(drv)
            if [d for d in roots if d not in delivered] and motor_alive(port):
                if _deliver_stop(proj, port, report):
                    delivered.update(roots)
        if time.monotonic() >= deadline:
            break
        time.sleep(STOP_POLL_SECS)

    try:
        orch, drv = orchestrator_pids(proj), driver_pids(proj)
    except ProcProbeError as e:
        _probe_failed(report, f"si el pipeline de '{proj}' ya salió ({e})")
        return _finish(report, proj)
    report["pipeline_pids"] = {"orchestrator": orch, "driver": drv}
    report["stop_delivered"] = bool(delivered)
    if not orch and not drv:
        report["pipeline_stopped"] = True
        report["stopped"] = True
        report["stopped_by"] = "pipeline"
        clear_stop_flag(proj)
        try:
            leftover = engine_pids()
        except ProcProbeError:
            leftover = []
        if leftover and depth < _MAX_DEPTH:
            # quedó un sqcli sin driver ni orquestador (p. ej. el que un driver dejó vivo por un sync sin
            # confirmar): camino directo (§2.3) sobre él
            return _direct(proj, port, report, kill, force_kill, wait_secs, depth + 1)
        return _finish(report, proj)
    report["pipeline_stopping"] = True
    if not orch:
        clear_stop_flag(proj)             # solo se conserva con un orquestador vivo (el único que la lee)
    report["warnings"].append(
        f"parando: el pipeline saldrá al terminar de guardar la task en curso (sigue vivo tras {wait_secs:.0f} s: "
        f"orquestador {orch or '-'}, driver {drv or '-'}). "
        + ("La bandera de parada queda puesta: el orquestador no lanzará ninguna task más y saldrá en cuanto su "
           "driver termine (vuelve a ejecutar Stop & persist, o mira `pgrep`, para comprobarlo)."
           if orch else
           "Solo queda el driver de la task en curso, que persistirá al terminar."))
    if not delivered:
        report["warnings"].append(
            "el `stop` no llegó a entregarse (el motor no respondía por HTTP o el proyecto no estaba corriendo): "
            "la task en curso seguirá hasta su fin natural.")
    return _finish(report, proj)


def _direct(proj: str, port: int, report: dict, kill: bool, force_kill: bool, wait_secs: float,
            depth: int) -> dict:
    """Camino directo (sin driver ni orquestador vivos, §2.3). A6: justo antes de enviar `stop` o matar se
    re-comprueba el pipeline; si apareció alguno → modo pipeline (bandera + delegación)."""
    # 1. ¿hay motor?
    if not motor_alive(port):
        try:
            pids = engine_pids()
        except ProcProbeError as e:
            _probe_failed(report, f"si hay un motor vivo ({e})")
            return _finish(report, proj)
        if not pids:
            report["nothing_to_stop"] = True
            return _finish(report, proj)
        # Proceso vivo con HTTP ciego: o ZOMBI (colgado, bloquea la GUI) o SATURADO trabajando
        # (landmine fitab 2026-09-11: bajo saturación el HTTP también calla con el motor trabajando).
        # Se distingue por CPU: matar al saturado destruye horas de trabajo sin guardar.
        cpu = max(engine_cpu(p) for p in pids)
        report["engine_cpu"] = round(cpu, 1)
        if cpu >= CPU_BUSY_PCT and not force_kill:
            report["engine_busy"] = True
            report["warnings"].append(
                f"motor saturado trabajando (HTTP sin respuesta pero CPU {cpu:.0f} % ≥ {CPU_BUSY_PCT:.0f} %): "
                "NO lo mato (perdería su trabajo sin guardar). Reintenta cuando responda o usa "
                "--force-kill si estás seguro de que está colgado.")
        else:
            alive = _pipeline_alive(proj, report)      # A6: justo antes de matar
            if report["probe_failed"]:
                return _finish(report, proj)
            if alive and depth < _MAX_DEPTH:
                return _stop_pipeline(proj, port, report, wait_secs, kill, force_kill, depth + 1)
            if cpu >= CPU_BUSY_PCT:
                report["warnings"].append(
                    f"--force-kill: matado un motor que estaba trabajando (CPU {cpu:.0f} %); "
                    "su trabajo no guardado se pierde.")
            report["zombi_killed"] = kill_engine(pids)
            report["note"] = ("motor zombi (HTTP inactivo, proceso vivo) liberado" if report["zombi_killed"]
                              else "motor zombi (HTTP inactivo, proceso vivo): NO se pudo matar")
            report["nothing_to_stop"] = True
        return _finish(report, proj)

    # A6: justo antes de enviar `stop` (el pipeline pudo arrancar entre el primer chequeo y ahora)
    alive = _pipeline_alive(proj, report)
    if report["probe_failed"]:
        return _finish(report, proj)
    if alive and depth < _MAX_DEPTH:
        return _stop_pipeline(proj, port, report, wait_secs, kill, force_kill, depth + 1)

    # 2-3. stop graceful
    report["stopped"] = stop_graceful(proj, port)
    if not report["stopped"]:
        report["warnings"].append(
            "stop no confirmó 'not running' en 120s — persistiendo bajo riesgo "
            "(el build podría seguir corriendo; .sqx potencialmente inconsistente)")

    # 3b. barrera de carga (MM-H10/A3): sincronizar un banco a medio cargar borra del disco lo que el
    # motor aún no tiene en RAM. Sin estabilizar → ni sincroniza ni mata (force_kill solo salta el kill).
    stable, last = wait_loaded(proj, port)
    report["load_stable"] = stable
    if not stable:
        report["load_unstable"] = True
        report["warnings"].append(
            f"carga no estabilizada ('In databank' sigue cambiando; último={last}): NO sincronizo "
            "(un synctofiles sobre una carga a medias borra ficheros del disco) ni mato el motor. "
            "Reintenta cuando termine de cargar.")
        if kill and force_kill:
            report["warnings"].append("--force-kill: mato el motor SIN sincronizar (carga sin estabilizar).")
            report["motor_killed"] = kill_engine()
        return _finish(report, proj)

    # 4. decidir qué persistir según conteo en RAM (count funciona con espacios — §6.2).
    #    Sólo guardamos databanks CON estrategias y persistibles (sin espacios).
    banks = databanks_of(proj)
    ram = _motor_counts(proj, banks, port)
    for bank in banks:
        n = ram.get(bank)
        if n is None:                          # count falló → no asumir vacío (fix deepseek P3)
            report["banks_unknown"].append(bank)
            report["warnings"].append(
                f"count en RAM falló para '{bank}' — no persistido (¿motor lento?)")
            continue
        if n == 0:                             # vacío en RAM → nada que guardar
            report["banks_empty"].append(bank)
            continue
        if " " in bank:                        # tiene estrategias PERO no persistible por HTTP
            report["banks_at_risk"][bank] = n
            continue
        if persist_bank(proj, bank, port):
            report["banks_persisted"].append(bank)
        else:
            report["banks_http_failed"].append(bank)
    time.sleep(8)  # que el disco se asiente (deepseek: 3s era arbitrario/corto)

    # 5. liberar motor. R1: si un synctofiles o un count FALLÓ NO se mata (salvo --force-kill): el banco
    # solo existe en la RAM del motor y matarlo lo pierde.
    if kill:
        failed = [*report["banks_http_failed"], *report["banks_unknown"]]
        if failed and not force_kill:
            report["persist_failed"] = True
            report["warnings"].append(
                f"NO mato el motor: no se pudo guardar/contar {', '.join(failed)} (el banco solo está en su RAM y "
                "matarlo lo perdería). Reintenta o usa --force-kill si aceptas perderlo.")
        else:
            alive = _pipeline_alive(proj, report)      # A6: justo antes de matar
            if report["probe_failed"]:
                return _finish(report, proj)
            if alive and depth < _MAX_DEPTH:
                return _stop_pipeline(proj, port, report, wait_secs, kill, force_kill, depth + 1)
            if failed:
                report["warnings"].append(
                    f"--force-kill: mato el motor aunque {', '.join(failed)} NO se guardó: se pierde.")
            report["motor_killed"] = kill_engine()

    # 6. conteo real en disco + veredicto de la GUI (siempre — no se asume)
    return _finish(report, proj)


def run(proj: str, port: int = 5050, kill: bool = True, force_kill: bool = False,
        wait_secs: float = PIPELINE_STOP_WAIT) -> dict:
    """Stop determinista + persistir todos los databanks + (opcional) liberar motor.

    Con orquestador o driver del proyecto vivos entra en MODO PIPELINE (R6): bandera de parada + un `stop`
    por driver + espera hasta `wait_secs` a que el pipeline salga; no persiste ella (lo hace el driver). Sin
    pipeline: camino directo.

    `force_kill`: mata aunque el motor esté saturado trabajando (HTTP ciego, CPU ≥5 %), la carga no
    se estabilice o un banco no se haya podido guardar. NUNCA fuerza un `synctofiles` sobre una carga sin
    estabilizar.

    Devuelve un reporte estructurado:
      {proj, nothing_to_stop, stopped, banks_persisted: [..], banks_http_failed: [..],
       databanks: {name: n_sqx_disco}, motor_killed, warnings: [..],
       pipeline_alive, pipeline_stopped, pipeline_stopping, stopped_by ("pipeline"|"direct"|None),
       safe_for_gui, reasons, persist_failed, probe_failed, engine_busy, load_unstable (solo si aplican)}
    """
    report: dict = {"proj": proj, "nothing_to_stop": False, "stopped": False,
                    "banks_persisted": [], "banks_http_failed": [],
                    "banks_empty": [], "banks_at_risk": {},   # {bank: n_en_RAM} con espacios
                    "banks_unknown": [],                      # count en RAM falló (no asumir vacío)
                    "databanks": {}, "motor_killed": False, "warnings": [],
                    "pipeline_alive": False, "pipeline_stopped": False, "pipeline_stopping": False,
                    "stopped_by": None, "safe_for_gui": False, "reasons": [],
                    "persist_failed": False, "probe_failed": False}

    # 0. ¿pipeline vivo? → modo pipeline (R6). Sondeo roto → no actúo (C4).
    alive = _pipeline_alive(proj, report)
    if report["probe_failed"]:
        return _finish(report, proj)
    if alive:
        return _stop_pipeline(proj, port, report, wait_secs, kill, force_kill, 0)
    return _direct(proj, port, report, kill, force_kill, wait_secs, 0)


def _print_gui_verdict(rep: dict) -> None:
    """R9: "ya puedes abrir la GUI" SOLO con `safe_for_gui`; si no, los motivos. Sin la clave (reporte de otra
    versión) no se afirma nada."""
    if "safe_for_gui" not in rep:
        return
    if rep["safe_for_gui"]:
        print("  ✅ Ya puedes abrir la GUI de SQX (nada vivo, sin bancos apartados ni .bak pendiente).")
    else:
        print("  ⛔ NO abras la GUI todavía:")
        for r in rep.get("reasons") or []:
            print(f"      · {r}")


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Stop determinista + persist + liberar motor SQX.")
    ap.add_argument("proj", nargs="?", help="proyecto SQX (p.ej. dax40_4h)")
    ap.add_argument("port", nargs="?", type=int, default=5050)
    ap.add_argument("--no-kill", action="store_true", help="no matar el motor (sólo stop+persist)")
    ap.add_argument("--force-kill", action="store_true",
                    help="matar aunque el motor esté saturado trabajando (HTTP ciego, CPU alta), la "
                         "carga no se estabilice o un banco no se haya podido guardar. Nunca sincroniza a ciegas.")
    # PLAN-DESVIO (menor): opción nueva (el plan no la prevé): tope de espera del modo pipeline desde la CLI.
    ap.add_argument("--wait-secs", type=float, default=None,
                    help=f"modo pipeline: segundos máximos esperando a que el pipeline salga "
                         f"(default {PIPELINE_STOP_WAIT:.0f})")
    args = ap.parse_args()

    if not args.proj:
        # default: el proyecto con más .sqx en disco (el más probable de estar corriendo)
        cands = []
        base = projects_dir()
        if base.is_dir():
            for p in base.iterdir():
                if p.is_dir() and (p / "project.cfx").exists():
                    cands.append(p.name)
        if not cands:
            print("No hay proyectos en user/projects/. Indica uno: stop_and_persist.py <proj>")
            return 2
        if len(cands) == 1:
            args.proj = cands[0]
        else:
            print("Varios proyectos. Indica cuál:", ", ".join(cands))
            return 2

    kwargs = {} if args.wait_secs is None else {"wait_secs": args.wait_secs}
    rep = run(args.proj, args.port, kill=not args.no_kill, force_kill=args.force_kill, **kwargs)
    rc = 0
    if rep.get("probe_failed"):
        rc = 1
        print(f"⛔ Stop & persist de '{rep['proj']}' NO completado (no se pudo comprobar qué hay vivo):")
        for w in rep.get("warnings", []):
            print(f"  ⚠ {w}")
    elif rep.get("pipeline_alive"):
        if rep.get("pipeline_stopped"):
            print(f"✅ Pipeline de '{rep['proj']}' parado: la task en curso persistió por su driver y no se "
                  "lanzó ninguna más.")
        else:
            rc = 1
            print(f"⏳ Pipeline de '{rep['proj']}' PARÁNDOSE (no ha salido aún):")
        for w in rep.get("warnings", []):
            print(f"  ⚠ {w}")
    elif rep.get("engine_busy") or rep.get("load_unstable") or rep.get("persist_failed"):
        # No se hizo (o no se completó) la parada: que salga con rc != 0 y diga por qué.
        rc = 1
        print(f"⛔ Stop & persist de '{rep['proj']}' NO completado:")
        for w in rep.get("warnings", []):
            print(f"  ⚠ {w}")
    elif rep["nothing_to_stop"]:
        if rep.get("zombi_killed"):
            print("💀 Motor zombi (HTTP inactivo, proceso vivo) detectado y matado.")
            for w in rep.get("warnings", []):
                print(f"  ⚠ {w}")
        elif "zombi_killed" in rep:   # había proceso, se intentó matar y sigue vivo
            rc = 1
            print("⛔ Motor zombi (HTTP inactivo, proceso vivo) detectado pero NO se pudo matar "
                  "(¿proceso atascado en el kernel? verifica `pgrep -x sqcli`).")
        else:
            print("⚪ Nada que parar: el motor sqcli no está corriendo.")
    else:
        print(f"Proyecto: {rep['proj']}")
        print(f"  stop graceful: {'✅' if rep['stopped'] else '⚠ timeout (no confirmó not-running)'}")
        for w in rep.get("warnings", []):
            print(f"  ⚠ {w}")
        if rep["banks_persisted"]:
            print(f"  ✅ persistidos: {', '.join(rep['banks_persisted'])}")
        if rep["banks_at_risk"]:
            print("  ⚠ EN RIESGO (estrategias en RAM, no persistibles por HTTP — espacios):")
            for b, n in rep["banks_at_risk"].items():
                print(f"      {b}: {n} estrategias")
        if rep.get("banks_unknown"):
            print(f"  ⚠ count falló (no persistidos, no asumidos vacíos): {', '.join(rep['banks_unknown'])}")
        if rep["banks_http_failed"]:
            print(f"  no persistidos por HTTP (sin count claro): {', '.join(rep['banks_http_failed'])}")
        if rep["banks_empty"]:
            print(f"  vacíos (sin guardar): {len(rep['banks_empty'])} databanks")
    print("  databanks en disco:")
    for name, n in sorted(rep["databanks"].items(), key=lambda x: -x[1]):
        if n:
            print(f"    {name}: {n} .sqx")
    if rep.get("motor_killed"):
        print("  motor: sqcli matado.")
    elif (not rep["nothing_to_stop"] and not args.no_kill and rc == 0 and not rep.get("pipeline_alive")):
        print("  motor: ⚠ no se pudo matar del todo (verifica pgrep -x sqcli).")
    _print_gui_verdict(rep)
    return rc


if __name__ == "__main__":
    sys.exit(_cli())
