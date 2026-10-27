#!/bin/bash
# run_cascade_projects.sh — encadena PROYECTOS SQX en secuencia, cada uno con el
# orquestador per-task (un motor fresco por task — landmine de memoria 2026-09-27,
# obligatorio en cascadas de retest sobre bancos gordos; NO usar run_production_night.sh
# con estos, que corre el proyecto entero en UN motor).
#
# Patrón de barrera entre proyectos tomado de run_production_night.sh: espera a que
# muera sqcli y a que el puerto 5050 quede libre antes del siguiente (sin ella, el
# guardia de instancia única del siguiente puede abortar si la JVM tarda en morir).
# La barrera NO MATA NADA (R3): el motor de cada task ya lo remata el orquestador; un sqcli que
# siga vivo aquí es ajeno. El puerto tiene tope PORT_WAIT_SECS: pasado, ABORTA la cascada entera
# diciendo quién lo ocupa (antes esperaba para siempre).
#
# Los proyectos son independientes: si uno falla se LOGUEA y la cadena sigue con el
# siguiente (el fallo se reanuda con run_pipeline_per_task.sh <proy> <idx> a mano).
# EXCEPCIÓN (R6, decisión D2 de Sergio 2026-10-21): si el orquestador de un proyecto sale con rc 3
# ("PARADA por el usuario": Stop & persist) la cascada se DETIENE ENTERA —no se lanza ninguno de los demás— y
# sale con rc 3. Quien lea el rc (crons, alertas) verá 3 ≠ 0.
#
# Uso:   bash sqx/run_cascade_projects.sh <from_idx> <proy1> [proy2 ...]
#        from_idx = índice 0-based de `python sqx/run_task_headless.py <proy> --list`
#        (mismo índice para todos los proyects — deben compartir estructura de tasks).
#        GUI cerrada y sin sqcli vivo. Lanzar en background (run_in_background/nohup):
#        ⚠ nohup NO protege de un SIGTERM directo (lección 2026-09-29).
# Log:   sqx/output/cascada_<proy1>_..._<fecha>.log   (raíz: SQX_OUTDIR si está definida)
#
# Knobs de entorno: PORT (5050), PORT_WAIT_SECS (300), BARRIER_POLL_SECS (5),
# SQX_ENGINE_PROC_NAME (nombre EXACTO del proceso del motor, default sqcli; solo tests),
# SQX_OUTDIR (raíz del log).
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FROM_IDX="$1"; shift
PROJS=("$@")
ts(){ date '+%Y-%m-%d %H:%M:%S'; }
ENGINE_PROC="${SQX_ENGINE_PROC_NAME:-sqcli}"   # nombre EXACTO del proceso del motor (R5)
PORT="${PORT:-5050}"
PORT_WAIT_SECS=${PORT_WAIT_SECS:-300}
BARRIER_POLL_SECS=${BARRIER_POLL_SECS:-5}
OUTBASE="${SQX_OUTDIR:-$REPO/sqx/output}"
mkdir -p "$OUTBASE"
OUT="$OUTBASE/cascada_$(echo "$PROJS" | tr ' ' '_' | tr '/' '_')_$(date +%Y%m%d_%H%M%S).log"
echo "=== $(ts) CASACADA desde task idx $FROM_IDX: ${PROJS[*]} ===" | tee "$OUT"

K=0
for P in "${PROJS[@]}"; do
  K=$((K + 1))
  echo "--- $(ts) barrera proceso+puerto antes de $P ---" | tee -a "$OUT"
  while true; do
    pgrep -x "$ENGINE_PROC" >/dev/null 2>&1; PRC=$?
    [ "$PRC" -eq 1 ] && break                    # rc 1 = ningún sqcli vivo
    if [ "$PRC" -gt 1 ]; then
      # PLAN-DESVIO (menor): el plan fija C1 en la barrera del orquestador, wait_driver, A7 y el vigilante; aquí se aplica
      # el mismo fail-closed a la barrera de la cascada: pgrep ROTO ≠ "no hay motor", sin saberlo no se lanza el siguiente
      echo "!!! $(ts) ABORT de la cascada: pgrep falló (rc=$PRC) comprobando si hay un sqcli vivo: sin saberlo no lanzo $P." | tee -a "$OUT"
      exit 1
    fi
    echo "$(ts) esperando a que muera sqcli previo..." | tee -a "$OUT"; sleep "$BARRIER_POLL_SECS"
  done
  if command -v nc >/dev/null; then
    t_port=$(date +%s)
    while nc -z -w 3 localhost "$PORT" 2>/dev/null; do
      if [ $(( $(date +%s) - t_port )) -ge "$PORT_WAIT_SECS" ]; then
        if command -v lsof >/dev/null; then
          owner=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)
        else
          owner="(lsof no disponible)"
        fi
        # el `exit` va FUERA del pipe: dentro de `{ … } | tee` saldría solo el subshell
        msg="!!! $(ts) ABORT de la cascada: el puerto $PORT sigue ocupado tras ${PORT_WAIT_SECS}s y no se mata nada. Dueño:
${owner:-(lsof no ve ningún LISTEN en :$PORT)}
Libera el puerto y relanza desde $P: bash sqx/run_cascade_projects.sh $FROM_IDX $P ..."
        printf '%s\n' "$msg" | tee -a "$OUT"
        exit 1
      fi
      echo "$(ts) esperando puerto $PORT libre..." | tee -a "$OUT"; sleep "$BARRIER_POLL_SECS"
    done
  fi
  echo "=== $(ts) lanzando $P (per-task, from_idx=$FROM_IDX) ===" | tee -a "$OUT"
  bash "$REPO/sqx/run_pipeline_per_task.sh" "$P" "$FROM_IDX" 2>&1 | tee -a "$OUT"
  RC=${PIPESTATUS[0]}   # estado del orquestador, no del tee
  if [ "$RC" -eq 0 ]; then
    echo "=== $(ts) $P COMPLETADO ===" | tee -a "$OUT"
  elif [ "$RC" -eq 3 ]; then
    # parada del usuario: NO sigo con el resto (el usuario pidió parar, no "saltar este proyecto")
    REST="${PROJS[*]:$K}"
    echo "$(ts) ⏹ PARADA por el usuario: no sigo con los demás proyectos (el orquestador de $P salió con rc 3; sin lanzar: ${REST:-ninguno}). Reanuda: bash sqx/run_cascade_projects.sh $FROM_IDX $P ${REST} (el orquestador de $P dijo desde qué task)" | tee -a "$OUT"
    exit 3
  else
    echo "!!! $(ts) $P FALLÓ (rc=$RC) — ver log arriba; la cadena sigue con el siguiente proyecto" | tee -a "$OUT"
  fi
done
echo "=== $(ts) CASACADA TERMINADA: ${PROJS[*]} ===" | tee -a "$OUT"
