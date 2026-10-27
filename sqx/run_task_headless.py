#!/usr/bin/env python3
"""Lanzar 1 task (o desde 1 task) de un proyecto SQX headless, desde la app.

Vía robusta (debatido con deepseek-v4-pro, consenso against+neutral): **active-flags**.
Evita el landmine §2 del `task=N` (ID interno opaco, duplicados, sin comando que lo liste).
Mutar `active="false"` en las tasks a saltar + `-project action=start` (corre sólo las
activas, en orden del config.xml):
  - mode="only" (sólo esta, no sigue): active=false en TODAS las demás.
  - mode="from" (desde esta, sigue):   active=false en las PREVIAS.

El `.cfx` se restaura INMEDIATAMENTE tras confirmar el start (el motor ya cargó el plan de
tasks al arrancar; no lo relee a mitad — VERIFICADO con el experimento smoke_p2, 2026-07-19)
→ la ventana de riesgo (GUI pisaría el .cfx mutado) pasa de horas a segundos. Aun así: no
abras la GUI mientras corre.

Spawn: `subprocess.Popen(run_pipeline_headless.sh, start_new_session=True)` → sobrevive al
cierre del navegador y a que mates el servidor Streamlit. El driver hace motor-caliente +
start + poll + persist + stop final.

Pre-checks: instancia única (no lanzar si ya hay motor o GUI abierta) y, antes de tocar nada,
`launch_block_reason` (R7, guardrail sqx-headless-fallos): ni un segundo driver u orquestador del
proyecto ni un sqcli vivo (detección por nombre exacto de proceso, ver `stop_and_persist`).
Si el arranque no se confirma, solo se mata el motor del grupo de procesos de ESTE driver.

Parada del pipeline (R6): con `RUNNER_PIPELINE=1` (lo exporta el orquestador) `run` mira la bandera
`<proyecto>/.pipeline_stop_request` (la escribe Stop & persist) ANTES de mutar el .cfx y OTRA VEZ justo
antes del `Popen`: si existe, no lanza (restaura el .cfx si ya lo había mutado). Entre la 2.ª comprobación
y el `Popen` queda una ventana de milisegundos que se acepta y se compensa (A5): el driver que nace recibe
su `stop` de Stop & persist. Un lanzamiento SUELTO (sin `RUNNER_PIPELINE`) borra una bandera rancia al
empezar y lo dice: lanzar de nuevo es una decisión explícita.

Uso (CLI):  python sqx/run_task_headless.py <proj> --list
            python sqx/run_task_headless.py <proj> --task "<title>" --mode only|from
Importable: from run_task_headless import run, tasks_of; run("dax40_4h", "Real OOS", "only")
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402  PROJECTS
import stop_and_persist as SP  # noqa: E402  motor_alive, _http
import bank_plan as BP  # noqa: E402  reconcile_at_startup, marcador --slim

DRIVER = Path(__file__).resolve().parent / "run_pipeline_headless.sh"
ORCHESTRATOR = Path(__file__).resolve().parent / "run_pipeline_per_task.sh"


def _projects_dir() -> Path:
    """Raíz de proyectos SQX: env PROJECTS_DIR si existe (la inyectan los drivers
    bash y los tests con fixtures en tmp); si no, PROJECTS real del registro.
    Delega en `stop_and_persist.projects_dir` (fuente única del stack, 2026-10-21)."""
    return SP.projects_dir()


# ----------------------------- lectura del config.xml --------------------------

def tasks_of(proj: str) -> list[dict]:
    """Lista ordenada de tasks del config.xml: [{xml, label, type, active}].
    `label` = title (humano, único) si existe; si no, name; si no, taskXMLFile.
    El orden = orden de ejecución en SQX. La lectura del .cfx vive en
    bank_plan._tasks_of_cfx (reusada por el plan --slim)."""
    return BP._tasks_of_cfx(_projects_dir() / proj / "project.cfx")


# ----------------------------- mutación active --------------------------------

def _set_active(tag: str, val: bool) -> str:
    new = "true" if val else "false"
    if 'active="' in tag:
        return re.sub(r'active="[^"]*"', f'active="{new}"', tag)
    # sin atributo active → añadir antes del cierre
    return re.sub(r'(/?)>\s*$', f' active="{new}" \\1>', tag)


def mutate_active(proj: str, keep_xml: str, mode: str) -> Path:
    """Muta el .cfx según el modo. Devuelve el path del backup.
    keep_xml = taskXMLFile de la task seleccionada."""
    cfx = _projects_dir() / proj / "project.cfx"
    bak = cfx.with_suffix(".cfx.pre_run_task.bak")
    tasks = tasks_of(proj)
    idx = next((i for i, t in enumerate(tasks) if t["xml"] == keep_xml), None)
    if idx is None:
        raise SystemExit(f"task {keep_xml} no encontrada en {proj}")
    # qué tasks quedan activas: only → sólo la sel; from → la sel y las posteriores
    want = [(i == idx) if mode == "only" else (i >= idx) for i in range(len(tasks))]

    with zipfile.ZipFile(cfx) as z:
        names = z.namelist()
        members = {n: z.read(n) for n in names}
    cfg = members["config.xml"].decode("utf-8", "surrogateescape")
    matches = list(re.finditer(r'<Task\b[^>]*>', cfg))
    if len(matches) != len(tasks):
        raise SystemExit(f"#{len(matches)} <Task> vs #{len(tasks)} parseadas — aborto")
    # reemplazar de atrás hacia adelante para no invalidar offsets
    for m, w in zip(reversed(matches), reversed(want)):
        cfg = cfg[:m.start()] + _set_active(m.group(0), w) + cfg[m.end():]
    members["config.xml"] = cfg.encode("utf-8", "surrogateescape")

    if not bak.exists():
        shutil.copy2(cfx, bak)
    else:
        # H8 (debate 2026-09-27): un .bak huérfano + `if not bak.exists()` = la siguiente
        # mutación NO respalda el estado actual y el restore posterior pisa el .cfx con un
        # backup VIEJO (p.ej. perdiendo ediciones de GUI hechas entre corridas). Abortar es
        # el único comportamiento seguro; el monitor tiene botón de restore (pending_restore).
        raise SystemExit(
            f"Existe {bak.name} huérfano: un lanzamiento previo murió entre mutar y restaurar. "
            f"Resuélvelo ANTES de relanzar (restaura con RT.restore / botón 'restore pendiente' "
            f"del monitor, o bórralo si el .cfx está bien) — continuar pisaría el .cfx con un "
            f"backup viejo (H8).")
    with zipfile.ZipFile(cfx, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, members[n])
    return bak


def restore(proj: str, bak: Path) -> bool:
    """Restaura el .cfx desde el .bak y ELIMINA el .bak.

    Un .bak que sobrevive a un restore exitoso es indistinguible de un .bak huérfano
    (lanzamiento muerto a mitad) → haría saltar la alerta 'restore pendiente' del
    monitor para siempre (falso positivo tras cada lanzamiento sano).
    """
    cfx = _projects_dir() / proj / "project.cfx"
    if bak.exists():
        shutil.copy2(bak, cfx)
        bak.unlink()
        return True
    return False


# ----------------------------- pre-checks -------------------------------------

def gui_open() -> bool:
    """¿La GUI de SQX está abierta? Fail-closed (C5): si `pgrep` falla no sabemos si lo está → True (no se
    lanza nada); el motivo real lo da `gui_block_msg()`."""
    try:
        return bool(SP.gui_pids())
    except SP.ProcProbeError:
        return True


def gui_block_msg() -> str:
    """Motivo (legible) por el que `gui_open()` dio True."""
    try:
        SP.gui_pids()
    except SP.ProcProbeError as e:
        return f"No pude comprobar si la GUI de SQX está abierta ({e}): no lanzo por seguridad."
    return "La GUI de SQX está abierta (corrompería databanks). Ciérrala."


def stop_requested(proj: str) -> bool:
    """¿Hay bandera de parada del pipeline de `proj` (R6)? Función aparte para que los tests puedan inyectar
    la bandera entre las dos comprobaciones de `run` (A5)."""
    return SP.stop_requested(proj)


def launch_block_reason(proj: str, in_pipeline: bool | None = None) -> str | None:
    """Motivo por el que NO se puede lanzar una task/pipeline de `proj` ahora, o None si se puede (R7).

    - driver de `proj` vivo → una task del proyecto ya está en marcha.
    - orquestador de `proj` vivo → el pipeline sigue en marcha (aunque el motor esté parado entre
      tasks). Se salta cuando quien lanza ES el orquestador (`RUNNER_PIPELINE=1`: lo exporta él a sus
      hijos; si no, el lanzador de cada task se rechazaría a sí mismo).
    - algún `sqcli` vivo (instancia única de toda la máquina; detección por nombre exacto) → salvo
      SLIM_TESTING=1 (tests con mocks: en la máquina puede haber un motor real ajeno).
    Fail-closed: si `pgrep` falla no sabemos qué hay vivo → se bloquea.

    Lo usan `run`, `run_pipeline_per_task` (defensa en profundidad) y el botón "Ejecutar" del monitor."""
    if in_pipeline is None:
        in_pipeline = os.environ.get("RUNNER_PIPELINE") == "1"
    testing = bool(os.environ.get("SLIM_TESTING"))
    try:
        drivers = SP.driver_pids(proj)
        orchs = [] if in_pipeline else SP.orchestrator_pids(proj)
        engines = [] if testing else SP.engine_pids()
    except SP.ProcProbeError as e:
        return f"No pude comprobar los procesos vivos ({e}): no lanzo por seguridad."

    def _fmt(pids: list[int]) -> str:
        return ", ".join(str(p) for p in pids)

    if orchs:
        return (f"Hay un orquestador de '{proj}' vivo (pids {_fmt(orchs)}): el pipeline sigue en marcha, "
                f"aunque el motor esté parado entre tasks. Páralo (Stop & persist o `kill` al orquestador) "
                f"antes de lanzar otro.")
    if drivers:
        return (f"Hay un driver de '{proj}' vivo (pids {_fmt(drivers)}): ya hay una task de este proyecto "
                f"en marcha. Espera a que termine o usa Stop & persist.")
    if engines:
        return (f"Hay un sqcli vivo (pids {_fmt(engines)}): instancia única. Páralo primero con Stop & persist "
                f"(botón del monitor si responde por HTTP, o `python sqx/stop_and_persist.py <proyecto>`).")
    return None


def own_engines(driver_pid: int) -> list[int]:
    """Motores vivos del grupo de procesos del driver `driver_pid`. El driver nace con
    `start_new_session=True` (pgid = su PID) y el `tail -f /dev/null | sqcli` hereda ese grupo, así que
    el pgid del motor identifica a SU driver aunque este ya haya muerto (R3: nunca matar un motor ajeno)."""
    return [p for p in SP.engine_pids() if SP.engine_owner_pgid(p) == driver_pid]


# ----------------------------- reconciliación --slim (B2) ---------------------

def reconcile_at_startup(proj: str) -> dict:
    """Reconciliación al arranque del runner (TODO arranque, antes de tocar nada).

    Si `databanks_archive/.runner_session.json` existe y el pid del driver que lo
    escribió ya no vive, la corrida --slim anterior murió a mitad → restaurar
    presence-aware (idempotente: salta los bancos que ya tengan ficheros en
    databanks/, restaura solo si el archive tiene ficheros y el destino está
    vacío/ausente) y borrar el marcador. Si hay motor/GUI vivo NO se toca nada:
    action='blocked' (abortar pidiendo stop). Si el pid vive, action='live'
    (corrida en curso; el pre-check de instancia única abortará después).

    Ojo: en pipeline per-task el launcher corre con SLIM_IN_PIPELINE=1 (lo pone
    el orquestador) y NO reconcilia por task — el restore acumulado lo hace el
    orquestador al final de la cadena; reconciliar por task restauraría los
    bancos entre task y task y borraría el marcador que el driver de la
    siguiente iteración necesita (punto 1, L3 por task).

    SLIM_TESTING=1 (solo tests con mocks) salta las guardas de GUI/motor vivo.
    """
    projdir = _projects_dir() / proj
    marker = BP.marker_read(projdir)
    if marker is None:
        return {"action": "none"}
    if BP.marker_pid_alive(marker):
        return {"action": "live", "run_id": marker.get("run_id")}
    if os.environ.get("SLIM_TESTING"):
        return {"action": "reconciled", **BP.reconcile(projdir)}
    if gui_open():
        if gui_block_msg().startswith("No pude"):   # C5: pgrep roto (fail-closed), no es que la GUI esté abierta
            return {"action": "blocked",
                    "msg": "hay bancos archivados de una corrida --slim muerta y no pude comprobar si la "
                           "GUI está abierta (pgrep falló) — no toco nada"}
        return {"action": "blocked",
                "msg": "hay bancos archivados de una corrida --slim muerta y la "
                       "GUI está abierta — ciérrala antes de relanzar (no toco nada)"}
    try:
        if SP.motor_alive(5050):
            return {"action": "blocked",
                    "msg": "hay bancos archivados de una corrida --slim muerta y un "
                           "motor vivo (¿arrancado a mano?) — mátalo (stop & persist) "
                           "y reintenta; no restauro con el motor vivo (L4)"}
    except Exception:
        pass
    return {"action": "reconciled", **BP.reconcile(projdir)}


# ----------------------------- orquestación -----------------------------------

def run(proj: str, title: str, mode: str, port: int = 5050, wait_start: int = 90,
        slim: bool = False) -> dict:
    """Lanza la task con `title` en modo only|from. Devuelve reporte.

    slim=True propaga `--slim` al driver (archivo de databanks que la task no
    usa); el launcher NO archiva ni restaura bancos — el driver es el dueño
    único del ciclo (B1, diseño v3)."""
    report: dict = {"proj": proj, "title": title, "mode": mode, "ok": False, "msg": ""}

    # R7 (defensa en profundidad, ANTES de tocar nada): ni segundo driver ni segundo pipeline del
    # proyecto, ni un sqcli ajeno (instancia única). El botón del monitor ya lo deshabilita; esto cubre
    # la CLI y cualquier otro llamador.
    block = launch_block_reason(proj)
    if block:
        report["msg"] = block
        return report

    # R6: bandera de parada del pipeline. Dentro del pipeline (RUNNER_PIPELINE=1, lo exporta el orquestador)
    # es una ORDEN: no se lanza. En un lanzamiento suelto es una bandera RANCIA (no hay orquestador ni driver
    # vivos del proyecto: acabamos de comprobarlo arriba) y se borra, avisando (D3/A7).
    in_pipeline = os.environ.get("RUNNER_PIPELINE") == "1"
    stale_note = ""
    if in_pipeline:
        if stop_requested(proj):                                    # 1.ª comprobación: antes de mutar el .cfx
            report["stop_requested"] = True
            report["msg"] = ("Parada pedida por el usuario (bandera .pipeline_stop_request): no lanzo la "
                             "task.")
            return report
    elif stop_requested(proj):
        SP.clear_stop_flag(proj)
        report["stale_stop_flag_removed"] = True
        stale_note = (" (borrada una bandera de parada rancia .pipeline_stop_request de una parada anterior: "
                      "lanzar de nuevo es una decisión explícita)")

    # reconciliación B2 en TODO arranque (salvo launcher hijo del orquestador
    # per-task: allí la cadena gestiona el acumulado)
    if not os.environ.get("SLIM_IN_PIPELINE"):
        rec = reconcile_at_startup(proj)
        if rec["action"] == "blocked":
            report["msg"] = "Reconciliación bloqueada: " + rec["msg"]
            return report
        if rec["action"] == "reconciled":
            report["reconciled"] = rec  # aviso en el reporte para el caller/monitor

    # pre-checks (SLIM_TESTING=1: solo tests con mocks, sin procesos SQX reales)
    testing = bool(os.environ.get("SLIM_TESTING"))
    if not testing and SP.motor_alive(port):
        report["msg"] = "Ya hay un motor corriendo (instancia única). Usa 'Stop & persist' primero."
        return report
    if not testing and gui_open():
        report["msg"] = gui_block_msg()   # C5: "abierta" o "no pude comprobarlo" (fail-closed)
        return report
    tasks = tasks_of(proj)
    sel = next((t for t in tasks if t["label"] == title), None)
    if not sel:
        report["msg"] = f"Title '{title}' no encontrado. Disponibles: {[t['label'] for t in tasks]}"
        return report

    # 1. backup + mutar active (aborta con SystemExit si hay .bak huérfano, H8)
    try:
        bak = mutate_active(proj, sel["xml"], mode)
    except SystemExit as e:
        report["msg"] = str(e)
        return report

    # 2. spawn del driver en su propia sesión (sobrevive al servidor Streamlit).
    # H9 (debate 2026-09-27): log ÚNICO por lanzamiento — el path compartido
    # /tmp/<proj>_runtask.log se truncaba al reanudar con el driver viejo aún vivo
    # (escrituras intercaladas → falsos +/- en la clasificación por grep).
    idx = next(i for i, t in enumerate(tasks) if t["xml"] == sel["xml"])
    log = Path("/tmp") / f"{proj}_task{idx:02d}_{int(time.time())}.log"
    try:
        lf = open(log, "w")
        # 4º arg = from_task (vacío: el launcher ya mutó los active-flags),
        # 5º = task_idx 0-based para el plan --slim del driver
        driver_cmd = ["bash", str(DRIVER), proj, str(port), "", str(idx)]
        if slim:
            driver_cmd.append("--slim")
        # R6: 2.ª comprobación de la bandera, lo más tarde posible (justo antes del Popen). Si apareció tras
        # la 1.ª, el .cfx ya está mutado: se restaura y no se lanza. Queda una ventana de milisegundos entre
        # esta comprobación y el Popen que se ACEPTA (A5): el driver que nazca recibirá su `stop` de
        # Stop & persist (bucle del modo pipeline) y su task quedará `user_stop`.
        if in_pipeline and stop_requested(proj):
            lf.close()
            log.unlink(missing_ok=True)
            restore(proj, bak)
            report["stop_requested"] = True
            report["msg"] = ("Parada pedida por el usuario (bandera .pipeline_stop_request) justo antes de "
                             "lanzar el driver: no lo lanzo, .cfx restaurado.")
            return report
        proc = subprocess.Popen(driver_cmd,
                                stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        lf.close()  # el hijo tiene su propia copia del fd; cierro en el padre (no fuga)
    except Exception as e:
        restore(proj, bak)
        report["msg"] = f"No se pudo spawn el driver: {e}"
        return report
    report["driver_pid"] = proc.pid
    report["log"] = str(log)

    # 3. esperar a que el motor arranque Y procese el start (genera / running)
    started = False
    for _ in range(wait_start // 5):
        time.sleep(5)
        if SP.motor_alive(port):
            st = ""
            try:
                st = SP._http(f"-project action=status name={proj}", port, 10)
            except Exception:
                pass
            # arrancó si el status NO dice "not running"/"stopped" (vale para cualquier
            # tipo de task: Build/Filtering/Retest — evita falsos negativos, fix deepseek)
            if st and not re.search(r"not running|stopped", st, re.I):
                started = True
                break
    time.sleep(5)  # margen extra para que el start quede procesado

    # 4. restaurar el .cfx SIEMPRE (el motor ya cargó el plan al arrancar).
    #    ASUNCIÓN VERIFICADA (experimento smoke_p2, 2026-07-19): SQX NO relee el
    #    config.xml al avanzar de task — 2 Build tasks (3min+1min), modo 'only', .cfx
    #    restaurado 2 min ANTES de acabar la task 1, y la task 2 nunca arrancó
    #    (global_log: un solo TASK STARTED/FINISHED). El restore inmediato es seguro.
    restore(proj, bak)

    if started:
        report["ok"] = True
        report["msg"] = (f"Lanzado modo '{mode}' desde '{sel['label']}'. Motor corriendo "
                         f"(PID driver {proc.pid}). .cfx restaurado.{stale_note}")
    else:
        report["ok"] = False
        # H1 (debate 2026-09-27): el driver queda DETACHED y, tras el restore del .cfx,
        # correría el pipeline COMPLETO en un solo motor — el incidente que este diseño
        # evita. Sin start confirmado no hay trabajo que persistir: matar driver y motor
        # (stop best-effort por si el status estaba ciego pero la task sí arrancó).
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            SP._http(f"-project action=stop name={proj}", port, 5)
        except Exception:
            pass
        # Solo el motor de ESTE driver (su grupo de procesos: nace con start_new_session → pgid = PID
        # del driver). Un sqcli ajeno (p.ej. el que hizo abortar al driver por instancia única) no se
        # toca. TERM, gracia de 12 s y kill -9 (SIGTERM no siempre basta — landmine fitab).
        # PLAN-DESVIO: el plan dice "pasa a engine_pids()/kill_engine()" (= TODOS los sqcli); se filtra por
        # el grupo del driver para cumplir R3 ("nunca mata un motor que no sea de su propia cadena").
        SP.kill_engine(own_engines(proc.pid), grace=12)
        report["msg"] = (f"El motor no confirmó arranque en {wait_start}s → driver y motor "
                         f"MATADOS (evita pipeline completo en un motor, H1). "
                         f".cfx restaurado. Revisa {log}.")
    return report


def run_pipeline_per_task(proj: str, from_idx: int = 0, port: int = 5050,
                          slim: bool = False) -> dict:
    """Lanza el pipeline con UN MOTOR FRESCO POR TASK (orquestador run_pipeline_per_task.sh).

    Camino POR DEFECTO para lanzar pipelines (landmine memoria gold_h4_prod_oos
    2026-09-27: un solo motor acumula todos los bancos en RAM → colapso). En la app,
    el modo 'from' se enruta aquí; RT.run(modo='only') sigue para tasks sueltas
    (una sola task en un solo motor ya está aislada por construcción).
    `from_idx` = índice 0-based de `tasks_of` / `--list` (H13: NO el task=N 1-based
    de startFromTask).

    slim=True propaga `--slim`: archive/restore acumulado lo gestiona el
    orquestador; el launcher de cada task hereda SLIM_IN_PIPELINE=1 y no reconcilia.
    """
    report: dict = {"proj": proj, "from_idx": from_idx, "ok": False, "msg": ""}
    # R7: nunca un segundo orquestador/driver del proyecto ni un sqcli ajeno (antes de tocar nada).
    # in_pipeline=False explícito: lanzar un orquestador desde dentro de un pipeline no es legítimo.
    block = launch_block_reason(proj, in_pipeline=False)
    if block:
        report["msg"] = block
        return report
    rec = reconcile_at_startup(proj)  # arranque de cadena: siempre reconcilia
    if rec["action"] == "blocked":
        report["msg"] = "Reconciliación bloqueada: " + rec["msg"]
        return report
    if rec["action"] == "reconciled":
        report["reconciled"] = rec
    testing = bool(os.environ.get("SLIM_TESTING"))
    if not testing and SP.motor_alive(port):
        report["msg"] = "Ya hay un motor corriendo (instancia única). Usa 'Stop & persist' primero."
        return report
    if not testing and gui_open():
        report["msg"] = gui_block_msg()   # C5: "abierta" o "no pude comprobarlo" (fail-closed)
        return report
    log = Path("/tmp") / f"{proj}_pertask_{int(time.time())}.log"
    try:
        lf = open(log, "w")
        orch_cmd = ["bash", str(ORCHESTRATOR), proj, str(from_idx)]
        if slim:
            orch_cmd.append("--slim")
        proc = subprocess.Popen(orch_cmd,
                                stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True)
        lf.close()  # el hijo tiene su propia copia del fd; cierro en el padre (no fuga)
    except Exception as e:
        report["msg"] = f"No se pudo lanzar el orquestador: {e}"
        return report
    report.update(ok=True, driver_pid=proc.pid, log=str(log),
                  msg=(f"Pipeline lanzado con MOTOR FRESCO POR TASK desde idx {from_idx} "
                       f"(orquestador PID {proc.pid}, slim={slim}). Cada task corre en su "
                       f"propio motor; si algo falla aborta con el índice para reanudar."))
    return report


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Lanzar 1 task SQX headless vía active-flags.")
    ap.add_argument("proj", help="proyecto SQX")
    ap.add_argument("--task", help='title de la task (ver --list)')
    ap.add_argument("--mode", choices=["only", "from"], default="only",
                    help="only = sólo esa task; from = desde esa hasta el final")
    ap.add_argument("--list", action="store_true", help="lista las tasks y sale")
    ap.add_argument("--port", type=int, default=5050, help="puerto HTTP del motor (default 5050)")
    ap.add_argument("--slim", action="store_true",
                    help="modo ahorro RAM: el driver archiva los databanks que la task "
                         "no usa (restore garantizado vía marcador, incluso si el proceso muere)")
    args = ap.parse_args()

    if args.list:
        tasks = tasks_of(args.proj)
        if not tasks:
            print(f"No hay tasks en {args.proj} (¿existe el proyecto?).")
            return 1
        print(f"Tasks de {args.proj} (orden = ejecución):")
        for i, t in enumerate(tasks):
            flag = "" if t["active"] else "  [active=false]"
            print(f"  {i:2}. {t['label']:36} type={t['type']:10} xml={t['xml']}{flag}")
        return 0
    if not args.task:
        ap.error("--task es obligatorio (o usa --list)")
    rep = run(args.proj, args.task, args.mode, port=args.port, slim=args.slim)
    print(("✅ " if rep["ok"] else "⚠ ") + rep["msg"])
    if rep.get("reconciled"):
        r = rep["reconciled"]
        print(f"  ♻ reconciliada corrida previa muerta: restaurados={r.get('restored')} "
              f"saltados={r.get('skipped')}")
    if rep.get("driver_pid"):
        print(f"  driver PID={rep['driver_pid']} · log={rep.get('log')}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(_cli())
