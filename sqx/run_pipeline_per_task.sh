#!/bin/bash
# run_pipeline_per_task.sh — pipeline SQX con UN MOTOR FRESCO POR TASK.
#
# Por qué existe (landmine gold_h4_prod_oos, 2026-09-27): un pipeline multi-task en UN
# solo motor acumula TODOS los bancos en RAM (el input de cada retest NO se libera al
# acabar la task) → a ~14k estrategias residentes el heap de Java entra en espiral de
# GC (CPU 300%+ con throughput colapsado de 16/min a ~0.7/min, HTTP zombi, swap a tope
# en máquinas de 16 GB). Regla (la misma que los lotes fitab, 2026-09-12): motor corto
# por task, heap desde cero. Coste aceptado: cada motor recarga los databanks de disco
# al arrancar (auto-sync del motor, landmine fitab (d)) — segundos/minutos por task,
# nada comparado con el colapso.
#
# Cómo: para cada task ACTIVA del .cfx (en orden de ejecución):
#   1. barrera proceso+puerto POR PROPIEDAD (R3, guardrail sqx-headless-fallos): sin sqcli
#      vivo y 5050 libre antes de lanzar. El motor de la task anterior (su grupo de
#      procesos = PID de su driver) que no muera recibe TERM y, si resiste, kill -9 tras
#      BARRIER_OWN_GRACE_SECS (SIGTERM no basta — landmine fitab). Un sqcli AJENO nunca se
#      mata: tras BARRIER_FOREIGN_SECS el pipeline aborta diciendo quién es. El puerto
#      ocupado aborta tras PORT_WAIT_SECS con el dueño (lsof).
#   2. lanza SOLO esa task con run_task_headless.py --mode only (active-flags; el .cfx
#      se restaura segundos después del start — verificado smoke_p2, 2026-07-19).
#   3. espera a que el driver muera y clasifica por su log: "proyecto TERMINADO" = ok;
#      si no (MOTOR MUERTO, tope MAXITER, error de arranque) → ABORTA indicando el
#      índice para reanudar (vuelve a lanzar este script con <from_task_idx>).
#   4. copia los logs de la task a sqx/output/<proj>_<fecha>/task_<i>_<label>/
#      (/tmp no es persistente; el driver copia los suyos a OUTDIR pero se pisan
#      entre tasks — aquí quedan por task).
#
# Uso:   bash sqx/run_pipeline_per_task.sh <proyecto> [from_task_idx] [--slim]
#        from_task_idx = índice de `python sqx/run_task_headless.py <proy> --list`
#        (0-based, orden de ejecución; NO es el task=N 1-based de startFromTask del
#        driver — H13, debate 2026-09-27). Default 0. GUI cerrada. nohup/background.
#        MAXITER (tope por task, default 12h) se hereda al driver vía env.
#        Tras <proyecto>, `--slim` y el índice van en CUALQUIER orden (R8); un argumento
#        desconocido o un segundo índice → uso y exit 2.
#        Un solo pipeline por proyecto (A7): si ya hay otro orquestador o driver del MISMO
#        proyecto vivo, aborta sin tocar nada.
#
# --slim (ahorro RAM, plan ram-runner): cada driver de task archiva lo que su task no
# usa y el restore se hace ACUMULADO al final de la cadena (este script, trap EXIT) o
# por task cuando un banco archivado se necesita como input (L3 por task, punto 1 del
# driver). El launcher de cada task hereda SLIM_IN_PIPELINE=1 y NO reconcilia entre
# tasks (el marcador es de la cadena entera, no de un task). Al arranque de la cadena
# se reconcilia cualquier marcador huérfano de una corrida muerta (B2).
#
# Parada del usuario (R6, guardrail sqx-headless-fallos Fase 2): Stop & persist (CLI o monitor) escribe la
# bandera `<proyecto>/.pipeline_stop_request` y le manda `stop` al motor de la task en curso. Este script la
# mira ANTES de lanzar cada task y tras `wait_driver` (el driver ya persistió): bandera ⇒ la task en curso
# queda `failed/user_stop` en el state, "⏹ PARADA por el usuario … Reanuda: bash … <idx>", la bandera se
# borra y SALE CON rc 3 (la cascada, run_cascade_projects.sh, se detiene entera). Una bandera RANCIA al
# arrancar se borra y se avisa (lanzar de nuevo es una decisión explícita), pero solo DESPUÉS de comprobar
# que no hay otro pipeline vivo del proyecto (A7: no se toca la bandera de una parada en curso). Sync final
# sin confirmar (`<proj>_sync_unconfirmed.flag` del driver) ⇒ failed/sync_unconfirmed y ABORT (rc 1) con el
# motor VIVO a propósito. TERM/INT imprimen el índice de reanudación.
# rc: 0 completo · 1 aborto · 3 parada del usuario · 130/143 INT/TERM.
#
# Knobs de entorno: PROJECTS_DIR (raíz de proyectos), SQX_OUTDIR (raíz de artefactos),
# PORT (puerto HTTP del motor, default 5050), WAIT_MARGIN/WAIT_SLEEP (espera del driver),
# BARRIER_OWN_GRACE_SECS (120) / BARRIER_FOREIGN_SECS (120) / PORT_WAIT_SECS (300) /
# BARRIER_TERM_SECS (10) / BARRIER_POLL_SECS (5): tiempos de la barrera (R3),
# SQX_ENGINE_PROC_NAME (nombre EXACTO del proceso del motor, default sqcli; solo tests),
# SLIM_TESTING=1 (⚠ SOLO tests: desactiva pre-checks de GUI/sqcli y la barrera; con
# BARRIER_TESTING=1 se reactiva SOLO la barrera, y BARRIER_TEST_OWN_PGID simula "el
# driver de la task anterior era ese pgid" sin lanzar uno real).
set -u
# REPO: si venimos de un self-copy, BASH_SOURCE apunta a /tmp y el cálculo desde
# dirname daría "/"; el valor resuelto antes del exec viaja en SQX_RUNNER_REPO.
REPO="${SQX_RUNNER_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Rutas SQX (SQX_APP -> SQCLI, PROJECTS_DIR; respeta los overrides de entorno). Desde $REPO,
# NO desde dirname: tras el self-copy este script corre desde /tmp.
source "$REPO/sqx/paths.sh"
BANK_PLAN="$REPO/sqx/bank_plan.py"
PROJ="${1:?uso: $0 <proyecto> [from_task_idx] [--slim]}"
# R8: tras <proyecto>, `--slim` y UN índice en cualquier orden. Antes `FROM_IDX="${2:-0}"` y solo
# $3/$4 se miraban para --slim: `<proy> --slim` tomaba "--slim" como índice (y fallaba tarde).
usage(){ echo "uso: bash sqx/run_pipeline_per_task.sh <proyecto> [from_task_idx] [--slim]  (índice y --slim en cualquier orden)" >&2; }
case "$PROJ" in -*) usage; exit 2 ;; esac
FROM_IDX=0
FROM_SET=0
SLIM=0
if [ $# -gt 1 ]; then
  for a in "${@:2}"; do
    case "$a" in
      --slim) SLIM=1 ;;
      ''|*[!0-9]*) echo "arg desconocido: '$a'" >&2; usage; exit 2 ;;
      *)
        if [ "$FROM_SET" = 1 ]; then echo "índice repetido: '$a' (ya hay from_task_idx=$FROM_IDX)" >&2; usage; exit 2; fi
        FROM_IDX=$((10#$a)); FROM_SET=1 ;;
    esac
  done
fi

# Mismo landmine que run_pipeline_headless.sh: bash re-lee este script por offset
# durante horas; una reescritura in-place durante la cadena corrompe el re-parseo
# (syntax error a mitad de línea, cazado 2026-10-06). Defensa: copia estable.
if [ -z "${SQX_RUNNER_SELF_COPY:-}" ]; then
  SELF_COPY=$(mktemp "/tmp/${PROJ}_pertask_XXXXXX")
  cp "$0" "$SELF_COPY"
  export SELF_COPY SQX_RUNNER_REPO="$REPO"
  SQX_RUNNER_SELF_COPY=1 exec bash "$SELF_COPY" "$@"
fi
# La marca es solo para ESTE proceso: si la heredan los hijos, el driver no se
# auto-copia y wait_driver del orquestador (que busca la copia "<proj>_driver_")
# lo da por muerto a los 5 s: aborto en falso con el motor vivo (cazado 2026-10-12).
# mktemp de macOS (BSD) solo sustituye las X si van al FINAL: sin sufijo ".sh".
unset SQX_RUNNER_SELF_COPY

PROJDIR="$PROJECTS_DIR/$PROJ"
STOP_FLAG="$PROJDIR/.pipeline_stop_request"   # R6: la escribe stop_and_persist.py (D3: projects_dir()/<proj>/…)
OUTDIR="${SQX_OUTDIR:-$REPO/sqx/output}/${PROJ}_$(date +%Y%m%d)"   # fijado al inicio (cambio de día a media corrida)
# Fase 2: este script es el ÚNICO escritor del state en pipeline. El vigilante
# vive en el driver y escribe kill.flag; aquí lo recogemos al clasificar la task.
OUTBASE="${SQX_OUTDIR:-$REPO/sqx/output}"
STATE_FILE="$OUTBASE/${PROJ}_state.json"
KILLFLAG="$OUTBASE/${PROJ}_kill.flag"
SYNCFLAG="$OUTBASE/${PROJ}_sync_unconfirmed.flag"   # R1: el driver lo escribe con el sync final sin confirmar
TELEMETRY="$OUTBASE/${PROJ}_telemetry.csv"
STATE_BIN="$REPO/sqx/run_state.py"
PORT="${PORT:-5050}"
ts(){ date '+%H:%M:%S'; }
# PLAN-DESVIO (menor): el plan escribe `pgrep -x sqcli` inline; aquí el nombre sale de SQX_ENGINE_PROC_NAME
# (default `sqcli`, igual que engine_pids() en Python) para que los tests de la barrera usen un motor falso
# de nombre único y nunca vean ni toquen un sqcli real de la máquina. En producción no cambia nada.
ENGINE_PROC="${SQX_ENGINE_PROC_NAME:-sqcli}"   # nombre EXACTO del proceso del motor (R5: nunca texto de cmdline)
# ¿Seguro que NO queda ningún motor? Solo con `pgrep` rc 1. rc>1 = pgrep roto = NO libre (fail-closed, revisión
# de código D3 2026-10-25: un pgrep roto valía como "libre" y se restauraba el --slim con el motor quizá vivo).
no_engine_alive(){ pgrep -x "$ENGINE_PROC" >/dev/null 2>&1; [ $? -eq 1 ]; }
# Comando para reanudar. OJO: aquí `$0` es la auto-copia /tmp/<proj>_pertask_XXXXXX, que on_exit borra al
# salir: un "Reanuda: bash $0 …" apuntaría a un fichero que ya no existe.
# PLAN-DESVIO: el plan escribe `bash $0 $PROJ <idx>` en los mensajes; se usa el script ORIGINAL (bajo $REPO).
ORCH_SELF="$REPO/sqx/run_pipeline_per_task.sh"

# --- A7: un solo pipeline por proyecto ---------------------------------------------------------
# Si ya hay OTRO orquestador o driver del MISMO proyecto vivo → ABORT sin tocar nada. Va ANTES del trap
# EXIT completo a propósito: on_exit restauraría un .bak ajeno (pisaría el .cfx de la otra corrida) y
# tocaría su marcador slim. Regex ANCLADOS al inicio del cmdline, gemelos de
# stop_and_persist.orchestrator_re/driver_re (el test los compara): no casan con un `tail -f
# /tmp/<p>_pertask_<epoch>.log`, con `caffeinate -i bash …` (sí con su bash hijo) ni con otro proyecto
# de prefijo común. PROJ_ERE = PROJ con los metacaracteres de ERE escapados.
PROJ_ERE=$(printf '%s' "$PROJ" | sed 's/[][\\^$.|?*+(){}]/\\&/g')
ORCH_RE="^[^ ]*bash ([^ ]*/)?(run_pipeline_per_task\.sh|${PROJ_ERE}_pertask_[A-Za-z0-9]{6}) ${PROJ_ERE}( |\$)"
DRIVER_RE="^[^ ]*bash ([^ ]*/)?(run_pipeline_headless\.sh|${PROJ_ERE}_driver_[A-Za-z0-9]{6}) ${PROJ_ERE}( |\$)"
# PIDs vivos cuyo cmdline casa $1, SIN este proceso ni sus subshells (`$(…)` y pipelines comparten
# cmdline con el orquestador: se descartan por ppid == $$). rc 2 = `pgrep` ROTO (rc ≠ 0 y ≠ 1: regex rechazado,
# binario ausente…): no sabemos si hay otro pipeline vivo (C2, fail-closed) → el llamador ABORTA.
others_alive(){
  local p pp out="" list rc
  list=$(pgrep -f "$1" 2>/dev/null); rc=$?
  if [ "$rc" -gt 1 ]; then echo "pgrep rc=$rc"; return 2; fi
  for p in $list; do
    [ "$p" = "$$" ] && continue
    pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    [ "$pp" = "$$" ] && continue
    out="$out $p"
  done
  echo "${out# }"
}
OTHER_ORCH=$(others_alive "$ORCH_RE"); RC_O=$?
OTHER_DRV=$(others_alive "$DRIVER_RE"); RC_D=$?
if [ "$RC_O" -ne 0 ] || [ "$RC_D" -ne 0 ]; then
  echo "$(ts) ABORT: no pude comprobar si ya hay un pipeline vivo de $PROJ (${OTHER_ORCH:-}${OTHER_DRV:+ }${OTHER_DRV:-}): sin saberlo no arranco. No toco nada."
  rm -f "${SELF_COPY:-}" 2>/dev/null
  exit 1
fi
if [ -n "$OTHER_ORCH$OTHER_DRV" ]; then
  echo "$(ts) ABORT: ya hay un pipeline vivo de $PROJ (orquestador: ${OTHER_ORCH:-ninguno}; driver: ${OTHER_DRV:-ninguno}). No toco nada. Páralo antes (Stop & persist, o \`kill\` al orquestador) o espera a que termine."
  rm -f "${SELF_COPY:-}" 2>/dev/null
  exit 1
fi
# R6/A7: SOLO ahora (sin otro pipeline vivo del proyecto) una bandera de parada que siga ahí es RANCIA —de una
# parada anterior— y se borra avisando: una ejecución nueva es una decisión explícita. Antes de A7 no se
# toca: podría ser la orden de una parada en curso del otro pipeline.
if [ -f "$STOP_FLAG" ]; then
  echo "$(ts) ⚠ bandera de parada rancia ($STOP_FLAG) de una parada anterior: la borro (lanzar de nuevo es una decisión explícita)."
  rm -f "$STOP_FLAG"
fi
rm -f "$SYNCFLAG"   # señal sync_unconfirmed rancia de una cadena anterior

# task key "idx:label" para el state (una llamada al plan).
task_key(){
  python3 -c "
import json, subprocess, sys
out = subprocess.run([sys.executable, '$BANK_PLAN', '$PROJDIR', '--task-idx', sys.argv[1]],
                     capture_output=True, text=True)
try:
    print(f\"{sys.argv[1]}:\" + json.loads(out.stdout).get('task', '?'))
except Exception:
    print(f\"{sys.argv[1]}:?\")
" "$1" 2>/dev/null || echo "$1:?"
}

# peak RSS (MB) de la task en el CSV de telemetría acumulado
task_peak(){
  python3 "$STATE_BIN" "$STATE_FILE" peak-from-csv "$TELEMETRY" "$1" 2>/dev/null || echo 0
}

# H8 (debate 2026-09-27): si el orquestador muere a mitad (INT/TERM/exit por aborto) y
# quedó un .bak sin restaurar, restaurar el .cfx al salir. Un SIGKILL no llega al trap —
# para eso queda la alerta 'restore pendiente' del monitor (backstop ya existente).
restore_cfx_on_exit(){
  if [ -f "$PROJDIR/project.cfx.pre_run_task.bak" ]; then
    echo "$(ts) trap: restaurando .cfx desde .bak huérfano..."
    PYTHONPATH="$REPO/sqx" PROJECTS_DIR="$PROJECTS_DIR" python3 - "$PROJ" <<'PY'
import sys
import run_task_headless as RT
# PLAN-DESVIO (arreglo fuera del plan; lo necesitan los tests F2 de restore del .cfx): _projects_dir()
# respeta PROJECTS_DIR (tests/fixtures); REG.PROJECTS apuntaba siempre a la app real.
bak = RT._projects_dir() / sys.argv[1] / "project.cfx.pre_run_task.bak"
RT.restore(sys.argv[1], bak)
PY
  fi
}

# Restore slim ACUMULADO al final de la cadena (punto 3 del plan): cada driver de task
# dejó su archive en el marcador; aquí se restaura todo de golpe (presence-aware) y se
# borra el marcador. Si el motor no muere, se deja el marcador para la reconciliación
# del próximo arranque (M3/B2).
wait_free_orch(){
  [ "${SLIM_TESTING:-0}" = 1 ] && return 0
  local n=$(( ${SLIM_TRAP_WAIT_SECS:-300} / 5 )) i
  for i in $(seq 1 "$n"); do no_engine_alive && return 0; sleep 5; done
  return 1
}
ENGINE_LEFT_ALIVE=0   # 1 = el driver dejó el motor VIVO a propósito (sync final sin confirmar, R1)
SLIM_RESTORED=0       # el restore acumulado se intenta UNA vez (también se llama antes del mensaje final, R9)
restore_slim_on_exit(){
  if [ "$SLIM" != 1 ]; then return 0; fi
  if [ "$SLIM_RESTORED" = 1 ]; then return 0; fi
  if ! python3 "$BANK_PLAN" "$PROJDIR" --marker-banks >/dev/null 2>&1; then return 0; fi
  SLIM_RESTORED=1
  if [ "$ENGINE_LEFT_ALIVE" = 1 ]; then
    # restaurar bancos con el motor vivo y sin guardar lo corrompería; y esperar 300 s a que muera es inútil
    echo "$(ts) trap: el motor sigue vivo A PROPÓSITO (sync final sin confirmar): NO restauro el --slim; queda el marcador para la reconciliación del próximo arranque"
    return 0
  fi
  echo "$(ts) trap: restore slim acumulado de la cadena..."
  wait_free_orch || echo "$(ts) sqcli sigue vivo tras la espera — dejo el marcador para la reconciliación del próximo arranque (M3)"
  if [ "${SLIM_TESTING:-0}" = 1 ] || no_engine_alive; then
    python3 "$BANK_PLAN" "$PROJDIR" --slim-restore || echo "$(ts) ⚠ restore slim incompleto — queda el marcador para la reconciliación (B2)"
  fi
}
# Task en curso, GLOBALES (un trap las ve aunque salte dentro de una función). Valor inicial = índice de arranque.
CURRENT_IDX="$FROM_IDX"; CURRENT_LABEL=""; CURRENT_TKEY=""
WAIT_SLEEP_PID=""   # el `sleep` en segundo plano de wait_driver (los traps se atienden enseguida, ver wait_driver)
on_exit(){
  rc=$?
  [ -n "$WAIT_SLEEP_PID" ] && kill "$WAIT_SLEEP_PID" 2>/dev/null
  rm -f "${SELF_COPY:-}" 2>/dev/null   # copia estable del propio script (defensa re-parseo)
  # PLAN-DESVIO (menor): el plan borra la bandera solo en la ruta user_stop. Aquí también en cualquier otra salida:
  # si el orquestador sale por el motivo que sea, el pipeline ya está parado, la bandera cumplió su función y una
  # bandera huérfana confundiría a la siguiente task suelta. (Este trap se instala DESPUÉS de A7: un segundo
  # orquestador abortado por "ya hay un pipeline vivo" sale antes y NO toca la bandera del primero.)
  rm -f "$STOP_FLAG"
  restore_cfx_on_exit
  restore_slim_on_exit
  exit $rc
}
# TERM/INT: imprimen el índice para reanudar y salen 143/130 (el trap EXIT restaura .cfx y slim igual que siempre).
# El driver de la task en curso SIGUE vivo (otra sesión) y persistirá solo; reanudar antes de que acabe lo rechaza A7.
on_signal(){   # $1 = INT|TERM · $2 = rc
  echo "$(ts) ⚠ orquestador interrumpido ($1) en la task $CURRENT_IDX (${CURRENT_LABEL:-?}): su driver SIGUE vivo y persistirá solo. Reanuda: bash $ORCH_SELF $PROJ $CURRENT_IDX (cuando ese driver termine; antes lo rechaza A7) — o Stop & persist para pararlo"
  exit "$2"
}
trap on_exit EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

# --- reconciliación al arranque de la cadena (B2 slim + Fase 2 state) ---
if [ -f "$PROJDIR/databanks_archive/.runner_session.json" ]; then
  echo "$(ts) hay marcador slim previo — reconciliando corrida muerta..."
  REC_OUT=$(python3 "$BANK_PLAN" "$PROJDIR" --reconcile) || {
    rc=$?
    if [ "$rc" = 3 ]; then
      echo "$(ts) ABORT: el marcador slim previo tiene el pid del driver VIVO — hay una corrida en curso (¿arrancada a mano?). No toco nada."
    else
      echo "$(ts) ABORT: la reconciliación del marcador slim falló: $REC_OUT"
    fi
    exit 1
  }
  echo "$REC_OUT"
fi
# Fase 2: tasks en running cuyo pid (el de una cadena anterior) ya no vive ->
# failed/engine_died.
python3 "$STATE_BIN" "$STATE_FILE" reconcile >/dev/null 2>&1 || true

# --- pre-checks globales ---
if [ "${SLIM_TESTING:-0}" != 1 ] && pgrep -f "StrategyQuantX_nocheck|StrategyQuantX_ui" >/dev/null; then
  echo "$(ts) ABORT: la GUI de SQX está abierta (corrompería databanks). Ciérrala."; exit 1
fi

# --- tasks activas en orden de ejecución: "idx<TAB>label" por línea ---
# (bash 3.2 de macOS no tiene mapfile — while-read + array)
TASKS=()
while IFS= read -r line; do
  TASKS+=("$line")
done < <(PYTHONPATH="$REPO/sqx" PROJECTS_DIR="$PROJECTS_DIR" python3 - "$PROJ" <<'PY'
import sys
import run_task_headless as RT
for i, t in enumerate(RT.tasks_of(sys.argv[1])):
    if t["active"]:
        print(f"{i}\t{t['label']}")
PY
)
[ ${#TASKS[@]} -eq 0 ] && { echo "$(ts) ABORT: $PROJ sin tasks activas (¿existe el proyecto?)."; exit 1; }

# run_task_headless selecciona por label: labels duplicados entre activas = ambiguo
DUPES=$(printf '%s\n' "${TASKS[@]}" | cut -f2 | sort | uniq -d)
[ -n "$DUPES" ] && { echo "$(ts) ABORT: labels de task duplicados en $PROJ: $DUPES"; exit 1; }

# Última task activa (índice): para que el ABORT de la barrera tras la ÚLTIMA task no sugiera reanudar
# desde una task que no existe.
LAST_ACTIVE_IDX=$(printf '%s\n' "${TASKS[@]}" | cut -f1 | sort -n | tail -1)

# --- barrera proceso+puerto POR PROPIEDAD (R3) --------------------------------------------------
# Entre tasks hay que esperar a que muera el motor de la task anterior y a que el puerto quede libre.
# Antes: tras 120 s con CUALQUIER sqcli vivo, `kill -9` a todos (sin guardar, aunque fuera una corrida
# ajena). Ahora cada motor vivo se clasifica por su grupo de procesos (`ps -o pgid=`):
#   - PROPIO: pgid = LAST_DRIVER_PID, el PID del driver de la task anterior (lo imprime run_task_headless:
#     "PID driver N"). Su driver ya persistió y salió; si el motor no muere solo, tras
#     BARRIER_OWN_GRACE_SECS → TERM, BARRIER_TERM_SECS, kill -9 (como el kill final del driver).
#   - AJENO: cualquier otro (y cualquiera antes de la 1.ª task: LAST_DRIVER_PID vacío). NUNCA se mata:
#     puede estar trabajando con datos sin guardar. Tras BARRIER_FOREIGN_SECS → ABORT con instrucciones.
# Puerto: espera hasta PORT_WAIT_SECS y ABORT con el dueño (lsof). Los `tail -f /dev/null` solo se matan
# dentro del grupo propio (nunca `pkill -f` global). Activación: SIN `SLIM_TESTING` (producción) o con
# `BARRIER_TESTING=1` (tests aislados con tiempos cortos por env).
BARRIER_OWN_GRACE_SECS=${BARRIER_OWN_GRACE_SECS:-120}
BARRIER_FOREIGN_SECS=${BARRIER_FOREIGN_SECS:-120}
BARRIER_TERM_SECS=${BARRIER_TERM_SECS:-10}
BARRIER_POLL_SECS=${BARRIER_POLL_SECS:-5}
PORT_WAIT_SECS=${PORT_WAIT_SECS:-300}
LAST_DRIVER_PID=""
# seam de test: simula "el driver de la task anterior tenía este pgid" sin lanzar uno real
[ "${BARRIER_TESTING:-0}" = 1 ] && LAST_DRIVER_PID="${BARRIER_TEST_OWN_PGID:-}"

barrier_active(){ [ "${SLIM_TESTING:-0}" != 1 ] || [ "${BARRIER_TESTING:-0}" = 1 ]; }

# "pid pgid" de cada motor vivo (nombre EXACTO de proceso). rc 2 = `pgrep` ROTO (rc ≠ 0 y ≠ 1: regex rechazado,
# binario ausente…): el llamador ABORTA (C1, fail-closed). Con la lista vacía "por error" la barrera daría luz
# verde y se lanzaría un motor nuevo encima de uno vivo.
engine_list(){
  local p out rc
  out=$(pgrep -x "$ENGINE_PROC" 2>/dev/null); rc=$?
  [ "$rc" -gt 1 ] && return 2
  for p in $out; do
    echo "$p $(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')"
  done
  return 0
}

# C7: LAST_DRIVER_PID debe ser un PID entero > 1 (con vacío, "abc", 0 o 1 `pkill -g` apuntaría al grupo equivocado).
# Si no lo es → aviso y ningún motor se considera propio (nunca se mata nada por error).
sanitize_last_driver_pid(){
  [ -z "$LAST_DRIVER_PID" ] && return 0
  case "$LAST_DRIVER_PID" in
    *[!0-9]*) ;;
    *) [ "$LAST_DRIVER_PID" -gt 1 ] && return 0 ;;
  esac
  echo "$(ts) ⚠ LAST_DRIVER_PID='$LAST_DRIVER_PID' no es un PID válido (entero > 1): ningún motor se considera propio, no se matará ninguno"
  LAST_DRIVER_PID=""
}
sanitize_last_driver_pid   # cubre el valor sembrado por BARRIER_TEST_OWN_PGID

# idx a reanudar tras un ABORT de la barrera ($1 = idx de la siguiente task)
resume_hint(){
  if [ "$1" -gt "$LAST_ACTIVE_IDX" ]; then
    echo "las tasks de la cadena ya terminaron OK; solo queda liberar la máquina"
  else
    echo "reanuda: bash $ORCH_SELF $PROJ $1"
  fi
}

barrier_abort_foreign(){
  local next="$1" p pgid http
  shift
  echo "$(ts) ⚠ ABORT: hay un motor sqcli que NO es de esta cadena y NO lo mato (puede estar trabajando con datos sin guardar):"
  for p in "$@"; do
    pgid=$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')
    if [ -n "$(curl -s --max-time 3 "http://localhost:$PORT/call?cmd=-project%20action=list")" ]; then
      http="responde HTTP en :$PORT"
    else
      http="HTTP sin respuesta en :$PORT"
    fi
    echo "$(ts)   pid=$p pgid=${pgid:-?} ($http)"
  done
  echo "$(ts)   Páralo con 'python3 sqx/stop_and_persist.py <proyecto>' (persiste y libera) y $(resume_hint "$next")"
  exit 1
}

# TERM → gracia → kill -9 a los motores PROPIOS ($@ = pids) y a los tail de su grupo
barrier_kill_own(){
  local i n alive p
  echo "$(ts) el motor propio (pgid $LAST_DRIVER_PID, pids $*) sigue vivo tras ${BARRIER_OWN_GRACE_SECS}s (su driver ya persistió y salió) → TERM"
  kill -TERM "$@" 2>/dev/null
  n=$(( BARRIER_TERM_SECS * 5 ))
  for i in $(seq 1 "$n"); do
    alive=""
    for p in "$@"; do pgrep -x "$ENGINE_PROC" | grep -qx "$p" && alive="$alive $p"; done
    [ -z "$alive" ] && break
    sleep 0.2
  done
  if [ -n "${alive:-}" ]; then
    echo "$(ts) sigue vivo tras TERM → kill -9 (landmine fitab: SIGTERM no basta) a:$alive"
    kill -9 $alive 2>/dev/null
    sleep 1
  fi
  # SOLO los tail del grupo propio; C6: nunca `pkill -g` con el pgid vacío (apuntaría a un grupo equivocado)
  [ -n "$LAST_DRIVER_PID" ] && pkill -g "$LAST_DRIVER_PID" -f '^tail -f /dev/null$' 2>/dev/null
  return 0
}

# $1 = idx de la siguiente task a lanzar (para el mensaje de reanudación)
barrier(){
  barrier_active || return 0
  local next="${1:-0}" t_own="" t_for="" own_rounds=0 now list own foreign pid pgid t_port owner lrc
  while true; do
    list=$(engine_list); lrc=$?
    if [ "$lrc" -ne 0 ]; then
      echo "$(ts) ⚠ ABORT: pgrep falló (rc=$lrc) comprobando si hay un sqcli vivo: sin saberlo no lanzo ninguna task (podría haber un motor vivo). $(resume_hint "$next")"
      exit 1
    fi
    [ -z "$list" ] && break
    own=""; foreign=""
    while IFS=' ' read -r pid pgid; do
      [ -n "$pid" ] || continue
      if [ -n "$LAST_DRIVER_PID" ] && [ "$pgid" = "$LAST_DRIVER_PID" ]; then
        own="$own $pid"
      else
        foreign="$foreign $pid"
      fi
    done <<< "$list"
    now=$(date +%s)
    if [ -n "$foreign" ]; then
      [ -z "$t_for" ] && t_for=$now
      [ $(( now - t_for )) -ge "$BARRIER_FOREIGN_SECS" ] && barrier_abort_foreign "$next" $foreign
    else
      t_for=""
    fi
    if [ -n "$own" ]; then
      [ -z "$t_own" ] && t_own=$now
      if [ $(( now - t_own )) -ge "$BARRIER_OWN_GRACE_SECS" ]; then
        own_rounds=$(( own_rounds + 1 ))
        if [ "$own_rounds" -gt 3 ]; then
          # PLAN-DESVIO (menor): el plan no acota las rondas de TERM/kill -9. Ni TERM ni kill -9 en 3 rondas
          # = proceso atascado en el kernel (estado UE): no es un bucle que la barrera pueda resolver; que lo
          # mire una persona en vez de girar para siempre (la barrera antigua giraba sin fin).
          echo "$(ts) ⚠ ABORT: el motor propio (pids$own) no muere ni con kill -9 tras 3 intentos (proceso atascado en el kernel); $(resume_hint "$next")"
          exit 1
        fi
        barrier_kill_own $own
        t_own=""
        continue
      fi
    else
      t_own=""
    fi
    echo "$(ts) esperando a que muera sqcli previo..."; sleep "$BARRIER_POLL_SECS"
  done
  if command -v nc >/dev/null; then
    t_port=$(date +%s)
    while nc -z -w 3 localhost "$PORT" 2>/dev/null; do
      now=$(date +%s)
      if [ $(( now - t_port )) -ge "$PORT_WAIT_SECS" ]; then
        echo "$(ts) ⚠ ABORT: el puerto $PORT sigue ocupado tras ${PORT_WAIT_SECS}s y no hay ningún sqcli a la vista. Dueño:"
        if command -v lsof >/dev/null; then
          owner=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
          printf '%s\n' "${owner:-(lsof no ve ningún LISTEN en :$PORT)}" | sed "s/^/$(ts)   /"
        else
          echo "$(ts)   (lsof no disponible)"
        fi
        echo "$(ts)   Libera el puerto y $(resume_hint "$next")"
        exit 1
      fi
      echo "$(ts) esperando puerto $PORT libre..."; sleep "$BARRIER_POLL_SECS"
    done
  fi
}

# --- espera a que el driver de UNA task termine (spawn + muerte) ---
WAIT_MARGIN=${WAIT_MARGIN:-5}    # margen inicial para que el spawn sea visible
WAIT_SLEEP=${WAIT_SLEEP:-30}     # poll de la muerte del driver
# `sleep N` en SEGUNDO PLANO + `wait`: bash difiere los traps mientras corre un comando en primer plano, así que
# con `sleep 30` un TERM/INT tardaba hasta 30 s en atenderse (O4); `wait` se interrumpe al instante.
isleep(){ sleep "$1" & WAIT_SLEEP_PID=$!; wait "$WAIT_SLEEP_PID"; WAIT_SLEEP_PID=""; }
wait_driver(){
  isleep "$WAIT_MARGIN"
  local rc warned=0
  # OJO: el driver se auto-copia a /tmp/<proj>_driver_XXXXXX (defensa del re-parseo, 2026-10-06) ->
  # el regex debe casar la COPIA (el path original "run_pipeline_headless.sh" ya no aparece en ningún
  # cmdline una vez hecho el exec) y va ANCLADO (DRIVER_RE, arriba): el patrón ancho "<proj>_driver_"
  # casaba con cualquier proceso que contuviera ese texto.
  while true; do
    pgrep -f "$DRIVER_RE" >/dev/null 2>&1; rc=$?
    [ "$rc" -eq 1 ] && break                       # rc 1 = ningún driver vivo
    if [ "$rc" -gt 1 ]; then
      # C1 (fail-closed): `pgrep` ROTO no es "driver muerto": darlo por muerto clasificaría la task como "no
      # terminó limpio" y abortaría con el driver aún corriendo y el motor vivo. Sigo esperando y aviso (una vez).
      [ "$warned" = 0 ] && echo "$(ts) ⚠ pgrep falló (rc=$rc) comprobando el driver de la task $CURRENT_IDX: NO lo doy por muerto, sigo esperando"
      warned=1
    else
      warned=0
    fi
    isleep "$WAIT_SLEEP"
  done
}

# R6: ¿hay orden de parada del usuario? (la bandera la escribe stop_and_persist.py)
stop_requested(){ [ -f "$STOP_FLAG" ]; }
# R6: salida por parada del usuario, rc 3 (la cascada se detiene entera, D2). $1 = 1 si la task en curso llegó a
# lanzarse (su state pasa a failed/user_stop) · 0 si la bandera se vio ANTES de lanzar nada. Los traps EXIT
# (restore .cfx y --slim) corren igual y borran la bandera.
user_stop_exit(){
  if [ "$1" = 1 ]; then
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$CURRENT_TKEY" --status failed \
      --cause user_stop --peak-rss-mb "$(task_peak "$CURRENT_TKEY")" >/dev/null 2>&1 || true
    echo "$(ts) ⏹ PARADA por el usuario en la task $CURRENT_IDX ($CURRENT_LABEL). Reanuda: bash $ORCH_SELF $PROJ $CURRENT_IDX (repite la task) — o $((CURRENT_IDX + 1)) para darla por buena"
  else
    echo "$(ts) ⏹ PARADA por el usuario ANTES de lanzar la task $CURRENT_IDX ($CURRENT_LABEL): no se lanzó nada. Reanuda: bash $ORCH_SELF $PROJ $CURRENT_IDX"
  fi
  rm -f "$STOP_FLAG"
  exit 3
}

# R9: "ya puedes abrir la GUI" SOLO si `pre_gui_check` (global: motor, GUI, orquestadores/drivers/cascadas vivos de
# cualquier proyecto, --slim sin restaurar, .bak pendiente) sale seguro. Este orquestador y sus subshells se
# ignoran (están vivos mientras imprime). rc 0 = seguro; rc 1 = inseguro (imprime los motivos).
gui_verdict(){
  PYTHONPATH="$REPO/sqx" PROJECTS_DIR="$PROJECTS_DIR" python3 - "$PROJ" "$$" 2>/dev/null <<'PY'
import sys
import stop_and_persist as SP
rep = SP.pre_gui_check(sys.argv[1], ignore_pids=[int(sys.argv[2])])
if rep["safe"]:
    sys.exit(0)
print("; ".join(rep["reasons"]))
sys.exit(1)
PY
}

echo "=== $(ts) PIPELINE PER-TASK $PROJ (from_task=$FROM_IDX, ${#TASKS[@]} tasks activas, slim=$SLIM) ==="
barrier "$FROM_IDX"   # cubre restos de una corrida anterior antes de la primera task
if [ "$SLIM" = 1 ]; then
  export SLIM_IN_PIPELINE=1   # el launcher de cada task NO reconcilia (la cadena gestiona el acumulado)
fi
export RUNNER_PIPELINE=1      # Fase 2: los drivers de la cadena NO tocan el state (escritor = este script)

for entry in "${TASKS[@]}"; do
  IFS=$'\t' read -r idx label <<< "$entry"
  [ "$idx" -lt "$FROM_IDX" ] && continue
  CURRENT_IDX="$idx"; CURRENT_LABEL="$label"

  # R6: parada pedida ANTES de lanzar nada de esta task → no se lanza (rc 3)
  stop_requested && user_stop_exit 0

  TKEY=$(task_key "$idx"); CURRENT_TKEY="$TKEY"
  # Fase 2: la task entra en running (pid de ESTA cadena: si la cadena muere, la
  # reconciliación del próximo arranque marca failed/engine_died).
  python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status running \
    --pid $$ >/dev/null 2>&1 || true

  echo "=== $(ts) TASK $idx: $label — lanzando motor fresco ==="
  # El log de la corrida es ÚNICO por lanzamiento (H9): lo imprime el CLI como "log=<path>"
  LAUNCH_ARGS=("$PROJ" --task "$label" --mode only --port "$PORT")
  [ "$SLIM" = 1 ] && LAUNCH_ARGS+=(--slim)
  LAUNCH_OUT=$(PYTHONPATH="$REPO/sqx" PROJECTS_DIR="$PROJECTS_DIR" python3 "$REPO/sqx/run_task_headless.py" "${LAUNCH_ARGS[@]}") || {
    echo "$LAUNCH_OUT"
    # R6: el lanzador se negó (o falló) con la bandera puesta → parada del usuario, no aborto por fallo
    stop_requested && user_stop_exit 1
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status failed \
      --cause engine_died --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
    echo "$(ts) ⚠ ABORT: no se pudo lanzar la task $idx ($label). Reanuda: bash $ORCH_SELF $PROJ $idx"
    exit 1
  }
  echo "$LAUNCH_OUT"
  # R3: el PID del driver ("PID driver N", lo imprime run_task_headless) identifica a SU motor: el driver
  # nace con start_new_session → su pgid = ese PID y el motor hereda el grupo. Asignación INCONDICIONAL:
  # si no se pudo leer, vacío (= todo motor es ajeno y nunca se mata) mejor que el PID de la task anterior.
  LAST_DRIVER_PID=$(printf '%s\n' "$LAUNCH_OUT" | sed -n 's/.*PID driver \([0-9][0-9]*\).*/\1/p' | tail -1)
  sanitize_last_driver_pid
  LOG=$(printf '%s\n' "$LAUNCH_OUT" | sed -n 's/.*log=\([^ ]*\).*/\1/p' | tail -1)
  [ -z "$LOG" ] && { echo "$(ts) ⚠ ABORT: no pude extraer el log del lanzamiento."; exit 1; }
  wait_driver

  # R1: sync FINAL sin confirmar (lo escribió el driver de esta task): el driver dejó el motor VIVO a
  # propósito. failed/sync_unconfirmed + ABORT; el motor NO se mata aquí (puede tener datos sin guardar).
  # PLAN-DESVIO (menor): orden de las señales tras wait_driver = sync_unconfirmed > kill.flag (RAM) > user_stop >
  # clasificación normal (el plan no fija el orden): un fallo real (motor vivo sin guardar, RAM) pesa más que la parada.
  if [ -f "$SYNCFLAG" ]; then
    motivo=$(grep '^reason=' "$SYNCFLAG" | head -1 | cut -d= -f2-)
    rm -f "$SYNCFLAG"
    ENGINE_LEFT_ALIVE=1
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status failed \
      --cause sync_unconfirmed --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
    echo "$(ts) ⚠ ABORT: task $idx ($label): el guardado FINAL no se confirmó (${motivo:-sync_unconfirmed}) y el motor SIGUE VIVO a propósito: no lo mato. Persiste con 'python3 sqx/stop_and_persist.py $PROJ' (reintenta el guardado con el motor vivo) o revisa el motor; después reanuda: bash $ORCH_SELF $PROJ $idx"
    exit 1
  fi

  # Fase 2: kill.flag del vigilante RAM -> failed/ram_kill (el flag lo escribió el
  # driver de esta task; aquí lo recoge el escritor del state).
  if [ -f "$KILLFLAG" ]; then
    motivo=$(head -1 "$KILLFLAG")
    rm -f "$KILLFLAG"
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status failed \
      --cause ram_kill --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
    echo "$(ts) ⚠ ABORT: task $idx ($label) MATADA POR RAM ($motivo). Reanuda con menos RAM: bash $ORCH_SELF $PROJ $idx"
    exit 1
  fi

  # logs por task (el driver también copia a OUTDIR raíz, pero se pisan entre tasks)
  TDIR="$OUTDIR/task_$(printf '%02d' "$idx")_$(printf '%s' "$label" | tr ' /' '__')"
  mkdir -p "$TDIR"
  cp "$LOG" "$TDIR/runtask.log" 2>/dev/null
  cp "/tmp/${PROJ}_run/monitor.log" "/tmp/${PROJ}_run/engine.log" "$TDIR/" 2>/dev/null

  # R6: parada pedida mientras corría esta task: su driver ya persistió (la bandera la lee tras terminar) →
  # failed/user_stop, no se lanza ninguna más, rc 3
  stop_requested && user_stop_exit 1

  if ! grep -aq "proyecto TERMINADO" "$LOG"; then
    motivo=$(grep -aE "MOTOR MUERTO|ERROR|ABORT" "$LOG" | tail -1)
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status failed \
      --cause engine_died --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
    echo "$(ts) ⚠ ABORT: task $idx ($label) no terminó limpio ${motivo:+($motivo)}. Reanuda: bash $ORCH_SELF $PROJ $idx"
    exit 1
  fi

  # H11 (debate 2026-09-27): 'TERMINADO' no basta — validar que el Output declarado en el
  # .cfx quedó POBLADO (un banco misconfigurado/no declarado completa el pipeline sobre
  # vacío en silencio). Conteo de .sqx en DISCO (el motor ya está muerto; el driver hizo
  # el persist final). Un 0 puede ser legítimo (build sin aceptadas) → abortamos igual:
  # en un pipeline de producción un Output vacío es un evento que hay que mirar; si es
  # legítimo, reanuda desde la SIGUIENTE task.
  OUTBANK=$(PYTHONPATH="$REPO/sqx" PROJECTS_DIR="$PROJECTS_DIR" python3 - "$PROJ" "$label" <<'PY'
import sys
import builder_registry as REG
print(REG.task_output_databanks(sys.argv[1]).get(sys.argv[2]) or "")
PY
)
  if [ -z "$OUTBANK" ]; then
    # task terminada pero el Output no se pudo resolver en el .cfx -> aviso H11;
    # para el state la task SÍ terminó -> done
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status done \
      --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
    echo "$(ts) ⚠ task $idx OK pero sin Output databank resuelto en el .cfx — verifícalo a mano (H11)."
  else
    N=$(find "$PROJDIR/databanks/$OUTBANK" -maxdepth 1 -name '*.sqx' 2>/dev/null | wc -l | tr -d ' ')
    if [ "${N:-0}" -eq 0 ]; then
      python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status failed \
        --cause output_empty --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
      echo "$(ts) ⚠ ABORT: task $idx ($label) terminó 'OK' pero su Output '$OUTBANK' tiene 0 .sqx en disco (H11). Si la task NO corrió (p.ej. 'No strategies to retest' en el engine log), reanuda desde ESTA task: bash $ORCH_SELF $PROJ $idx — si el 0 es legítimo, reanuda desde la siguiente: bash $ORCH_SELF $PROJ $((idx + 1))"
      exit 1
    fi
    python3 "$STATE_BIN" "$STATE_FILE" set --task "$TKEY" --status done \
      --peak-rss-mb "$(task_peak "$TKEY")" >/dev/null 2>&1 || true
    echo "=== $(ts) TASK $idx OK: $label — Output '$OUTBANK' con $N .sqx en disco ✓ ==="
  fi
  barrier $((idx + 1))   # la JVM puede tardar en morir tras el DONE del driver
done

# PLAN-DESVIO (menor): el plan solo añade "ya puedes abrir la GUI" al mensaje final. El restore --slim acumulado se
# hace ANTES del veredicto (si no, pre_gui_check vería el marcador y nunca daría luz verde en una cadena --slim); el
# trap EXIT no lo repite (SLIM_RESTORED).
restore_slim_on_exit
if GUI_WHY=$(gui_verdict); then
  echo "=== $(ts) PIPELINE PER-TASK COMPLETO: $PROJ — artefactos en $OUTDIR — ya puedes abrir la GUI ==="
else
  echo "=== $(ts) PIPELINE PER-TASK COMPLETO: $PROJ — artefactos en $OUTDIR — NO abras la GUI todavía: ${GUI_WHY:-no pude comprobar si es seguro} ==="
fi
