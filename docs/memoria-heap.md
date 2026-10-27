# Memoria, heap y motor colgado: medidas, incidentes y reglas del orquestador

> Leer al diagnosticar OOM/swap/GC/HTTP zombi, al dimensionar el heap, al usar `--slim` o el
> vigilante, o antes de tocar el orquestador per-task.

## Cuánta RAM usa el motor (experimento ramtest_mnq, 2026-10-05)

Mismo retest de 200 estrategias: pico RSS
3,8 GB con bancos vacíos vs **9,1 GB con los bancos del proyecto presentes** (8.836 cargadas).
- El motor carga TODOS los databanks declarados, aunque ninguna task activa los use.
- **~0,9 GB de heap por 1.000 `.sqx`** residentes.
- **Capar el heap SIN archivar bancos trunca en silencio**: con Xmx6g el guard interno (~90%
  del heap) dejó un banco en 840/2.163 y otros a 0; si el truncado es el input, la task corre
  sobre datos parciales sin avisar.
- Arranque JVM ~12-13 s constante + parseo de bancos (~16 s/200 `.sqx`) → el motor fresco por
  task casi no penaliza.
- Modo adoptado: bancos archivados salvo input+output de la task + `-Xms1g -Xmx6g` + motor fresco por task +
  vigilante de RSS → pico medido 2,6 GB.

## Incidentes que fijaron las reglas

- **Pipeline multi-task en UN motor** (`gold_h4_prod_oos`, 2026-09-27): 8 retests acumularon
  ~14k estrategias residentes (el input de cada retest no se libera) → a ~7 h en Mac de 16 GB:
  CPU 328% con throughput de 16/min a ~0,7/min, 17 GB de swap y HTTP zombi. El sync incremental
  quincenal NO era la causa (1-47 s/ciclo, solo el delta). Relanzar desde la task siguiente en
  motor fresco: Real_OOS (2.311 estr.) en 76 s. Origen del orquestador per-task.
- **Retest de bancos grandes** (fitab A3, 2026-09-11/12), dos muros distintos: (a) in-project,
  los ciclos save/sync son O(n) sobre los ficheros del banco → banco de 25k+ se estrangula
  (~450× más lento, peor con HTTP concurrente del monitor); (b) monolítico, cargar todo el banco
  revienta el heap (OOM a ~45k residentes; lo no persistido se pierde). Regla: lotes de ~5k, un
  motor corto por lote, CSV por lote, merge final (el script de lotes no está incluido en esta
  copia; si se replica, drenar el stage de entrada solo tras rc=0 o el bucle reprocesa el lote 1
  sin fin). Ejecución real: 6 lotes × 5k en 47 min (~72/s).
- **Instalación nueva sin `-Xmx`** (B144, 2026-10-07): la config trae `-Xms4g` sin `-Xmx` → heap
  max = 25% de la RAM (~4 GB). Al abrir un proyecto con 1.797 estrategias: guard al 88%, ráfaga de
  `OutOfMemoryError: Java heap space`, GC al ~800% CPU. Configs de la instalación de referencia
  (2026-10-09): GUI `-Xms4g -Xmx10g`; motor `-Xms1g -Xmx6g -XX:+UseG1GC` (backup `*.bak_*` junto a
  cada config antes de editarla).
- **GUI congelada por bancos pesados** (2026-10-05): la GUI carga vistas de todos los bancos al
  abrir; hubo que matarla con kill -9. Remedio: `gui_slim.py archive` (ver
  `persistencia-databanks.md`) y verificar conteos + integridad del `.cfx` tras el kill.

## `--slim` y vigilante (2026-10-06)

- `sqx/bank_plan.py` resuelve input(s)/output de cada task (Retest = 1 input, Build = 0..N,
  Filtering = Source databank; fail-loud). El driver archiva vía gui_slim los bancos que la task
  no usa, salta su persist y restaura al final (presence-aware). Marcador
  `databanks_archive/.runner_session.json` (pid del driver) + reconciliación al arranque si la
  corrida anterior murió. En pipeline (`SLIM_IN_PIPELINE=1`) el restore es acumulado al final
  de la cadena. Estado en reposo = proyecto completo en disco.
- (2026-10-21) Hasta esta fecha el vigilante se apagaba tras la 1.ª muestra en el camino per-task
  (`driver_pid=$PPID` = el lanzador, que sale enseguida); arreglado con `$$`. Las telemetrías
  anteriores tienen 1 fila por task: no valen para calibrar umbrales.
- Vigilante del driver: telemetría cada 30 s a `sqx/output/<proj>_telemetry.csv` (RSS, % libre,
  swap); WARN a 8 GB o con derivada de RSS >500 MB/min (gateada a post-carga); KILL a 9 GB o
  RAM libre <10% (SIGTERM + gracia 60 s + SIGKILL); umbrales por env `WD_*`.
- `sqx/run_state.py`: `<proj>_state.json` con pending/running/done/failed y causa
  (`ram_kill`/`engine_died`/`output_empty`); un solo escritor (orquestador en pipeline, driver
  en task suelta).
- El motor recrea dirs de banco vacíos al arrancar: antes de un `gui_slim restore` manual,
  `rmdir` del dir vacío o el restore aborta (guarda anti-anidado).

## Reglas del orquestador per-task (debate 2026-09-27)

Vigentes:
- Si el arranque no se confirma en `wait_start`, `run_task_headless.run()` mata driver+motor
  (antes quedaba detached corriendo el pipeline entero) (H1).
- El driver detecta "Project finished" también en la fase quick (H2).
- Liveness probe de 3 s entre saves del persist; HTTP ciego se clasifica por CPU (<5% → kill -9
  sin persist HTTP; alta → saltar solo esa iteración) (H5).
- `mutate_active` aborta con `.bak` huérfano; el orquestador restaura el `.cfx` en trap EXIT (H8).
- Log único por lanzamiento `/tmp/<proj>_task<idx>_<ts>.log`, copiado a `sqx/output/` (H9).
- Tras cada task, Output declarado con count de `.sqx` en disco > 0 o aborta (H11).
- `from_task_idx` 0-based de `--list` ≠ `task=N` 1-based de startFromTask (H13).

Preguntas abiertas de conocimiento: (H3) ¿el update-in-place de un Retest re-corrido reemplaza
o mergea? — verificar count del Output antes de fiar una reanudación a medias; (H6) causalidad
GC vs swap del colapso de memoria no establecida (N=1; la telemetría del vigilante no mide GC).
