# Cascadas de proyectos y corridas desatendidas

> Leer al encadenar proyectos, planear una noche headless o exportar resultados entre tasks de
> una cascada viva.

## Cascada de proyectos (runbook 2026-09-30)

- `bash sqx/run_cascade_projects.sh <from_idx> <p1> [p2 ...]`: proyectos en secuencia, cada uno
  con el orquestador per-task; barrera proceso+puerto entre proyectos; si uno falla se loguea y
  sigue (reanudar a mano: `run_pipeline_per_task.sh <proy> <idx>`). Log
  `sqx/output/cascada_*_<fecha>.log`. Excepción (2026-10-26): si lo para el usuario (Stop & persist → el
  orquestador sale con rc 3), la cascada no lanza los proyectos que faltan y sale con rc 3.
- Un solo `from_idx` para todos → exige estructura de tasks idéntica (clones de test lo cumplen);
  si divergen, proyecto a proyecto.
- Lanzar en background; vigilar el log de cascada, el `global_log` del proyecto y el monitor.
  Entre tasks hay un gap sin motor de ~1-4 min (crece con los bancos): el monitor lo muestra como
  "⏸ EN PROCESO · motor en pausa" (detecta el orquestador, también su auto-copia en `/tmp`, y lee la task
  en curso de `<proy>_state.json`), no IDLE/FINISHED.
- Al terminar cada proyecto: exportar los PASSED (el script de export a local no está incluido en
  esta copia).

## Exportar CSV de una cascada

- Al terminar cada task, el driver exporta un CSV por banco (los que no tienen espacios y no están
  archivados por `--slim`) a `sqx/output/<proy>_<fecha>/databank_<banco>.csv`; cada task sobrescribe
  los de la anterior, así que al final queda la foto de la última. Para exports completos de
  bancos con espacios o de todo el proyecto: al cierre, banco a banco, con motor idle + barrera de
  carga lazy.
- Para exportar a mitad de cascada: justo tras "TASK N OK" no hay motor (connection refused);
  exportar contra el motor vivo de la task siguiente (lectura, no la interfiere).
- `run_pipeline_headless.sh` no soporta `EXPORT_CSVS` (variable de otro driver, no incluido en
  esta copia).

## Corridas largas desatendidas (runbook 2026-09-09)

- Varios proyectos en secuencia: `run_cascade_projects.sh` (arriba). Entre proyectos hace falta
  barrera proceso+puerto: sin ella, el guard de instancia única aborta el siguiente si la JVM
  tarda en morir.
- Protocolo post-build del driver antes del kill: save+synctofiles + `databank action=export` CSV
  por banco + diagnósticos de presión de selección (del engine.log) + copia de logs →
  `sqx/output/<proj>_<yyyymmdd>/`.
- `MAXITER` (tope por task, default 48×15 min = 12 h) sobreescribible por env.
- Tiempos a presupuestar: `MC_Randomize_parameters` failure-inclusive en H2 (Backtest mode 0,
  1.400 estr. × 200 sims) ≈ 2 estrategias/min = horas; con cadena filtrada o cache mode 1 vuela.
- Runner zombi tras completar (wrapper a 0% CPU tras exportar): matarlo es seguro si
  `sqx/output/<proj>_<fecha>/` ya tiene los CSVs.
- Para slicing temporal de supervivientes no hacen falta tasks FullRange si Real_OOS retestea el
  rango completo: sus `.sqx` ya son el artefacto.
