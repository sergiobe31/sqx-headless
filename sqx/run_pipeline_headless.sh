#!/bin/bash
# run_pipeline_headless.sh — lanza un custom project SQX MULTI-TASK (pipeline Build+Filter+Retest)
# de forma headless vía sqcli (motor caliente + HTTP :5050): verifica carga, arranca, poll + persist
# incremental de databanks SIN espacios, y stop graceful + persist final. Reutilizable para cualquier
# proyecto clonado (eurusd_2h, nq_1D, próximos).
#
# Diferencia vs run_miner_headless.sh: éste está pensado para 1 build y sólo persiste `Results`.
# Este script auto-descubre TODOS los databanks del config.xml y persiste los sin espacios, y soporta
# reanudar desde un task concreto (startFromTask) sin repetir los anteriores.
#
# Uso: run_pipeline_headless.sh <proyecto> [puerto=5050] [from_task] [task_idx] [--slim] [--slim-pipeline]
#       from_task = nº de task (1-indexado en config.xml) desde donde arrancar (startFromTask);
#                   si se omite, arranca el pipeline completo (start).
#                   Ej: ... eurusd_2h 5050 2  -> arranca en el task #2 (MR), saltándose el TF.
#       task_idx  = índice 0-based de la task para el PLAN --slim (lo pasa el launcher; default
#                   from_task-1 o 0). Sólo relevante con --slim.
#       --slim            = antes de lanzar el motor, archiva a databanks_archive/ los databanks que
#                           la task no usa (plan de sqx/bank_plan.py) y al final los restaura
#                           (presence-aware, marcador atómico .runner_session.json, trap EXIT).
#                           El restore final NO ocurre con --slim-pipeline: allí es acumulado al
#                           final de la cadena por run_pipeline_per_task.sh (diseño v3: el driver
#                           es dueño único del ciclo, el launcher solo propaga el flag).
#       --slim-pipeline   = este driver es un eslabón de una cadena per-task: deja el marcador
#                           (restore acumulado del orquestador al final de la cadena).
#
# Requiere: GUI de SQX CERRADA y sin sqcli vivo (instancia única). Ver docs/sqx_cli_commands.md §6.2, §9.2.
#
# Landmines:
#  - readiness = sondear hasta "-project action=list" devuelva "List of available projects" (no puerto).
#  - -project action=start / startFromTask es ASYNC: devuelve enseguida; fin = status "not running/finished".
#  - Databanks CON espacios (Second OOS, MC - *, Real OOS…) NO se persisten por HTTP (el %20 trunca
#    el nombre en synctofiles, §6.2) -> se fían a su auto-sync 'every 1 hour' + stop graceful.
#  - Persistir SIEMPRE antes de matar el motor (§6.1): las estrategias están sólo en RAM.
#  - Con --slim el persist SALTA los bancos archivados (M4): persistirlos recrearía dirs vacíos y
#    rompería el restore (gui_slim aborta si el destino existe).
#  - synctofiles es SÍNCRONO aunque responda "Synchronization started." (el motor escribe antes
#    `Databank '<proj>/<banco>' saved - files before sync X / after sync M / ...` en su stdout y la llamada
#    HTTP tarda lo que el sync). Se llama con timeout LARGO (sq_long, SQ_LONG_SECS=1800: con 120 s un sync de
#    un banco gordo se cortaba y el kill posterior lo truncaba) y se CONFIRMA por esa línea del stdout del
#    motor (R1, guardrail sqx-headless-fallos 2026-10-21). Ojo: un banco VACÍO no escribe esa línea (ver
#    sync_bank). Sync FINAL sin confirmar ⇒ el driver NO mata el motor (ni siquiera en el trap EXIT con
#    --slim): lo deja vivo, avisa y marca la task `sync_unconfirmed` (state de la task suelta, o
#    `<proj>_sync_unconfirmed.flag` para el orquestador). Ya no se llama al comando `save` de -databank (sin
#    `folder=` responde "Missing parameter 'folder'": lo que persiste es el synctofiles, R10).
#  - Parada del usuario (R6): `stop_and_persist.py` escribe `<proyecto>/.pipeline_stop_request` y manda un
#    `stop` al motor. El driver lo toma como fin normal ("Project finished": persist + export + kill) y, si la
#    bandera existe al terminar, lo dice (y en task suelta deja state `failed/user_stop`).
#
# Knobs de entorno (defaults = producción):
#  - PROJECTS_DIR: raíz de proyectos SQX (default la app real; los tests usan fixtures en tmp).
#  - SQCLI: binario del motor (default el de la app; los tests usan un mock).
#  - SQX_OUTDIR: raíz de artefactos de corrida (default sqx/output; los tests usan tmp — nunca
#    escribir en sqx/output/ desde los tests).
#  - CARGA_SLEEP/STABLE_N/QUICK_SECS/STOP_SECS: cadencias de las fases de espera.
#  - SQ_LONG_SECS (1800): timeout de curl de synctofiles/export. SYNC_CONFIRM_SECS (= SQ_LONG_SECS): cuánto
#    se espera la línea "Databank '<proj>/<b>' saved" tras cada synctofiles; SYNC_CONFIRM_POLL_SECS (5).
#    KILL_GRACE_SECS (30): gracia entre TERM y kill -9 del motor propio al terminar.
#    POLL_SECS (= POLL_MIN*60): sueño entre sondeos (se interrumpe en cuanto el motor escribe "Project
#    finished", p. ej. tras un Stop & persist).
#  - SLIM_TESTING=1: ⚠ SOLO tests — desactiva los pre-checks de GUI/sqcli (los mocks no son
#    procesos SQX reales).
#  - SQX_ENGINE_PROC_NAME: nombre EXACTO del proceso del motor (default sqcli; solo tests). El motor
#    se detecta SIEMPRE por nombre exacto (`pgrep -x`), nunca por texto de cmdline (R5).
#  - WD_PROC_PATTERN: vigilante de RAM. Vacío (producción) = `pgrep -x sqcli`; definido (tests) =
#    `pgrep -f "$WD_PROC_PATTERN"` apuntando al mock.
set -u
# REPO: si venimos de un self-copy (ver más abajo), BASH_SOURCE apunta a /tmp y el
# cálculo desde dirname daría "/"; el valor resuelto antes del exec viaja en
# SQX_RUNNER_REPO.
REPO="${SQX_RUNNER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Rutas SQX (SQX_APP -> SQCLI, PROJECTS_DIR; respeta los overrides de entorno). Desde $REPO,
# NO desde dirname: tras el self-copy este script corre desde /tmp.
source "$REPO/sqx/paths.sh"
BANK_PLAN="$REPO/sqx/bank_plan.py"
PROJ="${1:?uso: $0 <proyecto> [puerto] [from_task] [task_idx] [--slim] [--slim-pipeline]}"
PORT="${2:-5050}"
FROM_TASK="${3:-}"   # si viene, arranca con startFromTask task=N (saltar tasks previos).
                     # OJO (H13): task=N es el ID 1-based interno de startFromTask — NO el
                     # índice 0-based de `run_task_headless.py --list` / run_pipeline_per_task.sh
TASK_IDX="${4:-}"    # índice 0-based para el plan --slim (lo pasa el launcher exacto)
SLIM=0
SLIM_PIPELINE=0
for a in ${5:-} ${6:-}; do
  case "$a" in
    --slim) SLIM=1 ;;
    --slim-pipeline) SLIM=1; SLIM_PIPELINE=1 ;;
    *) echo "arg desconocido: $a" >&2; exit 2 ;;
  esac
done
if [ -z "$TASK_IDX" ]; then
  TASK_IDX=0
  [ -n "$FROM_TASK" ] && TASK_IDX=$(( FROM_TASK - 1 ))   # aprox. H13: launcher pasa el exacto
fi

# bash re-lee ESTE script por offset de byte durante TODA la corrida (los polls
# duran horas). Si el fichero se reescribe in-place mientras corre (edición,
# git stash pop / checkout de otro chat), el contenido se desplaza bajo el fd y
# el siguiente parseo muere con 'syntax error near unexpected token' a MITAD de
# una línea cualquiera (cazado en producción 2026-10-06: el driver murió tras
# 'proyecto TERMINADO' con un error apuntando a medio comentario; el persist
# final y el stop del motor NO corrieron). bash -n no lo detecta: la sintaxis del
# fichero era válida; lo roto es la re-lectura desplazada. Defensa: ejecutar una
# COPIA estable del propio script (el original puede cambiar sin efecto).
if [ -z "${SQX_RUNNER_SELF_COPY:-}" ]; then
  SELF_COPY=$(mktemp "/tmp/${PROJ}_driver_XXXXXX")
  cp "$0" "$SELF_COPY"
  export SELF_COPY SQX_RUNNER_REPO="$REPO"
  SQX_RUNNER_SELF_COPY=1 exec bash "$SELF_COPY" "$@"
fi
# La marca es solo para ESTE proceso: si la heredan los hijos, el driver no se
# auto-copia y wait_driver del orquestador (que busca la copia "<proj>_driver_")
# lo da por muerto a los 5 s: aborto en falso con el motor vivo (cazado 2026-10-12).
# mktemp de macOS (BSD) solo sustituye las X si van al FINAL: sin sufijo ".sh".
unset SQX_RUNNER_SELF_COPY

# --------------------------- Fase 2: vigilante + state ------------------------
# Telemetría permanente (con o sin --slim) y máquina de estados. UN SOLO ESCRITOR
# del state: este driver en task única; en pipeline (RUNNER_PIPELINE=1) el
# orquestador. El vigilante NUNCA escribe el state: escribe el kill.flag y el
# escritor lo recoge. Ver sqx/run_state.py.
OUTBASE="${SQX_OUTDIR:-$REPO/sqx/output}"
TELEMETRY="$OUTBASE/${PROJ}_telemetry.csv"
STATE_FILE="$OUTBASE/${PROJ}_state.json"
KILLFLAG="$OUTBASE/${PROJ}_kill.flag"
STATE_BIN="$REPO/sqx/run_state.py"
WD_SLEEP_SECS=${WD_SLEEP_SECS:-30}
WD_WARN_RSS_MB=${WD_WARN_RSS_MB:-8192}     # 8 GB
WD_KILL_RSS_MB=${WD_KILL_RSS_MB:-9216}    # 9 GB
WD_FREE_PCT_MIN=${WD_FREE_PCT_MIN:-10}
WD_DERIV_MB_MIN=${WD_DERIV_MB_MIN:-500}   # MB/min, ventana WD_HIST_SECS
WD_HIST_SECS=${WD_HIST_SECS:-300}         # 5 min
WD_GRACE_SECS=${WD_GRACE_SECS:-60}
ENGINE_PROC="${SQX_ENGINE_PROC_NAME:-sqcli}"   # nombre EXACTO del proceso del motor (R5)
# ¿Seguro que NO queda ningún motor? Solo con `pgrep` rc 1 ("ninguno"). rc>1 = pgrep roto = no lo sé = NO libre
# (fail-closed, revisión de código D3 2026-10-25: antes un pgrep roto valía como "libre" y se restauraba el
# --slim o se daba el motor por muerto con el motor quizá vivo).
no_engine_alive(){ pgrep -x "$ENGINE_PROC" >/dev/null 2>&1; [ $? -eq 1 ]; }
WD_PROC_PATTERN=${WD_PROC_PATTERN:-}           # vacío = pgrep -x $ENGINE_PROC; definido = pgrep -f (tests: el mock)
# PIDs del motor que vigila/mata el vigilante de RAM. rc ≠ 0 = `pgrep` ROTO (rc 2/3: regex rechazado, binario
# ausente…; "sin coincidencias" es rc 1 y devuelve 0 con lista vacía): no sabemos si hay motor (C3, fail-closed),
# así que el llamador NO mide ni mata esa ronda y reintenta en la siguiente.
wd_pids(){
  local out rc
  if [ -n "$WD_PROC_PATTERN" ]; then out=$(pgrep -f "$WD_PROC_PATTERN" 2>/dev/null); rc=$?
  else out=$(pgrep -x "$ENGINE_PROC" 2>/dev/null); rc=$?; fi
  [ -n "$out" ] && printf '%s\n' "$out"
  [ "$rc" -gt 1 ] && return "$rc"
  return 0
}
# RUNNER_PIPELINE=1 (lo exporta run_pipeline_per_task.sh) → este driver NO toca
# el state: lo escribe el orquestador. Task única (launcher/app o a mano) → driver.
STATE_WRITER=driver
[ "${RUNNER_PIPELINE:-0}" = 1 ] && STATE_WRITER=none

RUN_ID="${PROJ}_$(date +%s)"
POLL_MIN=${POLL_MIN:-15}
MAXITER=${MAXITER:-48}   # 48 * 15 min = 12 h tope duro por defecto; sobreescribible por env (batería E4 ~11h → MAXITER=64)
CARGA_SLEEP=${CARGA_SLEEP:-5}
STABLE_N=${STABLE_N:-3}
QUICK_SECS=${QUICK_SECS:-30}
STOP_SECS=${STOP_SECS:-10}
POLL_SECS=${POLL_SECS:-$((POLL_MIN * 60))}   # sueño entre sondeos (interrumpible, ver poll_sleep)
SQ_LONG_SECS=${SQ_LONG_SECS:-1800}           # R1: timeout de curl de synctofiles/export (1 banco gordo = minutos)
SYNC_CONFIRM_SECS=${SYNC_CONFIRM_SECS:-$SQ_LONG_SECS}   # espera de la línea "Databank '<p>/<b>' saved"
SYNC_CONFIRM_POLL_SECS=${SYNC_CONFIRM_POLL_SECS:-5}
SYNC_LINE_GRACE_SECS=${SYNC_LINE_GRACE_SECS:-10}  # A8: margen para la línea tras un synctofiles con respuesta rc 0
KILL_GRACE_SECS=${KILL_GRACE_SECS:-30}       # gracia TERM → kill -9 del motor propio al terminar
SCRATCH="/tmp/${PROJ}_run"
LOG="$SCRATCH/monitor.log"; ENG="$SCRATCH/engine.log"
PROJDIR="$PROJECTS_DIR/$PROJ"
CFX="$PROJDIR/project.cfx"
STOP_REQ="$PROJDIR/.pipeline_stop_request"        # R6: bandera de parada (la escribe stop_and_persist.py)
SYNCFLAG="$OUTBASE/${PROJ}_sync_unconfirmed.flag" # R1: señal al orquestador (sync final sin confirmar)
SYNC_UNCONFIRMED=0                                # 1 = sync FINAL sin confirmar: el motor se queda VIVO
USER_STOP=0
mkdir -p "$SCRATCH" || { echo "ABORT: no pude crear $SCRATCH" >&2; exit 1; }   # D4
ts(){ date '+%H:%M:%S'; }
sq(){ curl -s --max-time 120 "http://localhost:$PORT/call?cmd=$(printf '%s' "$1" | sed 's/ /%20/g')"; }
# Igual que sq() pero con timeout LARGO: synctofiles y export de un banco gordo tardan minutos y un curl corto
# los corta a mitad (el motor sigue y el kill posterior trunca el banco: incidente 2.754/4.999). sq() (120 s)
# queda para las consultas rápidas (status/stop/list/count).
sq_long(){ curl -s --max-time "$SQ_LONG_SECS" "http://localhost:$PORT/call?cmd=$(printf '%s' "$1" | sed 's/ /%20/g')"; }
num(){ grep -m1 "$1" | grep -oE '[0-9]+' | head -1; }
# vivo de verdad (un hijo muerto sin reap queda <defunct> y no cuenta)
pid_alive(){ local st; st=$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' '); [ -n "$st" ] && [ "${st#Z}" = "$st" ]; }

# espera (tope SLIM_TRAP_WAIT_SECS, default 300) a que no quede sqcli vivo; 1 = sigue vivo.
# SLIM_TESTING=1: entorno de mocks (puede haber un motor ajeno en la máquina) → no esperar.
wait_free_trap(){
  [ "${SLIM_TESTING:-0}" = 1 ] && return 0
  local n=$(( ${SLIM_TRAP_WAIT_SECS:-300} / 5 )) i
  for i in $(seq 1 "$n"); do no_engine_alive && return 0; sleep 5; done
  return 1
}

# --------------------------- guardado confirmado y kill propio (R1) -----------
# nº de líneas "Databank '<proj>/<banco>' saved" del stdout del motor. SQX la escribe al TERMINAR cada synctofiles
# (el HTTP responde "Synchronization started." pero el comando es síncrono). Con -F y la comilla de cierre el
# banco es EXACTO: `'proj/b'` no casa `'proj/bX'`.
saved_count(){
  local n
  n=$(grep -acF "Databank '$PROJ/$1' saved" "$ENG" 2>/dev/null)
  echo "${n:-0}"
}

# nº de estrategias del banco en la RAM del motor (HTTP `count`: "Records: N"); vacío si no responde.
bank_count(){
  sq "-databank action=count project=$PROJ name=$1" | sed -n 's/.*Records:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1
}

# synctofiles de UN banco CONFIRMADO por el stdout del motor. Cuenta las líneas antes/después y, si no ha
# aparecido una nueva, sondea $ENG cada SYNC_CONFIRM_POLL_SECS hasta SYNC_CONFIRM_SECS (el curl largo pudo
# agotarse con el sync aún en marcha: la línea llega al terminar).
# rc: 0 confirmado · 1 NO confirmado (motor vivo) · 2 motor muerto (no hay nada que confirmar) ·
#     3 banco VACÍO (sin línea que confirmar, ver PLAN-DESVIO).
#
# PLAN-DESVIO (necesario; el plan v2.1 no lo contempla): el motor REAL no escribe la línea
# "Databank '<proj>/<b>' saved …" para un banco VACÍO (synctofiles solo devuelve "Synchronization started.").
# Evidencia: sqx/output/ftmo_cfd_cartera_pmaster2_20261014/engine.log — de los 7 bancos persistibles, `Results` y
# `Carteras_21_s3_g1000` (CSV exportados con solo cabecera = vacíos) no tienen NINGUNA línea saved, y los otros 5
# sí en cada ronda; ningún "after sync 0" en todos los logs. Exigir la línea a un banco vacío dejaría el motor vivo
# (sync_unconfirmed) en cualquier proyecto con un banco vacío —y esperaría SYNC_CONFIRM_SECS por banco en cada
# ronda incremental—. Un `synctofiles` que devolvió rc 0 es SÍNCRONO: si no hay línea nueva ya, no va a llegar
# (el motor la escribe antes de responder), así que se mira el `count`: 0 estrategias ⇒ vacío, nada que
# confirmar (rc 3); >0 o `count` sin respuesta ⇒ se sigue esperando la línea como dice el plan (fail-closed).
sync_bank(){
  local b="$1" n0 n1 rc=0 resp t0 now cnt disk empty_checked=0
  n0=$(saved_count "$b")
  resp=$(sq_long "-databank action=synctofiles project=$PROJ name=$b") || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$(ts) ⚠ synctofiles $b: curl rc=$rc (28 = timeout de ${SQ_LONG_SECS}s; el sync puede seguir en el motor)" | tee -a "$LOG"
  else
    echo "$(ts) synctofiles $b → ${resp:-<vacío>}" | tee -a "$LOG"
  fi
  # PLAN-DESVIO (menor): un error del motor ("…doesn't exist", "Error: …", p. ej. un nombre de banco que el HTTP no
  # resuelve) con rc 0 NO es un sync en marcha: la línea de confirmación no va a llegar → "sin confirmar" de
  # inmediato, sin esperar SYNC_CONFIRM_SECS (30 min) por banco. (Sigue siendo "sin confirmar": no se mata el motor.)
  if [ "$rc" -eq 0 ] && printf '%s' "$resp" | grep -qiE "doesn't exist|^[[:space:]]*error"; then
    echo "$(ts) ⚠ synctofiles $b: el motor respondió con un error: no se espera confirmación" | tee -a "$LOG"
    return 1
  fi
  t0=$(date +%s)
  while true; do
    n1=$(saved_count "$b")
    if [ "$n1" -gt "$n0" ]; then
      echo "$(ts) sync de $b CONFIRMADO: $(grep -aF "Databank '$PROJ/$b' saved" "$ENG" | tail -1)" | tee -a "$LOG"
      return 0
    fi
    pid_alive "${SQPID:-0}" || return 2
    # PLAN-DESVIO (menor): fallo de conexión (rc ≠ 0 y ≠ 28: rechazada / respuesta vacía): la petición no llegó al
    # motor, esperar la confirmación entera no sirve de nada (el plan sondea siempre hasta SYNC_CONFIRM_SECS: con un
    # motor zombi serían 30 min por banco). rc 0 (la línea puede llegar con retraso) y rc 28 (el sync sigue) sí.
    case "$rc" in 0|28) ;; *) return 1 ;; esac
    if [ "$rc" -eq 0 ] && [ "$empty_checked" = 0 ]; then
      empty_checked=1
      cnt=$(bank_count "$b")
      if [ "$cnt" = "0" ]; then
        echo "$(ts) sync de $b: banco VACÍO (count=0): el motor no escribe línea de confirmación para un banco vacío; nada que guardar" | tee -a "$LOG"
        return 3
      fi
    fi
    # A8 (aceptación real 2026-10-27): el synctofiles es SÍNCRONO (con algo que guardar, la línea sale ANTES de la
    # respuesta) y B144 NO escribe línea si no hay nada nuevo que guardar. Con rc 0 y sin línea tras el margen, el sync
    # ya terminó: esperar SYNC_CONFIRM_SECS no sirve. Motor = disco (comprobable) → sin cambios, confirmado; si no cuadra
    # o el count no responde → sin confirmar YA (fail-closed: el motor se deja vivo; un .sqx con varias estrategias
    # cae aquí).
    if [ "$rc" -eq 0 ]; then
      now=$(date +%s)
      if [ $(( now - t0 )) -ge "$SYNC_LINE_GRACE_SECS" ]; then
        disk=$(find "$PROJDIR/databanks/$b" -maxdepth 1 -name '*.sqx' 2>/dev/null | wc -l | tr -d ' ')
        if [ -n "$cnt" ] && [ "$cnt" = "$disk" ]; then
          echo "$(ts) sync de $b sin cambios: el motor no escribe línea si no hay nada que guardar; disco $disk = motor $cnt" | tee -a "$LOG"
          return 3
        fi
        echo "$(ts) ⚠ sync de $b sin línea del motor y motor ≠ disco o sin count (disco $disk .sqx, motor ${cnt:-?}): sin confirmar" | tee -a "$LOG"
        return 1
      fi
    fi
    now=$(date +%s)
    [ $(( now - t0 )) -ge "$SYNC_CONFIRM_SECS" ] && return 1
    sleep "$SYNC_CONFIRM_POLL_SECS"
  done
}

# A4: verificación en disco, FICHEROS contra FICHEROS: el `after sync M` de la línea de confirmación del motor
# contra los .sqx de databanks/<b>/ (no contra `count`, que cuenta estrategias: un .sqx puede llevar varias).
# Aviso, no compuerta.
verify_disk(){
  local b="$1" line m d
  line=$(grep -aF "Databank '$PROJ/$b' saved" "$ENG" 2>/dev/null | tail -1)
  m=$(printf '%s' "$line" | sed -n 's/.*after sync \([0-9][0-9]*\).*/\1/p')
  d=$(find "$PROJDIR/databanks/$b" -maxdepth 1 -name '*.sqx' 2>/dev/null | wc -l | tr -d ' ')
  if [ -z "$m" ]; then
    echo "$(ts) verificación en disco de $b: la línea del motor no trae 'after sync N' (disco: ${d:-0} .sqx)" | tee -a "$LOG"
  elif [ "${d:-0}" -lt "$m" ]; then
    echo "$(ts) ⚠ verificación en disco: $b tiene ${d:-0} .sqx en disco pero el motor declaró 'after sync $m' — revisa el banco antes de abrir la GUI (aviso, no compuerta)" | tee -a "$LOG"
  else
    echo "$(ts) verificación en disco OK: $b ${d:-0} .sqx (motor: after sync $m)" | tee -a "$LOG"
  fi
}

# PIDs del motor PROPIO de este driver: $SQPID (el sqcli que lanzó el pipeline) y los motores de nombre exacto de
# MI grupo de procesos (solo si soy líder de grupo: el lanzador me crea con start_new_session, pgid = mi PID; un
# driver lanzado a mano comparte grupo con su shell y entonces solo cuenta $SQPID). NUNCA un motor ajeno.
own_engine_pids(){
  local pids="" p mypg list rc
  [ -n "${SQPID:-}" ] && pid_alive "$SQPID" && pids="$SQPID"
  mypg=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
  if [ "$mypg" = "$$" ]; then
    list=$(pgrep -x "$ENGINE_PROC" 2>/dev/null); rc=$?
    # D1 (revisión de código 2026-10-25): pgrep roto = no sé qué motores de mi grupo quedan; se avisa en el log
    # (no se da por limpio en silencio). $SQPID sigue contando.
    [ "$rc" -gt 1 ] && echo "$(ts) ⚠ pgrep falló (rc=$rc) enumerando los motores de mi grupo: solo cuento \$SQPID" >>"$LOG"
    for p in $list; do
      [ "$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')" = "$$" ] || continue
      case " $pids " in *" $p "*) ;; *) pids="$pids $p" ;; esac
    done
  fi
  echo "${pids# }"
}

# Kill final del motor PROPIO: TERM → gracia KILL_GRACE_SECS → kill -9 si resiste (SIGTERM no basta con zombis,
# landmine fitab). Sustituye al `pkill -9 -x` global del camino zombi (mataba también motores ajenos).
kill_own_engine(){
  local pids alive p i n
  pids=$(own_engine_pids)
  [ -n "$pids" ] && kill -TERM $pids 2>/dev/null
  [ -n "${TAILPID:-}" ] && kill "$TAILPID" 2>/dev/null
  [ -z "$pids" ] && return 0
  n=$(( KILL_GRACE_SECS * 5 ))
  alive=""
  for i in $(seq 1 "$n"); do
    alive=""
    for p in $pids; do pid_alive "$p" && alive="$alive $p"; done
    [ -z "$alive" ] && break
    sleep 0.2
  done
  if [ -n "$alive" ]; then
    echo "$(ts) motor sigue vivo tras TERM + ${KILL_GRACE_SECS}s → kill -9 a:$alive" | tee -a "$LOG"
    kill -9 $alive 2>/dev/null
    sleep 1
  fi
  return 0
}

# Sueño entre sondeos INTERRUMPIBLE: despierta en cuanto el motor escribe "Project finished" (p. ej. tras un
# Stop & persist: el driver persiste enseguida en vez de dormir hasta 15 min). $1 = segundos.
# PLAN-DESVIO (menor): el plan no toca la cadencia del poll; sin esto la parada delegada (R6) tardaría hasta
# POLL_MIN minutos en que el driver notara el fin del proyecto.
poll_sleep(){
  local total="$1" waited=0 step
  while [ "$waited" -lt "$total" ]; do
    grep -aq "Project finished" "$ENG" 2>/dev/null && return 0
    step=5; [ $(( total - waited )) -lt 5 ] && step=$(( total - waited ))
    sleep "$step"; waited=$(( waited + step ))
  done
  return 0
}

# R9: veredicto "¿ya puedes abrir la GUI?" de una task SUELTA. Solo con motor muerto + sin marcador slim + sin
# .bak pendiente. Imprime los motivos (rc 1) o nada (rc 0). Un pgrep roto cuenta como "no sé" (inseguro).
gui_verdict(){
  local engines why="" rc
  engines=$(pgrep -x "$ENGINE_PROC" 2>/dev/null); rc=$?
  if [ "$rc" -gt 1 ]; then why="no pude comprobar si hay un sqcli vivo (pgrep rc=$rc)"
  elif [ -n "$engines" ]; then why="hay un sqcli vivo (pids $(printf '%s' "$engines" | tr '\n' ' '))"; fi
  # (`bank_plan --marker-banks` devuelve rc 0 también SIN marcador: se mira el fichero, igual que pre_gui_check)
  if [ -f "$PROJDIR/databanks_archive/.runner_session.json" ]; then
    why="${why:+$why; }hay una corrida --slim sin restaurar (marcador databanks_archive/.runner_session.json)"
  elif [ -n "$(find "$PROJDIR/databanks_archive" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)" ]; then
    why="${why:+$why; }hay bancos archivados en databanks_archive/"
  fi
  [ -f "$PROJDIR/project.cfx.pre_run_task.bak" ] && why="${why:+$why; }queda project.cfx.pre_run_task.bak pendiente"
  [ "$SYNC_UNCONFIRMED" = 1 ] && why="${why:+$why; }el sync final no se confirmó (motor dejado vivo)"
  [ -z "$why" ] && return 0
  echo "$why"
  return 1
}

# --------------------------- vigilante compuesto (Fase 2) ---------------------
# % RAM libre anclado al label oficial (no un grep de [0-9]+% a ciegas).
mem_free_pct(){ memory_pressure -Q 2>/dev/null | awk -F': ' '/^System-wide memory free percentage/ {gsub(/%/,"",$2); print $2}'; }
# swap usado en MB desde sysctl vm.swapusage ("total = X used = Y free = Z").
swap_used_mb(){ sysctl -n vm.swapusage 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="used") {split($(i+2),a,"M"); print a[1]}}'; }

# KILL absoluto (la red real): kill.flag atómico (tmp+rename) -> SIGTERM a TODOS
# los procesos del patrón -> gracia -> SIGKILL a los que resistan.
wd_kill(){
  local reason="$1" pid="$2" tmp pids rc
  pids=$(wd_pids); rc=$?
  if [ "$rc" -ne 0 ]; then
    # C3: pgrep roto → no sé qué matar; NO declaro el kill (el kill.flag diría "ram_kill" sin que haya habido
    # kill) y el vigilante reintenta en la ronda siguiente
    echo "$(ts) WATCHDOG KILL ABORTADO ($reason): pgrep falló (rc=$rc), no puedo identificar el motor; reintento en la siguiente ronda" | tee -a "$LOG"
    return 1
  fi
  echo "$(ts) WATCHDOG KILL: $reason -> SIGTERM a [${pids:-ninguno}] (gracia ${WD_GRACE_SECS}s)" | tee -a "$LOG"
  tmp="$KILLFLAG.tmp.$$"
  { echo "cause=ram_kill"; echo "reason=$reason"; echo "ts=$(date '+%F %T')"; } > "$tmp" \
    && mv "$tmp" "$KILLFLAG"
  [ -n "$pids" ] && kill -TERM $pids 2>/dev/null
  sleep "$WD_GRACE_SECS"
  pids=$(wd_pids); rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$(ts) WATCHDOG KILL: pgrep falló (rc=$rc) tras la gracia; no puedo comprobar si el motor resiste" | tee -a "$LOG"
    return 0
  fi
  if [ -n "$pids" ]; then
    echo "$(ts) WATCHDOG KILL: [${pids}] resiste tras la gracia -> SIGKILL" | tee -a "$LOG"
    kill -9 $pids 2>/dev/null
  fi
}

# true si a está dentro de ±5% de b (RSS "plano" entre dos muestras consecutivas)
flat5() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(b*0.95 <= a && a <= b*1.05)}'; }

# Telemetría permanente + señales. La derivada solo se evalúa tras la barrera de
# carga ($SCRATCH/post_load.flag, creada tras estabilizar "In databank") y con
# RSS plano ±5% en las 2 muestras previas (para no disparar durante carga lazy).
watchdog(){
  mkdir -p "$OUTBASE"
  [ -f "$TELEMETRY" ] || echo "ts,task,pid,rss_mb,free_pct,swap_used_mb" >> "$TELEMETRY"
  local hist="" pid rss_kb rss_mb free swap now old_ts old_rss warn oldest prev p1 p2 r0 r1 r2 span deriv_mb_min wd_all wd_rc
  # Si el driver muere (kill -9 no dispara trap), el vigilante se apaga. `$$` y no `$PPID`: en
  # este subshell `$PPID` es el PADRE del driver (run_task_headless.py, que sale en cuanto la task
  # arranca) y el vigilante se apagaba tras la 1.ª muestra (2026-10-21, tests f2g/f2h).
  local driver_pid=$$
  while true; do
    kill -0 "$driver_pid" 2>/dev/null || return 0
    wd_all=$(wd_pids); wd_rc=$?
    if [ "$wd_rc" -ne 0 ]; then
      # C3: pgrep roto (no "sin coincidencias"): no sé si el motor vive → esta ronda no mido ni mato; reintento
      echo "$(ts) WATCHDOG: pgrep falló (rc=$wd_rc): no sé si el motor vive; esta ronda no mido ni mato, reintento en ${WD_SLEEP_SECS}s"
      sleep "$WD_SLEEP_SECS"
      continue
    fi
    pid=$(printf '%s\n' "$wd_all" | head -1)
    rss_kb=$(ps -o rss= -p "${pid:-0}" 2>/dev/null | tr -d ' ')
    rss_mb=$(( ${rss_kb:-0} / 1024 ))
    free=$(mem_free_pct); free=${free:-NA}
    swap=$(swap_used_mb); swap=${swap:-NA}
    printf '%s,%s,%s,%s,%s,%s\n' "$(date '+%F %T')" "$TASK_KEY" "${pid:-0}" \
      "$rss_mb" "$free" "$swap" >> "$TELEMETRY"
    now=$(date +%s)
    if [ -n "$pid" ] && [ "$rss_mb" -gt "$WD_KILL_RSS_MB" ]; then
      wd_kill "RSS ${rss_mb} MB > ${WD_KILL_RSS_MB} MB" "$pid" || sleep "$WD_SLEEP_SECS"   # rc≠0: kill abortado por pgrep roto
      continue
    fi
    if [ "$free" != "NA" ] && [ "${free%.*}" -lt "$WD_FREE_PCT_MIN" ]; then
      wd_kill "RAM libre ${free}% < ${WD_FREE_PCT_MIN}%" "$pid" || sleep "$WD_SLEEP_SECS"
      continue
    fi
    warn=""
    if [ "$rss_mb" -gt "$WD_WARN_RSS_MB" ]; then
      warn="RSS ${rss_mb} MB > ${WD_WARN_RSS_MB} MB"
    elif [ -f "$SCRATCH/post_load.flag" ]; then
      # derivada sobre la ventana WD_HIST_SECS, solo con RSS plano ±5% en las
      # 2 muestras previas
      oldest=$(printf '%s\n' $hist | head -1)
      if [ -n "$oldest" ]; then
        old_ts=${oldest%%:*}; old_rss=${oldest##*:}
        prev=$(printf '%s\n' $hist | tail -2)             # 2 muestras previas
        p1=$(printf '%s\n' $prev | head -1); p2=$(printf '%s\n' $prev | tail -1)
        if [ -n "$p1" ] && [ -n "$p2" ] && [ "$p1" != "$p2" ]; then
          r0=${p1##*:}; r1=${p2##*:}; r2=$rss_mb
          span=$(( now - old_ts ))
          if [ "$span" -ge "$WD_HIST_SECS" ]; then
            deriv_mb_min=$(( (rss_mb - old_rss) * 60 / span ))
            if flat5 "$r1" "$r0" && flat5 "$r2" "$r1" && [ "$deriv_mb_min" -gt "$WD_DERIV_MB_MIN" ]; then
              warn="derivada ${deriv_mb_min} MB/min > ${WD_DERIV_MB_MIN} MB/min (ventana ${WD_HIST_SECS}s, post-carga, plano ±5%)"
            fi
          fi
        fi
      fi
    fi
    [ -n "$warn" ] && echo "$(ts) WATCHDOG WARN: $warn (no mata)" | tee -a "$LOG"
    hist="$hist $now:$rss_mb"
    hist=$(printf '%s\n' $hist | awk -F: -v min=$(( now - WD_HIST_SECS )) '$1 >= min' | tr '\n' ' ')
    sleep "$WD_SLEEP_SECS"
  done
}

# task key de la telemetría/state: "idx:label" (una sola llamada al plan).
TASK_KEY="${TASK_IDX}:$(python3 -c "
import json, subprocess, sys
out = subprocess.run([sys.executable, '$BANK_PLAN', '$PROJDIR', '--task-idx', '$TASK_IDX'],
                     capture_output=True, text=True)
try:
    print(json.loads(out.stdout).get('task', '?'))
except Exception:
    print('?')
" 2>/dev/null || echo '?')"

# trap EXIT (M3): si quedó marcador --slim y NO somos eslabón de cadena, matar motor → esperar →
# restaurar (presence-aware); si el motor no muere, NO restaurar y dejar el marcador para la
# reconciliación del próximo arranque (B2).
# trap EXIT (M3): matar el vigilante; si quedó marcador --slim y NO somos eslabón
# de cadena, matar motor → esperar → restaurar (presence-aware); si el motor no
# muere, NO restaurar y dejar el marcador para la reconciliación del próximo
# arranque (B2).
# A1 (R1): con el sync FINAL sin confirmar (SYNC_UNCONFIRMED=1) el trap NO mata el motor (ni $SQPID ni el
# $TAILPID, que es su stdin: matarlo le da EOF) aunque haya marcador slim, y NO restaura los bancos apartados:
# el motor sigue vivo con datos que pueden no estar en disco. El marcador se queda; lo recogerá la
# reconciliación del próximo arranque (tras Stop & persist) o `gui_slim restore`.
on_exit(){
  local rc=$?
  rm -f "${SELF_COPY:-}" 2>/dev/null   # copia estable del propio script (defensa re-parseo)
  kill "${WDPID:-}" 2>/dev/null        # vigilante (Fase 2)
  if [ "${SYNC_UNCONFIRMED:-0}" = 1 ]; then
    echo "$(ts) trap EXIT: sync final NO confirmado — NO mato el motor ni restauro el --slim (el motor sigue vivo; queda el marcador si lo había)" >>"$LOG" 2>&1
  elif [ "$SLIM" = 1 ] && [ "$SLIM_PIPELINE" != 1 ] \
     && python3 "$BANK_PLAN" "$PROJDIR" --marker-banks >/dev/null 2>&1; then
    echo "$(ts) trap EXIT: hay marcador slim — mato motor y restauro bancos" >>"$LOG" 2>&1
    # D2 (revisión de código 2026-10-25): TERM → gracia → kill -9 por PID/grupo propios, como el kill final
    # (antes solo TERM: con un motor zombi no basta, landmine fitab)
    kill_own_engine >/dev/null 2>&1
    wait_free_trap || true
    if [ "${SLIM_TESTING:-0}" = 1 ] || no_engine_alive; then
      python3 "$BANK_PLAN" "$PROJDIR" --slim-restore >>"$LOG" 2>&1 || true
    else
      echo "$(ts) motor sigue vivo tras la espera: NO restauro; queda el marcador para la reconciliación del próximo arranque (M3)" >>"$LOG" 2>&1
    fi
  fi
  exit $rc
}
trap on_exit EXIT

echo "=== $(ts) LAUNCH $PROJ (from_task=${FROM_TASK:-completo}, slim=$SLIM pipeline=$SLIM_PIPELINE, task=$TASK_KEY) ===" | tee "$LOG"

# Fase 2: reconciliación de stale (corrida anterior muerta a mitad). Solo el
# escritor del state la hace (en pipeline la hace el orquestador al arranque).
[ "$STATE_WRITER" = driver ] && python3 "$STATE_BIN" "$STATE_FILE" reconcile >/dev/null 2>&1 || true
# señal `sync_unconfirmed` rancia de una corrida anterior: solo la limpia el escritor del state (en pipeline la
# limpia el orquestador al arrancar la cadena y la consume tras cada task)
[ "$STATE_WRITER" = driver ] && rm -f "$SYNCFLAG"
rm -f "$SCRATCH/post_load.flag"   # la barrera de carga se supera de nuevo en ESTA corrida

# Databanks del config.xml; partir sin/con espacios (HTTP sólo aguanta sin espacios)
ALL_BANKS=$(python3 -c "
import zipfile,re,sys
z=zipfile.ZipFile(sys.argv[1]); cfg=z.read('config.xml').decode('utf-8','replace')
print(';'.join(re.findall(r'<Databank name=\"([^\"]+)\"', cfg)))
" "$CFX" 2>/dev/null)
BANKS_HTTP=$(printf '%s' "$ALL_BANKS" | tr ';' '\n' | grep -v ' ' | tr '\n' ' ')
BANKS_AUTOSYNC=$(printf '%s' "$ALL_BANKS" | tr ';' '\n' | grep ' ' | tr '\n' ',' | sed 's/,$//')
echo "$(ts) databanks HTTP-persist (sin espacios): ${BANKS_HTTP:-ninguno}" | tee -a "$LOG"
echo "$(ts) databanks auto-sync (con espacios): ${BANKS_AUTOSYNC:-ninguno}" | tee -a "$LOG"

# 0. landmine §6.2
if [ "${SLIM_TESTING:-0}" != 1 ]; then
  if pgrep -f "StrategyQuantX_ui" >/dev/null || pgrep -f "StrategyQuantX_nocheck" >/dev/null || pgrep -f "StrategyQuantX[^/]*[.]app/Contents/MacOS" >/dev/null; then
    echo "$(ts) ABORT: la GUI de SQX está abierta (corrompería databanks). Ciérrala." | tee -a "$LOG"; exit 1; fi
  if pgrep -x "$ENGINE_PROC" >/dev/null; then
    echo "$(ts) ABORT: ya hay un sqcli corriendo (instancia única)." | tee -a "$LOG"; exit 1; fi
fi

# 0slim. archive pre-motor (punto 1 del driver; guarda L4 satisfecha: motor muerto confirmado arriba).
# El marcador se escribe ANTES de mover nada (kill -9 a mitad = reconciliable). Si el archive
# falla a medias, slim_archive hace rollback de lo movido y aborta (M10).
ARCHIVED_BANKS=""
if [ "$SLIM" = 1 ]; then
  echo "$(ts) SLIM: plan de archive para task_idx=$TASK_IDX (run_id=$RUN_ID)" | tee -a "$LOG"
  PIPELINE_FLAG=""
  [ "$SLIM_PIPELINE" = 1 ] && PIPELINE_FLAG="--pipeline"
  # rc de python3, no del pipe: con '| tee' un fallo de bank_plan quedaría enmascarado
  if ! SLIM_ARCHIVE_OUT=$(python3 "$BANK_PLAN" "$PROJDIR" --task-idx "$TASK_IDX" \
        --slim-archive --run-id "$RUN_ID" --pid $$ $PIPELINE_FLAG 2>&1); then
    printf '%s\n' "$SLIM_ARCHIVE_OUT" | tee -a "$LOG"
    echo "$(ts) ABORT: slim-archive falló (rollback aplicado por bank_plan)" | tee -a "$LOG"; exit 1
  fi
  printf '%s\n' "$SLIM_ARCHIVE_OUT" | tee -a "$LOG"
  # L3 por task: restaurar los inputs de ESTA task si quedaron archivados en una
  # iteración anterior de la cadena (pipeline per-task).
  if ! RESTORE_IN_OUT=$(python3 "$BANK_PLAN" "$PROJDIR" --task-idx "$TASK_IDX" \
        --slim-restore-inputs 2>&1); then
    printf '%s\n' "$RESTORE_IN_OUT" | tee -a "$LOG"
    echo "$(ts) ABORT: slim-restore-inputs falló" | tee -a "$LOG"; exit 1
  fi
  printf '%s\n' "$RESTORE_IN_OUT" | tee -a "$LOG"
  ARCHIVED_BANKS=$(python3 "$BANK_PLAN" "$PROJDIR" --marker-banks 2>/dev/null || true)
  echo "$(ts) SLIM: archivados ahora: ${ARCHIVED_BANKS:-ninguno}" | tee -a "$LOG"
fi

# 1. motor caliente (stdin abierto -> interactivo + HTTP :PORT vivo)
tail -f /dev/null | "$SQCLI" > "$ENG" 2>&1 &
SQPID=$!
# el tail del pipeline sobrevive al sqcli (nunca escribe -> no recibe SIGPIPE);
# lo registramos para matarlo al salir y no dejar el fd del log abierto.
TAILPID=$(pgrep -P $$ -f "tail -f /dev/null" | head -1)
# Fase 2: vigilante compuesto (telemetría permanente + WARN/KILL). Corre también
# sin --slim. En tests, WD_PROC_PATTERN apunta al mock y los umbrales/sleeps se
# comprimen vía env. Ojo: su stdout va al LOG (no al del driver): si heredara el
# pipe del driver, su 'sleep' retenía el EOF 30 s al acabar cada corrida.
watchdog >>"$LOG" 2>&1 & WDPID=$!
echo "$(ts) sqcli PID=$SQPID; esperando MOTOR listo..." | tee -a "$LOG"
READY=0
for i in $(seq 1 60); do
  sq "-project action=list" | grep -q "List of available projects" && { READY=1; break; }
  sleep 2
done
[ "$READY" = 1 ] || { echo "$(ts) ERROR: el motor no quedó listo" | tee -a "$LOG"; kill "$SQPID" 2>/dev/null; [ -n "${TAILPID:-}" ] && kill "$TAILPID" 2>/dev/null; exit 1; }
echo "$(ts) motor LISTO." | tee -a "$LOG"

# 2. status previo (verifica que el proyecto existe y carga -> sin 'unresolved resources')
echo "$(ts) status previo de $PROJ:" | tee -a "$LOG"
sq "-project action=status name=$PROJ" | tee -a "$LOG"

# 2b. barrera de CARGA: el motor responde HTTP mientras sigue cargando databanks en
# background (lazy). Si el START llega antes de que el banco input de la primera task
# esté cargado, la task corre con input VACÍO -> "No strategies to retest" + Project
# finished inmediato (cazado 2026-09-28 en gold_h4_prod_oos_retry task Real_OOS: 181 de
# 680 cargadas al mandar start). Esperar a que 'In databank' se estabilice (STABLE_N
# lecturas iguales cada CARGA_SLEEP), timeout ~5 min.
PREV=""; STABLE=0; LOADED=0
for i in $(seq 1 60); do
  CUR=$(sq "-project action=status name=$PROJ" | grep -m1 "In databank" | tr -d ',' | grep -oE '[0-9]+' | head -1)
  if [ -n "$CUR" ] && [ "$CUR" = "$PREV" ]; then
    STABLE=$((STABLE + 1))
    [ "$STABLE" -ge "$STABLE_N" ] && { LOADED=1; break; }
  else
    STABLE=0
  fi
  PREV=$CUR
  sleep "$CARGA_SLEEP"
done
[ "$LOADED" = 1 ] || { echo "$(ts) ERROR: carga de databanks no se estabilizó en 5 min (último In databank=${CUR:-?})" | tee -a "$LOG"; kill "$SQPID" 2>/dev/null; [ -n "${TAILPID:-}" ] && kill "$TAILPID" 2>/dev/null; exit 1; }
echo "$(ts) databanks cargados (In databank=$CUR estable)." | tee -a "$LOG"
touch "$SCRATCH/post_load.flag"   # Fase 2: a partir de aquí el vigilante evalúa la derivada

# 3. START (async): startFromTask si from_task, si no start completo
if [ -n "$FROM_TASK" ]; then
  echo "$(ts) >>> START FROM TASK #$FROM_TASK $PROJ" | tee -a "$LOG"
  sq "-project action=startFromTask name=$PROJ task=$FROM_TASK" | tee -a "$LOG"
else
  echo "$(ts) >>> START pipeline $PROJ" | tee -a "$LOG"
  sq "-project action=start name=$PROJ" | tee -a "$LOG"
fi
# Fase 2: la task arranca -> running (solo el escritor del state; en pipeline lo
# pone el orquestador antes de lanzar).
[ "$STATE_WRITER" = driver ] && python3 "$STATE_BIN" "$STATE_FILE" set \
  --task "$TASK_KEY" --status running --pid $$ >/dev/null 2>&1 || true

# 3b. cheque rápido: ¿genera? (6 x QUICK_SECS)
for q in $(seq 1 6); do
  sleep "$QUICK_SECS"
  # H2 (debate 2026-09-27): una task corta (p.ej. retest de 76 s) puede terminar DURANTE
  # la fase quick; sin este check su fin no se detectaba hasta el primer poll de 15 min
  # (~18 min mínimo por task, ~3.3 h de overhead en un pipeline de 11 tasks).
  grep -aq "Project finished" "$ENG" && break
  ST=$(sq "-project action=status name=$PROJ")
  GEN=$(printf '%s' "$ST" | num "Strategies generated")
  echo "$(ts) quick#$q generated=${GEN:-0}" | tee -a "$LOG"
done

# 4. poll + persist incremental (si terminó en la fase quick, no hay nada que pollear)
i=0
ZOMBIE=0
if grep -aq "Project finished" "$ENG"; then
  echo "$(ts) proyecto TERMINADO (engine stdout, fase quick)" | tee -a "$LOG"
  FIN_NOTED=1
fi
while [ $i -lt $MAXITER ] && ! grep -aq "Project finished" "$ENG"; do
  poll_sleep "$POLL_SECS"; i=$((i + 1))
  # despertar anticipado: el motor escribió "Project finished" durante el sueño (p. ej. un Stop & persist le
  # mandó stop) → sin sondeo ni persist incremental: el persist FINAL de abajo guarda lo mismo, ya
  if grep -aq "Project finished" "$ENG"; then
    echo "$(ts) proyecto TERMINADO (engine stdout, despertar anticipado)" | tee -a "$LOG"; FIN_NOTED=1; break
  fi
  ST=$(sq "-project action=status name=$PROJ")
  GEN=$(printf '%s' "$ST" | num "Strategies generated")
  INDB=$(printf '%s' "$ST" | num "In databank")
  echo "$(ts) [#$i] generated=${GEN:-?} in_databank=${INDB:-?}" | tee -a "$LOG"
  # H5 (debate 2026-09-27): liveness ENTRE saves — un motor zombi (HTTP ciego con
  # proceso vivo, como el incidente gold_h4_prod_oos 2026-09-27) encadenaba
  # 2 HTTP × banco × 120 s de stall (~40 min/iteración) sin que el driver lo percibiera.
  # OJO: HTTP ciego ≠ zombi — bajo saturación el status también devuelve vacío con el
  # motor TRABAJANDO (landmine fitab 2026-09-11) → distinguir por CPU antes de matar.
  DEAD=0
  for b in $BANKS_HTTP; do
    if [ "$SLIM" = 1 ] && printf '%s\n' "$ARCHIVED_BANKS" | grep -qxF -- "$b"; then
      echo "$(ts) skip persist $b (archivado --slim, M4)" | tee -a "$LOG"
      continue
    fi
    if [ -z "$(curl -s --max-time 3 "http://localhost:$PORT/call?cmd=-project%20action=list")" ]; then
      DEAD=$((DEAD + 1))
      if [ "$DEAD" -ge 2 ]; then
        SQPID_ALIVE=$(pgrep -x "$ENGINE_PROC" | head -1)
        CPU=$(ps -o %cpu= -p "${SQPID_ALIVE:-0}" 2>/dev/null | tr -d ' ')
        if [ -z "$SQPID_ALIVE" ] || awk "BEGIN{exit !(${CPU:-0} < 5)}"; then
          ZOMBIE=1
          echo "$(ts) ⚠ motor no responde 2 probes y CPU=${CPU:-0}% (colgado) — declarado ZOMBI" | tee -a "$LOG"
        else
          echo "$(ts) ⚠ HTTP ciego pero CPU=${CPU}% — motor SATURADO trabajando; omito el persist de esta iteración" | tee -a "$LOG"
        fi
        break
      fi
    else
      DEAD=0
    fi
    # R1/R10: solo synctofiles (el `save` sin folder= nunca persistió nada), con timeout largo y confirmado por el
    # stdout del motor. Incremental: sin confirmar solo avisa y sigue (el persist FINAL es el que decide).
    sync_bank "$b"; SRC=$?
    case "$SRC" in
      0|3) ;;
      1) echo "$(ts) ⚠ sync incremental de $b NO confirmado (sigo; el persist final decide)" | tee -a "$LOG" ;;
      2) echo "$(ts) ⚠ motor muerto durante el sync incremental de $b" | tee -a "$LOG"; break ;;
    esac
  done
  # fin fiable = "Project finished" en el STDOUT del motor (el status HTTP no siempre
  # reporta el fin — landmine 2026-08-30; motor muerto = status vacío, 2026-09-02)
  grep -aq "Project finished" "$ENG" && {
    echo "$(ts) proyecto TERMINADO (engine stdout)" | tee -a "$LOG"; FIN_NOTED=1; break; }
  # Fase 2: el vigilante mató el motor por RAM -> salir del poll ya (el cierre
  # sin persist HTTP ocurre tras el while, al recoger el kill.flag)
  [ -f "$KILLFLAG" ] && { echo "$(ts) ⚠ KILL FLAG del vigilante RAM — salto del poll al cierre" | tee -a "$LOG"; break; }
  [ "$ZOMBIE" = 1 ] && {
    echo "$(ts) ⚠ MOTOR ZOMBI (HTTP ciego, proceso vivo) — salto a cierre con kill -9" | tee -a "$LOG"; break; }
  if [ -z "$ST" ] && no_engine_alive; then
    echo "$(ts) ⚠ MOTOR MUERTO (status vacío + sin proceso) — salto a persist de emergencia" | tee -a "$LOG"; ENGINE_DIED=1; break
  fi
  printf '%s' "$ST" | grep -qiE "not running|is finished|project .*finished" && {
    echo "$(ts) proyecto TERMINADO (status)" | tee -a "$LOG"; FIN_NOTED=1; break; }
done

# Cierre de la detección del fin: si el while salió por su CONDICIÓN (el
# "Project finished" apareció entre iteraciones, tras el grep del cuerpo), el
# mensaje no se llegó a imprimir y el orquestador clasificaría la task como
# "no terminó limpio" -> imprimirlo aquí (carrera cazada en tests 2026-10-06).
if [ "${FIN_NOTED:-0}" != 1 ] && [ "$ZOMBIE" != 1 ] && [ "${ENGINE_DIED:-0}" != 1 ] \
   && grep -aq "Project finished" "$ENG"; then
  echo "$(ts) proyecto TERMINADO (engine stdout, condición del poll)" | tee -a "$LOG"
fi

# Fase 2: kill.flag del vigilante (RAM). El vigilante ya mató el motor (o lo está
# matando): cierre sin persist HTTP, igual que el camino zombi. En pipeline el
# flag lo recoge el orquestador; en task única lo recoge este driver.
RAM_KILL=0
if [ -f "$KILLFLAG" ]; then
  RAM_KILL=1
  echo "$(ts) ⚠ KILL FLAG del vigilante RAM ($(head -1 "$KILLFLAG")) — cierre sin persist HTTP" | tee -a "$LOG"
  [ "$STATE_WRITER" = driver ] && rm -f "$KILLFLAG"   # el orquestador conserva el suyo
fi

# 5. stop graceful + persist final (en zombi/kill-flag el HTTP colgaría 120 s ×
# llamada: se omite — los datos viven en el último sync incremental a disco,
# landmine gold_h4_prod_oos 2026-09-27)
echo "$(ts) STOP graceful + persist final (zombie=$ZOMBIE ram_kill=$RAM_KILL)" | tee -a "$LOG"
if [ "$ZOMBIE" = 0 ] && [ "$RAM_KILL" = 0 ]; then
  sq "-project action=stop name=$PROJ" | tee -a "$LOG"
  sleep "$STOP_SECS"
fi
OUTDIR="${SQX_OUTDIR:-$REPO/sqx/output}/${PROJ}_$(date +%Y%m%d)"
mkdir -p "$OUTDIR"
UNCONFIRMED_BANKS=""
if [ "$ZOMBIE" = 0 ] && [ "$RAM_KILL" = 0 ]; then
  for b in $BANKS_HTTP; do
    if [ "$SLIM" = 1 ] && printf '%s\n' "$ARCHIVED_BANKS" | grep -qxF -- "$b"; then
      echo "$(ts) skip persist final $b (archivado --slim, M4)" | tee -a "$LOG"
      continue
    fi
    # R1/R10: solo synctofiles (el `save` sin folder= nunca persistió nada), timeout largo y CONFIRMADO por la
    # línea "Databank '<proj>/<b>' saved" del stdout del motor. Sin confirmar → el motor NO se mata (abajo).
    sync_bank "$b"; SRC=$?
    case "$SRC" in
      0) verify_disk "$b" ;;
      3) ;;   # banco vacío o sin cambios (A8): sin línea del motor y disco = motor
      1) SYNC_UNCONFIRMED=1; UNCONFIRMED_BANKS="$UNCONFIRMED_BANKS $b"
         echo "$(ts) ⚠ sync de $b NO confirmado: dejo el motor vivo (persiste con stop_and_persist.py o revisa)" | tee -a "$LOG" ;;
      2) ENGINE_DIED=1
         echo "$(ts) ⚠ motor muerto: no se pudo sincronizar $b (lo guardado es lo del último sync incremental)" | tee -a "$LOG"
         continue ;;   # sin motor tampoco hay export
    esac
    # protocolo post-build (AGENTS.md): CSV de métricas IS/OOS/full por estrategia.
    # OJO docs/sqx_cli_commands.md §252: export de TRADES sale vacío; esto es la vista
    # de métricas del databank, que es lo que E1/análisis necesitan (trades: extract_sqx.py).
    # Timeout largo; si falla solo avisa: es un derivado, no compuerta.
    sq_long "-databank action=export project=$PROJ name=$b file=$OUTDIR/databank_$b.csv" >/dev/null; XRC=$?
    [ "$XRC" -ne 0 ] && echo "$(ts) ⚠ export de $b falló (curl rc=$XRC): el CSV es un derivado, no compuerta" | tee -a "$LOG"
  done
fi
# diagnósticos de presión de selección (file-based, del stdout del motor)
{
  echo "# diagnósticos $PROJ $(date '+%Y-%m-%d %H:%M')"
  echo "restarts ga: $(grep -ac 'restart' "$ENG" 2>/dev/null || echo 0)"
  echo "últimas líneas de stats del motor:"
  grep -aE "Strategies generated|In databank|Accepted strategies per hour" "$ENG" 2>/dev/null | tail -6
} > "$OUTDIR/diagnostico_presion.txt"
cp "$LOG" "$OUTDIR/monitor.log" 2>/dev/null; cp "$ENG" "$OUTDIR/engine.log" 2>/dev/null

# Kill final del motor PROPIO: TERM → gracia KILL_GRACE_SECS → kill -9 por PID/grupo propios (kill_own_engine;
# ya no hay `pkill -9 -x` global). R1: con el sync FINAL sin confirmar NO se mata (ni el $TAILPID, su stdin):
# el motor se queda vivo con datos que pueden no estar en disco; la task falla con `sync_unconfirmed` y quien
# la lanzó decide (Stop & persist reintenta el guardado con el motor vivo).
if [ "$SYNC_UNCONFIRMED" = 1 ]; then
  echo "$(ts) ⚠ SYNC FINAL NO CONFIRMADO (${UNCONFIRMED_BANKS# }): dejo el motor VIVO (pid ${SQPID:-?}). Persiste con 'python3 sqx/stop_and_persist.py $PROJ' o revisa el motor antes de matarlo." | tee -a "$LOG"
  if [ "$STATE_WRITER" != driver ]; then   # pipeline: señal al orquestador (como el kill.flag del vigilante)
    SYNC_TMP="$SYNCFLAG.tmp.$$"
    { echo "cause=sync_unconfirmed"; echo "reason=sync final sin confirmar:${UNCONFIRMED_BANKS}"; echo "ts=$(date '+%F %T')"; } > "$SYNC_TMP" \
      && mv "$SYNC_TMP" "$SYNCFLAG"
  fi
else
  kill_own_engine
fi

# 6. restore --slim (task única): esperar a que el motor muera del todo (L4) y restaurar
#    presence-aware lo archivado, borrando el marcador. En pipeline (--slim-pipeline) el
#    restore es acumulado: lo hace el orquestador al final de la cadena (punto 3).
if [ "$SLIM" = 1 ] && [ "$SLIM_PIPELINE" != 1 ]; then
  if [ "$SYNC_UNCONFIRMED" = 1 ]; then
    echo "$(ts) SLIM: NO restauro (el motor sigue vivo por el sync sin confirmar): queda el marcador para la reconciliación del próximo arranque" | tee -a "$LOG"
  elif python3 "$BANK_PLAN" "$PROJDIR" --marker-banks >/dev/null 2>&1; then
    echo "$(ts) SLIM: restore final (esperando a que muera el motor...)" | tee -a "$LOG"
    wait_free_trap || echo "$(ts) ⚠ sqcli sigue vivo tras la espera — intento el restore de todos modos (fallará y reconciliará al próximo arranque)" | tee -a "$LOG"
    python3 "$BANK_PLAN" "$PROJDIR" --slim-restore | tee -a "$LOG" || \
      echo "$(ts) ⚠ restore slim incompleto — queda el marcador; la reconciliación del próximo arranque lo completará (B2)" | tee -a "$LOG"
  fi
fi

# R6: parada pedida por el usuario (Stop & persist escribió la bandera): la task persistió y termina; en task
# suelta el state queda failed/user_stop, en pipeline lo clasifica el orquestador (rc 3, no lanza más).
if [ -f "$STOP_REQ" ]; then
  USER_STOP=1
  if [ "$SYNC_UNCONFIRMED" = 1 ]; then   # aceptación 2026-10-27: no decir "persistió" con el guardado sin confirmar
    echo "$(ts) ⏹ task parada por el usuario (bandera .pipeline_stop_request): el guardado final NO se confirmó (motor vivo); no se lanzará ninguna más" | tee -a "$LOG"
  else
    echo "$(ts) ⏹ task parada por el usuario (bandera .pipeline_stop_request): persistió y termina; no se lanzará ninguna más" | tee -a "$LOG"
  fi
fi

# Fase 2: transición final del state (solo el escritor; en pipeline el orquestador
# la hace al clasificar el log de la task). Peak RSS de la telemetría de la task.
if [ "$STATE_WRITER" = driver ]; then
  PEAK=$(python3 "$STATE_BIN" "$STATE_FILE" peak-from-csv "$TELEMETRY" "$TASK_KEY" 2>/dev/null || echo 0)
  if [ "$RAM_KILL" = 1 ]; then
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TASK_KEY" --status failed \
      --cause ram_kill --peak-rss-mb "$PEAK" >/dev/null 2>&1 || true
  elif [ "$ZOMBIE" = 1 ] || [ "${ENGINE_DIED:-0}" = 1 ]; then
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TASK_KEY" --status failed \
      --cause engine_died --peak-rss-mb "$PEAK" >/dev/null 2>&1 || true
  elif [ "$SYNC_UNCONFIRMED" = 1 ]; then
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TASK_KEY" --status failed \
      --cause sync_unconfirmed --peak-rss-mb "$PEAK" >/dev/null 2>&1 || true
  elif [ "$USER_STOP" = 1 ]; then
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TASK_KEY" --status failed \
      --cause user_stop --peak-rss-mb "$PEAK" >/dev/null 2>&1 || true
  else
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TASK_KEY" --status done \
      --peak-rss-mb "$PEAK" >/dev/null 2>&1 || true
  fi
fi

# R9: "Ya puedes abrir la GUI" SOLO si es verdad. En pipeline NUNCA (el orquestador lanza la siguiente task y
# su mensaje final decide); en task suelta solo con motor muerto + sin marcador --slim + sin .bak (gui_verdict).
if [ "${RUNNER_PIPELINE:-0}" = 1 ]; then
  if [ "$USER_STOP" = 1 ]; then
    DONE_NOTE="task parada por el usuario: el orquestador saldrá sin lanzar más. NO abras la GUI hasta que salga."
  else
    DONE_NOTE="el pipeline sigue: NO abras la GUI hasta \"PIPELINE PER-TASK COMPLETO\"."
  fi
  echo "=== $(ts) DONE task $TASK_KEY. Artefactos en $OUTDIR — $DONE_NOTE ===" | tee -a "$LOG"
else
  STOP_NOTE=""
  [ "$USER_STOP" = 1 ] && STOP_NOTE=" Task parada por el usuario."
  if GUI_WHY=$(gui_verdict); then
    echo "=== $(ts) DONE. Artefactos en $OUTDIR —${STOP_NOTE} Ya puedes abrir la GUI. ===" | tee -a "$LOG"
  else
    echo "=== $(ts) DONE. Artefactos en $OUTDIR —${STOP_NOTE} NO abras la GUI todavía: $GUI_WHY. ===" | tee -a "$LOG"
  fi
fi
