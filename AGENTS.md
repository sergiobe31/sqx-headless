# AGENTS.md — sqx-headless

Instrucciones para asistentes de IA (Claude Code, Codex, Cursor…) que operan este repo en el Mac
del usuario. También valen como referencia rápida para personas; la guía de uso es `README.md`.

## Qué es y qué no

Stack para **ejecutar, vigilar, parar y guardar** custom projects de StrategyQuant X (SQX) sin la
GUI, en macOS, a través de su motor de línea de comandos `sqcli` (servidor HTTP en el puerto 5050).
Los proyectos los crea y configura el usuario en la GUI de SQX. Este repo **no** crea ni edita
configuraciones de build (la excepción es `retry_project.py build`, que crea un proyecto
`<proy>_retry` para repetir pruebas), y no funciona en Windows.

Es una **copia generada** desde un repo privado: no propongas cambios al código como si fueran a
integrarse. Si encuentras un fallo, descríbelo al usuario (fichero, línea, síntoma) para que lo
reenvíe.

Probado con SQX **build 144** (Apple Silicon). Con otras versiones, revisa `docs/version-b144.md`.

## Reglas para el asistente

- **Pide confirmación antes de**: parar o matar procesos (`sqcli`, drivers, la GUI), mover o
  archivar databanks (`gui_slim.py`), restaurar o editar un `project.cfx`, borrar cualquier cosa
  dentro de `user/projects/`, o lanzar algo que vaya a durar horas.
- **Nunca** abras la GUI de SQX, ni le digas al usuario que la abra, mientras haya un `sqcli` vivo.
- **Nunca** borres databanks con `rm`. Para apartar bancos usa `gui_slim.py`, que los mueve y es
  reversible.
- **Para comprobar si el motor está vivo usa `pgrep -x sqcli`** (el proceso se llama así en B144). Los
  scripts detectan el motor por nombre de proceso y el orquestador/driver por patrones anclados, así que tus
  comandos de vigilancia no interfieren con ellos. Para buscar en ficheros con corchetes, ponlos entre
  comillas (en zsh `sq[c]li` sin comillas es un glob).
- No edites `sqx/*.sh` mientras se ejecutan: bash relee el script por offset.
- Trabaja siempre con el venv activado (`source .venv/bin/activate`): los drivers llaman a
  `python3` del `PATH`.

## Primeros pasos en una sesión

```bash
source .venv/bin/activate
python sqx/paths.py                 # app de SQX resuelta; sqcli y user/projects existen
pgrep -x sqcli                      # ¿motor vivo? (vacío = no)
lsof -iTCP:5050 -sTCP:LISTEN        # ¿puerto del motor ocupado?
python sqx/run_task_headless.py <proyecto> --list   # tasks e índices (0-based)
```

La ruta de SQX se resuelve así: variable `SQX_APP` > la única `StrategyQuantX*.app` de
`/Applications` o `~/Applications` > el valor por defecto. Si hay varias instalaciones, el usuario
tiene que fijar `SQX_APP`.

## Arquitectura

| Fichero | Qué hace |
| --- | --- |
| `sqx/run_pipeline_per_task.sh` | Orquestador **por defecto**. Un motor nuevo por task; entre tasks remata el motor de SU task anterior si tarda en morir y aborta si hay uno ajeno (nunca lo mata); comprueba que el Output de la task tenga estrategias; si algo falla, aborta indicando el índice para reanudar; con la parada del usuario sale con rc 3. Aborta si ya hay otro orquestador del mismo proyecto. |
| `sqx/run_pipeline_headless.sh` | Driver de una task: arranca `sqcli`, espera a que carguen los bancos, `start`, sondeos y guardado incremental, vigilante de RAM y, al final, guardar (confirmado en el log del motor) + exportar CSV + parar. Si el guardado final no se confirma, deja el motor vivo. |
| `sqx/run_task_headless.py` | Lanza una task (`--mode only`) o desde una task (`--mode from`) activando y desactivando tasks en el `.cfx`, que restaura al terminar. `--list`, `--slim`. Ojo: por CLI, `--mode from` usa **un solo motor** para el resto del pipeline (el patrón que colapsa la memoria con bancos grandes); para pipelines usa el orquestador. En el monitor, "desde esta" sí usa el orquestador. |
| `sqx/run_cascade_projects.sh` | Varios proyectos seguidos, cada uno con el orquestador por task. |
| `sqx/run_state.py`, `sqx/bank_plan.py` | Estado de la corrida (`<proj>_state.json`); qué bancos usa cada task como entrada y salida (`python sqx/bank_plan.py <proy> --list`), archivado `--slim` y su reconciliación (`--reconcile`). |
| `sqx/stop_and_persist.py` | Parada. Con pipeline vivo: bandera `<proy>/.pipeline_stop_request` + `stop` a la task en curso; guarda su driver y el orquestador no lanza más (rc 3). Sin pipeline: `stop`, espera a que carguen los bancos, `synctofiles` (timeout `SQ_LONG_SECS`) y kill; no mata si un guardado falla ni si el motor sin HTTP tiene CPU alta (salvo `--force-kill`). `--wait-secs` (1800 por defecto; el monitor, 180). Dice si es seguro abrir la GUI. |
| `sqx/persist_before_gui.py` | Comprobación de solo lectura antes de abrir la GUI (no arranca el motor): rc 0 si es seguro, 1 con los motivos (motor, GUI, pipeline o cascada vivos; bancos `--slim` apartados; `.bak`). |
| `sqx/gui_slim.py` | Archiva y restaura bancos pesados en `<proj>/databanks_archive/`, con manifiesto. |
| `sqx/pass_matrix.py`, `sqx/retry_project.py` | Matriz estrategia × prueba y repetición solo de las pruebas que faltan. |
| `sqx/sqx_monitor.py`, `sqx/snapshot_loop.py` | Monitor Streamlit y auto-muestreo (`~/.sqx_monitor/`). |
| `sqx/inspect_build_tasks.py`, `sqx/show_reject_breakdown.py` | Qué configura cada Build task; desglose de rechazos del `global_log`. |
| `sqx/paths.py`, `sqx/paths.sh` | Única fuente de las rutas de SQX. |
| `sqx/builder_registry.py` | Esquema concepto → XML del Builder (lo usan el monitor y el inspector). |

## Runbook

1. Antes de lanzar: GUI cerrada, `pgrep -x sqcli` vacío y puerto 5050 libre.
2. Pipeline completo o desde una task:
   `nohup caffeinate -i bash sqx/run_pipeline_per_task.sh <proy> [from_task_idx] [--slim] > sqx/output/<proy>_launch.log 2>&1 &`.
   `from_task_idx` es el índice 0-based de `--list`. No es el `task=N` de `startFromTask`, que es un
   ID interno de SQX (`docs/sqx_cli_commands.md` §2). Índice y `--slim` en cualquier orden;
   un argumento desconocido da el uso y rc 2.
3. Una task suelta: `python sqx/run_task_headless.py <proy> --task "<title>" --mode only [--slim]`.
   Vuelve en cuanto la task arranca y el driver sigue en segundo plano: `caffeinate` aparte.
4. Varios proyectos con la misma estructura de tasks (sin `--slim`):
   `nohup caffeinate -i bash sqx/run_cascade_projects.sh <from_idx> <p1> [p2 ...] > sqx/output/cascada_launch.log 2>&1 &`.
5. Vigilar: `streamlit run sqx/sqx_monitor.py`, el log de lanzamiento, `sqx/output/<proy>_telemetry.csv`,
   `sqx/output/<proy>_state.json` y el `log/global_log_*.log` del proyecto. Lanzado desde el
   monitor, el log del orquestador está en `/tmp/<proy>_pertask_<ts>.log`.
6. Parar: `python sqx/stop_and_persist.py <proy> [port] [--wait-secs N] [--force-kill]` (o el botón del
   monitor). Con un pipeline en marcha lo detiene entero: el driver de la task en curso guarda, el orquestador
   escribe `⏹ PARADA por el usuario en la task N … Reanuda: …`, marca la task `failed/user_stop` y sale con
   rc 3; una cascada se detiene entera. Si la espera se agota, el reporte dice "parando" y la parada sigue
   (la bandera se queda hasta que el orquestador sale).
7. Antes de abrir la GUI: `python sqx/persist_before_gui.py <proy>` (solo comprueba; rc 0 = seguro).

Variables de entorno de los drivers: `POLL_MIN` (minutos entre sondeos, 15 por defecto; en cada
sondeo se guardan todos los bancos), `QUICK_SECS` (30: intervalo de los 6 sondeos iniciales),
`MAXITER` (sondeos máximos por task, 48 por defecto = 12 h con 15 min), `WD_WARN_RSS_MB` y
`WD_KILL_RSS_MB` (vigilante: 8192 y 9216 por defecto, pensados para 16 GB), `WD_FREE_PCT_MIN`
(10), `PORT` (5050), `SQX_OUTDIR` (raíz de la salida, `sqx/output/` por defecto), `SQ_LONG_SECS`
(1800: timeout de cada guardado), `SYNC_CONFIRM_SECS` (espera a la confirmación del guardado si el curl
se agotó), `SYNC_LINE_GRACE_SECS` (10: margen para la línea de confirmación tras un guardado respondido),
`KILL_GRACE_SECS` (30: gracia entre TERM y kill -9), `BARRIER_OWN_GRACE_SECS` y `BARRIER_FOREIGN_SECS`
(120: espera entre tasks a un motor propio / ajeno), `PORT_WAIT_SECS` (300: espera al puerto).

## Reglas operativas (trampas conocidas)

**Arranque y fin**
- Mac dormido = motor muerto. Las corridas largas van siempre envueltas en `caffeinate -i`, con el
  Mac enchufado y la tapa abierta (`caffeinate -i` no evita el sueño al cerrar la tapa). Dentro de una
  misma task, huecos de bastante más de 30 s en `<proy>_telemetry.csv` delatan que el Mac se
  durmió (entre tasks, 1-4 min sin muestras es normal).
- Cadencia del driver: 6 sondeos rápidos de 30 s y después uno cada `POLL_MIN` (15 min). Una task
  de 3-6 min termina justo después de la fase rápida, y el orquestador no se entera hasta 15 min
  después, con el motor parado al 0 % de CPU. No es un cuelgue: `TASK FINISHED` ya está en el
  `global_log`. Para retests cortos, `POLL_MIN=2`.
- Con `POLL_MIN` bajo, sube `MAXITER`: con `POLL_MIN=1` y el tope por defecto (48), el driver
  para la task a los 48 minutos. Por ejemplo, `MAXITER=600 POLL_MIN=1`.
- Fin real = `Project finished` en la salida del motor o `TASK FINISHED` en
  `log/global_log_*.log`. El estado HTTP puede quedarse congelado.
- Estado HTTP vacío no significa motor muerto. Compara `pgrep -x sqcli` con su % de CPU: menos
  del 5 % = colgado (kill -9); CPU alta = saturado pero trabajando. `stop_and_persist.py` aplica la
  misma regla.
- Estado vacío en 2 o más sondeos con el proceso muerto = el motor cayó (puede morir en silencio
  durante un sync). Se recupera relanzando desde esa task.
- `Running time so far` es acumulado del proyecto. `Strategies generated` sí se reinicia en cada
  task. El "In databank" del orquestador también es acumulado.
- Task activa = el último `TASK STARTED` sin `TASK FINISHED` en el `global_log`.
- Una task que "termina" en segundos con `No strategies to retest` y el output a 0 tuvo el banco de
  entrada vacío: carrera de carga lazy o banco no declarado. Ver `docs/persistencia-databanks.md`.
- Build random-generation con NPE `IntArrayList.getInt` al cerrar: el banco está íntegro; guarda
  ya. El fin se confirma con "Task finished".
- Instancia única: la GUI (`StrategyQuantX_ui` / `_nocheck`) y el motor no conviven.
- `sqcli` necesita `kill -9`, porque SIGTERM no basta. `nohup` no protege de un SIGTERM directo.
- Entre tasks, el orquestador espera a que no quede ningún `sqcli`. Al motor de su task anterior
  (mismo grupo de procesos que el driver) lo remata con TERM y `kill -9` si tarda; a uno ajeno no lo
  toca: tras `BARRIER_FOREIGN_SECS` aborta con instrucciones. Con el puerto ocupado por otro programa,
  aborta tras `PORT_WAIT_SECS` indicando el dueño.
- La parada por RAM del vigilante (SIGTERM, 60 s de gracia, SIGKILL) **no guarda**: se pierde lo
  generado desde el último sondeo.
- El driver solo dice "Ya puedes abrir la GUI" en una task suelta y cuando es seguro (motor muerto,
  sin bancos `--slim` apartados, sin `.bak`). En un pipeline, espera a `PIPELINE PER-TASK COMPLETO`.
- El monitor detecta el orquestador (también su auto-copia en `/tmp`), muestra la task en curso desde
  `state.json` y no deja lanzar otra corrida del proyecto mientras haya una viva.
- Si queda un `.cfx.pre_run_task.bak`, el `.cfx` puede tener tasks desactivadas en silencio. El
  monitor ofrece restaurarlo.

**Memoria** (detalle: `docs/memoria-heap.md`)
- El motor carga en RAM **todos** los bancos declarados (unos 0,9 GB de heap por 1.000 `.sqx`) y
  no libera el input de cada retest. Por eso se usa un motor nuevo por task y no uno para todo
  el proyecto.
- Limitar `-Xmx` sin archivar bancos **trunca en silencio** la carga (hay un guard interno al
  ~90 % del heap). Para eso está `--slim`.
- Cada instalación nueva de SQX trae `-Xms4g` sin `-Xmx` en `sqcli.config` y en
  `StrategyQuantX.config`. Hay que reajustarlos con SQX cerrado.

**Guardado y sync** (detalle: `docs/persistencia-databanks.md`)
- El motor sincroniza los databanks solo al arrancar. **Nunca** lances `syncfromfiles` a mano
  encima: carga doble y duplicados `(1)`.
- Sin `synctofiles`, los `.sqx` del disco no se reescriben. Guarda siempre antes de matar el motor.
- Los bancos con espacios en el nombre no se guardan por HTTP (`%20` trunca el nombre).
- `synctofiles` y exportar bancos gordos pueden durar minutos. El driver y `stop_and_persist.py` usan
  `SQ_LONG_SECS` (1800 s). El driver confirma cada guardado por la línea `Databank '<proy>/<banco>' saved
  - files before sync X / after sync M …` del motor (el `synctofiles` es síncrono aunque responda
  "Synchronization started.") y avisa si en disco hay menos `.sqx` que `M`. Si el guardado FINAL no se
  confirma, **no mata el motor**: lo deja vivo, marca la task `sync_unconfirmed` y el pipeline aborta;
  entonces `stop_and_persist.py`, que reintenta el guardado.
- Un guardado sin nada nuevo desde el anterior de ese motor no escribe esa línea: si el `count` del motor coincide con los `.sqx` en disco, el
  driver lo da por guardado ("sin cambios"); si no, queda sin confirmar al momento.
- El status de B144 tras un `stop` no cambia (solo contadores): `stop_and_persist.py` confirma la parada por la
  respuesta al stop ("Project execution stopped.") y lo envía una sola vez.
- El driver ya no lanza `save` (sin `folder=`, B144 responde "Missing parameter 'folder'"); guarda con
  `synctofiles`.
- `persist_before_gui.py` ya no escribe: un motor recién arrancado solo tiene lo que hay en disco, y
  con la carga lazy un `synctofiles` podía dejar bancos incompletos. Solo comprueba.
- Antes de `start` o de exportar, espera a que acaben de cargar los bancos ("In databank" estable
  3 lecturas × 5 s). `sqcli -run` **no** espera a esa carga.
- En los exports, `file=` se resuelve contra el directorio de trabajo del motor: usa rutas absolutas.
- Nunca abras la GUI sobre un `sqcli` vivo. Antes de tocar la GUI o exportar, haz una copia de los
  databanks.

**Databanks**
- Un banco solo existe para el motor si está declarado en `config.xml`. Los bancos de entrada que
  se crean a mano van con `syncType="Auto-sync never"`.
- Un banco con estrategias en RAM que no es el Output de ninguna task es invisible para
  list/count/export.
- `count` sin una línea `Records: N` significa banco a 0 o aún sin cargar, no un error.
- Un fichero `.sqx` puede contener varias estrategias. Los `.sqx` corruptos se descartan al cargar
  y el sync los borra: contrasta los conteos con los CSV.
- No arranques el motor con bancos en `databanks_archive/` salvo con `--slim`, que solo gestiona
  los que archivó él (los archivados a mano con `gui_slim.py` se ven vacíos). Limpia bancos solo
  con el motor MUERTO.
- Tras una corrida `--slim` que murió a medias: `python sqx/gui_slim.py <proy> status` y, si quedan
  bancos apartados, `python sqx/bank_plan.py <proy> --reconcile` antes de abrir la GUI (relanzar el
  pipeline también reconcilia al arrancar). Revisa después `databanks_archive/`: el restore se salta
  los bancos cuyo destino ya tiene ficheros.

**Shell**
- Bancos con decenas de miles de ficheros: usa `find -maxdepth 1 -name '*.sqx'`, nunca globs.
  Con globs, `ARG_MAX` falla en silencio y `ls | wc -l` da 0.
- Bajo `set -o pipefail`, `find | head` aborta por SIGPIPE: usa `sed -n 1,Np`.

**Monitor**: el plateau del fitness (pendiente de la mediana <1 %/h) es una alarma, no una orden de
parar. Si `running_secs` cae entre dos muestras, es una corrida nueva. `snapshot_loop.py` sigue
vivo aunque se cierre Streamlit; se para desde el botón o por PID.

## Diagnóstico rápido

| Síntoma | Causa probable | Acción |
| --- | --- | --- |
| `ABORT: ya hay un sqcli corriendo` o `hay un motor sqcli que no es de esta cadena` | Motor vivo o zombi de otra corrida | `pgrep -x sqcli`; si no hay corrida legítima, `stop_and_persist.py` (con confirmación) y relanzar con el índice del mensaje |
| `sync de <banco> NO confirmado: dejo el motor vivo` | El guardado final no se confirmó (sin línea del motor y `count` ≠ `.sqx` en disco, o curl agotado) | `stop_and_persist.py` (reintenta el guardado) y revisar `.sqx` en disco |
| Pipeline abortado | La causa está en `state.json` (`ram_kill` / `engine_died` / `output_empty` / `sync_unconfirmed` / `user_stop`) | Índice en la línea `ABORT … Reanuda:` del log de lanzamiento; logs por task en `sqx/output/<proy>_<fecha>/task_<n>_<nombre>/` |
| Task OK en segundos, output 0 | Banco de entrada vacío o no declarado | `docs/persistencia-databanks.md` |
| CPU 300 %+, throughput hundido, swap alto | Bancos acumulados en un solo motor | Orquestador por task, `--slim` |
| Banco cargado a medias sin error | `-Xmx` bajo sin archivar | `--slim` o subir el heap |
| La GUI no ve las estrategias del headless | No se guardaron a disco | Con motor vivo, `stop_and_persist.py`; `persist_before_gui.py` solo comprueba |

## Documentación (leer bajo demanda)

- `docs/sqx_cli_commands.md`: referencia de `sqcli`, anatomía del `.cfx`, guardado (§6.1-6.2),
  Build headless (§9) y resumen de trampas (§13). El código cita sus secciones.
- `docs/memoria-heap.md`: OOM, swap, GC, motor zombi; dimensionar el heap; `--slim` y vigilante.
- `docs/persistencia-databanks.md`: retest masivo, carga lazy, sync y export, bancos no declarados,
  repetir solo lo ausente, `gui_slim`.
- `docs/cascadas.md`: proyectos en secuencia, exportar entre tasks, protocolo post-build.
- `docs/version-b144.md`: cambios de B143 a B144 en la CLI, MCP integrado, configs de la JVM.

Los comentarios del código citan a veces documentos o herramientas del repo de origen que no
están en esta copia. Ignóralos.
