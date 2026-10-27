#!/usr/bin/env python3
"""Monitor on-demand de proyectos SQX headless vía Streamlit.

Lee el `global_log_<ts>.log` más reciente de un proyecto y lo parsea para mostrar
el estado del pipeline SIN abrir la GUI de SQX. Reutiliza el parser de
`show_reject_breakdown.py` (regex + latest_log) y lo amplía a multi-task + métricas
de Retest (Total tested / Passed / Failed) y Filtering (Moved).

On-demand: NO auto-refresh. Selecciona proyecto + click "Actualizar" → lee el log.
Auto-muestreo OPCIONAL: el botón "Auto-muestrear" del sidebar lanza `snapshot_loop.py`
detached (sobrevive a cerrar la app) y rellena el histórico sin clics.

Histórico de fitness (gráfico de evolución):
  - Segmentado por CORRIDA (`assign_runs`): una caída de `running_secs` entre muestras
    delata un run nuevo; sin esto el gráfico "cosía" corridas distintas del mismo
    proyecto sobre el mismo eje de minutos y falseaba la señal de parar/seguir.
  - Métricas: banda IQR (p25–p75) + líneas p50/p90/máx. La mediana y el rango
    intercuartílico describen el grueso del databank (robustos a outliers); la media
    de ~1500 estrategias la arrastran los extremos.
  - Detector de PLATEAU (`fitness_trend`): pendiente relativa de la p50 en las últimas
    k muestras de la corrida actual; <1%/h → candidato a parar.

Uso:
  streamlit run sqx/sqx_monitor.py

Requisitos: GUI de SQX puede estar abierta o cerrada (leemos disco, no el motor).
Si el motor sqcli está vivo, los databanks en disco pueden ir retrasados vs RAM
(según el syncType del databank: 'never' = solo al guardar, 'every 1 hour' = cada hora).
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

# --- Parser importado del parser canónico de logs ---
sys.path.insert(0, str(Path(__file__).resolve().parent))
import builder_registry as REG  # noqa: E402  PROJECTS = user/projects de SQX

# Reusamos las regex y helpers de show_reject_breakdown (fuente única del parsing)
import show_reject_breakdown as SRB  # noqa: E402
import stop_and_persist as SP  # noqa: E402  Stop & persist determinista (botón sidebar)
import run_task_headless as RT  # noqa: E402  Ejecutar task headless vía active-flags
import inspect_build_tasks as IBT  # noqa: E402  What-to-build de las Build tasks

# Regex adicionales (campos que el parser original no extrae)
TASK_HEADER_RE = re.compile(r"Task:\s*(?P<name>.+?),\s*Type:\s*(?P<type>\w+)")
DURATION_RE = re.compile(r"TASK FINISHED at .+? in (.+?)\.\s*$")
RETEST_RE = re.compile(r"Total tested:\s*(\d+).*?Passed:\s*(\d+).*?Failed:\s*(\d+)", re.S)
MOVED_RE = re.compile(r"Moved:\s*(\d+)")
DATABANKS_BEFORE_RE = re.compile(r"Databanks before start:\s*(.+)")
DATABANKS_AFTER_RE = re.compile(r"Databanks after finish:\s*(.+)")
SPH_RE = re.compile(r"Strategies per hour:\s*([\d.]+)")


@dataclass
class TaskBlock:
    """Un bloque de task dentro de un global_log (un log puede tener N tasks)."""
    name: str = ""
    type: str = ""           # Build / Retest / Filtering
    started: str = ""        # hora local (str del log)
    finished: str = ""
    duration: str = ""
    finished_flag: bool = False
    # Build
    generated: int | None = None
    accepted: int | None = None
    rejected: int | None = None
    strat_per_hour: float | None = None
    # Retest
    total_tested: int | None = None
    passed: int | None = None
    failed: int | None = None
    # Filtering
    moved: int | None = None
    # Databanks (dict name->count)
    banks_before: dict = field(default_factory=dict)
    banks_after: dict = field(default_factory=dict)
    rejected_details: list = field(default_factory=list)   # [(reason, count, pct)]
    accepted_details: list = field(default_factory=list)


def _parse_databanks(s: str) -> dict:
    """'Results (0), Last generation (5), ...' -> {'Results': 0, 'Last generation': 5, ...}"""
    out = {}
    for m in re.finditer(r"(.+?)\s*\((\d+)\)", s):
        out[m.group(1).strip()] = int(m.group(2))
    return out


def parse_tasks(text: str) -> list[TaskBlock]:
    """Parsea TODOS los task blocks de un global_log (split por separador de guiones)."""
    # Cada task empieza tras una línea de guiones; splitamos por el separador largo.
    # El separador real es una línea de ~110 guiones. Usamos como ancla 'TASK STARTED'.
    blocks = re.split(r"-{60,}", text)
    tasks: list[TaskBlock] = []
    for blk in blocks:
        if "TASK STARTED" not in blk:
            continue
        t = TaskBlock()
        m = TASK_HEADER_RE.search(blk)
        if m:
            t.name, t.type = m.group("name").strip(), m.group("type").strip()
        st_ = SRB.STARTED_RE.search(blk)
        if st_:
            t.started = st_.group(1).strip()
        fin = SRB.FINISHED_RE.search(blk)
        if fin:
            t.finished_flag = True
            t.finished = fin.group(1).strip()
            dm = re.search(r"in (.+?)\.\s*$", fin.group(0))
            if dm:
                t.duration = dm.group(1).strip()
        summ = SRB.SUMMARY_RE.search(blk)
        if summ:
            t.generated, t.accepted, t.rejected = (int(x) for x in summ.groups())
        sph = SPH_RE.search(blk)
        if sph:
            t.strat_per_hour = float(sph.group(1))
        rt = RETEST_RE.search(blk)
        if rt:
            t.total_tested, t.passed, t.failed = (int(x) for x in rt.groups())
        mv = MOVED_RE.search(blk)
        if mv:
            t.moved = int(mv.group(1))
        db = DATABANKS_BEFORE_RE.search(blk)
        if db:
            t.banks_before = _parse_databanks(db.group(1))
        da = DATABANKS_AFTER_RE.search(blk)
        if da:
            t.banks_after = _parse_databanks(da.group(1))
        # Reutiliza el parser de details de show_reject_breakdown (bloques Rejected/Accepted)
        _, _, detail_blocks, _, _ = SRB.parse(blk)
        t.rejected_details = detail_blocks["Rejected"]
        t.accepted_details = detail_blocks["Accepted"]
        tasks.append(t)
    return tasks


def databanks_on_disk(proj: str) -> dict:
    """Cuenta .sqx por carpeta de databank (verdad de disco)."""
    d = REG.PROJECTS / proj / "databanks"
    out = {}
    if not d.is_dir():
        return out
    for sub in sorted(d.iterdir()):
        if sub.is_dir():
            n = sum(1 for f in sub.iterdir() if f.suffix == ".sqx")
            out[sub.name] = n
    return out


def project_status(proj: str) -> dict:
    """Estado consolidado de un proyecto: tasks del último log + databanks en disco."""
    log = SRB.latest_log(proj)
    info = {"log": log, "tasks": [], "log_empty": True, "log_size": 0}
    if log is None:
        return info
    info["log_size"] = log.stat().st_size
    text = log.read_text("utf-8", "replace")
    if not text.strip():
        return info
    info["log_empty"] = False
    info["tasks"] = parse_tasks(text)
    info["raw"] = text
    return info


# Regex para parsear el `-project action=status` (output del motor HTTP)
ST_LINE_RE = re.compile(r"^\s*(.+?)\s{2,}(.+?)\s*$")  # "clave    valor" (2+ espacios)


def motor_status(proj: str) -> dict | None:
    """Lee el status EN VIVO del motor sqcli vía HTTP :5050 (si está corriendo).

    Devuelve dict con las claves del status (Strategies generated, In databank, Running
    time, Accepted %, etc.) o None si el motor no responde. Codificación: espacios→%20,
    '=' literal (landmine doc §9.2).
    """
    cmd = f"-project action=status name={proj}".replace(" ", "%20")
    url = f"http://localhost:5050/call?cmd={cmd}"
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    if "Error" in text or "does not exist" in text or "not ready" in text.lower():
        return {"error": text.strip()[:200]}
    out = {}
    for line in text.splitlines():
        m = ST_LINE_RE.match(line)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out or None


def motor_alive() -> bool:
    """¿El motor sqcli está corriendo (HTTP :5050 responde)?"""
    try:
        with urllib.request.urlopen("http://localhost:5050/call?cmd=-project%20action=list", timeout=4) as r:
            return b"available projects" in r.read()
    except Exception:
        return False


def motor_databank_counts(proj: str, banks: list[str]) -> dict:
    """Cuenta de estrategias por databank EN EL MOTOR (RAM), vía HTTP :5050.

    Más fiable que el disco durante una corrida (los databanks 'every 1 hour' / 'never'
    pueden no haberse escrito aún). Devuelve {bank: count}. Misma lectura que Stop & persist
    (`-databank action=list`, que sí cuenta los bancos con espacios en B144).
    """
    return {b: n for b, n in SP._motor_counts(proj, banks, timeout=6).items() if n is not None}


def parse_running_time(s: str) -> float | None:
    """'1 min. 14 s.' -> 74.0 segundos. None si no parsea.

    Maneja el formato del motor SQX: day(s)/hour(s)/min(.)/s(.)  (usa 's.' para segundos).
    """
    if not s or s == "—":
        return None
    # Palabras largas primero (la alternation es left-to-right); 's' al final (segundos abrev.)
    pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*"
        r"(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|s)\b\.?",
        re.I,
    )
    unit_map = {"day": 86400, "days": 86400, "hour": 3600, "hours": 3600, "hr": 3600,
                "hrs": 3600, "minute": 60, "minutes": 60, "min": 60, "mins": 60,
                "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1}
    secs = 0.0
    found = False
    for val, unit in pattern.findall(s):
        found = True
        secs += unit_map.get(unit.lower(), 0) * float(val)
    return secs if found else None


def fmt_duration(secs: float | None) -> str:
    """7540.0 -> '2 h 5 min' — formato compacto para métricas de tiempo."""
    if secs is None:
        return "—"
    secs = int(round(secs))
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, s = divmod(secs, 60)
    parts = ([f"{d} d"] if d else []) + ([f"{h} h"] if h else [])
    if m or not parts:
        parts.append(f"{m} min")
    if not (d or h):
        parts.append(f"{s} s")
    return " ".join(parts)


def log_ts_epoch(ts: str) -> float | None:
    """Epoch del timestamp LOCAL de dentro del log ('2026.09.27 14:41:44.580').

    None si no parsea. Ojo: hora local (el nombre del FICHERO va en UTC; los de
    dentro en local — mina de 2026-06-30, ver docs/sqx_cli_commands.md §9.2.1).
    """
    from datetime import datetime
    for fmt in ("%Y.%m.%d %H:%M:%S.%f", "%Y.%m.%d %H:%M:%S"):
        try:
            return datetime.strptime(ts.strip(), fmt).timestamp()
        except ValueError:
            pass
    return None


def parse_project_config(proj: str) -> dict:
    """Lee el `.cfx` del proyecto y extrae campos clave del primer Build-Task.

    Regex (no ElementTree) porque los Task XML pueden tener bytes no-UTF8 (doc §12: leer
    con grep -a). Devuelve dict con stop_type/hours/minutes/passedStrategies, motor
    (generationType), max_strategies, symbol, timeframe, commissions, sides.
    """
    import zipfile
    cfx = REG.PROJECTS / proj / "project.cfx"
    out = {"has_cfx": cfx.exists()}
    if not cfx.exists():
        return out
    try:
        with zipfile.ZipFile(cfx) as z:
            cfg = z.read("config.xml").decode("utf-8", "replace")
            m = re.search(r'taskXMLFile="(Build-Task\d+\.xml)"', cfg)
            taskfile = m.group(1) if m else None
            task = (z.read(taskfile).decode("utf-8", "replace")
                    if taskfile and taskfile in z.namelist() else "")
    except Exception as e:
        out["error"] = str(e)
        return out
    sc = re.search(r'<StopCondition\s+type="([^"]*)"([^>]*)', task)
    if sc:
        out["stop_type"] = sc.group(1)
        for k in ("hours", "minutes", "days", "passedStrategies"):
            mm = re.search(rf'{k}="([^"]*)"', sc.group(2))
            if mm:
                out[f"stop_{k}"] = mm.group(1)
    m = re.search(r'generationType="([^"]*)"', task)
    if m:
        out["motor"] = "genético" if "genetic" in m.group(1) else ("random" if "random" in m.group(1) else m.group(1))
    m = re.search(r"<MaxStrategies>(\d+)</MaxStrategies>", task)
    if m:
        out["max_strategies"] = int(m.group(1))
    m = re.search(r'<Chart symbol="([^"]*)" timeframe="([^"]*)"', task)
    if m:
        out["symbol"], out["timeframe"] = m.group(1), m.group(2)
    m = re.search(r'<Commissions>\s*<Method type="([^"]*)"', task)
    if m:
        out["commissions"] = m.group(1)
    m = re.search(r'<MarketSides type="([^"]*)"', task)
    if m:
        out["sides"] = m.group(1)
    return out


def task_output_databanks(proj: str) -> dict:
    """{nombre de la task en el log: databank destino} — wrapper del helper compartido
    `builder_registry.task_output_databanks` (home único desde 2026-09-27: lo consumen
    también run_pipeline_per_task.sh y run_task_headless, sin Streamlit)."""
    return REG.task_output_databanks(proj)


def task_stop_condition(proj: str, task_name: str) -> dict:
    """StopCondition de UNA task del `.cfx`: {"type": ..., "secs": float|None}.

    `task_name` puede ser el title o el name del tag <Task> (el log usa ambos).
    secs=None para stops no temporales (passed-count) o si no resuelve.
    """
    import zipfile
    cfx = REG.PROJECTS / proj / "project.cfx"
    try:
        with zipfile.ZipFile(cfx) as z:
            cfg = z.read("config.xml").decode("utf-8", "replace")
            for m in re.finditer(r"<Task\b[^>]*>", cfg):
                tag = m.group(0)
                names = [am.group(1) for am in re.finditer(r'(?:title|name)="([^"]*)"', tag)]
                if task_name not in names:
                    continue
                xf = re.search(r'taskXMLFile="([^"]*)"', tag)
                if not xf or xf.group(1) not in z.namelist():
                    return {}
                task = z.read(xf.group(1)).decode("utf-8", "replace")
                sc = re.search(r'<StopCondition\s+type="([^"]*)"([^>]*)', task)
                if not sc:
                    return {}
                secs = 0.0
                for k, mult in (("days", 86400), ("hours", 3600), ("minutes", 60)):
                    mm = re.search(rf'{k}="([\d.]+)"', sc.group(2))
                    if mm:
                        secs += mult * float(mm.group(1))
                return {"type": sc.group(1), "secs": secs or None}
    except Exception:
        pass
    return {}


# =============================================================================
# Resumen del What-to-build (Build tasks del .cfx)
# =============================================================================

def what_to_build_summary(proj: str) -> list[dict]:
    """Resumen compacto de cada Build task del .cfx (entradas/salidas/lados/simetría/stop/SL/PT).

    Lee el `.cfx` directamente, así refleja los cambios que hayas guardado al cerrar SQX.
    """
    try:
        return IBT.summary_for_monitor(proj)
    except Exception as e:
        st.error(f"No se pudo leer el What-to-build de `{proj}`: {e}")
        return []


# =============================================================================
# Snapshots para el gráfico de evolución del fitness (persistencia local)
# =============================================================================

CACHE_DIR = Path.home() / ".sqx_monitor"
SNAP_THROTTLE_S = 90  # mínimo segundos entre snapshots (evita duplicar al clicar seguido)


def _snap_path(proj: str) -> Path:
    safe = re.sub(r"[^\w.-]", "_", proj)
    return CACHE_DIR / f"{safe}_snap.json"


def _safe_int(s):
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _safe_float(s):
    try:
        return float(str(s).replace("%", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def export_fitness_stats(proj: str, bank: str) -> dict:
    """Exporta un databank a CSV temporal y computa stats de 'Fitness (IS)'.

    Devuelve {} si falla (motor caído, databank vacío, columna ausente). El MOTIVO
    del fallo queda en LAST_FIT_ERROR — antes cualquier excepción se tragaba en
    silencio y el gráfico simplemente dejaba de crecer sin explicación (#6).
    Parsing robusto (#7): delimitador sniffed (no ";" fijo) y números normalizados
    con _norm_num (separador de miles en cualquier locale).
    """
    global LAST_FIT_ERROR
    LAST_FIT_ERROR = None
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
    cmd = f"-databank action=export project={proj} name={bank} file={tmp}".replace(" ", "%20")
    try:
        with urllib.request.urlopen(f"http://localhost:5050/call?cmd={cmd}", timeout=30) as r:
            r.read()
        df = _read_databank_csv(tmp)
        col = [c for c in df.columns if "Fitness (IS)" in c]
        if not col:
            LAST_FIT_ERROR = f"columna 'Fitness (IS)' ausente (cols: {list(df.columns)[:6]})"
            return {}
        s = df[col[0]].map(_norm_num).dropna()
        if s.empty:
            LAST_FIT_ERROR = "la columna 'Fitness (IS)' no tiene valores numéricos"
            return {}
        return {
            "fit_mean": round(float(s.mean()), 3),
            "fit_q1": round(float(s.quantile(0.25)), 3),
            "fit_median": round(float(s.median()), 3),
            "fit_q3": round(float(s.quantile(0.75)), 3),
            "fit_p90": round(float(s.quantile(0.9)), 3),
            "fit_max": round(float(s.max()), 3),
        }
    except Exception as e:
        LAST_FIT_ERROR = f"{type(e).__name__}: {e}"
        return {}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


LAST_FIT_ERROR: str | None = None  # motivo del último fallo de export_fitness_stats (#6)


def _read_databank_csv(path: str) -> pd.DataFrame:
    """Lee el CSV exportado por SQX con delimitador SNIFFED (no ";" fijo, #7).

    Hoy SQX exporta con ';', pero si cambia a ',' o '\\t' el parseo anterior daba
    un DataFrame de 1 columna y la columna 'Fitness (IS)' "desaparecía" en silencio.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        head = f.readline()
    try:
        sep = csv.Sniffer().sniff(head, delimiters=";,\t").delimiter
    except csv.Error:
        sep = ";"
    return pd.read_csv(path, sep=sep)


def _norm_num(x) -> float | None:
    """Normaliza un número del CSV del databank a float, o None si no parsea (#7).

    Regla determinista para miles/decimales en cualquier locale:
      - con ',' Y '.' → el ÚLTIMO separador es el decimal; el otro es miles (fuera).
        '1.234,56' (EU) → 1234.56 · '1,234.56' (US) → 1234.56
      - solo ',' → decimal europeo: '1,5' → 1.5
      - espacios/nbsp (miles franceses) se eliminan antes.
    El replace(",",".") anterior corrompía '1,234.56' → '1.234.56' → ValueError → NaN
    silencioso: fitness INCORRECTO en vez de ausente.
    """
    if x is None:
        return None
    s = re.sub(r"[\s ]", "", str(x))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def pick_active_bank(counts: dict, prev_snap: dict | None = None) -> str | None:
    """Elige el databank que se está LLENANDO (para seguir su fitness), #4.

    Heurística: el bank que más ha CRECIDO respecto al snapshot anterior (si hay
    conteos previos); si ninguno crece o no hay previo, el más poblado. El `max`
    a secas anterior fallaba tras un Filtering: el bank más poblado ya no era el
    activo y el gráfico cambiaba de databank sin avisar.
    """
    filled = {b: n for b, n in counts.items() if n and n > 0}
    if not filled:
        return None
    prev_counts = (prev_snap or {}).get("bank_counts") or {}
    if prev_counts:
        growth = {b: n - prev_counts.get(b, 0) for b, n in filled.items()}
        best = max(growth, key=growth.get)
        if growth[best] > 0:
            return best
    return max(filled, key=filled.get)


def take_snapshot(proj: str, motor_counts: dict) -> dict | None:
    """Muestra del estado actual: status motor + fitness del databank activo (#4)."""
    ms = motor_status(proj)
    if not ms or "error" in ms:
        return None
    snap = {
        "ts": time.time(),
        "running_secs": parse_running_time(ms.get("Running time so far", "")),
        "generated": _safe_int(ms.get("Strategies generated")),
        "in_databank": _safe_int(ms.get("In databank")),
        "accepted_pct": _safe_float(ms.get("Accepted")),
        "accepted_per_hour": _safe_float(ms.get("Accepted strategies per hour")),
    }
    if motor_counts:
        snap["bank_counts"] = dict(motor_counts)  # base del crecimiento del próximo pick
        prev = load_snapshots(proj)[-1] if load_snapshots(proj) else None
        active = pick_active_bank(motor_counts, prev)
        if active:
            fit = export_fitness_stats(proj, active)
            if fit:
                snap["active_bank"] = active
                snap["n_strat"] = motor_counts[active]
                snap.update(fit)
    return snap


def load_snapshots(proj: str) -> list[dict]:
    p = _snap_path(proj)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def save_snapshot(proj: str, snap: dict) -> list[dict]:
    """Append con throttle: ignora si el último snap fue hace < SNAP_THROTTLE_S."""
    CACHE_DIR.mkdir(exist_ok=True)
    snaps = load_snapshots(proj)
    if snaps and (snap["ts"] - snaps[-1].get("ts", 0)) < SNAP_THROTTLE_S:
        return snaps
    snaps.append(snap)
    _snap_path(proj).write_text(json.dumps(snaps))
    return snaps


def clear_snapshots(proj: str):
    p = _snap_path(proj)
    if p.exists():
        p.unlink()


# =============================================================================
# Segmentación del histórico por corrida (una caída del reloj = run nuevo)
# =============================================================================

RUN_RESET_TOL_S = 60  # caída de running_secs mayor que esto = nueva corrida (no jitter)


def assign_runs(snaps: list[dict]) -> list[dict]:
    """Etiqueta cada muestra con `run` (0, 1, 2, ...): la corrida a la que pertenece.

    El motor resetea "Running time so far" al arrancar → una CAÍDA de `running_secs`
    entre muestras consecutivas delata una corrida nueva. Sin esto, el gráfico de
    evolución "cosía" corridas distintas del mismo proyecto sobre el mismo eje de
    minutos: la 2ª se plegaba sobre la 1ª y falseaba la señal de parar/seguir.

    Tolerancia de RUN_RESET_TOL_S para no partir por jitter del status. OJO: en
    pipelines multi-task, si el motor resetea el reloj al cambiar de task
    (Build→Retest), cada task cuenta como "corrida" — para el caso de uso (seguir
    la evolución de un Build) es el comportamiento deseado.

    No muta el fichero: se aplica al cargar, así los históricos antiguos (sin `run`)
    se segmentan igualmente. Devuelve COPIAS de los dicts con la clave `run` añadida.
    """
    out, run, prev = [], 0, None
    for s in snaps:
        rs = s.get("running_secs")
        if prev is not None and rs is not None and rs < prev - RUN_RESET_TOL_S:
            run += 1
        out.append({**s, "run": run})
        if rs is not None:
            prev = rs
    return out


# =============================================================================
# Detector de plateau (pendiente relativa de la p50 de la corrida actual)
# =============================================================================

PLATEAU_REL_PER_H = 0.01  # <1%/h de mejora en la p50 = plateau (heurística, relativa)


def fitness_trend(fit_snaps: list[dict], k: int = 5) -> dict | None:
    """Pendiente de la p50 en las últimas `k` muestras (mínimo 3 con datos).

    Devuelve {"slope_h", "rel_h", "n"} o None si hay <3 puntos válidos:
      - slope_h: unidades de fitness/hora (mínimos cuadrados grado 1, a mano para
        no depender de numpy).
      - rel_h: slope_h / |media(p50)| → fracción de mejora por hora, comparable
        entre proyectos (el fitness no tiene escala fija: ReturnDDRatio ~1-3,
        NetProfit ~miles). rel_h < PLATEAU_REL_PER_H → plateau.
    """
    pts = [(s["running_secs"] / 60.0, s["fit_median"]) for s in fit_snaps[-k:]
           if s.get("running_secs") and s.get("fit_median") is not None]
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    xm = sum(xs) / len(xs)
    ym = sum(ys) / len(ys)
    den = sum((x - xm) ** 2 for x in xs)
    if den == 0 or ym == 0:
        return None
    slope_h = sum((x - xm) * (y - ym) for x, y in pts) / den * 60.0
    return {"slope_h": slope_h, "rel_h": slope_h / abs(ym), "n": len(pts)}


# =============================================================================
# Salud del .cfx: backup huérfano de un lanzamiento que murió a mitad
# =============================================================================

def pending_restore(proj: str) -> Path | None:
    """Backup `.cfx.pre_run_task.bak` huérfano, o None si no hay restore pendiente.

    Existe cuando un lanzamiento (RT.run) murió ENTRE mutar los active-flags y
    restaurar (p.ej. Streamlit matado durante la espera de arranque del motor).
    Mientras exista, el `project.cfx` actual puede tener tasks con `active="false"`
    SILENCIOSAS: el siguiente run correría menos tasks de las esperadas sin aviso.
    La restauración la hace RT.restore (copia el .bak sobre el .cfx y lo elimina —
    un .bak persistente tras un run sano sería un falso positivo permanente aquí).
    """
    bak = REG.PROJECTS / proj / "project.cfx.pre_run_task.bak"
    return bak if bak.exists() else None


def _is_frozen(prev_snap: dict | None, running_secs, generated) -> bool:
    """¿El status del motor está CONGELADO (proyecto terminado pero motor vivo)?

    El motor NO dice "not running" al acabar la última task: deja el status con los
    valores finales de esa task para siempre (observado en el experimento smoke_p2,
    2026-07-19: status clavado en "3 min. 2 s." durante 2+ min tras el TASK FINISHED).
    Dos lecturas consecutivas con running_secs Y generated idénticos = congelado.
    Sin muestra previa no se puede saber → False (fail-safe hacia RUNNING).
    """
    return (prev_snap is not None and running_secs is not None and generated is not None
            and prev_snap.get("running_secs") == running_secs
            and prev_snap.get("generated") == generated)


def outbase() -> Path:
    """Raíz de artefactos de corrida (`<proj>_state.json`, telemetría): `SQX_OUTDIR` o `sqx/output`
    (la misma que usan los scripts: `${SQX_OUTDIR:-$REPO/sqx/output}`)."""
    return Path(os.environ.get("SQX_OUTDIR") or (Path(__file__).resolve().parent / "output"))


def running_task_from_state(proj: str) -> str | None:
    """"índice nombre" de la task en `running` de `<outbase>/<proj>_state.json`, o None.

    El state lo escribe el orquestador (único escritor en pipeline) con claves "<idx>:<label>"; es la
    fuente fiable de la task en curso (el log `cascada_orch*.log` que se leía antes no lo escribe
    nadie). Con varias en running (no debería pasar) gana la de índice mayor."""
    f = outbase() / f"{proj}_state.json"
    try:
        tasks = json.loads(f.read_text(encoding="utf-8")).get("tasks") or {}
    except (OSError, ValueError, AttributeError):
        return None
    best: tuple[int, str, str] | None = None
    for key, t in tasks.items():
        if not isinstance(t, dict) or t.get("status") != "running":
            continue
        idx, _, label = str(key).partition(":")
        n = int(idx) if idx.isdigit() else -1
        if best is None or n > best[0]:
            best = (n, idx, label)
    return f"{best[1]} {best[2]}" if best else None


def orchestrator_status(proj: str) -> tuple[bool, str | None]:
    """¿Hay un orquestador `run_pipeline_per_task.sh` vivo para este proyecto?

    El orquestador apaga el motor al acabar cada task y lo relanza fresco para la
    siguiente (~1-4 min de pausa). Sin esta señal la app mostraba IDLE/FINISHED en
    plena cascada, como si el proyecto se hubiera detenido (lección 2026-09-29).
    Detección por `stop_and_persist.orchestrator_pids` (regex anclado: casa también la auto-copia
    `/tmp/<proj>_pertask_XXXXXX` que es el cmdline real entre tasks; el patrón ancho de antes solo
    veía el orquestador mientras `caffeinate -i bash …` seguía en la tabla de procesos).
    Devuelve (vivo, "índice nombre" de la task en curso según el state del orquestador).
    """
    try:
        alive = bool(SP.orchestrator_pids(proj))
    except SP.ProcProbeError:
        alive = False
    return alive, (running_task_from_state(proj) if alive else None)


def pipeline_up(proj: str) -> bool:
    """¿Hay un orquestador o un driver de `proj` vivos? (R6) Entre tasks el motor está apagado pero el
    pipeline sigue: el botón Stop & persist tiene que verse también entonces (con el motor muerto no hay
    HTTP, pero la bandera de parada sí frena al orquestador)."""
    try:
        return bool(SP.orchestrator_pids(proj) or SP.driver_pids(proj))
    except SP.ProcProbeError:
        return False


#: Ayuda del botón Stop & persist (R9): NO promete la GUI; el veredicto sale del reporte (`safe_for_gui`).
STOP_BUTTON_HELP = ("Para el build, persiste TODOS los databanks a .sqx y libera el motor sqcli. Con un pipeline "
                    "vivo delega en su driver y no se lanza ninguna task más. Al acabar te dice si ya es "
                    "SEGURO abrir la GUI (y si no, por qué).")


def gui_verdict(rep: dict) -> tuple[bool, str]:
    """(seguro, texto) del veredicto de la GUI de un reporte de `SP.run` (R9): "ya puedes abrir la GUI" SOLO con
    `safe_for_gui` (= `pre_gui_check`: motor, GUI, pipeline/cascada vivos, --slim sin restaurar, .bak); si no,
    los motivos."""
    if rep.get("safe_for_gui"):
        return True, "✅ Ya puedes abrir la GUI de SQX (nada vivo, sin bancos apartados ni .bak pendiente)."
    reasons = rep.get("reasons") or []
    if reasons:
        return False, "⛔ NO abras la GUI todavía: " + "; ".join(reasons)
    return False, "⛔ NO abras la GUI todavía: no se pudo confirmar que sea seguro."


# =============================================================================
# Auto-muestreo en background (snapshot_loop.py) — lanzar/parar desde el sidebar
# =============================================================================

LOOP_SCRIPT = Path(__file__).resolve().parent / "snapshot_loop.py"
LOOP_INTERVAL_S = 180  # 3 min, igual que el default del script


def loop_pid(proj: str) -> int | None:
    """PID del `snapshot_loop.py` corriendo para `proj`, o None si no hay.

    `pgrep -f` sobre la cmdline completa: el script recibe el proyecto como argv[1],
    así que el patrón discrimina por proyecto (pueden coexistir loops de varios).
    """
    r = subprocess.run(["pgrep", "-f", f"{LOOP_SCRIPT.name} {proj}"],
                       capture_output=True, text=True)
    pids = r.stdout.split()
    return int(pids[0]) if r.returncode == 0 and pids else None


def start_loop(proj: str, interval: int = LOOP_INTERVAL_S) -> int:
    """Lanza snapshot_loop.py detached (start_new_session) → sobrevive al cierre
    del servidor Streamlit (mismo patrón que RT.run). Devuelve el PID del loop.
    Log en /tmp/<proj>_snaploop.log (append)."""
    log = Path("/tmp") / f"{proj}_snaploop.log"
    lf = open(log, "a")
    proc = subprocess.Popen([sys.executable, str(LOOP_SCRIPT), proj, str(interval)],
                            stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    lf.close()  # el hijo tiene su propia copia del fd; cierro en el padre (no fuga)
    return proc.pid


def stop_loop(proj: str) -> bool:
    """SIGTERM al loop de `proj` (muere entre muestras). False si no había loop."""
    pid = loop_pid(proj)
    if pid is None:
        return False
    os.kill(pid, 15)
    return True


# =============================================================================
# UI
# =============================================================================

st.set_page_config(page_title="SQX Monitor", page_icon=":material/monitoring:", layout="wide")

st.title(":material/monitoring: Monitor SQX (headless)")
st.caption("Lee el `global_log` + el motor HTTP de cada proyecto. On-demand: pulsa **Actualizar**.")

# --- Sidebar: selección + actualizar ---
with st.sidebar:
    st.header("Proyecto")
    # Sin instalación de SQX (o SQX_APP mal puesto): aviso en vez de traceback (2026-10-21).
    if not REG.PROJECTS.is_dir():
        st.error(f"No encuentro la carpeta de proyectos de SQX: `{REG.PROJECTS}`. "
                 "Comprueba la instalación con `python sqx/paths.py` (o define `SQX_APP`).")
        st.stop()
    projs = sorted(p.name for p in REG.PROJECTS.iterdir() if p.is_dir())
    proj = st.selectbox("Proyecto SQX", projs, index=projs.index("Esqueleto") if "Esqueleto" in projs else 0)
    refresh = st.button(":material/refresh: Actualizar", type="primary", width="stretch")
    alive = motor_alive()
    st.caption(("🟢 Motor sqcli ACTIVO (HTTP :5050) — lectura en vivo disponible"
                if alive else "⚪ Motor sqcli inactivo — solo lectura de disco (log)"))
    st.caption("ℹ️ El log se vuelca SOLO al `TASK FINISHED`. Mientras un Build corre, mira **Estado en vivo**.")
    # Restore pendiente: un lanzamiento murió a mitad y el .cfx puede tener tasks
    # con active=false silenciosas (ver pending_restore). Bloquea el determinismo.
    bak = pending_restore(proj)
    if bak:
        st.warning("⚠ **Restore pendiente**: quedó un `.cfx.pre_run_task.bak` sin restaurar "
                   "(un lanzamiento murió a mitad). El `project.cfx` actual puede tener "
                   "tasks desactivadas en silencio.")
        if st.button(":material/settings_backup_restore: Restaurar .cfx original",
                     width="stretch"):
            RT.restore(proj, bak)
            st.rerun()
    # Stop & persist determinista: para el proyecto, persiste TODOS los databanks a disco
    # y libera el motor (docs/sqx_cli_commands.md §6.1/§6.2). Con un pipeline vivo (R6) delega en su driver y
    # espera hasta 180 s: lo normal con una task que tarda en guardar es volver con `pipeline_stopping`.
    # PLAN-DESVIO (menor): el plan solo habla de cambiar la llamada (`SP.run(proj, wait_secs=180)`); aquí el
    # botón se ve también con un pipeline vivo y el motor apagado (entre tasks, `alive` es False), si no el
    # monitor no podría parar un pipeline en ese hueco (R6).
    if alive or pipeline_up(proj):
        if st.button(":material/stop: Stop & persist", type="secondary", width="stretch",
                     help=STOP_BUTTON_HELP):
            with st.spinner("Parando build + persistiendo databanks..."):
                st.session_state["_stop_report"] = SP.run(proj, wait_secs=180)
            st.rerun()
    if st.session_state.get("_stop_report"):
        rep = st.session_state["_stop_report"]
        with st.expander("🛑 Último Stop & persist", expanded=True):
            if rep.get("probe_failed"):
                st.error(f"⛔ Stop & persist de `{rep['proj']}` NO completado: no se pudo comprobar qué hay vivo "
                         "(no se ha parado ni matado nada).")
                for w in rep.get("warnings") or []:
                    st.warning(w)
            elif rep.get("pipeline_alive"):
                # R6: delegado en el driver/orquestador. `pipeline_stopping` = el tope de espera se agotó con la
                # task en curso aún guardando: la bandera garantiza que no se lanza ninguna más.
                if rep.get("pipeline_stopped"):
                    st.success(f"✅ Pipeline de `{rep['proj']}` parado: la task en curso persistió por su driver "
                               "y no se lanzó ninguna más.")
                else:
                    st.info("⏳ parando: el pipeline saldrá al terminar de guardar la task en curso "
                            f"(`{rep['proj']}`). La bandera de parada ya está puesta (no se lanzará ninguna task "
                            "más). Vuelve a pulsar Stop & persist (o pulsa Actualizar) para ver cuándo termina.")
                for w in rep.get("warnings") or []:
                    st.warning(w)
            elif rep.get("engine_busy") or rep.get("load_unstable") or rep.get("persist_failed"):
                # No se actuó (o no se completó): motor saturado trabajando, carga sin estabilizar o un banco
                # sin guardar. Los motivos vienen en `warnings` (R1/R2).
                st.error(f"⛔ Stop & persist de `{rep['proj']}` NO completado: no se ha matado el motor.")
                for w in rep.get("warnings") or []:
                    st.warning(w)
            elif rep.get("nothing_to_stop"):
                st.info("El motor no estaba corriendo (nada que parar).")
            else:
                ok = "✅" if rep.get("stopped") else "⚠"
                st.markdown(f"{ok} **Stop graceful** de `{rep['proj']}`")
                persisted = rep.get("banks_persisted") or []
                failed = rep.get("banks_http_failed") or []
                at_risk = rep.get("banks_at_risk") or {}
                if persisted:
                    st.caption(f"✅ Persistidos: {', '.join(persisted)}")
                if at_risk:
                    st.warning("⚠ **En riesgo** — estrategias en RAM no persistibles (databanks con espacios): "
                               + ", ".join(f"`{b}` ({n})" for b, n in at_risk.items())
                               + ". Se perderán si cierras sin acción.")
                if failed:
                    st.caption(f"No persistidos por HTTP (sin count claro): {', '.join(failed)}")
                if rep.get("banks_empty"):
                    st.caption(f"Vacíos (sin guardar): {len(rep['banks_empty'])} databanks")
                rows = [{"Databank": k, ".sqx en disco": v}
                        for k, v in sorted(rep.get("databanks", {}).items(), key=lambda x: -x[1]) if v]
                if rows:
                    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                if rep.get("motor_killed"):
                    st.caption("🔌 sqcli matado.")
                else:
                    st.caption("Motor NO matado del todo (revisa `pgrep -x sqcli`).")
            # R9: "ya puedes abrir la GUI" SOLO con `safe_for_gui`; si no, los motivos (en cualquier rama).
            if "safe_for_gui" in rep:
                gui_ok, gui_txt = gui_verdict(rep)
                (st.success if gui_ok else st.warning)(gui_txt)
        if st.button("Descartar reporte", width="stretch"):
            del st.session_state["_stop_report"]
            st.rerun()

    # Ejecutar task headless (sólo si no hay motor vivo — para lanzar; si lo hay, Stop & persist)
    st.divider()
    if not alive:
        st.subheader(":material/play_arrow: Ejecutar task")
        # Guardia: carpetas de user/projects SIN project.cfx (Builder, Optimizer, CSVs
        # sueltos...) hacían crashear tasks_of con FileNotFoundError → app entera caída.
        try:
            rt_tasks = RT.tasks_of(proj)
        except (FileNotFoundError, KeyError, zipfile.BadZipFile):
            rt_tasks = []
            st.caption(f"⚠ `{proj}` no tiene `project.cfx` legible — no hay tasks que ejecutar.")
        if rt_tasks:
            # R7: "Ejecutar" queda DESHABILITADO (con el motivo) si hay orquestador o driver de este
            # proyecto vivos o algún sqcli vivo (instancia única; también un zombi o uno con el HTTP
            # ciego, que `alive` no ve). RT.run / RT.run_pipeline_per_task lo vuelven a comprobar.
            run_block = RT.launch_block_reason(proj)
            if run_block:
                st.warning(run_block)
            rt_labels = [f"{t['label']} ({t['type']})" for t in rt_tasks]
            rt_sel = st.selectbox("Task", rt_labels, key="rt_task")
            rt_mode = st.radio(
                "Modo", ["only", "from"], horizontal=True,
                format_func=lambda m: {"only": "sólo esta (no sigue)",
                                       "from": "desde esta — pipeline, motor por task"}[m],
                help="'only' = una task en un motor (aislada por construcción). "
                     "'from' = pipeline desde esta task con UN MOTOR FRESCO POR TASK "
                     "(orquestador run_pipeline_per_task.sh — evita el colapso de memoria "
                     "de 2026-09-27).")
            if st.button(":material/play_arrow: Ejecutar", type="primary", width="stretch",
                         disabled=bool(run_block),
                         help=(f"Deshabilitado: {run_block}" if run_block else
                               "No abras la GUI mientras corre. 'from' lanza el orquestador "
                               "per-task; si algo falla, aborta con el índice de reanudación.")):
                rt_idx = rt_labels.index(rt_sel)
                rt_title = rt_tasks[rt_idx]["label"]
                with st.spinner(f"Lanzando '{rt_title}' ({rt_mode})..."):
                    if rt_mode == "from":
                        # Enrutado por defecto al orquestador per-task (2026-09-27)
                        st.session_state["_run_report"] = RT.run_pipeline_per_task(proj, rt_idx)
                    else:
                        st.session_state["_run_report"] = RT.run(proj, rt_title, rt_mode)
                st.rerun()
        if st.session_state.get("_run_report"):
            rr = st.session_state["_run_report"]
            with st.expander(":material/play_arrow: Última ejecución", expanded=True):
                st.markdown(("✅ " if rr["ok"] else "⚠ ") + rr["msg"])
                if rr.get("driver_pid"):
                    st.caption(f"driver PID={rr['driver_pid']} · log={rr.get('log')}")
                if st.button("Descartar", width="stretch"):
                    del st.session_state["_run_report"]
                    st.rerun()
    st.divider()
    # Auto-muestreo: lanza/para snapshot_loop.py detached — rellena el histórico de
    # fitness sin clics (determinismo: no hace falta acordarse del "Actualizar").
    pid = loop_pid(proj)
    if pid:
        st.caption(f"📈 Histórico: {len(load_snapshots(proj))} muestras · "
                   f"🟢 auto-muestreo ACTIVO (PID {pid}, cada {LOOP_INTERVAL_S // 60} min)")
        if st.button(":material/stop_circle: Parar auto-muestreo", width="stretch"):
            stop_loop(proj)
            st.rerun()
    else:
        st.caption(f"📈 Histórico: {len(load_snapshots(proj))} muestras")
        if st.button(":material/play_circle: Auto-muestrear cada 3 min", width="stretch",
                     help="Lanza snapshot_loop.py en background: toma muestras del motor "
                          "y rellena el gráfico de evolución sin clics. Sobrevive a cerrar "
                          "esta app; se para con 'Parar auto-muestreo'."):
            start_loop(proj)
            st.rerun()
    if st.button(":material/delete: Limpiar histórico", width="stretch"):
        clear_snapshots(proj)
        st.rerun()

# --- Cargar todo (config .cfx + log + motor) ---
cfg = parse_project_config(proj)
info = project_status(proj)
ms = motor_status(proj) if alive else None
motor_ok = bool(ms) and "error" not in (ms or {})
running_secs = parse_running_time(ms.get("Running time so far", "")) if motor_ok else None

# --- Tomar snapshot para el histórico de evolución (con throttle) ---
# Banks a consultar en RAM: TODOS los del config.xml (un bank en RAM que aún no ha
# sincronizado a disco era INVISIBLE antes → fitness ausente en silencio, #4) ∪ disco.
_disk_banks = databanks_on_disk(proj)
_all_banks = list(dict.fromkeys([*SP.databanks_of(proj), *_disk_banks.keys()]))
_motor_counts = motor_databank_counts(proj, _all_banks) if alive else {}
# Histórico ANTES de guardar la muestra de este refresh: es la referencia para
# detectar el status congelado (comparar contra uno mismo sería falso positivo).
_prev_snaps = load_snapshots(proj)
if motor_ok:
    snap = take_snapshot(proj, _motor_counts)
    if snap:
        save_snapshot(proj, snap)
snaps = load_snapshots(proj)

# ¿Último task del log finalizado? (para distinguir FINISHED de IDLE)
log_tasks = info["tasks"]
last_finished = bool(log_tasks) and log_tasks[-1].finished_flag

# Task EN CURSO: último bloque del log sin TASK FINISHED (de CUALQUIER tipo: Build,
# Retest, Filtering), mapeada a su Output databank del .cfx. El log escribe el
# TASK STARTED al arrancar la task, así que esto la ve en vivo. None si no hay task
# abierta o no resuelve.
_task_outputs = task_output_databanks(proj)
_running_task = next((t for t in reversed(log_tasks) if not t.finished_flag), None)
_dest_bank = _task_outputs.get(_running_task.name) if _running_task else None

# Tiempos: "Running time so far" del motor es ACUMULADO del proyecto desde que arrancó
# el motor — NO se resetea entre tasks (verificado 2026-09-27 en el engine.log de
# gold_h4_prod_oos: siguió contando tras "Task finished"). El tiempo de la task en
# curso = reloj del proyecto − Σ duraciones de las tasks terminadas de este log.
# Las duraciones del log se truncan a minutos ("3 hrs. 12 min.") → el tiempo de task
# puede ir ~1 min de más por task terminada. Fallback sin motor: now − started.
_finished_secs = sum(parse_running_time(t.duration) or 0.0
                     for t in log_tasks if t.finished_flag)
if _running_task and running_secs is not None:
    task_secs = max(0.0, running_secs - _finished_secs)
elif _running_task and _running_task.started:
    _st = log_ts_epoch(_running_task.started)
    task_secs = max(0.0, time.time() - _st) if _st else None
else:
    task_secs = None

# --- Badge de estado del proyecto ---
# El motor NO dice "not running" al acabar: el status queda CONGELADO con los valores
# finales de la última task (experimento smoke_p2, 2026-07-19). Sin esta detección el
# badge enseñaba RUNNING para siempre tras terminar.
frozen = _is_frozen(_prev_snaps[-1] if _prev_snaps else None, running_secs,
                    _safe_int(ms.get("Strategies generated")) if motor_ok else None)
is_running = motor_ok and (running_secs or 0) > 0 and not frozen
orch_alive, orch_task = orchestrator_status(proj)
if is_running:
    badge, color = "● RUNNING", "#1a7f37"
elif orch_alive:
    # Orquestador per-task vivo con el motor parado entre tasks (reinicio fresco):
    # el proyecto SIGUE en marcha aunque el motor esté apagado.
    badge, color = "⏸ EN PROCESO · motor en pausa", "#bf8700"
elif last_finished or frozen:
    badge, color = "✓ FINISHED", "#0969da"
else:
    badge, color = "○ IDLE", "#6e7781"
st.markdown(
    f"<div style='display:inline-block;background:{color};color:white;font-weight:700;"
    f"font-size:1.15rem;padding:6px 18px;border-radius:8px;'>{badge} &nbsp;{proj}</div>",
    unsafe_allow_html=True,
)
if orch_alive:
    st.caption(
        "⏳ **Orquestador activo** (`run_pipeline_per_task.sh`): el proyecto sigue en marcha. "
        "Entre tasks el motor se cierra y se relanza fresco (~1-4 min de pausa sin motor)."
        + (f" Task en curso según el state del orquestador: **{orch_task}**." if orch_task else ""))

# --- Card de contexto del proyecto (del .cfx) ---
with st.container(border=True):
    st.markdown(f"**📋 Config del proyecto** · `{proj}`")
    ctx_cols = st.columns(6)
    pairs = [
        ("Símbolo", cfg.get("symbol", "—")),
        ("Timeframe", cfg.get("timeframe", "—")),
        ("Motor", cfg.get("motor", "—")),
        ("Lados", cfg.get("sides", "—")),
        ("Max strat.", f"{cfg.get('max_strategies', '—'):,}" if isinstance(cfg.get("max_strategies"), int) else "—"),
        ("Coste", "Cost-blind (None)" if cfg.get("commissions") == "None"
                 else (cfg.get("commissions") or "—")),
    ]
    for col, (label, val) in zip(ctx_cols, pairs):
        col.metric(label, val)
    # StopCondition legible
    sc_desc = "—"
    stp = cfg.get("stop_type")
    if stp == "time-limit":
        h = int(cfg.get("stop_hours", 0) or 0); mi = int(cfg.get("stop_minutes", 0) or 0); d = int(cfg.get("stop_days", 0) or 0)
        parts = [f"{d}d" for _ in range(1) if d] + [f"{h}h" for _ in range(1) if h] + [f"{mi}m" for _ in range(1) if mi]
        sc_desc = f"⏱ time-limit {''.join(parts) or '?'}"
    elif stp == "passed-count":
        sc_desc = f"🎯 passed-count {cfg.get('stop_passedStrategies', '?')}"
    elif stp:
        sc_desc = f"stop={stp}"
    st.caption(f"StopCondition: **{sc_desc}**")

# --- What-to-build resumen (Build tasks del .cfx) ---
with st.container(border=True):
    st.markdown("**🛠️ What-to-build** · configuración actual de los Build tasks")
    wtb_rows = what_to_build_summary(proj)
    if wtb_rows:
        st.dataframe(pd.DataFrame(wtb_rows), hide_index=True, width="stretch")
        st.caption("Se lee del `.cfx` directamente: recoge los cambios que guardes al cerrar SQX.")
    else:
        st.caption("No se encontraron Build tasks o no se pudo leer el `.cfx`.")

# --- Estado en vivo del motor (si sqcli está corriendo) ---
if motor_ok:
    st.subheader(":material/speed: Estado en vivo (motor)")

    # Task en curso (del log: último TASK STARTED sin FINISHED) + su databank destino
    if is_running and _running_task:
        st.markdown(f"**▶ Task en curso:** `{_running_task.name}` · tipo **{_running_task.type}**"
                    + (f" · salida → databank **{_dest_bank}**" if _dest_bank else ""))

    # Barra de progreso vs StopCondition (si time-limit). Con task conocida usa SU
    # límite y SU reloj (task_secs); el fallback (cfg = primer Build task del .cfx +
    # reloj de proyecto) cubre el arranque, cuando el log aún no ha volcado la task.
    _sc = task_stop_condition(proj, _running_task.name) if (is_running and _running_task) else {}
    if _sc.get("type") == "time-limit" and _sc.get("secs") and task_secs is not None:
        target_secs = _sc["secs"]
        frac = min(task_secs / target_secs, 1.0)
        pc = st.columns([4, 1])
        pc[0].progress(frac, text=f"task: {task_secs/60:.1f} min / {target_secs/60:.0f} min  ({frac*100:.0f}%)")
        pc[1].metric("Quedan (task)", f"~{(target_secs-task_secs)/60:.0f} min")
    elif cfg.get("stop_type") == "time-limit" and running_secs is not None:
        target_h = int(cfg.get("stop_hours", 0) or 0)
        target_m = int(cfg.get("stop_minutes", 0) or 0)
        target_d = int(cfg.get("stop_days", 0) or 0)
        target_secs = target_d * 86400 + target_h * 3600 + target_m * 60
        if target_secs > 0:
            frac = min(running_secs / target_secs, 1.0)
            pc = st.columns([4, 1])
            pc[0].progress(frac, text=f"{running_secs/60:.1f} min / {target_secs/60:.0f} min  ({frac*100:.0f}%)")
            pc[1].metric("Quedan", f"~{(target_secs-running_secs)/60:.0f} min")

    def _g(key):
        v = ms.get(key)
        return v if v not in (None, "") else "—"
    # Accepted % con color (red<2% orange2-5% green>5%); Streamlit solo soporta nombres
    def _accepted_badge(val_str):
        try:
            v = float(val_str.replace("%", "").strip())
        except (ValueError, AttributeError):
            return val_str
        color = "red" if v < 2 else ("orange" if v < 5 else "green")
        return f":{color}[{val_str}]"
    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Generated", _g("Strategies generated"))
    k2.metric("In databank (total)", _g("In databank"),
              help="Suma de TODOS los databanks. El de la task en curso está en la línea ▶ de abajo.")
    k3.markdown("**Accepted %**")
    k3.markdown(_accepted_badge(_g("Accepted")))
    k4.metric("Rejected %", _g("Rejected"))
    k5.metric("⏱ Task actual", fmt_duration(task_secs) if (is_running and _running_task) else "—",
              help=(f"Task: {_running_task.name}. " if _running_task else "") +
                   "Reloj del proyecto − Σ duraciones de las tasks terminadas. El log "
                   "trunca las duraciones a minutos → puede ir ~1 min de más por task.")
    k6.metric("⏱ Proyecto total", _g("Running time so far"),
              help="Reloj ACUMULADO del motor desde que arrancó esta corrida: no se "
                   "resetea entre tasks (verificado 2026-09-27).")
    extra_cols = [c for c in ("Strategies per hour", "Accepted strategies per hour",
                              "Passed", "Failed", "Time per strategy")
                  if c in ms]
    if extra_cols:
        extras = st.columns(len(extra_cols))
        for col, c in zip(extras, extra_cols):
            col.metric(c, ms[c])
    # Desglose por databank (RAM) + total guardado en el databank de salida de la
    # task en curso. "In databank" del status es la SUMA de todos los banks; lo que
    # importa durante una task es cuántas han entrado en SU databank destino.
    _filled = {b: n for b, n in _motor_counts.items() if n is not None}
    _parts = " · ".join(f"**{b}**: {n:,}" for b, n in
                        sorted(_filled.items(), key=lambda x: -x[1]))
    if is_running and _running_task and _dest_bank:
        _n_ram = _motor_counts.get(_dest_bank)
        if _n_ram is not None:
            _cnt = f"**{_n_ram:,}** estrategias guardadas (RAM)"
        elif _dest_bank in _disk_banks:
            # Bancos con espacios ("Second OOS") no siempre responden al count HTTP
            # (mina del %20); el disco es el fallback (ficheros .sqx ≠ estrategias:
            # un .sqx puede llevar varias).
            _cnt = f"**{_disk_banks[_dest_bank]:,}** ficheros .sqx en disco (el motor no contó el banco)"
        else:
            _cnt = "sin conteo disponible (ni RAM ni disco)"
        st.success(f"▶ **{_running_task.name}** ({_running_task.type}) → databank **{_dest_bank}**: "
                   f"{_cnt}" + (f" · desglose: {_parts}" if _parts else ""))
    elif is_running and _running_task:
        st.info(f"▶ **{_running_task.name}** ({_running_task.type}) en curso — "
                "databank de salida no resuelto en el `.cfx`.")
    elif _parts:
        st.caption(f"Desglose por databank (RAM): {_parts}")
    st.caption("↻ Pulsa **Actualizar** para refrescar. Datos directos del motor, no del log.")
elif alive:
    st.warning(f"Motor activo pero no devuelve status de `{proj}` (¿corre otro proyecto? ¿calentando?).")
st.divider()

# --- Gráfico de evolución del fitness (histórico de snapshots) ---
# Segmentado por corrida: sin esto, corridas distintas del mismo proyecto se "cosían"
# sobre el mismo eje de minutos y falseaban la señal (ver assign_runs).
all_fit = [s for s in assign_runs(snaps) if s.get("fit_median") is not None and s.get("running_secs")]
runs = sorted({s["run"] for s in all_fit})
cur_run = runs[-1] if runs else 0
fit_snaps = [s for s in all_fit if s["run"] == cur_run]      # corrida actual (la última)
prev_snaps = [s for s in all_fit if s["run"] < cur_run]      # corridas anteriores
if len(fit_snaps) >= 2:
    st.subheader(":material/show_chart: Evolución del fitness del databank")
    df_evo = pd.DataFrame(fit_snaps)
    df_evo["min"] = df_evo["running_secs"] / 60

    cols_evo = st.columns([3, 2])
    with cols_evo[0]:
        # Fitness: banda IQR (p25–p75) + líneas p50 / p90 / máx.
        # La mediana y el rango intercuartílico describen el GRUESO del databank
        # (robustos a outliers); la media de ~1500 estrategias la arrastran los extremos.
        melt = df_evo.melt("min", value_vars=["fit_median", "fit_p90", "fit_max"],
                           var_name="Métrica", value_name="Fitness")
        rename = {"fit_median": "p50 (mediana)", "fit_p90": "p90", "fit_max": "máx"}
        melt["Métrica"] = melt["Métrica"].map(rename)
        lines = (alt.Chart(melt).mark_line(point=True, strokeWidth=2).encode(
            x=alt.X("min:Q", title="Minutos de corrida"),
            y=alt.Y("Fitness:Q", scale=alt.Scale(zero=False), title="Fitness (IS)"),
            color=alt.Color("Métrica:N", legend=alt.Legend(orient="top")),
            tooltip=["min:Q", "Métrica:N", "Fitness:Q"],
        ))
        # Banda IQR: sólo muestras con p25/p75 (los snapshots antiguos no los tienen;
        # si NINGUNO los tiene, la columna ni existe → rellenar a NaN para no romper)
        for c in ("fit_q1", "fit_q3"):
            if c not in df_evo:
                df_evo[c] = float("nan")
        df_band = df_evo.dropna(subset=["fit_q1", "fit_q3"])
        c_fit = lines
        if len(df_band) >= 2:
            band = (alt.Chart(df_band).mark_area(opacity=0.18, color="#1a7f37").encode(
                x=alt.X("min:Q", title="Minutos de corrida"),
                y=alt.Y("fit_q1:Q", title="Fitness (IS)"),
                y2=alt.Y2("fit_q3:Q"),
                tooltip=["min:Q", "fit_q1:Q", "fit_q3:Q"],
            ))
            c_fit = band + lines
        # Corridas anteriores como contexto (p50 en gris, una línea por run)
        if prev_snaps and st.checkbox("Ver corridas anteriores (p50, gris)", value=False):
            df_old = pd.DataFrame(prev_snaps)
            df_old["min"] = df_old["running_secs"] / 60
            old_lines = (alt.Chart(df_old).mark_line(color="#bbbbbb", strokeDash=[3, 3],
                                                     strokeWidth=1).encode(
                x=alt.X("min:Q", title="Minutos de corrida"),
                y=alt.Y("fit_median:Q", title="Fitness (IS)"),
                detail="run:N",
                tooltip=["min:Q", "run:N", "fit_median:Q"],
            ))
            c_fit = old_lines + c_fit
        st.altair_chart(c_fit.properties(height=280), width="stretch")
        st.caption("Banda = **IQR (p25–p75)**: dónde vive el 50% central del databank. "
                   "Banda que **sube y se estrecha** → el genético mejora el grueso y converge. "
                   "**p50 aplanada** → rendimientos decrecientes (señal de parar).")
    with cols_evo[1]:
        # In databank + Accepted% (eje dual implícito: 2 charts solapados no va; usamos Accept/hora)
        melt2 = df_evo.melt("min", value_vars=["accepted_per_hour"],
                            var_name="Métrica", value_name="Valor")
        melt2["Métrica"] = "Accept/hora"
        c_acc = (alt.Chart(melt2).mark_bar(opacity=0.5, color="#0969da").encode(
            x=alt.X("min:Q", title="Minutos"),
            y=alt.Y("Valor:Q", title="Estrategias aceptadas/hora"),
            tooltip=["min:Q", "Valor:Q"],
        ).properties(height=280))
        st.altair_chart(c_acc, width="stretch")
        st.caption("Accept/hora = cuántas entran por unidad de tiempo. Cae = el bar sube (databank selectivo).")
    # Detector de plateau: pendiente relativa de la p50 en las últimas k muestras
    # DE ESTA corrida. <1%/h → el genético ya no mejora el grueso → candidato a parar.
    trend = fitness_trend(fit_snaps)
    if trend:
        pct_h = trend["rel_h"] * 100
        if trend["rel_h"] < PLATEAU_REL_PER_H:
            st.warning(f"⚠ **Plateau**: la p50 apenas mejora ({pct_h:+.2f}%/h en las últimas "
                       f"{trend['n']} muestras). Candidato a **Stop & persist**.")
        else:
            st.caption(f"📈 p50 mejorando al {pct_h:+.2f}%/h (últimas {trend['n']} muestras "
                       "de esta corrida).")
    last_fit = fit_snaps[-1]
    iqr_txt = (f" IQR=[{last_fit.get('fit_q1')}, {last_fit.get('fit_q3')}]"
               if last_fit.get("fit_q1") is not None else "")
    runs_txt = (f" · corrida {cur_run + 1}/{len(runs)}" if len(runs) > 1 else "")
    st.caption(f"Dataset: {len(df_evo)} muestras{runs_txt} · databank seguido: "
               f"`{last_fit.get('active_bank', '?')}` · última: "
               f"p50={last_fit.get('fit_median')}{iqr_txt} / máx={last_fit.get('fit_max')}. "
               "Pulsa **Actualizar** periódicamente para añadir muestras.")
    st.divider()
elif motor_ok:
    st.caption(f"📈 Aún no hay muestras de fitness suficientes en la corrida actual "
               f"({len(fit_snaps)}/2). Pulsa **Actualizar** durante la corrida (o activa "
               "el **auto-muestreo** del sidebar para medir cada 3 min sin clics).")
    if LAST_FIT_ERROR:
        st.warning(f"⚠ El export de fitness está fallando: `{LAST_FIT_ERROR}`")

# Si no hay log
if info["log"] is None:
    st.warning(f"No hay carpeta `log/` ni `global_log_*.log` en `{proj}`.")
    st.stop()
if info["log_empty"]:
    if alive:
        st.info(f"Log todavía vacío ({info['log_size']} bytes) — el task en curso no ha hecho `TASK FINISHED`. "
                "Mira **Estado en vivo** arriba. El desglose se vuelca SOLO al finalizar.")
        # Databanks en disco sí pueden verse
        if _disk_banks:
            st.subheader(":material/database: Databanks en disco")
            st.dataframe(pd.DataFrame([{"Databank": k, ".sqx": v} for k, v in _disk_banks.items()]),
                         hide_index=True, width="stretch")
    else:
        st.error(f"Log VACÍO ({info['log_size']} bytes) y motor inactivo. El proyecto NO llegó a "
                 "TASK FINISHED (¿matado/parado a media?). El desglose se vuelca SOLO al finalizar.")
        st.code(info["log"].read_text("utf-8", "replace") or "(vacío)")
    st.stop()

tasks = info["tasks"]
log_name = info["log"].name

# --- Estado global ---
n_total = len(tasks)
n_done = sum(1 for t in tasks if t.finished_flag)
last = tasks[-1] if tasks else None

c1, c2, c3, c4 = st.columns(4)
with c1:
    st.metric("Tasks en el log", f"{n_done}/{n_total}")
with c2:
    st.metric("Último task", last.type if last else "—")
with c3:
    st.metric("Log", log_name)
with c4:
    st.metric("Generadas (último Build)",
              f"{(last.generated or 0):,}" if last and last.type == "Build" and last.generated else "—")

st.divider()

# --- Timeline de tasks ---
st.subheader(":material/timeline: Timeline de tasks")
rows = []
for i, t in enumerate(tasks, 1):
    estado = "✅" if t.finished_flag else "🔄"
    # Output clave según tipo de task
    if t.type == "Build":
        out = f"gen {t.generated:,} → acc {t.accepted:,}" if t.generated is not None else "(en curso)"
    elif t.type == "Retest":
        out = (f"tested {t.total_tested:,} → passed {t.passed:,}"
               if t.total_tested is not None else "(en curso)")
    elif t.type == "Filtering":
        out = f"moved {t.moved:,}" if t.moved is not None else "(en curso)"
    else:
        out = ""
    rows.append({
        "#": i, "Estado": estado, "Task": t.name, "Tipo": t.type,
        "Duración": t.duration or "—",
        "Resultado": out,
        "Strat/h": f"{t.strat_per_hour:,.0f}" if t.strat_per_hour else "—",
    })
st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

# --- Databanks ---
st.subheader(":material/database: Databanks")
disk = _disk_banks
# Última foto del log (banks_after del último task finalizado)
banks_log = {}
for t in reversed(tasks):
    if t.banks_after:
        banks_log = t.banks_after
        break
# Counts en vivo del motor (ya calculados arriba como _motor_counts)
motor_counts = _motor_counts

if disk:
    # Tabla unificada: databank | en motor (RAM) | en disco | foto log (todo str → sin ArrowTypeError)
    def _fmt(v):
        return str(v) if v not in (None, "") else "—"
    rows = []
    for name in dict.fromkeys([*disk.keys(), *motor_counts.keys()]):  # ∪ disco y RAM (un bank solo en RAM era invisible)
        n_disk = disk.get(name)
        rows.append({
            "Databank": (f"▶ {name}") if (is_running and name == _dest_bank) else name,
            "En motor (RAM)": _fmt(motor_counts.get(name)) if alive else "—",
            "En disco": str(n_disk) if n_disk is not None else "—",
            "Foto log": _fmt(banks_log.get(name)),
        })
    df_db = pd.DataFrame(rows)
    # Ordenar: los que tienen algo en motor o disco arriba
    df_db["_sort"] = df_db[["En motor (RAM)", "En disco"]].apply(
        lambda r: sum(int(x) for x in r if x not in ("—", "")), axis=1)
    df_db = df_db.sort_values("_sort", ascending=False).drop(columns="_sort")
    st.dataframe(df_db, width="stretch", hide_index=True)
    st.caption("🟢 **En motor** = cuenta viva vía HTTP (lo que realmente hay ahora). "
               "💾 **En disco** = `.sqx` escritos (puede ir retrasado según el `syncType`). "
               "📄 **Foto log** = banks_after del último task finalizado. "
               "▶ = databank destino de la task en curso.")
else:
    st.info("Sin databanks en disco para este proyecto.")

st.divider()

# --- Breakdown de rechazos (acumulado del último Build) ---
builds = [t for t in tasks if t.type == "Build" and t.rejected_details]
if builds:
    st.subheader(":material/filter_alt: Breakdown de rechazos (último Build)")
    last_build = builds[-1]
    rd = last_build.rejected_details
    if rd:
        df_rd = pd.DataFrame(
            [{"Motivo": r, "Count": c, "% all": p, "Etapa": SRB.stage_of(r)}
             for r, c, p in sorted(rd, key=lambda x: -x[2])]
        )
        st.dataframe(df_rd, width="stretch", hide_index=True)
        st.caption(f"Build: **{last_build.name}** — generado {last_build.generated:,}, "
                   f"aceptado {last_build.accepted:,}.")

# --- Log crudo ---
with st.expander(":material/description: Log crudo (completo)", expanded=False):
    st.code(info["raw"], language="text")
