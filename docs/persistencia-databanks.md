# Persistencia, sync y databanks en ejecución headless

> Leer al ejecutar retests masivos, exportar o sincronizar bancos, materializar bancos a mano,
> re-ejecutar ausentes de una matriz, o archivar bancos/proyectos (gui_slim, cuarentena). Básicos
> de persistencia y GUI-sobre-sqcli: `docs/sqx_cli_commands.md` §6.1-6.2.

## Flujo de retest masivo que funciona (piloto 2026-08-30)

Proyecto Retest generado clonando y mutando un `.cfx` (el generador no está incluido en esta
copia). Inyectar `.sqx` en `databanks/Results/` → motor caliente
(`tail -f /dev/null | sqcli ...`) → `-project action=start` **sin syncfromfiles manual** → fin por
`TASK FINISHED` en `log/global_log_*.log` → `-databank action=synctofiles name=Results` → kill.
Ritmo: 25 estrategias en ~2 s.

## Carga, sync y export

- **Auto-sync al arrancar**: el motor carga los bancos de disco solo; `syncfromfiles` manual
  encima = doble carga (4999→9998, cada estrategia retesteada 2×).
- **Carga lazy vs START** (2026-09-28): el motor responde HTTP mientras sigue cargando bancos;
  un `start` inmediato corre la task con el input vacío (`No strategies to retest` + `Project
  finished` a los ~30 s). Barrera en `run_pipeline_headless.sh` §2b y `retry_project.wait_loaded`:
  "In databank" estable 3 lecturas × 5 s (timeout 5 min), quitando separadores de miles
  (`tr -d ','`). Misma barrera antes de exportar con motor idle.
- **`sqcli -run` no espera la carga**: un export en `-run` sobre un proyecto de 3,5 GB generó un
  CSV con solo cabecera. Barrera con motor interactivo + HTTP, o bancos pequeños.
- `count` de banco vacío/no cargado devuelve "Number of strategies" sin `Records: N`.
- `count` con espacios en el nombre trunca igual que `synctofiles` ("Databank 'Last' doesn't exist", B144).
  `-databank action=list project=<p>` da `<banco>, Records: N` de todos los bancos: es lo que usan
  `stop_and_persist.py` y el monitor. En un Build genético, `Last generation` puede tener estrategias en RAM y 0 en
  disco: no se puede guardar por HTTP y el reporte lo marca "en riesgo".
- **Persist con timeout largo**: `save`/`synctofiles` de bancos gordos (~290 KB/`.sqx` con
  `orders.bin`) superan 120 s; curl a 120 s + kill truncó un sync (2754/4999). Usar curl con
  timeout largo (1500 s / `--max-time 1800`). Desde 2026-10-26 el driver y `stop_and_persist.py` usan
  `SQ_LONG_SECS` (1800) y el driver confirma cada banco por la línea `Databank '<proy>/<banco>' saved - files
  before sync X / after sync M …` del motor (el `synctofiles` es síncrono aunque responda "Synchronization
  started."); si el guardado final no se confirma, no mata el motor.
  Si no hay nada nuevo desde el último guardado de ese motor, B144 no escribe ninguna línea (2026-10-27; el primer
  guardado de un motor recién arrancado sí la escribe): el driver lo da por guardado
  "sin cambios" si el `count` del motor coincide con los `.sqx` en disco, y si no, sin confirmar al momento (sin
  esperar los 30 min). Ojo: un `.sqx` puede llevar varias estrategias; en ese caso no cuadra y queda sin confirmar.
- Export: `file=` se resuelve contra el cwd del motor → ruta absoluta. Export de TRADES por HTTP
  sale vacío → `orderstocsv` (`docs/sqx_cli_commands.md` §6). Re-exportar tras recargar = mismo contenido en
  otro orden de filas → comparar con `sort | diff`.
- El input de un retest sigue en RAM y se re-sincroniza a disco al cerrar → limpiar después, no
  abortar. Limpiar bancos solo con el motor MUERTO (el sync borra ficheros que el motor no tiene
  en RAM).
- `.sqx` corruptos sueltos (~3/27k, "zip headers not found"): la carga los descarta y el sync los
  borra → reconciliar conteos contra CSV. Un `.sqx` puede llevar varias estrategias (27.266
  ficheros ↔ 37.210 estrategias).
- El motor añade el banco `Results` al config al arrancar (los guards de config deben tolerarlo).
- `save` con "Missing parameter 'folder'" era bug de B143 (cosmético); arreglado en B144.

## Bancos declarados y no declarados

- **Banco copiado a mano en `databanks/` no existe** hasta declararlo en `config.xml`
  (`<Databank name=…/>`); el `Input` de la task no basta (MC_ER 2026-10-05: 0 ms, `Total tested:
  0`). Declararlo con **`syncType="Auto-sync never"`** (con auto-sync el sync puede borrar
  ficheros no cargados).
- **Banco poblado en RAM que no es Output de ninguna task** = invisible para list/count/copy/
  export aunque el pipeline funcione.
- **El `Filters result` viaja dentro del `.sqx`**: un banco re-poblado desde snapshot exporta sus
  etiquetas PASSED/FAILED históricas → base para reconstruir columnas de la matriz.
- **GUI**: sincroniza a disco desde SU RAM (carga lazy: contadores parciales) y su "clear all"
  vacía los bancos Y lo sincroniza (detectable por el mtime del `.cfx`) → snapshot de databanks
  antes de tocar GUI/exports.

## Re-ejecución selectiva de ausentes (2026-09-28)

`sqx/pass_matrix.py --dir <csvs> [--roster Results] [--strict]` (estrategia×prueba:
PASSED/FAILED/vacío) + `sqx/retry_project.py plan|build|export|merge`: crea `<proj>_retry` con una
task por prueba y bancos `Retry_<prueba>` copiando los `.sqx` ausentes desde un snapshot
(`Strategy Name`→`<name>.sqx` 1:1), corre con el orquestador per-task y mergea a la matriz.
Dato: de los ausentes MC re-ejecutados solo ~25-44% pasaron → los ausentes MC no son puro azar.

## gui_slim y cuarentena de proyectos (2026-10-05)

`python sqx/gui_slim.py <proy> <archive|restore|status> [--banks A,B]` mueve bancos pesados a
`<proj>/databanks_archive/` (rename, nada se borra) con manifiesto y restore verificado 1:1.
Fail-loud: GUI/motor vivos (pgrep), destino existente (incluso vacío: `shutil.move` anidaría
`Banco/Banco/`), banco inexistente, restore sin manifiesto o con descuadre. Backup diario
`project.cfx.bak_guislim_<fecha>`; `--no-process-check` solo vale para `status`. Sin `--banks`
usa `DEFAULT_ARCHIVE[proyecto]`, que solo tiene un proyecto del autor: en la práctica, pasa siempre
`--banks`. Tests: `tests/sqx/test_gui_slim.py`.
- Ocultar proyectos enteros: mover la carpeta a `user/projects_archive/` (vista) o
  `user/projects_quarantine_<fecha>/` (eliminables reversibles; no `rm`).
- Al matar la GUI a la fuerza recrea esqueletos (~200-800 KB) de los proyectos que tenía
  abiertos: un proyecto "eliminado" que reaparece pequeño es un shell; los datos reales están en
  la cuarentena.
