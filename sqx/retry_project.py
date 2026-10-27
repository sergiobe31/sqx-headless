"""retry_project.py — re-ejecución selectiva de estrategias AUSENTES en una prueba.

Contexto (gold_h4_prod_oos, 2026-09-27/28): una Retest task pierde estrategias
cuando el cómputo falla (MC aleatorio, 0 trades) o el pipeline se interrumpe —
los bancos de salida quedan con ausentes respecto al roster. ``pass_matrix.py``
las detecta (celdas vacías). Este script las re-ejecuta SIN re-correr la cadena:

1. ``plan``:    lista los ausentes por prueba desde pass_matrix.csv.
2. ``build``:   crea un proyecto NUEVO ``<proj>_retry`` (el original NO se toca)
                con una task por prueba: databank ``Retry_<prueba>`` poblado
                copiando los .sqx de los ausentes desde la referencia congelada
                (snapshot de Second_OOS), y salida a ``Retry_<prueba>_out``.
3. ``export``:  tras correr el proyecto retry (orquestador per-task), exporta
                los ``Retry_*_out`` a CSV vía sqcli (motor idle, sin correr nada).
4. ``merge``:   vuelca los CSV retry a pass_matrix.csv: ausente -> True/False;
                los que sigan sin aparecer = fallo de cómputo repetido (aviso).

Flujo:
    python sqx/retry_project.py plan --dir sqx/output/<proj>_<fecha>
    python sqx/retry_project.py build --proj <proj> --dir sqx/output/<proj>_<fecha> \
        --reference sqx/output/<proj>_<fecha>/reference_second_oos \
        --tasks MC_Execution_Realism,MC_Randomize_parameters,Real_OOS
    # GUI CERRADA:
    bash sqx/run_pipeline_per_task.sh <proj>_retry
    python sqx/retry_project.py export --proj <proj>_retry --dir sqx/output/<proj>_<fecha>
    python sqx/retry_project.py merge --dir sqx/output/<proj>_<fecha>
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402
import stop_and_persist as SP  # noqa: E402  engine_pids / gui_pids / kill_engine (R5)

try:  # ruta de instalación SQX: única fuente en sqx/paths.py
    from sqx.paths import SQCLI as _SQCLI
except ImportError:  # ejecutado como script (sys.path[0] = sqx/)
    from paths import SQCLI as _SQCLI

SQCLI = str(_SQCLI)
PORT = 5050
RE_IN = re.compile(r'(<Databank[^>]*name="Input"[^>]*value=")[^"]*(")')
RE_OUT = re.compile(r'(<Databank[^>]*name="Output"[^>]*value=")[^"]*(")')


def san(prueba: str) -> str:
    """Nombre de prueba -> token seguro para banco/fichero (espacios fuera).

    Los bancos con espacio rompen el HTTP-persist del driver y ensucian los
    taskXMLFile; el merge hace el mapeo inverso ('_' -> ' ')."""
    return prueba.replace(" ", "_")


# --------------------------------------------------------------------------
# helpers compartidos
# --------------------------------------------------------------------------

def load_matrix(dir_export: Path) -> pd.DataFrame:
    m = dir_export / "pass_matrix.csv"
    if not m.exists():
        raise SystemExit(f"FAIL-LOUD: falta {m} (corre antes sqx/pass_matrix.py)")
    d = pd.read_csv(m, sep=";")
    # al releer el CSV las pass_* vienen como strings "True"/"False" + NaN: normalizar a bool
    for c in [c for c in d.columns if c.startswith("pass_")]:
        d[c] = d[c].replace({"True": True, "False": False})
    return d


def pruebas_de(matriz: pd.DataFrame, tasks: str) -> list[str]:
    disponibles = [c[len("pass_"):] for c in matriz.columns if c.startswith("pass_")]
    if not tasks:
        return disponibles
    sel = [s.strip() for s in tasks.split(",") if s.strip()]
    faltan = set(sel) - set(disponibles)
    if faltan:
        raise SystemExit(f"FAIL-LOUD: --tasks desconocidas: {sorted(faltan)} "
                         f"(disponibles: {disponibles})")
    return sel


def ausentes(matriz: pd.DataFrame, prueba: str) -> list[str]:
    """Nombres crudos de estrategias del roster SIN celda en esa prueba."""
    col = matriz[f"pass_{prueba}"]
    return matriz.loc[col.isna(), "strategy"].tolist()


def retry_dir(proj_retry: str, prueba: str) -> Path:
    return REG.PROJECTS / proj_retry / "databanks" / f"Retry_{san(prueba)}"


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def cmd_plan(a: argparse.Namespace) -> int:
    matriz = load_matrix(Path(a.dir))
    total = 0
    for p in pruebas_de(matriz, a.tasks):
        n = len(ausentes(matriz, p))
        total += n
        print(f"{p:35s} {n:5d} ausentes")
    print(f"\ntotal celdas a re-ejecutar: {total}")
    return 0


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def read_cfx(proj: str) -> dict[str, str]:
    """project.cfx -> {nombre_entrada: texto}."""
    cfx = REG.PROJECTS / proj / "project.cfx"
    if not cfx.exists():
        raise SystemExit(f"FAIL-LOUD: no existe {cfx}")
    with zipfile.ZipFile(cfx) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def task_output_map(entries: dict[str, str]) -> dict[str, str]:
    """output databank -> taskXMLFile, según config.xml + XML de cada task."""
    cfg = ET.fromstring(entries["config.xml"])
    mapa = {}
    for t in cfg.find("Tasks"):
        m = RE_OUT.search(entries[t.get("taskXMLFile")])
        if m:
            mapa[re.search(r'value="([^"]*)"', m.group(0)).group(1)] = t.get("taskXMLFile")
    return mapa


def clone_task_xml(xml: str, prueba: str) -> str:
    xml, n_in = RE_IN.subn(rf"\g<1>Retry_{san(prueba)}\g<2>", xml)
    xml, n_out = RE_OUT.subn(rf"\g<1>Retry_{san(prueba)}_out\g<2>", xml)
    if n_in != 1 or n_out != 1:
        raise SystemExit(f"FAIL-LOUD: task de {prueba}: sustituciones Input={n_in} Output={n_out} "
                         "(esperaba 1 y 1; ¿formato de databanks distinto?)")
    return xml


def cmd_build(a: argparse.Namespace) -> int:
    dir_export = Path(a.dir)
    matriz = load_matrix(dir_export)
    reference = Path(a.reference)
    if not reference.is_dir():
        raise SystemExit(f"FAIL-LOUD: --reference {reference} no es directorio "
                         "(snapshot de Second_OOS)")
    proj_retry = a.name or f"{a.proj}_retry"
    dest = REG.PROJECTS / proj_retry
    if dest.exists():
        raise SystemExit(f"FAIL-LOUD: {dest} ya existe — bórralo antes (no se pisa)")

    entries = read_cfx(a.proj)
    out2task = task_output_map(entries)
    cfg = ET.fromstring(entries["config.xml"])

    # tasks retry: una por prueba con ausentes
    tasks_xml: dict[str, str] = {}
    plan: list[tuple[str, list[str]]] = []
    for p in pruebas_de(matriz, a.tasks):
        faltan = ausentes(matriz, p)
        if not faltan:
            print(f"{p}: 0 ausentes — sin task retry")
            continue
        if p not in out2task:
            raise SystemExit(f"FAIL-LOUD: {p} no es output de ninguna task del .cfx "
                             f"(outputs: {sorted(out2task)})")
        tasks_xml[f"Retry-Task-{san(p)}.xml"] = clone_task_xml(entries[out2task[p]], p)
        plan.append((p, faltan))
    if not plan:
        raise SystemExit("FAIL-LOUD: ninguna prueba con ausentes — nada que construir")

    # materializar bancos Retry_<prueba> desde la referencia
    for p, faltan in plan:
        d = retry_dir(proj_retry, p)
        d.mkdir(parents=True)
        sin_archivo = [n for n in faltan if not (reference / f"{n}.sqx").exists()]
        if sin_archivo:
            raise SystemExit(f"FAIL-LOUD: {len(sin_archivo)} ausentes sin .sqx en {reference}; "
                             f"ejemplos: {sin_archivo[:5]}")
        for n in faltan:
            shutil.copy2(reference / f"{n}.sqx", d / f"{n}.sqx")

    # config.xml del proyecto retry: sólo las tasks retry, databanks declarados
    cfg.set("name", proj_retry)
    tasks_el = cfg.find("Tasks")
    for t in list(tasks_el):
        tasks_el.remove(t)
    for p, _ in plan:
        ET.SubElement(tasks_el, "Task", {
            "type": "Retest", "name": f"Retry {p}", "showSettingsOverview": "false",
            "sampleName": "Custom", "active": "true",
            "taskXMLFile": f"Retry-Task-{san(p)}.xml", "title": f"Retry {p}"})
    dbs_el = cfg.find("Databanks")
    for d in list(dbs_el):
        dbs_el.remove(d)
    for i, (p, _) in enumerate(plan):
        ET.SubElement(dbs_el, "Databank", {"name": f"Retry_{san(p)}", "view": "Default - Main data",
                                           "syncType": "Auto-sync every 1 hour", "position": str(2 * i)})
        ET.SubElement(dbs_el, "Databank", {"name": f"Retry_{san(p)}_out", "view": "Default - Main data",
                                           "syncType": "Auto-sync every 1 hour", "position": str(2 * i + 1)})

    with zipfile.ZipFile(dest / "project.cfx", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("config.xml", ET.tostring(cfg, encoding="unicode"))
        for fname, xml in tasks_xml.items():
            z.writestr(fname, xml)

    print(f"\nproyecto retry: {dest}")
    for p, faltan in plan:
        print(f"  Retry {p:30s} {len(faltan):5d} estrategias -> Retry_{san(p)} -> Retry_{san(p)}_out")
    print(f"\nsiguiente paso (GUI CERRADA):  bash sqx/run_pipeline_per_task.sh {proj_retry}")
    return 0


# --------------------------------------------------------------------------
# export (motor idle: arranca sqcli, exporta Retry_*_out, muere)
# --------------------------------------------------------------------------

def http_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://localhost:{PORT}/call?cmd=-help", timeout=3) as r:
            r.read()
        return True
    except Exception:
        return False


def http_call(cmd: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(f"http://localhost:{PORT}/call?cmd={cmd.replace(' ', '%20')}",
                                timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def wait_loaded(proj: str, timeout_s: int = 300) -> None:
    """Barrera de carga de databanks (misma race que run_pipeline_headless.sh §2b):
    espera a que 'In databank' del status se estabilice (3 lecturas iguales / 5s)."""
    prev, stable, t0 = None, 0, time.time()
    while time.time() - t0 < timeout_s:
        try:
            st = http_call(f"-project action=status name={proj}")
        except Exception:
            st = ""
        cur = None
        for line in st.splitlines():
            if "In databank" in line:
                cur = line.split()[-1].replace(",", "")
        if cur is not None and cur == prev:
            stable += 1
            if stable >= 3:
                print(f"databanks cargados (In databank={cur} estable)")
                return
        else:
            stable = 0
        prev = cur
        time.sleep(5)
    raise SystemExit(f"FAIL-LOUD: carga de {proj} no se estabilizó en {timeout_s}s "
                     f"(último In databank={prev})")


def cmd_export(a: argparse.Namespace) -> int:
    dir_export = Path(a.dir)
    outdir = dir_export / "retry_exports"
    proj = a.proj
    if a.banks:
        bancos = [s.strip() for s in a.banks.split(",") if s.strip()]
    else:
        bancos = sorted(p.name for p in (REG.PROJECTS / proj / "databanks").glob("Retry_*_out"))
    if not bancos:
        raise SystemExit(f"FAIL-LOUD: {proj} sin databanks Retry_*_out (¿corriste el orquestador?)")
    if SP.gui_pids():
        raise SystemExit("FAIL-LOUD: GUI de SQX abierta — ciérrala antes (instancia única)")
    if SP.engine_pids():  # por nombre exacto del proceso (R5), no por texto de cmdline
        raise SystemExit("FAIL-LOUD: ya hay un sqcli corriendo (instancia única)")

    outdir.mkdir(exist_ok=True)
    proc = subprocess.Popen([SQCLI], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        t0 = time.time()
        while time.time() - t0 < 120:
            if http_ok():
                break
            time.sleep(2)
        else:
            raise SystemExit("FAIL-LOUD: motor sqcli no respondió en 120s")
        wait_loaded(proj)
        for b in bancos:
            dst = (outdir / f"databank_{b}.csv").resolve()   # absoluta: el motor resuelve contra SU cwd
            resp = http_call(f"-databank action=export project={proj} name={b} file={dst}", timeout=300)
            if not dst.exists():
                raise SystemExit(f"FAIL-LOUD: export de {b} no generó {dst}; respuesta: {resp[:300]}")
            with open(dst, encoding="utf-8-sig") as fh:
                n = sum(1 for _ in fh) - 1
            print(f"{b}: {n} estrategias -> {dst}")
    finally:
        # Solo el motor que arrancamos aquí (nace con start_new_session → su pgid = proc.pid): nunca un
        # sqcli ajeno (R3/R5). TERM, gracia de 3 s y kill -9 (SIGTERM no basta — landmine fitab).
        # PLAN-DESVIO: el plan dice "pasa a engine_pids()/kill_engine()" (= TODOS los sqcli); se filtra por
        # el grupo propio (misma razón que en run_task_headless.run).
        SP.kill_engine([p for p in SP.engine_pids() if SP.engine_owner_pgid(p) == proc.pid], grace=3)
        try:
            proc.wait(timeout=5)  # reapar: un hijo muerto sin wait queda <defunct>
        except Exception:
            pass
    print(f"\nsiguiente paso:  python sqx/retry_project.py merge --dir {dir_export}")
    return 0


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------

def cmd_merge(a: argparse.Namespace) -> int:
    dir_export = Path(a.dir)
    retry_dir_ = dir_export / "retry_exports"
    if not retry_dir_.is_dir():
        raise SystemExit(f"FAIL-LOUD: falta {retry_dir_} (corre antes el subcomando export)")
    matriz = load_matrix(dir_export)
    mtime = time.strftime("%Y%m%d_%H%M%S")
    backup = dir_export / f"pass_matrix.csv.bak_{mtime}"
    shutil.copy2(dir_export / "pass_matrix.csv", backup)

    for csv_retry in sorted(retry_dir_.glob("databank_Retry_*_out.csv")):
        raw = csv_retry.name[len("databank_Retry_"):-len("_out.csv")]
        prueba = raw
        if f"pass_{prueba}" not in matriz.columns and f"pass_{raw.replace('_', ' ')}" in matriz.columns:
            prueba = raw.replace("_", " ")   # banco sanitizado (espacios -> '_') en build
        col = f"pass_{prueba}"
        if col not in matriz.columns:
            print(f"aviso: {prueba} no es prueba de la matriz — se omite")
            continue
        d = pd.read_csv(csv_retry, sep=";", encoding="utf-8-sig", dtype=str)
        if "Filters result" not in d.columns:
            print(f"aviso: {csv_retry.name} sin columna 'Filters result' — se omite")
            continue
        vals = d.set_index("Strategy Name")["Filters result"].str.strip() == "PASSED"
        era_ausente = matriz[col].isna()
        mask = era_ausente & matriz["strategy"].isin(vals.index)
        matriz.loc[mask, col] = matriz.loc[mask, "strategy"].map(vals)
        siguen = int(era_ausente.sum() - mask.sum())
        print(f"{prueba}: {int(mask.sum())} ausentes resueltos "
              f"({int(matriz[col].eq(True).sum())} passed totales), "
              f"{siguen} siguen sin computar (fallo repetido)")

    # recomputar score/n_absent sobre las pruebas actuales
    cols = [c for c in matriz.columns if c.startswith("pass_")]
    matriz["score"] = matriz[cols].eq(True).sum(axis=1).astype(int)
    matriz["n_absent"] = matriz[cols].isna().sum(axis=1).astype(int)
    matriz.to_csv(dir_export / "pass_matrix.csv", sep=";", index=False)
    print(f"\nmatriz actualizada (backup en {backup.name})")
    return 0


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="lista ausentes por prueba")
    p.add_argument("--dir", required=True)
    p.add_argument("--tasks", default="")
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("build", help="crea el proyecto retry (no toca el original)")
    p.add_argument("--proj", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--reference", required=True)
    p.add_argument("--tasks", default="")
    p.add_argument("--name", default="")
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("export", help="exporta Retry_*_out (o --banks) a CSV (motor idle)")
    p.add_argument("--proj", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--banks", default="", help="bancos concretos a exportar, separados por coma "
                                               "(default: todos los Retry_*_out del proyecto)")
    p.set_defaults(fn=cmd_export)
    p = sub.add_parser("merge", help="vuelca los CSV retry a pass_matrix.csv")
    p.add_argument("--dir", required=True)
    p.set_defaults(fn=cmd_merge)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
