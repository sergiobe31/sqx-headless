# SQX B144: cambios frente a B143 relevantes para el stack headless

> Leer al dudar de si un comando/landmine documentado para B143 sigue vigente, al usar el MCP
> integrado o al instalar otra versión. Upgrade a B144: 2026-10-07, con migración manual de `user/`.

- **CLI** (diff empírico 2026-10-07): `-project`, `-databank`, `-symbol`, `-instrument`, `-tools`,
  `-run`, `-gui`, `-waitfor`, `-execute`, `-deletefile`, `-license`, `-exit` idénticos, y los
  formatos de respuesta clave también ("List of available projects", `Records: N`). Novedades:
  `-data` gana datasource `mt5api` (`path`/`datefrom`/`dateto`); `-stockgroups` → `-stockgroup`
  (actions `update`, `importstocks`, `exportstocks`, `import`, `export`, `list`); comando nuevo
  `-brokerprofile`.
- **Arreglado en B144**: `action=save` ya no falla con "Missing parameter 'folder'" (`save
  folder=...` salvó 2.162 `.sqx`). Ojo (2026-10-21): `save` SIN `folder=` (como lo lanza el driver)
  sigue respondiendo "Missing parameter 'folder'"; persiste el `synctofiles` posterior. Sin re-probar en B144: `-symbol action=edit` (roto en B143).
- **Respuestas del motor que el stack lee** (verificado 2026-10-27): el status tras `-project action=stop` es igual
  que en marcha (solo contadores; ni "stopped" ni "not running"); la respuesta al stop es `HH:MM:SS Stopping project
  <p>` + `Project execution stopped.` (también con el proyecto ya parado), y es lo que usa `stop_and_persist.py`;
  `synctofiles` sin nada nuevo desde el último sync de ese motor responde "Synchronization started." sin escribir la línea `Databank '<p>/<b>' saved …`; `count` con espacios en el nombre lo corta en el primer espacio (usar `-databank action=list project=<p>`).
- B144 corre ARM nativo (B143 iba bajo Rosetta 2). El hardware ID de licencia cambia por
  instalación (vigilar seats).
- **Configs JVM**: la instalación trae `-Xms4g` sin `-Xmx` en `StrategyQuantX.config` (GUI) y
  `sqcli.config` (motor) → re-tunear ambas con SQX cerrado y manteniendo CRLF (valores e
  incidente en `memoria-heap.md`).
- **Servidor MCP integrado** (verificado 2026-10-07): `http://localhost:8080/mcp` (streamable
  HTTP; comparte puerto con Remote Access: exponer uno expone el otro). Tools `list_projects`,
  `list_databanks`, `list_strategies`, `get_strategy_stats`, `run_project`, `stop_project`. Sin
  export/synctofiles/copy, sin control por task ni persistencia → no sustituye al stack
  headless. Requiere SQX 144 corriendo. Para registrarlo en un cliente MCP (p. ej. el `.mcp.json`
  de un proyecto de Claude Code): `"sqx": {"type": "http", "url": "http://localhost:8080/mcp"}`.
