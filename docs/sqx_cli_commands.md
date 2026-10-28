# SQX desde terminal (`sqcli`) + anatomía de los custom projects (`.cfx`)

Recopilación de **todo lo que nos ha llevado a operar StrategyQuant X (B143) sin GUI**, vía su CLI
headless `sqcli`, **más la estructura de los Custom Projects** (los `.cfx`) tal como la hemos leído en
disco. Pensado para que **otro LLM** pueda retomar el trabajo: cada afirmación lleva su estado de
verificación, y los esquemas XML son los reales extraídos del bundle.

> **Regla de mantenimiento:** cualquier cosa nueva que aprendamos usando SQX por terminal o inspeccionando
> un `.cfx` se añade AQUÍ (no se queda en el chat).
>
> **Regla de limpieza (2026-06-26):** si generas un proyecto SQX **para testear algo** (calibración,
> diagnóstico, smoke test), eres responsable de **quitarlo después**: muévelo a `user/projects_quarantine_<fecha>/` (reversible) y que el
> usuario lo borre cuando quiera. No
> acumular proyectos basura en `user/projects/`. Solo se conservan las corridas reales y las plantillas
> (`Builder`, `Retester`, `PortfolioMaster`; `sqcli_testing` es un proyecto del autor de estas notas y no
> estará en tu instalación). Aplica también a símbolos custom (`-symbol delete`).
>
> Fuentes: `help.txt` oficial del bundle (`internal/web/SQUANT/help.txt`), inspección de
> `user/projects/*/project.cfx`, plugins `internal/plugins/Task*`. Sesiones 2026-06-06/07 (CLI+HTTP),
> 06-19 (piloto sintéticas end-to-end), 06-21 (mapa pipeline), 06-24 (anatomía custom project).

## Leyenda de verificación

- ✅ **Verificado en vivo** — ejecutado con `sqcli` y comprobado el resultado.
- 📄 **Doc oficial local** — del `help.txt` bundleado o del XML real de un `.cfx` en disco (no inventado).
- 🟡 **Mapeado read-only** — deducido de plugins/help, sin correr.
- ❌ **No se puede / GUI-only**.

---

## 0. Lo esencial

- **Binario:** `/Applications/StrategyQuantXB143.app/Contents/Resources/sqcli` (Mach-O x86_64, Rosetta).
  **Versión 143.2708.** (En B144 la app es ARM nativa — ver `version-b144.md`; la ruta efectiva de
  la instalación la resuelve `sqx/paths.py`.)
- **Carpeta de usuario:** `…/Contents/Resources/user/` → `projects/`, `data/History/`, `customdata/`,
  `databanks/`, `settings/`.
- **Arranque en frío ~12 s** → no spawnear un `sqcli` por *comando*: motor caliente HTTP o `-run file=`. ✅ (El orquestador
  sí arranca un motor por *task* del pipeline a propósito, por memoria: `memoria-heap.md`.)
- **GUI cerrada obligatoria** — `sqcli` es instancia única y comparte workspace/BD con la GUI. ✅
- **Seguridad:** en stdout sale el **Hardware ID** (riesgo bajo). El **código de licencia NO** sale salvo
  `-license action=info` → no correr `-license` en capturas compartidas.

## 1. Tres formas de invocarlo

| Modo | Cómo | Cuándo |
|---|---|---|
| **Shell interactivo** ✅ | `sqcli` sin args → queda abierto, motor caliente, HTTP en `:5050`. Dentro se teclea el comando **SIN el prefijo `sqcli`** (p.ej. `-project action=list`). | Explorar, depurar un comando. |
| **HTTP API** ✅ | Con el shell abierto, desde OTRA terminal: `curl 'http://localhost:5050/call?cmd=-databank%20action=export%20...'` (espacios → `%20`). Auto-incrementa `:5050→:5059`. | Disparar desde Python (`requests.get('http://localhost:5050/call', params={'cmd': ...})`) con el motor caliente. |
| **Batch en una sesión** ✅ | `sqcli -run file=comandos.txt` ejecuta la lista en una sola sesión caliente; `-exit` al final. | Lotes deterministas. |

---

## 2. Referencia CLI (del `help.txt` oficial, 📄)

Args **literales** del help bundleado. `sqcli.exe` en el help = `sqcli` en Mac.

### `-project` — gestionar proyectos
```
action: [list, start, startOnlyTask, startFromTask, stop, pause, resume, remove, status, loadconfig, saveconfig]
name:   (opcional) nombre del proyecto
file:   (opcional) ruta del .cfx
task:   (opcional) número de task, indexado desde 1
```
Ejemplos del help: `-project action=list` · `-project action=start name=Builder` ·
`-project action=startFromTask name="Example custom project" task=2` ·
`-project action=startOnlyTask name="…" task=2` · `-project action=status name=Builder` ·
`-project action=loadconfig name=Builder file=Builder.cfx` · `… action=saveconfig …`.
✅ corridos por nosotros: `list`, `start`, `status`.

> ⚠ **`startFromTask`/`startOnlyTask` `task=N` = ID INTERNO de SQX, NO posición en `config.xml`**
> (verificado 2026-07-13, `eurusd_2h`): `task=2` arrancó el **TF** (1er task del config) en vez del MR.
> El ID casa con el **nº del nombre** del task (`"Build strategies 3"`=MR→3, `"Build strategies 4"`=Forex→4;
> el TF, sin nº, era ID 2 — el task 1 no existía). El "indexado desde 1" del help es engañoso.
> **Método fiable y position-independent para reanudar/saltar tasks**: poner `active="false"` en los tasks
> a saltar (atributo `active` del `<Task>` en el `.cfx`) y lanzar `-project action=start` → corre sólo los
> activos, en orden. **Verificar SIEMPRE** en `user/projects/<p>/log/global_log_*.log` el `Task:` que
> arranca (línea `TASK STARTED at … / Task: <nombre>, Type: <type>`) — no fiarse del índice.
> `pause`/`resume` (en la lista de actions) SÍ verificados: congelan el build sin perder progreso (el nº de
> generadas y el `Running time` se congelan al pausar y continúan al reanudar, sin reset); sirven para
> un duty run/pause durante tasks de Build en Macs fanless (script `cooling_cycle.sh`: herramienta no
> incluida en esta copia).

### `-databank` — gestionar databanks
```
action: [list, count, save, load, delete, clear, create, remove, synctofiles, syncfromfiles, copy, move, export]
project: nombre del proyecto         name: (opc) databank      position: (opc)
folder:  (opc) carpeta               destproject/destdatabank: (opc) destino de copy/move
file:    (opc) ruta de export        view: (opc) nombre de vista
strategies: (opc) lista separada por ';'  →  "Strategy 0.1487,Strategy 0.1488"  (selección por NOMBRE)
```
Ejemplos del help: `-databank action=count project=Builder name=Results` ·
`-databank action=copy project=Builder name=Results destproject=Retester destdatabank=Results` ·
`-databank action=syncfromfiles project=Builder name=Results` ·
`-databank action=export project=Builder name=Results file=…/x.csv [view=Custom]`.
✅ `syncfromfiles`. ⚠ **`export` para sacar TRADES sale vacío** (ver §6); sirve para el índice (PF/#trades/net).

### Otros comandos (📄/✅)
| Comando | Uso | Estado |
|---|---|---|
| `-symbol action=[list,add,edit,delete,clear] symbols=X instrument=Y datasource=file timeframe=D1` | Crear/borrar símbolos custom. | ✅ add/delete |
| `-data action=[import,clone,export,update,timezones]` | Feeds; importar CSV; alinear timezone; exportar barras. | ✅ import/clone/export (§8) |
| `-instrument action=[list,edit] instrument=X commissions=… swap=… defaultspread=…` | Coste/swap/spread/point value. | ✅ list |
| `-tools action=orderstocsv file="X.sqx" usecomma=true data=main` | Extraer trades embebidos de un `.sqx`. | ✅ |
| `-run file=commands.txt` | Lista de comandos en una sesión caliente. | ✅ |
| `-waitfor action=[user,file]` / `-execute file=script` | Puente pausar SQX ↔ script externo (sentinel). | 🟡 |
| `-gui` | Webserver GUI remota en `:8080`. | 🟡 |
| `-license action=[info,update]` | Licencia — no en output compartido. | 🟡 |
| `-exit` | Cerrar sesión interactiva. | ✅ |

---

## 3. Anatomía de un custom project (el `.cfx`)

**Definición oficial (texto de la GUI de SQX):** *"Custom projects allow creating custom workflows."*

Un **custom project = una cadena ordenada de Tasks que operan sobre Databanks compartidos.** Es **la
única unidad de trabajo que SQX ejecuta** — en GUI y headless (no existe backtest ad-hoc; todo pasa por
un proyecto). En disco: `user/projects/<Nombre>/project.cfx`.

### 3.1 El `.cfx` es un ZIP 📄
`unzip project.cfx` →
- **`config.xml`** — el índice del proyecto (tasks + recursos + databanks).
- **un `<Tipo>-Task<N>.xml` por task** — la config real con todos los params (un `Retest-Task1.xml` ≈ 38 KB;
  un `Build-Task1.xml` ≈ 2 MB).

Se parametriza **mutando el XML y re-zipeando** (igual que un `.sqx`).

### 3.2 Esquema de `config.xml` 📄 (ejemplo real: proyecto `Builder`)
```xml
<Project name="Builder" version="143.2708">
  <Tasks>                              <!-- ORDEN del array = ORDEN de ejecución -->
    <Task type="Build" name="Build" active="true" taskXMLFile="Build-Task1.xml"
          showSettingsOverview="false" sampleName="Custom" version="126.2189"/>
  </Tasks>
  <Resources>                          <!-- compartidos por todas las tasks -->
    <Symbols/> <Sessions/> <CustomIndicators/> <CustomBlocks/>
  </Resources>
  <Databanks>                          <!-- tablas de resultados que las tasks leen/escriben -->
    <Databank name="Results"              view="Personalizada"        syncType="Auto-sync every 1 hour" position="100"/>
    <Databank name="Initial population"   view="Default - Main data"  syncType="Auto-sync never"        position="100"/>
    <Databank name="Last generation"      view="Default - Main data"  syncType="Auto-sync never"        position="100"/>
    <Databank name="Strategies to improve" view="Default - Main data" syncType="Auto-sync never"        position="1"/>
    <Databank name="Existing portfolio"   view="Default - Main data"  syncType="Auto-sync never"        position="100"/>
  </Databanks>
</Project>
```
Un proyecto mínimo (`sqcli_testing`, proyecto del autor) es **1 task Retest + 1 databank `Results`**:
```xml
<Project name="sqcli_testing" version="143.2708">
  <Tasks><Task type="Retest" name="Retest strategies" active="true" taskXMLFile="Retest-Task1.xml"/></Tasks>
  <Databanks><Databank name="Results" view="Default - Main data" syncType="Auto-sync never" position="10"/></Databanks>
</Project>
```

### 3.3 Tipos de Task (los bloques de un workflow) 🟡
Inventario real de plugins `internal/plugins/Task*`. Se ejecutan con `-project action=start` (o
`startFromTask`/`startOnlyTask task=N`).

| Task `type=` | Qué hace | Headless |
|---|---|---|
| `Build` | El genético (genera estrategias). | ✅ (smoke test 2026-06-25, §9) |
| `Retest` | Re-backtest de `.sqx` sobre símbolo(s)/fechas/coste, con cross-checks opcionales. | ✅ |
| `Optimize` | Optimización de params. | 🟡 |
| `AutomaticRetest` | Retest con aceptación automática. | 🟡 |
| `AutomaticPortfolioBuilder` | **Portfolio Master** (ensambla cartera desde un databank de `.sqx`). | 🟡 |
| `CreatePortfolio` | Crea una estrategia-portfolio. | 🟡 |
| `Filtering` | Filtra el databank por condiciones. | 🟡 |
| `CustomAnalysis` | Análisis custom sobre resultados. | 🟡 |
| `CallExternalScript` | **Ejecuta un script externo** (puente a Python). | 🟡 |
| `WaitFor` | Espera a usuario / fichero sentinel. | 🟡 |
| `GoToTask` | **Control de flujo condicional** (saltar/bucle si se cumplen condiciones). | 🟡 |
| `LoadFromFiles` / `SaveToFiles` | Cargar/guardar `.sqx` entre disco y databank. | 🟡 |
| `UpdateData` | Actualizar feeds. | 🟡 |
| `ApplyMassConfig` | Aplicar config en masa. | 🟡 |
| `ClearDatabanks` / `DeleteFile` / `LogDatabankStats` / `Notification` / `GoToTask` / `StopAndStart` / `NeuralNetworkTrainer` | Utilidades de orquestación. | 🟡 |

> `Portfolio Composer` **NO** está aquí: es ❌ GUI-only (su `.cfx` tiene `<Tasks/>` vacío). Único corte a
> GUI de toda la cadena.

### 3.4 Databanks 📄
Tablas de resultados con nombre, persistidas con el proyecto. Las tasks las consumen/producen. Nombres que
importan en el Builder: **`Initial population`** (semilla del genético), **`Strategies to improve`** (modo
Improver), `Last generation`, `Existing portfolio`, `Results`. Para sembrar el genético con reglas
generadas fuera de SQX: meter las `.sqx` en `Initial population` (copiar a `…/databanks/Initial population/` + `syncfromfiles`,
o `-databank action=copy`). Snapshot 2026-06-23: esos databanks de siembra estaban VACÍOS.

### 3.5 Cómo se genera un custom project
1. **Clonar plantilla + editar XML + re-zip** (vía PROBADA). Plantillas en `user/projects/`: `sqcli_testing`
   (Retest mínimo del autor; no viene con SQX), `Builder`, `Retester`, `PortfolioMaster`. Cambiar SOLO los campos que tocan (símbolo,
   fechas, comisión, dirección, blocks, parada). Mutar el `<Tipo>-Task<N>.xml` con lxml + re-zipear. **No**
   escribir un Task XML desde cero (frágil, 2 MB en Build).
2. **`-project action=loadconfig name=X file=X.cfx`** registra una config; `saveconfig` la vuelca.
3. **`-databank action=create/copy/syncfromfiles`** prepara los databanks de entrada/salida.

**Ejecutar:** `-project action=start name=X` (todo), o `startFromTask`/`startOnlyTask task=N` (desde/solo
una task; task indexado desde 1). Polling con `action=status`.

### 3.6 ¿Podemos crear uno? — Sí ✅ (Retest), 🟡 (Build)
Por la vía 1 (clonar+mutar+rezip): **Retest y Build validados end-to-end** (smoke test de Build
2026-06-25, §9; los scripts generadores no están incluidos en esta copia). Caveat B143: regresión **10-15× más lento +
memory leaks en MC/WFM** → cronometrar builds largos.

### 3.7 Clonar un proyecto a otro instrumento 📄/✅(Retest)/🟡(Build)
Un `.cfx` se clona a otro instrumento cambiando **solo los campos que llevan el símbolo**; el resto (genoma,
cross-checks, aceptación, ranking, MM) queda idéntico → es lo que permite "las mismas pruebas para otro
activo". Campos a sustituir (de un Build-Task real):
- `<Setups><Setup><Chart symbol="…" timeframe="…" spread="…"/>` + su bloque `<Commissions>`.
- `<Symbols><Symbol name="…" uSymbol="…" timezone="…" dateFrom=… dateTo=…>` + `<InstrumentInfo …>`
  (tickSize, `pointValue`, comisión, swap, decimals).
- `<Instruments><InstrumentInfo instrument="…" …>` (registro paralelo de instrumentos).

Dos vías: (a) copiar el dir `user/projects/<N>/`, descomprimir el `.cfx`, editar esos campos (lxml) y
re-zipear; (b) `-project action=saveconfig name=<src> file=x.cfx` → editar → `-project action=loadconfig
name=<Nuevo> file=x.cfx`. ⚠ El `<InstrumentInfo>` trae el **coste horneado** → verificar comisión/spread/swap
del activo nuevo (no heredar valores stale; se ha visto `Commission=9` viejo conviviendo con `4.5` correcto).

---

## 4. Receta A — retestear `.sqx` y sacar resultados ✅
Probado 2026-06-07 (`sqcli_testing`, 48 short USDCHF 4H → trades en las 48, idéntico a GUI).
1. Proyecto Retest `.cfx` (clonar un Retest mínimo, p. ej. tu `Retester`). Inyectar `.sqx` en `<proj>/databanks/Results/`.
2. ~~`-databank action=syncfromfiles project=<proj> name=Results`~~ — superado (2026-08-30): el motor
   auto-sincroniza los bancos al arrancar y un `syncfromfiles` manual encima los carga dos veces
   (`persistencia-databanks.md`). Arrancar el motor con los `.sqx` ya en disco basta.
3. `-project action=start name=<proj>` (polling `-project action=status`)
4. Extraer (§6).

CSV de `-databank export`: `;`-delimitado; cols `# of trades (IS/OOS)`, `Profit factor (IS)`,
`Net profit (IS)`, `Filters result`. ⚠ `# of trades (OOS)=0` si no hay sample OOS (usar IS);
`Filters result=FAILED` = filtro propio de SQX, **no** significa 0 trades.

## 5. Receta B — sintéticas como símbolos custom y retest sobre ellas ✅
Probado end-to-end 2026-06-19 (XAUUSD D1, 10 sintéticas + real). Scripts del piloto no incluidos en
esta copia.
1. **Generar CSV (Python):** cada serie en **formato MetaTrader4 bar** = coma, **SIN header**, 7 cols
   `YYYY.MM.DD,HH:MM,O,H,L,C,Volume` (D1 → hora `00:00`, volumen dummy `1`). Escribir también la serie REAL.
2. **Crear símbolo + importar** (por serie):
   ```
   -symbol action=add    symbols=XAUUSD_SYN001 instrument=XAUUSD datasource=file timeframe=D1
   -data   action=import  symbol=XAUUSD_SYN001 instrument=XAUUSD filepath=/abs/XAUUSD_SYN001.csv \
                          timeframe=D1 format="MetaTrader4 bar format"
   ```
   - `instrument=XAUUSD` hereda point value/tick/coste. ⚠ `format=` obligatorio y solo built-in
     (`MetaTrader4 bar format`); `app_RE`/`Custom` NO aplican en import.
   - Símbolos en `user/data/History/<SÍMBOLO>/<SÍMBOLO>_<TF>.dat` + `user/customdata/customdata.db`.
3. **Proyecto Retest:** editar el Setup (`<Chart symbol="…" timeframe="D1" spread="…"/>`,
   `dateFrom/dateTo`, `engine="MetaTrader5 (hedged)"`, comisión SizeBased §7).
4. **Correr** (Receta A 2-3). Net por-símbolo inequívoco → **bucle 1 símbolo/retest** (reescribir
   `<Chart symbol>`). Multi-`<Setup>` existe 🟡 pero **no verificado** que separe el net por símbolo.
5. **Extraer** (§6) tras CADA símbolo (el `.sqx` guarda el resultado del ÚLTIMO retest).
6. **(STS)** `STS = (net_real − mean(net_synth))/std(net_synth)`. Leer **z y distribución**, NUNCA "% verdes"
   (pseudoreplicación: sintéticas comparten ~85% de barras → `std` estrecho por diseño).
7. **Limpieza:** `-symbol action=delete symbols=XAUUSD_SYN001,…`.

## 6. Extraer trades — `orderstocsv` (no `databank export`) ✅
⚠ `-databank action=export` para trades sale **VACÍO** (el índice no refleja lo que el retest escribe DENTRO
del `.sqx`). Vía buena:
```
-tools action=orderstocsv file=<proj>/databanks/Results/<estrategia>.sqx usecomma=true data=main
```
→ CSV junto al `.sqx`: `;`-sep, **decimal europeo `,`**, col `Profit/Loss`, `Close type`="Exit After X Bars".
**net = suma de `Profit/Loss`.** `data=main` = serie principal (no cross-checks).

## 6.1 Persistir el databank a disco para que la GUI lo vea ✅ (verificado 2026-06-28)
⚠⚠ **Un build headless tiene las estrategias SOLO en la memoria del motor.** `-databank action=export` escribe el
**CSV de métricas** (índice), pero **NO escribe los `.sqx`** a disco. Si matas el `sqcli` sin persistir, la GUI
abre el proyecto y lee el databank en disco **VACÍO** (0 `.sqx`) aunque el build generara N estrategias —
**y se pierden** (solo queda el CSV de métricas, no las estrategias). El `syncType` del databank suele ser
`Auto-sync every 1 hour`, así que una corrida de minutos + kill no auto-sincroniza.

**FIX OBLIGATORIO** (antes de matar el motor, con el motor aún vivo, tras `-project action=stop`):
```
-databank action=save        project=<proj> name=Results
-databank action=synctofiles project=<proj> name=Results   # escribe los .sqx a databanks/Results/
```
Verificado: corrida 3 min → tras `synctofiles`, **21 `.sqx` en `databanks/Results/`** → la GUI los ve. Sin esto,
una corrida de 20 min con 78 generadas dejó el databank en disco a 0. **Ya incorporado en esta copia**
a `sqx/run_pipeline_headless.sh` (persist incremental + `save`/`synctofiles` antes del stop) y a
`sqx/stop_and_persist.py`. Imprescindible para el flujo
**minar headless → inspeccionar/usar en GUI** (o alimentar Portfolio Master).

## 6.2 Abrir la GUI sobre sqcli VIVO corrompe databanks ❌❌ (verificado 2026-07-10)

Variante PEOR que §6.1: no hace falta matar el motor — basta con **abrir la GUI mientras el
`sqcli` headless sigue corriendo**. `sqcli` y la GUI son **instancia única** y comparten el mismo
workspace/BD; al solaparse, la GUI lee/escribe un estado **parcial/inconsistente** del databank y
al guardar lo sobreescribe → **se pierden estrategias sin aviso**.

**Caso real (2026-07-10, `Esqueleto`):** tras correr el Diff Markets headless con `Results=4000` en
RAM (no persistidos completos al disco — solo un auto-sync parcial), el usuario abrió la GUI con el
`sqcli` aún vivo (un `kill` se rechazó). La GUI leyó un `Results` parcial y, al "guardar", lo dejó en
**136 `.sqx`** (perdió ~3864). `Second OOS` (2163) sobrevivió porque sí estaba sync'do a disco.

**Regla DURA (flujo `headless → GUI`):**
1. **Cerrar el `sqcli` al 100%** ANTES de abrir la GUI: `pgrep -f sqcli` → vacío. Si resiste al
   `pkill`, `kill -9 <PID>` (y `pkill -9 -f "tail -f /dev/null"` si lo lanzaste con stdin abierto).
2. **Persistir TODOS los databanks** con `save`+`synctofiles` (§6.1) **antes** de cerrar/abrir GUI.
   `syncType="every 1 hour"` **NO es garantía** — solo persiste cada hora; un cierre abrupto o un
   solapamiento deja el databank incompleto en disco.
3. Solo entonces abrir la GUI.

> En esta copia, con el motor vivo el orden es **guardar y luego cerrar**: `python sqx/stop_and_persist.py
> <proyecto>` (o Stop & persist en el monitor) hace las dos cosas. Matar el motor primero pierde lo que solo
> estaba en su RAM.

**Comprobación antes de la GUI (2026-10-26):** `python sqx/persist_before_gui.py <proyecto>` ya NO escribe (antes
arrancaba `sqcli -run` y lanzaba `save`+`synctofiles`: no podía añadir nada, porque un motor recién arrancado solo
tiene lo del disco, y sin barrera de carga lazy podía dejar un banco incompleto). Ahora solo comprueba que no haya
motor, GUI, pipeline ni cascada vivos, ni bancos `--slim` apartados ni `.bak`, muestra los `.sqx` en disco y sale con
rc 0 (seguro) o 1 (con los motivos).

**Encoding landmine (HTTP vs -run):** `-databank action=count name=Second%20OOS` funciona por HTTP,
pero `-databank action=syncfromfiles name=Second%20OOS` por HTTP **lo trunca a "Second"** → "Databank
'Second' doesn't exist". Los nombres con espacios en `-run file=` van **ENTRE COMILLAS**
(`name="Second OOS"`); sin comillas el parser de `-run` también trunca en el primer espacio
(verificado 2026-07-29: `name=Long Short TF` literal → "Databank 'Long' doesn't exist";
`name="Initial population"` → count OK y synctofiles OK).

**Caso real (2026-07-29, `gold_4h`):** build de 29 min acabó con 696 estrategias en `Long Short TF`
(RAM), pero el driver no persiste databanks con espacios por HTTP y el run < 1 h no llegó al
auto-sync → kill del motor = 696 perdidas (0 `.sqx` en disco). **FIX aplicado:** todos los databanks
de `gold_4h` renombrados sin espacios (`Long_Short_TF`, `Second_OOS`, `MC_*`… en `config.xml` + task
XMLs del `.cfx` y carpetas de `databanks/`) → el persist HTTP del driver ya es determinista.
EXCEPCIÓN: `Last generation`, `Initial population`, `Strategies to improve`, `Existing portfolio`
**NO se renombran** — el motor los auto-recrea con esos nombres canónicos si faltan (verificado:
renombrarlos genera duplicados). Bonus verificado: `-exit` graceful dispara "Synchronizing databanks
to files" (un kill NO).

## 7. Coste correcto — comisión `SizeBased` ✅
`type="None"` es el default de TODAS las plantillas = cost-blind → **no usar**. En el `<Setup>`:
```xml
<Commissions><Method type="SizeBased" use="true"><Params>
  <Param key="Commission" className="SizeBased">4.5</Param>
</Params></Method></Commissions>
```
- **Ejemplo (broker AXI): 4.5 USD/lote ROUND-TRIP** → SizeBased **4.5, NO 9.0** (el piloto usó 9.0 = 2× el
  real). Empírico B143: cobra `Commission × lote × nº trades` (4.5 → −140.83 = 4.5×0.1×313) → **escala**, no
  es el bug `PerTrade` (fijo).
- Cordura: `-instrument action=list` → `pointvalue` (oro=100), `ticksize` (0.01), `defaultspread` (60). El
  retest **recalcula indicadores** sobre cada serie (fills/SL/PT/gaps reales), no es proxy de máscara.
- **MM del Retest** (visto en `Retest-Task1.xml`): `<MoneyManagement>` con métodos `FixedSize`,
  `RiskFixedBalancePct`, `RiskFixedPctOfAccount`… `use="true/false"` por método + `<InitialCapital>`. Regla
  de la casa: para exit-1-barra y el Composer, estandarizar en ATR Risk-Based.

**⚠ El coste vive en el SETUP, no se hereda del instrumento (gotcha verificado 2026-06-25).** SQX tiene DOS
capas: (a) el instrumento linkeado (`<InstrumentInfo>`, p.ej. `XAUUSD_AXI_axi` con `SizeBased 4.5` + swap
`points -58.3/38.7`) y (b) el **Setup del test** (`<Commissions>` / `<Swap>` / `spread`). **El backtest usa la
del Setup**; el instrumento solo la SIEMBRA al añadir el símbolo. **Aplicar un predefined config resetea el
Setup a cost-blind**: `Method type="None"`, `spread="0"`, `<Swap use="false" long="0">`, `MainTestValues
swap="false"`, `engine="MetaTrader4"` → el backtest corre SIN coste aunque el instrumento esté bien (caso real:
el primer smoke test salió cost-blind por esto). También hay que poner `MainTestValues swap="true"` para que
el swap del Setup se aplique.

**Proceso fijo anti-invención (al meter coste por activo):** derivar SIEMPRE de una fuente única versionada
de costes del broker (un YAML por broker; mantén la tuya, no viene en esta copia) — comisión round-trip → `SizeBased`; swap long/short + tipo de swap +
día de swap triple → `<Swap>`; spread típico en points → `spread`. **Cross-chequear contra el
`<InstrumentInfo>` del `.cfx` y FLAGGEAR mismatch** (no elegir en silencio). Engine canon = **`MetaTrader5
(hedged)`**. (El script que lo automatiza no está incluido en esta copia.)
**Verificado 2026-06-25** (`smoke_build_costs`, XAUUSD): cross-check YAML⟺registro = MATCH; build con coste
corrió headless (M5 hedged, 571 estrategias vs 1.051 cost-blind); en `orderstocsv`, la comisión es exacta —
0.1 lote: bruto $21.10 → neto $20.65 = **−$0.45 = SizeBased 4.5 × 0.1** por trade. (Swap on; se ve solo en
holds overnight — los trades de muestra eran intradía.)

## 8. Alinear timezone sin GUI ✅
```
-data action=timezones                                                # listar (EST+7/NY-US-DST ≈ EETUS)
-data action=clone symbols=USATECHIDXUSD timezone=EETUS postfix=_axi  # timezone= (DST-aware), NO hours= fijo
-data action=import symbol=X instrument=X filepath=… timezone=Etc/UTC # timezone = zona ORIGEN del CSV
```
Ojo: el broker profile NO re-temporiza tus barras.

**Exportar barras M1 sin GUI ✅ (2026-10-14; el script `export_ohlc_headless.py` no está incluido en esta copia)**
```
-data action=export symbols=EURUSD_ftmo timeframe=M1 datefrom=2003.01.01 dateto=2026.10.13 format=Custom cIncludeHeader=false cHeader=Date,Time,Open,High,Low,Close,Volume cFormat=[Date:yyyy.MM.dd],[Time:HH:mm],[Open],[High],[Low],[Close],[Volume] outputdir=/ruta
```
- Con `sqcli -run file=…` el export corre en segundo plano y `-exit` lo corta sin escribir nada. Hay que mandarlo con el
  motor caliente por HTTP y esperar a que el fichero deje de crecer.
- `format=Custom` exige `cHeader` aunque `cIncludeHeader=false` ("Header length <> Format length").
- En la URL HTTP, los `=` van sin codificar (`%3D` no lo entiende el servidor).
- Admite símbolos con paréntesis (`XAUUSD_ftmo(2)`).
- Exporta en la zona horaria del símbolo (EETUS = servidor FTMO).
- 25-31 s por símbolo de 23 años; nº de barras = `ROWS` de `data.db` (verificado en EURUSD, oro y US100 FTMO).

### 8.1 Añadir símbolos futures SQ Ultimate + rename (FTMO micros, 2026-10-03) ✅/❌
Caso real: alta de los micros FTMO `@GR` (MGC oro), `@RX` (M2K Russell), `@MF` (M6E EUR).

**1. Descarga del feed SQ Futures — ❌ GUI-only.** `-data action=update symbols=@GR` (y con sufijo
`@GR_M1`) responde "Updates started" y NO hace nada: el update solo refresca símbolos YA registrados.
El alta de un futuro nuevo vive en el plugin `DataSourceSQFuturesData` (popup "SQ Futures data: Find
data" del Data Manager). Headless se puede SONDEAR (arrancar `sqcli -gui` → webserver :8080):
`GET :8080/sqFuturesData/getExchanges` y `/verifySubscription` funcionan (devuelven exchanges y
suscripción EOD/minute activa + free futures), pero `lookup`/`add` por HTTP no son reproducibles sin
el flujo completo del GUI. → Alta por GUI una vez; el resto (clone TZ, retest, minado) sigue headless.

**2. Clone TZ de los nuevos — ✅ igual que §8.** `-data action=clone symbols=@GR timezone=EETUS
postfix=_ny` → `@GR_ny` con records idénticos y TZ "(EST+07) New York". Verificar siempre records ==
original y TZ label. Specs de instrumento correctas para micros out-of-the-box: `-instrument
action=list` → GR: pointvalue 10.0 / tick 0.1; RX: PV 5.0; MF: PV 12.500; NM: PV 2.0 / tick 0.25.

**3. Rename de símbolo — ❌ headless no viable, hacer por GUI.** Dos vías intentadas y cerradas:
- `-symbol action=edit symbol=@GR_ny name=X` (documentado en `help.txt`): **ROTO en B143** — ignora
  todos los params (con y sin `swap`, `instrument`, etc.) y responde siempre "Cannot update data.
  Parameter 'swap' is missing." (6 variantes probadas).
- Servlet del GUI `/data/editData` (plugin `DataManagerData`; params: `connection`, `symbol`,
  `symbolNew`, `swap`, `instrument`, `barType`; requiere `sqcli -gui` y **query params, no JSON**):
  respondió `{"success":"Data updated."}` pero **NO persistió el rename y dejo el registro de
  símbolos VACÍO** (vista en memoria corrompida; `-symbol action=list` devolvió 0 filas). Al REINICIAR
  el motor el registro volvió completo e intacto (datos en disco ilesos: `History/sq_futures/@/` con
  los 9 folders y tamaños correctos). → **No reintentar headless; rename = GUI (clic derecho → Edit)**
  o vivir con los nombres `@XX_ny`.

## 9. El genético (Build) headless ✅ (verificado 2026-06-25)
- Corre como `Task type="Build"` vía `-project action=start` sobre un `.cfx` de Builder configurado.
- **VERIFICADO EN VIVO (smoke test 2026-06-25):** clonar `Builder` → renombrar + `StopCondition` corta →
  `sqcli -run` con `clear`/`start`/`count`/`export`. El Build genético corrió **headless hasta el final
  (3m 2s)** y dejó **1.051 estrategias** en `Results` (baseline 0 → 1.051), export a CSV OK (XAUUSD_axi H1).
- Config en disco: `user/projects/Builder/project.cfx` → `Build-Task<N>.xml`. Secciones: `WhatToBuild`
  (`RulesComplexity`: min/max conditions, period, shift), `MarketSides`, `OptimizeExitTypes/Periods`,
  `CrossChecks`, `Setup` (dateFrom/dateTo, engine), `Symbols`, `InstrumentInfo`, `Swap`.
- Sembrar población inicial = nativo (databank `Initial population`; §3.4).
- ⚠ Regresión B143 **10-15× más lento + memory leaks en MC/WFM** → para builds largos, cronometrar y vigilar
  memoria (el smoke fue build puro, `CrossChecks use="false"`).
- **⚡ Headless ~6× más rápido que la UI para GENERAR** (bench 2026-06-25, XAUUSD H1, 3 min, mismo `.cfx`;
  ⚠ **motor = `random-generation`** heredado de un preset de estilo, NO genético → el genético rinde menos
  estrategias/hora por estrategia; el ratio UI↔headless vale, los absolutos son de random):
  headless **3.989 generadas / 637 en databank / 45 ms-estrategia / 79.757 por hora** vs UI **646 / 98 /
  274 ms / 13.106 por hora**. La **tasa de aceptación es ≈ igual** (15.97% vs 15.17%) → es el mismo genético,
  pura diferencia de throughput. Causa (doc/foro oficial SQX): **el build/backtest es CPU-only, la GPU solo
  acelera la UI** → render de equity-charts/panel/flow-log roba recursos que headless no gasta; y el **builder
  en GUI infrautiliza la CPU** (issue conocido, usuarios reportan 10-15% de CPU; vía oficial de throughput
  pleno = headless / varias instancias). En el Air fanless se amplía por throttling térmico (UI+motor). NO es
  una setting de equity-curve (mismo `.cfx`, `StoreChartData=false` en ambos): es runtime, no config.
  **Descomposición medida (2026-06-25):** UI con visual 13.106/h → UI sin visual (flow log Text + ventana
  minimizada) 19.161/h → headless 79.757/h. O sea **render ~1,5× × motor-en-GUI ~4,2× = ~6×**: apagar el
  render de la UI gana solo +46%; el factor grande (~4×) es la infrautilización de CPU del builder en GUI y
  NO se arregla desde la UI. **→ generar headless, inspeccionar en la UI.**
- **Genético vs random (headless, 3 min, XAUUSD_axi H1, con coste, 2026-06-25):** genético **genera ~20% más
  lento** (63.478/h vs 79.757/h; evoluciona población+cruce+mutación+dedup) **PERO acepta 3× más** (49% vs
  16% — evoluciona hacia estrategias que pasan los filtros) → **llena el databank 2,3× más rápido** (1.462 vs
  637 estrategias; 31.199 vs 12.736 aceptadas/h). Para output ÚTIL el genético gana. Config del bench:
  `genetic-evolution` (pop 50, maxgen 10, crossover 50%, mutation 20%, islands 5), ranking **Weighted**
  (heredado del preset de estilo, NO el `ReturnDDRatio` canónico), complejidad 1-2 cond / period 15-80,
  MarketSides both, 119 blocks, coste SizeBased 4.5 + swap -58.3/38.7 + spread 8 (del YAML de costes del
  broker), CrossChecks off, datos 2004-2019, engine backtest MT5 hedged. ⚠ Es A/B del MOTOR (solo se flipó
  `generationType`), no un genético canónico.
- **Nota `-databank export`:** SÍ vuelca el índice de un databank de **Build** (1051 filas con
  Fitness/Net/PF/#trades IS). El "sale vacío" de §6 es específico del **Retest** (resultados dentro del
  `.sqx` → `orderstocsv`); para Build el índice está poblado.

## 9.1 Build-Task: stop, cap del databank y auto-filtros (verificado 2026-06-26)

Aprendido configurando el miner EURUSD D1 MR (`miner_eurusd_d1_mr`), confirmado leyendo el `.cfx`
guardado por la GUI. Todo vive dentro de `<Rankings>` (hijo de `<Settings>`) salvo donde se indique.

- **"Stop generation when" = radio EXCLUSIVO** (`<StopCondition type="…">`). Valores del `type`:
  - `time-limit` → *After D días / H horas / M min* (usa `days`/`hours`/`minutes`).
  - `passed-count` → *Totally N strategies (that passed filters) were generated* (usa `passedStrategies`).
  - (también *Never* y *Databank is full*).
  - ⚠ **LANDMINE:** poner `passedStrategies="1000"` **junto a** `type="time-limit"` **NO** para a las 1000 —
    el `type` manda y el otro atributo queda inerte. Para "parar a las N que pasen" hay que poner
    **`type="passed-count"`**. (Corrige la nota vieja del miner H2 "1000 pasadas O 10h": era **solo** 10h.)
- **"Maximum strategies to store in databank"** = `<MaxStrategies>` (hijo de `<Rankings>`). Default heredado
  del preset: genético Default/Forex = 1000; estilo MR/TF (`Weighted`) = **24999** → poner uno sano (usamos 3000).
- **Auto-filtros** ("No trades", "Too many ambiguous trades", "Outlier trade", "Zero-duration"…) = bloque
  **`<AutomaticDismissal>`** con hijos `<Problem code="N" dismiss="true|false"/>`. **Son flags numéricos
  (bitmask), NO texto** → un `grep "ambiguous"` sobre el `.cfx` **no los encuentra** (susto real: parecían
  no persistirse). Cada filtro = un `code`; `dismiss="true"` lo activa. En el D1 MR hay 2 activos (`code=1`
  y `code=1024` = los dos encendidos en la configuración de referencia). El `.cfx` solo guarda el número: la etiqueta de cada `code` es la que enseña la GUI.
- **Dedup** = `<DismissTooSimilarStrategies>true</DismissTooSimilarStrategies>` (separado del AutomaticDismissal).
- **Restart del genético** (dentro de `<BuildMode>`, dos toggles independientes):
  - `<EvoRestartOnFinish status="false"/>` = *"Start again when finished (continuous repeating evolution)"*.
    OFF = una sola búsqueda evolutiva (sin re-roll → menos multiple-testing).
  - `<EvoRestartOnStagnation status="true" generations="5"/>` = re-siembra una isla estancada N gen (mecanismo
    INTRA-búsqueda, distinto del anterior; sigue ON por default aunque apagues el continuous-repeating).
- **Hiperparámetros del genético** (`<BuildMode generationType="genetic-evolution">`): `PopulationSize`,
  `MaxGenerations`, `CrossoverProbability`, `MutationProbability`, `Islands`, `MigrationModulo` (cada cuántas
  gen migra; si > `MaxGenerations` **nunca** dispara), `MigrationRate` (%), `DecimationCoef`, `FreshBloodWeakestPct`/
  `…Generations`, `ShowAdvancedGeneticSettings` (ponlo `true` para ver islas/migración/decimación en la GUI).
  ⚠ **Estos NO se heredan de "tu" config si tu script no los escribe** — un `.cfx` clonado conserva los del
  template (bug real: el miner corría pop 50/10 gen en vez de la config decidida). Estámpalos explícitamente.
- **`MainTestValues swap="true"`** es obligatorio para que el `<Swap use="true">` del Setup se aplique (ver §7);
  un clon puede traerlo en `false` (coste de swap silenciosamente apagado).

## 9.2 Lanzar un Build headless por HTTP — 3 landmines (verificado 2026-06-26)

Aprendido con un driver anterior (`run_miner_headless.sh`, no incluido en esta copia); en esta copia
`sqx/run_pipeline_headless.sh` ya aplica las barreras 1 y 2. Sangre real:

1. **Readiness ≠ puerto abierto.** El HTTP abre `:5050` a los **~3 s**, pero el motor responde
   **"Error: CLI not ready."** hasta **~12 s**. Si lanzas comandos en cuanto el puerto escucha, se
   **rechazan en silencio** (el build no arranca y no te enteras). → sondear hasta que
   `-project action=list` devuelva **"List of available projects"**, no hasta que el puerto abra.
2. **Encoding de `cmd=`.** `sqcli` espera **espacios → `%20` y `=` LITERAL** (como el ejemplo del help).
   `curl --data-urlencode` mete `+` y `%3d` → **"Unrecognized command -project+action%3dstatus…"**.
   Usar `cmd=$(printf '%s' "$C" | sed 's/ /%20/g')`.
3. **`-project action=start` es ASÍNCRONO.** Devuelve **al instante**: *"Project is running on the
   background. To get project status use -project action=status…"*. El build corre en el motor de fondo.
   → **NO** lo trates como bloqueante: si usas "el curl de start retornó" como señal de fin, **cortas el
   build al primer poll** (bug real 26-jun: paró la corrida a los 10 min y mató el motor). Señal de fin
   correcta = **`Strategies generated` CONGELADO** N polls (genera ~850k/h → congelado real = terminado),
   o un estado explícito si lo hubiera. Parar/exportar SOLO tras confirmarlo.

**Status del build** (`-project action=status name=…`) trae justo lo que se reporta: `Strategies generated`
(generadas), `Passed`/`In databank` (las que pasan el bar), `Accepted %`/`Rejected %`, `Strategies per hour`,
`Running time so far`. **Stop+export** = `-project action=stop` + `-databank action=export … file=X.csv`
(para Build el índice SÍ se vuelca; el "export vacío" de §6 es solo para Retest → ahí `orderstocsv`).

## 9.2.1 Desglose de rechazos por MOTIVO (= GUI "Rejected → [Detailed]") — SÍ se persiste ✅ (verificado 2026-06-30)

El `-project action=status` solo da el **agregado** (`Rejected %`). El **desglose por motivo** que la GUI
pinta al pinchar **Rejected → [Detailed]** (`Reason to dismiss / Count / % of all`) **NO es GUI-only**: SQX lo
vuelca en **`user/projects/<proj>/log/global_log_<ts>.log`**, en un bloque `Rejected details:` / `Accepted
details:`, con la MISMA tabla. Ejemplo real (`tf_eurusd_h1`, build que terminó con **0 aceptadas / 3811
rechazadas**):
```
Rejected details:
Initial population filter: Profit factor[Main data, IS] >= 1.05, Count: 1754, % of all: 46.0 %
Automatic filter: no trades, Count: 1030, % of all: 27.0 %
Initial population filter: # of trades[Main data] > 100.00, Count: 711, % of all: 18.7 %
initial population, Count: 307, % of all: 8.1 %
Automatic filter: too many ambiguous trades, Count: 9, % of all: 0.2 %
```
- **Leerlo on-demand:** `python sqx/show_reject_breakdown.py <proyecto>` (sin args = dashboard de
  todos los proyectos: `gen/acc/rej` + si el log está `FINISHED` o `VACÍO`). El helper etiqueta cada motivo
  con su **etapa** (SEEDING / AUTO-FILTER / RANKING / CROSS-CHECK / DEDUP) para ver **dónde** mueren.
- ⚠ **El NOMBRE del fichero `global_log_<ts>.log` va en UTC; los timestamps de DENTRO (`TASK STARTED/FINISHED`)
  en hora LOCAL** (cazado 2026-06-30: un log llamado `…_055800` confundió porque el equipo estaba apagado a las
  05:58 *local* — pero 05:58 UTC = 07:58 CEST, que es cuando corrió de verdad). Para saber a qué corrida
  pertenece un log, fíate del `TASK STARTED` interno (lo imprime el helper), no del nombre.
- ⚠⚠ **El bloque se escribe SOLO al `TASK FINISHED`.** Si matas el motor a media (o lo paras a lo bruto), el
  `global_log` queda **VACÍO (0 líneas)** y el desglose se **pierde** (caso real 29-jun: `tf_xauusd_h4` y los
  `diag_iso_*` quedaron a 0). Para tenerlo: deja que llegue a su `StopCondition` o usa `-project action=stop`
  (graceful) **antes** de apagar. La GUI puede retener el bloque en memoria hasta cerrar la sesión, así que
  cerrar la GUI también lo flusha.
- **CONFIRMADO (2026-06-30)**: los rechazos de **cross-checks SÍ se itemizan** como líneas propias cuando el
  build corre lo bastante para que las estrategias los alcancen. Formato exacto:
  `Cross Check filter in 'Monte Carlo trades manipulation':  Net profit (MC trades, Conf. level 80%)…` y
  `Cross Check filter in 'Higher backtest precision':  Net profit (Higher backtest precis., IS)…`. O sea el
  desglose cubre TODAS las etapas: seeding (Initial population filter) · auto-filter · **ranking** (Global
  filter: PF/Stability/#trades sobre [IS] y [OOS]) · **cross-check** (MC/M1) · dedup. (`show_reject_breakdown.py`
  los etiqueta por etapa.) En las corridas TF reales el grueso muere en **ranking**, no en los cross-checks.

## 9.3 Clonar a OTRO activo: binding de datos + RESET-POR-GUI (verificado 2026-06-26)

Al portar un miner a otro símbolo editando el `.cfx` a mano (p.ej. EURUSD→NZDUSD), dos trampas grandes:

1. **El `<Symbol>` debe declarar el RANGO REAL de datos, no la ventana de test.** El binding de datos vive
   en `<Data><Symbols><Symbol name="X_axi" … dateFrom=EPOCH dateTo=EPOCH …>`. Esos `dateFrom/dateTo` (en
   **milisegundos epoch**) tienen que ser el **rango disponible del símbolo en el DataManager** (lo que da
   `-symbol action=list`), NO tu ventana de minado. Si copias la ventana de otro activo (2015-2023) en vez del
   rango real (2003-2026), SQX arranca con **"Cannot start project. Project has unresolved resources."** y
   `Strategies generated=0 / Running time=0ms` (no es bug de config; el motor responde al status pero no corre).
   El símbolo puede estar **perfectamente registrado** (`-symbol`/`-instrument action=list` lo listan con su
   timezone/rango/Dukascopy) y aun así dar unresolved si el `<Symbol>` declara un rango fuera del disponible.
   La ventana de TEST va en `<Setup dateFrom dateTo>` (formato `YYYY.MM.DD`), que es **independiente** del rango
   del `<Symbol>`.
2. **⚠⚠ Abrir en la GUI un proyecto con recursos sin resolver RESETEA todo el build-task a los defaults del
   template Builder.** Si resuelves el instrumento abriendo el `.cfx` en la UI, SQX **re-escribe el Build-Task**:
   `generationType`→`random-generation`, fitness→`Weighted`, genético→pop50/gen10/islas5, ranking→laxo
   (`NetProfit>50`+`trades>50`), SL/TP→trend-following (SL 1.5-3×/**TP 2-5×**, TP>SL ≠ MR), `MaxStrategies`→24999,
   `StopCondition`→`time-limit`, OOS Range→el del template, `CrossChecks`→off, `MainTestValues swap`→false.
   **Solo sobrevive** el binding resuelto + `<Blocks>` (MR 72) + `MarketSides` + coste a nivel instrumento
   (swap/comm). → Tras un reset-por-GUI, **no re-clones del activo origen** (vuelve a romper el binding);
   **transplanta los contenedores de config** (`<WhatToBuild>` + `<Rankings>` + `<OutOfSample>`) desde el `.cfx`
   bueno y **preserva intacto** `<Data><Symbols>` + `<Resources>` del que ya resolvió la GUI. Verifica por **auditoría del `.cfx`, no reabriendo la GUI**
   (reabrir vuelve a arriesgar el reset; aunque con el dato ya resuelto el trigger desaparece).

## 10. Mapa del pipeline (qué es headless)
| Etapa | Headless | Estado |
|---|---|---|
| Build, Optimize, Retest, AutomaticRetest, Filtering, CustomAnalysis, UpdateData, Load/SaveFromFiles | ✅ vía `-project action=start` | **Build ✅, Retest ✅**; resto 🟡 |
| **Portfolio Master** (`AutomaticPortfolioBuilder`) | ✅ headless (consume databank de `.sqx`) | 🟡 |
| Cross-checks del Retester (MonteCarlo, WalkForward, RetestOnAdditionalMarkets…) | **corren** dentro del Retest (`<CrossChecks use="true">`) pero el **veredicto numérico NO sale por CLI** | 🟡 |
| **Portfolio Composer** | ❌ **GUI-only** (`.cfx` con `<Tasks/>` vacío) | — |

→ Loop **`minar → retest cost-aware → z-score Python → supervivientes → Portfolio Master`** = headless
end-to-end; sólo el ensamblaje final (pesos/MM/max-DD del Composer) baja a GUI. Gate correcto: variantes de
retest headless + `orderstocsv` + veredicto en Python (los cross-checks nativos = sanity-check visual, no
gate automático).

## 11. Arquitecturas componibles (genérico)
Un custom project es un grafo lineal de **tasks** sobre **databanks compartidos** → se pueden COMPONER
pipelines sin código, solo encadenando tasks y enrutando databanks:

- **Multi-task / multi-build en un proyecto:** `<Tasks>` (en `config.xml`) es un array ordenado; puede
  haber varias `<Task type="Build">` (p.ej. una por TF o por estilo) seguidas de Retest / Filtering /
  AutomaticPortfolioBuilder. El orden del array = orden de ejecución.
- **Routing a databanks por task:** cada task declara su entrada/salida en un `<Databanks>` interno (dentro
  de su Task XML) con `<Databank name="Output" value="<NombreDatabank>"/>` y `name="Input"`. Cambiando
  `value=` cada Build escribe a un databank distinto. Los databanks referenciados deben existir en el
  `<Databanks>` a nivel proyecto (`config.xml`). 🟡 (campo visto en el XML, no corrido con 2 builds).
- **Cross-checks = gates DENTRO de una task:** OOS, precisión M1 (`RetestWithHigherPrecision`), Walk Forward
  (`WalkForwardOptimization`/`Matrix`), Monte Carlo… son entradas del bloque `<CrossChecks>` con su
  `<AcceptanceSettings>`. La estrategia que no cumple **no entra** en el databank de salida → **los
  supervivientes fluyen por databank, sin código**. (El veredicto numérico no sale por CLI, pero el gate se
  aplica internamente; ver §10.)
- **Replicar para N instrumentos:** clonar el proyecto por instrumento (§3.7). El Build es **por-símbolo**,
  así que un "for cada activo" se expresa como **N proyectos (o N pares Build/Retest), no como un bucle** —
  SQX no tiene un foreach paramétrico sobre símbolos dentro de una task.
- **Encadenar a cartera:** un databank de "aprobados" alimenta la task `AutomaticPortfolioBuilder`
  (Portfolio Master, headless). El Portfolio Composer final (pesos/MM/max-DD) es ❌ GUI-only.

**Ejemplo concreto (ilustrativo):** un proyecto con `Build-A` (TF alto, sus building blocks) → databank
`A_approved`, `Build-B` (TF bajo, otros blocks) → databank `B_approved`, **ambos con el mismo bloque de
cross-checks** (OOS + M1 + Walk Forward + aceptación); luego una task `AutomaticPortfolioBuilder` que consume
la unión de los dos databanks. Para varios instrumentos, se **clona ese proyecto** cambiando solo el bloque
de símbolo (§3.7). Así un mismo embudo de robustez se reutiliza across TFs e instrumentos.

## 12. Predefined build configs (`simpleTemplates`) 📄
El desplegable **"Use predefined config"** de la pestaña Builder está respaldado por XML en disco:
`…/Contents/Resources/internal/web/BUILDER/simpleTemplates/`. Son **9 ficheros**, cada uno un **Build-Task
XML completo** (~1.2 MB) → se pueden **leer y reutilizar** (motor, ranking, complejidad, market sides y la
lista entera de building blocks).

| Fichero (= opción del desplegable) | Motor `generationType` | `<Ranking type>` | nº `<Block>` | maxConditions |
|---|---|---|---|---|
| `Default`, `DefaultForex` | **genetic-evolution** | ReturnDDRatio | 354 | 3 |
| `DefaultFutures` | **genetic-evolution** | ReturnDDRatio | 354 | 2 |
| `DefaultStockpicker` | (stockpicker, fichero corto) | — | — | — |
| `Market`, `TrendFollowing`, `Mean-Reversal` | random-generation | Weighted | 344 | 2 |
| `Fuzzy` | random-generation | Weighted | 344 | 8 |
| `Daily` | random-generation | Weighted | 344 | 3 |

**Campos clave** (todos en el Build-Task XML, son ficheros con bytes no-UTF8 → leer con `grep -a`):
- **Motor:** `generationType="genetic-evolution"` vs `"random-generation"`.
- **Ranking:** `<Ranking type="…">`.
- **Complejidad:** `<Chart name="Main chart" minConditions maxConditions minExitConditions maxExitConditions
  minPeriod maxPeriod minShift maxShift/>`.
- **Building blocks:** `<Block key="…" weight="1" use="true|false" category="…">` (cada bloque lleva hijos
  `<Generated>/<Param>` con sus rangos). **`use` = flag de selección, `weight` = peso** → los bloques se
  activan/desactivan **programáticamente**. Catálogo de 344–354 por preset; lo que define un preset es
  **qué bloques tienen `use="true"`** (no la presencia del bloque). Categorías vistas: `signals`,
  `indicators`, `stopLimitBlocks`, `exitTypes`, `orderTypes`.
- **Market sides / SLPT / cross-checks:** `<MarketSides type>`, `<SLPTOptions>`, `<CrossChecks>` — mismos
  tags que un Build-Task normal.

**Qué cambia entre presets — building blocks** (verificado con un script de inspección no incluido en esta copia):

| Preset | Motor | Ranking | Blocks `use=true` | Por categoría |
|---|---|---|---|---|
| Default/Forex | genetic-evolution | ReturnDDRatio | 123 | signals 88, **stopLimitBlocks 29**, exitTypes 5, orderTypes 1 |
| TrendFollowing | random-generation | Weighted | 119 | signals 96, **indicators 16**, exitTypes 5, orderTypes 2 |
| Mean-Reversal | random-generation | Weighted | 72 | signals 48, **indicators 16**, exitTypes 5, orderTypes 3 |

- Los presets de estilo (random) usan `indicators`+`signals`; los Default/Forex (genético) usan
  `stopLimitBlocks` (entradas stop/limit) y **no** `indicators` → familias estructuralmente distintas.
- **TF vs MR = familia de señales** (no un toggle): TF habilita señales de **continuación**
  (`*Up/*Rising/*CrossUp/*Higher` + patrones alcistas como `BullishEngulfing`); MR habilita señales de
  **giro/fade** (`*Down/*Falling/*CrossDown/*Lower` + patrones bajistas `BearishEngulfing/DarkCloud/
  ShootingStar` + `EnterReverseAtMarket`). El estilo = el conjunto de bloques con `use="true"`.

**Símbolo embebido (artefacto a vigilar):** cada preset trae un `<Setup><Chart symbol="…">` de ejemplo
(`EURUSD_M1_dukas`/`GBPUSD_M1_dukas`; TF/Forex en H1, **MR en D1**) — NO el símbolo activo. En la GUI, aplicar
un preset puede **arrastrar/resetear el símbolo del tab de datos**. Por **comandos** el símbolo es el que TÚ
fijas en `<Chart symbol=>`/`<Symbols>` → **no se fuerza** a ninguno (verificado: los presets no llevan el
símbolo activo embebido). **Mismo evento, peor consecuencia:** aplicar un preset **resetea el Setup a
cost-blind** (`None`/spread 0/swap off/engine MT4) → pierdes el coste del instrumento. Ver §7 ("el coste vive
en el Setup") y el proceso fijo anti-invención.

**Qué cambia entre presets — las otras 4 secciones** (misma inspección): **TF y MR son idénticos** en What To
Build, Trading Options, Cross Checks y Ranking — **solo difieren en los building blocks**. El outlier es
**Forex** (familia genético-Default). Diferencias (lo no listado = idéntico en los 3):
- **What To Build:** Forex = genético, maxConditions 3, periodo 5–200, additionalCharts 2; TF/MR = random,
  maxConditions 2, periodo 15–80, 1 chart. Iguales: minConditions 1, **MarketSides both**, StrategyType
  simple, ExitTypes habilitados.
- **Trading Options:** Forex usa esquema propio (tiempos en segundos-del-día, `Min/MaxSL`+`MaxPT`+
  `ReservedBars=50`); TF/MR usan HHMM + `Min/MaxSLPT` + `ComputeDailyStats`.
- **Cross Checks:** casi idéntico — solo difieren el **toggle global** `CrossChecks.use` (Forex **true**,
  TF/MR **false**) y `MonteCarloManipulation.use` (Forex false, TF/MR true). ⚠ En TF/MR el bloque global está
  **OFF** → sus sub-checks `use=true` (`RetestWithHigherPrecision`, `MonteCarloManipulation`) **NO corren**
  hasta encender el toggle global.
- **Ranking:** Forex `ReturnDDRatio` + `MaxStrategies=1000`; TF/MR `Weighted` + `24999`.

→ **TF→MR = cambiar solo los blocks.** Una "config general" (motor/ranking/complejidad/trading options) se
fija desde Forex/Default y solo se varía la selección de bloques por estilo.

**Decoupling (lo que la GUI NO deja hacer):** en la GUI, elegir un preset **sobreescribe todo** (motor +
ranking + bloques). Pero motor (`generationType`), ranking y `<Blocks>` son **secciones XML independientes**
→ se puede tomar el conjunto `use="true"` de un preset de estilo y **graftarlo** sobre un Build-Task que
conserve tu `generationType="genetic-evolution"` + tu `<Ranking>` + tu complejidad. → **config general fija
(motor/ranking/población inicial/condiciones máximas) + building blocks variables por estilo**, sin perder el
genético.

## 13. Landmines (resumen)
- ❌ `type="None"` en comisión = cost-blind (default plantillas). Usar `SizeBased` (4.5 en el ejemplo AXI, §7).
- ❌ `-databank export` para trades → vacío. Usar `orderstocsv`.
- ❌❌ Matar el motor headless sin `-databank save`+`synctofiles` → la GUI ve el databank VACÍO y se PIERDEN las estrategias generadas (export=CSV de métricas, NO los `.sqx`). Persistir SIEMPRE antes de apagar (§6.1).
- ❌❌❌ **Abrir la GUI con `sqcli` VIVO** → corrompe databanks (instancia única, workspace compartido; la GUI lee estado parcial y al guardar sobreescribe). Caso real: `Results` 4000→136 (2026-07-10). Cerrar sqcli + persistir TODO antes de abrir GUI (§6.2). Script: `sqx/persist_before_gui.py`.
- ❌❌ Matar el motor **antes de `TASK FINISHED`** → el `global_log` queda VACÍO = pierdes el desglose Rejected/Accepted por motivo (§9.2.1). Parar graceful (`-project action=stop`) o dejar que llegue al `StopCondition`.
- ❌ `synctofiles`/`syncfromfiles` por HTTP con nombre con espacio (`Second%20OOS`) → **trunca** a "Second". Usar `sqcli -run file=` con el nombre **entre comillas** (`name="Second OOS"` — sin comillas también trunca). `count` con `%20` también trunca en B144 ("Databank 'Last' doesn't exist", 2026-10-27); para contar bancos con espacios, `-databank action=list project=<p>` → `<banco>, Records: N` de todos.
- ❌ `format=Custom`/`app_RE` en `-data import` → no aplica. Usar `"MetaTrader4 bar format"` (sin header).
- ❌ Multi-`<Setup>` para net por-símbolo → no verificado. Bucle 1-símbolo/retest.
- ❌ GUI abierta + `sqcli` → bloqueo. Cerrar GUI. ❌ Composer headless → GUI-only.
- ⚠ `startFromTask`/`startOnlyTask` `task=N` = **ID interno SQX** (nº del nombre del task), NO posición del config → reanudar con `active="false"` en los tasks a saltar + `-project action=start`; verificar el `Task:` arrancado en el `global_log` (§2).
- ⚠ Spawnear `sqcli` por cada comando (12 s frío) → motor caliente HTTP o `-run file=` (un motor por task del
  pipeline sí es intencionado, §0).
- ⚠ B143: 10-15× + leaks MC/WFM → benchmark antes de batch grande.
- ⚠ STS: leer distribución/z, no "% verdes" (pseudoreplicación 85%).
- ⚠ No escribir un Task XML desde cero → clonar plantilla y mutar.

## 14. Data range por task / walk-forward multi-región (headless) 📄/🟡 (verificado 2026-07-14, `dax40_4h`)

Un custom project de robustez tiene **varios tasks (Build + N Retest) y cada uno retestea sobre
una ventana temporal distinta**. El data range NO es global: vive en el `<Setup>` de cada task-XML.
Clave para walk-forwards con holdouts múltiples (Second OOS / Build / Real OOS).

### 14.1 Dónde vive cada rango (3 cosas distintas, NO confundir)
| Rango | Tag | Formato | Dónde |
|---|---|---|---|
| **Binding de datos disponibles** (lo que evita "unresolved", §9.3) | `<Symbol dateFrom dateTo>` | **epoch ms** | cada task-XML (Data/Symbols) |
| **Ventana de test del task** (la casilla Start/End del Builder) | `<Setups><Setup dateFrom dateTo>` | `YYYY.MM.DD` | cada task-XML — el **1er `<Setup>`** |
| **Split IS/OS dentro del Build** | `<OutOfSample><Range dateFrom dateTo/></OutOfSample>` | `YYYY.MM.DD` | sólo `Build-Task*.xml` |

> Hay un **2º bloque `<Setups detailed="true">`** con `<Setup>` por sub-chart (additional markets).
> Para la ventana del test principal basta el **primer** `<Setup>` (el de `<Setups>` sin `detailed`).

### 14.2 Cómo identificar el ROL de un task (determinista) 📄
Cada Retest declara su rol en `<Databanks>` dentro del task-XML:
```xml
<Databanks retestSelected="false">
  <Databank label="Output databank" name="Output" value="Second OOS" />
  <Databank label="Input databank"  name="Input"  value="Results" />
</Databanks>
```
**Usar el `value=` del `Output`** ("Second OOS", "Real OOS", "High Precission", "Different Markets
and TimeFrames", "MC - …") para mapear task→rol. **NO fiarse** del `name=` en `config.xml`
("Retest strategies 4" es ambiguo) ni del orden (landmine `task=N` del §2).

### 14.3 El pipeline es una CADENA de databanks (patrón §11 llevado al extremo) 📄
Encadenando Input→Output, los supervivientes fluyen:
```
Initial pop → [Build → Results] → [Retest → Second OOS] → [Retest → Different Markets/TF]
  → [Retest → High Precision (M1)] → [Retest → MC ×3] → [Retest → Real OOS]
```
Cada etapa retestea sobre **un rango distinto** y filtra. 2 holdouts (Second OOS pre-build, Real OOS
post-build) rodean un build con tests de robustez en cadena sobre la build window.

### 14.4 LANDMINE: "Modify symbols" + "Apply default settings" NO re-mapea los `<Setup>` ❌ (verificado)
Al clonar un proyecto a otro activo vía la GUI (*Modify symbols* `SÍMBOLO/TF` + *Apply default
settings*):
- ✅ Reescribe el binding del `<Symbol>` al rango disponible real (ej: DAX 2013-09-30 → 2026-07-13).
- ✅ Carga coste del `<InstrumentInfo>` del nuevo símbolo (spread/comm/swap).
- ❌ **NO re-mapea los `<Setup>` de los Retest tasks** — quedan con las fechas del proyecto origen.
  Caso real (`dax40_4h`, clon de `gold 4h`): Second OOS apuntaba a 2003-2007 (rango del oro, el DAX
  empieza en 2013 → **vacío**) y los tests a 2007-2022 (parcialmente fuera del dato DAX).
- ❌ **NO reescribe el `<OutOfSample>`** del Build (lo deja con el IS/OS del origen o un OOS trivial).
- ❌ **NO re-mapea los bindings del `<Symbol>` (epoch ms) de los Retest tasks** — sólo reescribe el
  del **Build/chart principal**. Cada Retest conserva el binding del origen → al abrir, SQX reporta
  "Missing N days" para los años que el nuevo símbolo no tiene (caso `dax40_4h`: Retest con bindings
  del oro 2003-2007/2007-2022 sobre un DAX que empieza en 2013 → "Missing 3801 days"). Fix: alinear
  TODOS los `<Symbol name=<sym>>` al rango disponible (epoch), dejando intactos los additional-markets
  ajenos (p.ej. XAUUSD en el Retest "Different Markets").

→ Tras Modify symbols hay que **inyectar las fechas por task a mano** (GUI: casillas Start/End del
Builder por etapa; o headless: mutar el 1er `<Setup>` de cada task-XML + el `<OutOfSample>` del build).
No existe comando `sqcli` para esto (§14.5); es mutación del `.cfx`.

### 14.5 Inyectar fechas por task (headless = mutar el `.cfx`) ✅
**Herramienta: `sqx/cfx_edit.py`** (2026-10-27). Cambia la ventana de cada task, el rango OOS, la fitness
de un Build, el máximo de estrategias y la condición de parada; simula por defecto, y con `--apply` hace
copia de seguridad, comprueba cada valor escrito y aborta si algo no cuadra (`python sqx/cfx_edit.py -h`).
Lo que hace por dentro:
- Por task-XML: reescribe `dateFrom`/`dateTo` del **1er** `<Setup>` de `<Data><Setups>` → ventana del test.
- Reescribe el bloque `<OutOfSample>…</OutOfSample>` (split IS/OS); existe en Build y en Retest.
- **No toca** el binding del `<Symbol>` (epoch ms): si la ventana nueva se sale de él, se niega y pide
  ampliar las fechas en la GUI.
- ⚠ GUI cerrada obligatoria al mutar (igual que §9.3); dejar el proyecto **resuelto** para que al
  reabrir no se dispare el reset-por-GUI.

Receta de particionado (ej: 3 regiones con 2 holdouts): para cada task, el `<Setup>` objetivo =
su región (Second OOS = pre-build; Build = ventana IS/OS; Real OOS = post-build; resto de tests =
build window). Ejemplo aplicado en `dax40_4h`.

## 15. Punteros
- Diagnóstico de por qué un build no produce supervivientes: `sqx/show_reject_breakdown.py`
  (desglose Rejected/Accepted por motivo desde el `global_log`, §9.2.1).
- Help oficial local: `…/StrategyQuantX<versión>.app/Contents/Resources/internal/web/SQUANT/help.txt`.
- Plantillas `.cfx`: `…/Contents/Resources/user/projects/{sqcli_testing,Builder,Retester,PortfolioMaster}/`.
- Predefined build configs: `…/Contents/Resources/internal/web/BUILDER/simpleTemplates/*.xml` (§12).
- Otros docs de esta copia: `memoria-heap.md` (RAM, heap, `--slim`, vigilante), `persistencia-databanks.md`
  (carga lazy, sync, export, bancos declarados, `gui_slim`), `cascadas.md` (proyectos en secuencia,
  protocolo post-build), `version-b144.md` (cambios B143→B144).
