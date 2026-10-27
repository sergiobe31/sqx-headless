"""Tests de la lógica pura del monitor SQX headless (sqx/sqx_monitor.py).

Cubre las piezas sin UI:
  - assign_runs: segmentación del histórico por corrida (caída de running_secs).
  - fitness_trend: detector de plateau (pendiente relativa de la p50).
  - pending_restore: backup .cfx.pre_run_task.bak huérfano.
  - export_fitness_stats: cuartiles del CSV exportado del databank (HTTP mockeado).
  - loop_pid / save_snapshot: auto-muestreo y throttle de snapshots.

El módulo ejecuta UI Streamlit a nivel de import: se importa con `st.stop` parcheado
para que lance _StopApp (en bare mode NO lanza y la ejecución seguiría hasta un
AttributeError). Las funciones puras quedan definidas aunque la UI se corte.

Correr desde la raíz del repo:
    ~/miniconda3/envs/robotrader/bin/python -m pytest tests/sqx/test_sqx_monitor.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import time
import urllib.request
from pathlib import Path
from unittest import mock
from uuid import uuid4

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx
    sys.path.append(str(ROOT / "tests"))

import streamlit as _st
from test_slim_hooks import (  # noqa: E402  infraestructura de procesos falsos
    fake_launcher, make_proj, procs, unique_engine_name,
)


@pytest.fixture(autouse=True)
def _nunca_el_sqcli_real(monkeypatch, tmp_path):
    """Seguridad de plataforma (incidente de la Fase 1, 2026-10-21: un test arrancó el sqcli REAL de SQX): `SQCLI` y
    `SQX_APP` a rutas inexistentes y un nombre de motor ÚNICO por test. Ningún test de este módulo puede ver, arrancar
    ni matar un sqcli real (los tests que necesitan un motor falso lo fabrican con su propio nombre)."""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", f"sqcli_{uuid4().hex[:8]}")


class _StopApp(Exception):
    """st.stop() real corta el script; en bare mode no lanza → lo simulamos."""


def _raise_stop(*_a, **_k):
    raise _StopApp()


_st.stop = _raise_stop

_spec = importlib.util.spec_from_file_location(
    "sqx.sqx_monitor", ROOT / "sqx" / "sqx_monitor.py")
M = importlib.util.module_from_spec(_spec)
sys.modules["sqx.sqx_monitor"] = M
try:
    _spec.loader.exec_module(M)
except _StopApp:
    pass  # UI cortada en el primer st.stop(); las funciones puras ya están definidas


# ---------------------------------------------------------------------------
# assign_runs: una caída del reloj del motor = corrida nueva
# ---------------------------------------------------------------------------

def test_assign_runs_splits_on_clock_reset():
    snaps = [{"running_secs": 600}, {"running_secs": 1200},      # corrida 0
             {"running_secs": 100}, {"running_secs": 400}]       # corrida 1 (reloj reseteado)
    out = M.assign_runs(snaps)
    assert [s["run"] for s in out] == [0, 0, 1, 1]


def test_assign_runs_jitter_does_not_split():
    # Caídas menores que RUN_RESET_TOL_S son jitter del status, no un run nuevo.
    snaps = [{"running_secs": 1000}, {"running_secs": 1000 - M.RUN_RESET_TOL_S + 5}]
    out = M.assign_runs(snaps)
    assert [s["run"] for s in out] == [0, 0]


def test_assign_runs_missing_clock_does_not_split_nor_crash():
    snaps = [{"running_secs": 600}, {}, {"running_secs": 900}]
    out = M.assign_runs(snaps)
    assert [s["run"] for s in out] == [0, 0, 0]


def test_assign_runs_does_not_mutate_input():
    snaps = [{"running_secs": 600}]
    out = M.assign_runs(snaps)
    assert "run" not in snaps[0] and out[0]["run"] == 0


# ---------------------------------------------------------------------------
# fitness_trend: plateau = p50 que apenas mejora (%/h relativo)
# ---------------------------------------------------------------------------

def _snaps(ys, step_min=10):
    return [{"running_secs": (i + 1) * step_min * 60, "fit_median": y}
            for i, y in enumerate(ys)]


def test_fitness_trend_none_with_few_points():
    assert M.fitness_trend(_snaps([1.0, 1.1])) is None
    assert M.fitness_trend([]) is None


def test_fitness_trend_flat_is_plateau():
    t = M.fitness_trend(_snaps([1.0, 1.0, 1.01, 1.0, 1.005]))
    assert t is not None and abs(t["rel_h"]) < M.PLATEAU_REL_PER_H


def test_fitness_trend_growing_is_not_plateau():
    # p50 pasa de 1.0 a 2.0 en 40 min → +150%/h, muy lejos del plateau.
    t = M.fitness_trend(_snaps([1.0, 1.25, 1.5, 1.75, 2.0]))
    assert t is not None
    assert t["rel_h"] > M.PLATEAU_REL_PER_H
    assert t["slope_h"] > 0
    assert t["n"] == 5


def test_fitness_trend_uses_only_last_k():
    # Las 2 primeras muestras crecen mucho; las últimas 5 están planas → plateau.
    snaps = _snaps([1.0, 5.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    t = M.fitness_trend(snaps, k=5)
    assert t is not None and abs(t["rel_h"]) < M.PLATEAU_REL_PER_H


# ---------------------------------------------------------------------------
# pending_restore: .cfx.pre_run_task.bak huérfano
# ---------------------------------------------------------------------------

def test_pending_restore_detects_orphan_bak(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    monkeypatch.setattr(M.REG, "PROJECTS", tmp_path)
    assert M.pending_restore("proj_x") is None
    bak = proj_dir / "project.cfx.pre_run_task.bak"
    bak.write_bytes(b"zip")
    assert M.pending_restore("proj_x") == bak


# ---------------------------------------------------------------------------
# export_fitness_stats: cuartiles del CSV del databank (HTTP mockeado)
# ---------------------------------------------------------------------------

class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"ok"


def test_export_fitness_stats_quartiles():
    csv = "Name;Fitness (IS);Net Profit\n" + "\n".join(
        f"s{i};{str(round(1.0 + i * 0.01, 2)).replace('.', ',')};{i}" for i in range(100))
    with mock.patch("urllib.request.urlopen", return_value=_Resp()), \
         mock.patch("pandas.read_csv", return_value=pd.read_csv(io.StringIO(csv), sep=";")):
        stats = M.export_fitness_stats("proj", "bank")
    assert stats["fit_q1"] < stats["fit_median"] < stats["fit_q3"]
    assert stats["fit_q3"] < stats["fit_p90"] <= stats["fit_max"]


def test_export_fitness_stats_missing_column_returns_empty():
    csv = "Name;Net Profit\ns1;10\ns2;20\n"
    with mock.patch("urllib.request.urlopen", return_value=_Resp()), \
         mock.patch("pandas.read_csv", return_value=pd.read_csv(io.StringIO(csv), sep=";")):
        assert M.export_fitness_stats("proj", "bank") == {}


def test_export_fitness_stats_http_error_returns_empty():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("motor caído")):
        assert M.export_fitness_stats("proj", "bank") == {}


# ---------------------------------------------------------------------------
# Auto-muestreo y snapshots: loop_pid + throttle de save_snapshot
# ---------------------------------------------------------------------------

def test_loop_pid_none_when_no_loop():
    assert M.loop_pid("proyecto_que_no_existe_xyz_123") is None


def test_save_snapshot_throttles_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "CACHE_DIR", tmp_path)
    s1 = {"ts": time.time(), "running_secs": 600, "fit_median": 1.0}
    snaps = M.save_snapshot("proj_t", s1)
    assert len(snaps) == 1
    # Segundo save inmediato (< SNAP_THROTTLE_S) → ignorado
    s2 = {"ts": time.time(), "running_secs": 700, "fit_median": 1.1}
    assert len(M.save_snapshot("proj_t", s2)) == 1
    # En disco hay exactamente 1 muestra
    on_disk = json.loads((tmp_path / "proj_t_snap.json").read_text())
    assert len(on_disk) == 1


# ---------------------------------------------------------------------------
# #4 pick_active_bank: el bank que CRECE, no el más poblado
# ---------------------------------------------------------------------------

def test_pick_active_bank_growth_beats_size():
    counts = {"Results": 1500, "Filtering": 120}   # Results lleno (task previa), Filtering llenándose
    prev = {"bank_counts": {"Results": 1500, "Filtering": 60}}
    assert M.pick_active_bank(counts, prev) == "Filtering"


def test_pick_active_bank_fallback_to_biggest():
    counts = {"Results": 1500, "Filtering": 120}
    assert M.pick_active_bank(counts, None) == "Results"
    # Sin crecimiento (todo estático) → el más poblado
    prev = {"bank_counts": {"Results": 1500, "Filtering": 120}}
    assert M.pick_active_bank(counts, prev) == "Results"


def test_pick_active_bank_ignores_empty_and_zero():
    assert M.pick_active_bank({}, None) is None
    assert M.pick_active_bank({"A": 0, "B": None}, None) is None


# ---------------------------------------------------------------------------
# #6 LAST_FIT_ERROR: los fallos del export dejan motivo visible
# ---------------------------------------------------------------------------

def test_export_fitness_stats_error_visible_on_http_failure():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("motor caído")):
        assert M.export_fitness_stats("proj", "bank") == {}
    assert "motor caído" in M.LAST_FIT_ERROR


def test_export_fitness_stats_error_cleared_on_success():
    csv = "Name;Fitness (IS)\ns1;1,5\ns2;2,5\n"
    with mock.patch("urllib.request.urlopen", return_value=_Resp()), \
         mock.patch("pandas.read_csv", return_value=pd.read_csv(io.StringIO(csv), sep=";")):
        assert M.export_fitness_stats("proj", "bank")["fit_median"] == 2.0
    assert M.LAST_FIT_ERROR is None


# ---------------------------------------------------------------------------
# #7 _norm_num / _read_databank_csv: parsing robusto del CSV del databank
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1,5", 1.5),            # decimal europeo
    ("1.5", 1.5),            # decimal US
    ("1.234,56", 1234.56),   # miles EU + decimal ','
    ("1,234.56", 1234.56),   # miles US + decimal '.'
    ("1 234,5", 1234.5),     # miles con espacio (francés)
    ("1 234.5", 1234.5),    # miles con nbsp
    ("-0,75", -0.75),
])
def test_norm_num_locales(raw, expected):
    assert M._norm_num(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", None, "1.2.3,4,5"])
def test_norm_num_invalid_returns_none(raw):
    assert M._norm_num(raw) is None


def test_read_databank_csv_sniffs_delimiter(tmp_path):
    f = tmp_path / "db.csv"
    f.write_text("Name,Fitness (IS)\ns1,1.5\ns2,2.5\n")   # separador coma
    df = M._read_databank_csv(str(f))
    assert list(df.columns) == ["Name", "Fitness (IS)"]
    f.write_text("Name;Fitness (IS)\ns1;1,5\ns2;2,5\n")    # separador punto y coma
    df = M._read_databank_csv(str(f))
    assert list(df.columns) == ["Name", "Fitness (IS)"]


# ---------------------------------------------------------------------------
# _is_frozen: status del motor congelado tras terminar (experimento smoke_p2)
# ---------------------------------------------------------------------------

def test_is_frozen_same_values_is_frozen():
    prev = {"running_secs": 182.0, "generated": 61069}
    assert M._is_frozen(prev, 182.0, 61069) is True


def test_is_frozen_progress_is_not_frozen():
    prev = {"running_secs": 161.0, "generated": 54550}
    assert M._is_frozen(prev, 182.0, 61069) is False
    # mismo reloj pero generated cambió (p.ej. lecturas en el mismo segundo) → corre
    assert M._is_frozen({"running_secs": 182.0, "generated": 100}, 182.0, 200) is False


def test_is_frozen_no_prev_or_none_values():
    assert M._is_frozen(None, 182.0, 61069) is False
    assert M._is_frozen({"running_secs": None, "generated": None}, None, None) is False



# ---------------------------------------------------------------------------
# task_output_databanks / task_stop_condition: cruce .cfx de CUALQUIER tipo
# de task (Build/Retest/Filtering) → databank destino y StopCondition
# ---------------------------------------------------------------------------

def _make_cfx(proj_dir):
    import zipfile
    config = (
        '<root>'
        '<Task type="Build" name="Build strategies" title="Build Long TF" '
        'taskXMLFile="Build-Task1.xml" active="true"/>'
        '<Task type="Retest" name="Retest strategies 5" title="Retest MC Rand" '
        'taskXMLFile="Retest-Task5.xml" active="true"/>'
        '</root>')
    build = ('<Task><Setup><StopCondition type="time-limit" days="0" hours="8" '
             'minutes="30" passedStrategies="0"/></Setup>'
             '<Databank label="Output databank" value="Long_TF"/></Task>')
    retest = ('<Task><Setup><StopCondition type="passed-count" passedStrategies="200"/></Setup>'
              '<Databank label="Output databank" value="MC_Randomize_parameters"/></Task>')
    with zipfile.ZipFile(proj_dir / "project.cfx", "w") as z:
        z.writestr("config.xml", config)
        z.writestr("Build-Task1.xml", build)
        z.writestr("Retest-Task5.xml", retest)


def test_task_output_databanks_covers_build_and_retest(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    _make_cfx(proj_dir)
    monkeypatch.setattr(M.REG, "PROJECTS", tmp_path)
    outs = M.task_output_databanks("proj_x")
    # indexado por title Y por name (el log usa ambos según la task)
    assert outs["Build Long TF"] == "Long_TF"
    assert outs["Build strategies"] == "Long_TF"
    assert outs["Retest MC Rand"] == "MC_Randomize_parameters"
    assert outs["Retest strategies 5"] == "MC_Randomize_parameters"


def test_task_output_databanks_missing_cfx_returns_empty(tmp_path, monkeypatch):
    (tmp_path / "proj_x").mkdir()
    monkeypatch.setattr(M.REG, "PROJECTS", tmp_path)
    assert M.task_output_databanks("proj_x") == {}


def test_task_stop_condition_time_limit(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    _make_cfx(proj_dir)
    monkeypatch.setattr(M.REG, "PROJECTS", tmp_path)
    sc = M.task_stop_condition("proj_x", "Build Long TF")
    assert sc == {"type": "time-limit", "secs": 8 * 3600 + 30 * 60}


def test_task_stop_condition_passed_count_has_no_secs(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    _make_cfx(proj_dir)
    monkeypatch.setattr(M.REG, "PROJECTS", tmp_path)
    sc = M.task_stop_condition("proj_x", "Retest strategies 5")
    assert sc == {"type": "passed-count", "secs": None}
    assert M.task_stop_condition("proj_x", "task que no existe") == {}


# ---------------------------------------------------------------------------
# fmt_duration / log_ts_epoch: tiempos de task vs proyecto en Estado en vivo
# ---------------------------------------------------------------------------

def test_fmt_duration_scales():
    assert M.fmt_duration(None) == "—"
    assert M.fmt_duration(74) == "1 min 14 s"
    assert M.fmt_duration(7545) == "2 h 5 min"          # sin segundos con horas
    assert M.fmt_duration(90061) == "1 d 1 h 1 min"
    assert M.fmt_duration(0) == "0 min 0 s"


def test_log_ts_epoch_parses_local_log_timestamps():
    ep = M.log_ts_epoch("2026.09.27 14:41:44.580")
    assert ep is not None
    assert M.log_ts_epoch("2026.09.27 14:41:44") == pytest.approx(ep - 0.58)
    assert M.log_ts_epoch("basura") is None
    assert M.log_ts_epoch("") is None


# ---------------------------------------------------------------------------
# orchestrator_status (R7, guardrail sqx-headless-fallos Fase 1): orquestador per-task vivo con motor
# parado entre tasks. Detección por regex ANCLADO (SP.orchestrator_pids) y task en curso desde
# <OUTBASE>/<proj>_state.json (lo escribe el orquestador; el log cascada_orch*.log no lo escribía nadie).
# ---------------------------------------------------------------------------

def _state(tmp_path, proj, tasks):
    """state.json sintético: tasks = {"idx:label": status}."""
    import json
    (tmp_path / f"{proj}_state.json").write_text(json.dumps({
        "project": proj, "tasks": {k: {"status": v, "cause": None} for k, v in tasks.items()}}))


def test_orchestrator_status_dead_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path))
    _state(tmp_path, "proj_x", {"4:MC_Execution_Realism": "running"})
    # sin orquestador vivo la task "running" del state es un resto: no se muestra
    monkeypatch.setattr(M.SP, "orchestrator_pids", lambda proj=None: [])
    assert M.orchestrator_status("proj_x") == (False, None)


def test_orchestrator_status_alive_reads_task_from_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path))
    _state(tmp_path, "proj_x", {"3:High_Precission": "done", "4:Real OOS": "running",
                                "5:MC_Execution_Realism": "pending"})
    monkeypatch.setattr(M.SP, "orchestrator_pids", lambda proj=None: [12345])
    alive, task = M.orchestrator_status("proj_x")
    assert alive is True and task == "4 Real OOS"           # "idx label" (la etiqueta puede llevar espacios)


def test_orchestrator_status_alive_sin_state_o_sin_running(tmp_path, monkeypatch):
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path))
    monkeypatch.setattr(M.SP, "orchestrator_pids", lambda proj=None: [1])
    assert M.orchestrator_status("proj_sin_state") == (True, None)           # state inexistente
    _state(tmp_path, "proj_x", {"0:T": "done"})
    assert M.orchestrator_status("proj_x") == (True, None)                   # ninguna en running
    (tmp_path / "proj_x_state.json").write_text("{no es json")
    assert M.orchestrator_status("proj_x") == (True, None)                   # corrupto: no rompe el monitor


def test_running_task_from_state_varias_gana_el_indice_mayor(tmp_path, monkeypatch):
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path))
    _state(tmp_path, "p", {"2:A": "running", "10:B": "running", "1:C": "running"})
    assert M.running_task_from_state("p") == "10 B"


def test_outbase_respeta_sqx_outdir_y_por_defecto_sqx_output(tmp_path, monkeypatch):
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path / "o"))
    assert M.outbase() == tmp_path / "o"
    monkeypatch.delenv("SQX_OUTDIR")
    assert M.outbase() == (ROOT / "sqx" / "output")


def test_orchestrator_status_con_orquestador_real_autocopiado(tmp_path, monkeypatch):
    """Cmdline REAL del orquestador entre tasks: `bash /tmp/<proj>_pertask_XXXXXX <proj> 0` (sin el caffeinate
    delante). El patrón ancho de antes NO lo veía; el regex anclado sí."""
    import os
    import signal
    import subprocess
    import uuid
    proj = f"mon{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path))
    _state(tmp_path, proj, {"7:Retest_X": "running"})
    script = tmp_path / f"{proj}_pertask_AbC123"
    script.write_text("#!/bin/bash\nsleep 120\ntrue\n")
    p = subprocess.Popen(["bash", str(script), proj, "0"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            if M.orchestrator_status(proj)[0]:
                break
            time.sleep(0.1)
        assert M.orchestrator_status(proj) == (True, "7 Retest_X")
    finally:
        os.killpg(p.pid, signal.SIGKILL)
        p.wait(timeout=5)


# ---------------------------------------------------------------------------
# Fase 2 (R6/R9, guardrail sqx-headless-fallos): botón Stop & persist y veredicto veraz de la GUI.
# `gui_verdict` y `pipeline_up` son funciones puras; el cableado REAL del botón y del reporte se ejercita con
# AppTest sobre sqx/sqx_monitor.py (patrón de tests/test_sqx_procs.py::test_f1d_boton_ejecutar_real_*).
# ---------------------------------------------------------------------------

def test_gui_verdict_solo_afirma_con_safe_for_gui_y_si_no_da_los_motivos():
    ok, txt = M.gui_verdict({"safe_for_gui": True, "reasons": []})
    assert ok is True and "Ya puedes abrir la GUI de SQX" in txt
    ok, txt = M.gui_verdict({"safe_for_gui": False, "reasons": ["hay un sqcli vivo (pids 7)", "queda el .bak"]})
    assert ok is False and "ya puedes abrir la gui" not in txt.lower()
    assert "NO abras la GUI todavía" in txt and "sqcli vivo (pids 7)" in txt and "queda el .bak" in txt
    # sin motivos explícitos tampoco afirma nada; un reporte sin la clave NO es "seguro"
    assert M.gui_verdict({"safe_for_gui": False})[0] is False
    assert M.gui_verdict({"motor_killed": True})[0] is False


def test_el_help_del_boton_stop_no_promete_abrir_la_gui():
    low = M.STOP_BUTTON_HELP.lower()
    assert "ya puedes abrir la gui" not in low and "seguro abrir la gui" in low


def test_pipeline_up_ve_orquestador_o_driver_y_tolera_pgrep_roto(monkeypatch):
    monkeypatch.setattr(M.SP, "orchestrator_pids", lambda proj=None: [])
    monkeypatch.setattr(M.SP, "driver_pids", lambda proj=None: [])
    assert M.pipeline_up("p") is False
    monkeypatch.setattr(M.SP, "driver_pids", lambda proj=None: [5])
    assert M.pipeline_up("p") is True
    monkeypatch.setattr(M.SP, "driver_pids", lambda proj=None: [])
    monkeypatch.setattr(M.SP, "orchestrator_pids", lambda proj=None: [6])
    assert M.pipeline_up("p") is True

    def roto(proj=None):
        raise M.SP.ProcProbeError("pgrep roto (test)")

    monkeypatch.setattr(M.SP, "orchestrator_pids", roto)
    assert M.pipeline_up("p") is False                     # el monitor solo informa: no rompe


@pytest.fixture()
def app_monitor(tmp_path, monkeypatch):
    """AppTest REAL del monitor sobre un proyecto fixture, sin motor (HTTP caído), sin SQX instalado y con un
    nombre de motor falso único: nunca se ve ni se toca un sqcli real."""
    from streamlit.testing.v1 import AppTest
    try:  # otros módulos de test parchean st.stop a nivel global: usar el real
        from streamlit.commands.execution_control import stop as real_stop
        import streamlit
        monkeypatch.setattr(streamlit, "stop", real_stop)
    except ImportError:
        pass
    projects = tmp_path / "projects"
    proj = f"mon{uuid4().hex[:6]}"
    make_proj(projects, proj, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setattr(M.REG, "PROJECTS", projects)
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    monkeypatch.setenv("SQX_OUTDIR", str(tmp_path / "out"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", unique_engine_name())
    monkeypatch.delenv("SLIM_TESTING", raising=False)

    def _sin_motor(*a, **k):
        raise OSError("sin motor en :5050 (test)")

    monkeypatch.setattr(urllib.request, "urlopen", _sin_motor)
    at = AppTest.from_file(str(ROOT / "sqx" / "sqx_monitor.py"), default_timeout=90)
    at.proj = proj
    return at


def _boton(at, texto):
    bs = [b for b in at.sidebar.button if texto in b.label]
    return bs[0] if bs else None


def _vista_reporte(at, rep):
    """Pinta `rep` como "Último Stop & persist" y devuelve los textos del expander, por tipo."""
    at.session_state["_stop_report"] = rep
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    ex = [e for e in at.sidebar.get("expander") if "Stop & persist" in (e.label or "")]
    assert ex, "no se pintó el expander del último Stop & persist"
    return {k: [e.value for e in ex[0].get(k)] for k in ("success", "warning", "info", "error", "caption")}


def test_boton_stop_se_ve_con_un_pipeline_vivo_y_el_motor_apagado_y_llama_con_wait_secs_180(app_monitor, procs,
                                                                                            tmp_path, monkeypatch):
    """R6: entre tasks el motor está apagado pero el orquestador vive: el botón tiene que verse. Al pulsarlo se
    llama a `SP.run(proj, wait_secs=180)` (el monitor no espera los 1800 s por defecto)."""
    at = app_monitor
    at.run()
    assert _boton(at, "Stop & persist") is None, "sin motor ni pipeline no hay nada que parar"
    fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", at.proj, 0)
    llamadas = []

    def run_falso(proj, *args, **kwargs):
        llamadas.append((proj, args, kwargs))
        return {"proj": proj, "pipeline_alive": True, "pipeline_stopped": False, "pipeline_stopping": True,
                "warnings": ["parando: el pipeline sigue vivo"], "safe_for_gui": False,
                "reasons": ["hay un orquestador per-task vivo (pids 9)"], "databanks": {}}

    monkeypatch.setattr(M.SP, "run", run_falso)
    at.run()
    btn = _boton(at, "Stop & persist")
    assert btn is not None, "con un orquestador vivo el botón Stop & persist debe verse"
    assert "ya puedes abrir la gui" not in (btn.help or "").lower(), "el help no promete la GUI (R9)"
    btn.click().run()
    assert llamadas == [(at.proj, (), {"wait_secs": 180})]


def test_reporte_de_parada_dice_ya_puedes_abrir_la_gui_solo_con_safe_for_gui(app_monitor):
    at = app_monitor
    at.run()
    base = {"proj": at.proj, "nothing_to_stop": False, "stopped": True, "banks_persisted": ["Out"],
            "banks_http_failed": [], "banks_empty": [], "banks_at_risk": {}, "databanks": {"Out": 3},
            "motor_killed": True, "warnings": []}
    # motor matado Y veredicto seguro: el mensaje positivo
    v = _vista_reporte(at, {**base, "safe_for_gui": True, "reasons": []})
    assert any("Ya puedes abrir la GUI de SQX" in s for s in v["success"])
    # motor matado pero la GUI NO es segura (p. ej. queda un .bak): NO lo dice y da el motivo
    v = _vista_reporte(at, {**base, "safe_for_gui": False,
                            "reasons": ["queda project.cfx.pre_run_task.bak (el .cfx puede tener tasks desactivadas)"]})
    todo = " ".join(sum(v.values(), []))
    assert "Ya puedes abrir la GUI" not in todo
    assert any("NO abras la GUI todavía" in s and "pre_run_task.bak" in s for s in v["warning"])
    # el reporte antiguo (sin safe_for_gui) tampoco lo afirma
    v = _vista_reporte(at, {k: x for k, x in base.items()})
    assert "Ya puedes abrir la GUI" not in " ".join(sum(v.values(), []))


def test_reporte_de_pipeline_parandose_y_parado_y_estados_de_no_completado(app_monitor):
    at = app_monitor
    at.run()
    p = at.proj
    # pipeline_stopping: se muestra como "Parando", con la bandera, y el veredicto de la GUI es NO
    v = _vista_reporte(at, {"proj": p, "pipeline_alive": True, "pipeline_stopped": False, "pipeline_stopping": True,
                            "warnings": ["parando: el pipeline sigue vivo tras 180 s"], "safe_for_gui": False,
                            "reasons": ["hay un orquestador per-task vivo (pids 9)"], "databanks": {}})
    assert any("parando: el pipeline saldrá al terminar de guardar la task en curso" in s and "bandera de parada" in s
               for s in v["info"]), v
    assert any("parando: el pipeline sigue vivo" in s for s in v["warning"])
    assert any("NO abras la GUI" in s and "orquestador per-task vivo" in s for s in v["warning"])
    # pipeline parado por el driver
    v = _vista_reporte(at, {"proj": p, "pipeline_alive": True, "pipeline_stopped": True, "pipeline_stopping": False,
                            "stopped_by": "pipeline", "warnings": [], "safe_for_gui": True, "reasons": [],
                            "databanks": {}})
    assert any("parado" in s and "persistió por su driver" in s for s in v["success"])
    assert any("Ya puedes abrir la GUI" in s for s in v["success"])
    # un banco sin guardar: NO se mató el motor (R1) y se dice
    v = _vista_reporte(at, {"proj": p, "persist_failed": True, "motor_killed": False, "pipeline_alive": False,
                            "warnings": ["NO mato el motor: no se pudo guardar/contar Results"],
                            "safe_for_gui": False, "reasons": ["hay un sqcli vivo (pids 3)"], "databanks": {}})
    assert any("NO completado" in s and "no se ha matado el motor" in s for s in v["error"])
    assert any("NO mato el motor" in s for s in v["warning"])
    # sondeo de procesos roto (C4)
    v = _vista_reporte(at, {"proj": p, "probe_failed": True, "warnings": ["no pude comprobar si hay un pipeline vivo"],
                            "safe_for_gui": False, "reasons": [], "databanks": {}})
    assert any("no se pudo comprobar qué hay vivo" in s for s in v["error"])
