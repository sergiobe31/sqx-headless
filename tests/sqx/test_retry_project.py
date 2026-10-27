"""Tests de sqx/retry_project.py — re-ejecución selectiva de ausentes.

Todo sintético (mini .cfx + .sqx zip + pass_matrix); el motor sqcli no se toca.

Correr desde la raíz del repo:
    python -m pytest tests/sqx/test_retry_project.py -q
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQX = ROOT / "sqx"
if str(SQX) not in sys.path:
    sys.path.insert(0, str(SQX))
TESTS = ROOT / "tests"
if str(TESTS) not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx
    sys.path.append(str(TESTS))

import retry_project as RP  # noqa: E402
from test_slim_hooks import (  # noqa: E402,F401  infraestructura de procesos falsos
    _wait_for, cmdline_of, fake_engine, pid_alive, procs, unique_engine_name,
)

TASK_XML = """<Settings>
  <Databanks retestSelected="false">
    <Databank label="Output databank" name="Output" value="MC_Execution_Realism" />
    <Databank label="Input databank" name="Input" value="High_Precission" />
  </Databanks>
</Settings>
"""

CONFIG_XML = """<Project name="mini" version="143.2708">
  <Tasks>
    <Task type="Retest" name="Retest strategies" showSettingsOverview="false" sampleName="Custom" active="true" taskXMLFile="Retest-Task7.xml" title="MC_Execution_Realism" />
  </Tasks>
  <Databanks>
    <Databank name="High_Precission" view="Default - Main data" syncType="Auto-sync never" position="0" />
    <Databank name="MC_Execution_Realism" view="Default - Main data" syncType="Auto-sync never" position="1" />
  </Databanks>
</Project>
"""


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """Mini proyecto 'mini' en un PROJECTS falso + export con matriz + referencia."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(RP.REG, "PROJECTS", projects)
    mini = projects / "mini"
    mini.mkdir(parents=True)
    with zipfile.ZipFile(mini / "project.cfx", "w") as z:
        z.writestr("config.xml", CONFIG_XML)
        z.writestr("Retest-Task7.xml", TASK_XML)
    # referencia con 3 estrategias (.sqx = zip mínimo)
    ref = tmp_path / "ref"
    ref.mkdir()
    for n in ("A", "B", "C"):
        with zipfile.ZipFile(ref / f"{n}.sqx", "w") as z:
            z.writestr("strategy_Portfolio.xml", f"<portfolio name='{n}'/>")
    # export con matriz: A passed, B ausente, C failed
    out = tmp_path / "out"
    out.mkdir()
    pd.DataFrame({
        "strategy": ["A", "B", "C"], "key": ["A", "B", "C"],
        "pass_MC_Execution_Realism": [True, None, False],
        "pass_Real_OOS": [True, True, True],
        "score": [2, 1, 1], "n_absent": [0, 1, 0],
    }).to_csv(out / "pass_matrix.csv", sep=";", index=False)
    return projects, ref, out


def test_plan_lista_ausentes(entorno, capsys):
    _, ref, out = entorno
    rc = RP.main(["plan", "--dir", str(out)])
    assert rc == 0
    s = capsys.readouterr().out
    assert "MC_Execution_Realism" in s and "1 ausentes" in s
    assert "Real_OOS" in s and "0 ausentes" in s


def test_build_crea_proyecto_retry(entorno):
    projects, ref, out = entorno
    rc = RP.main(["build", "--proj", "mini", "--dir", str(out), "--reference", str(ref),
                  "--tasks", "MC_Execution_Realism"])
    assert rc == 0
    retry = projects / "mini_retry"
    # banco materializado: solo B (la ausente)
    copiados = sorted(p.name for p in (retry / "databanks" / "Retry_MC_Execution_Realism").glob("*.sqx"))
    assert copiados == ["B.sqx"]
    # .cfx re-extraído: config con solo la task retry y los databanks declarados
    entries = RP.read_cfx("mini_retry")
    assert "Retry-Task-MC_Execution_Realism.xml" in entries
    cfg = entries["config.xml"]
    assert 'name="mini_retry"' in cfg
    assert 'taskXMLFile="Retry-Task-MC_Execution_Realism.xml"' in cfg and 'active="true"' in cfg
    assert 'name="Retry_MC_Execution_Realism"' in cfg and 'name="Retry_MC_Execution_Realism_out"' in cfg
    # task clonada con Input/Output reescritos y nada más tocado
    task = entries["Retry-Task-MC_Execution_Realism.xml"]
    assert 'name="Input" value="Retry_MC_Execution_Realism"' in task.replace('" ', '"').replace(' /', '/') \
        or 'name="Input" value="Retry_MC_Execution_Realism"' in task
    assert 'value="Retry_MC_Execution_Realism_out"' in task
    assert "High_Precission" not in task and '"MC_Execution_Realism" />' not in task


def test_build_fail_loud_si_ya_existe(entorno):
    projects, ref, out = entorno
    (projects / "mini_retry").mkdir()
    with pytest.raises(SystemExit, match="ya existe"):
        RP.main(["build", "--proj", "mini", "--dir", str(out), "--reference", str(ref)])


def test_build_fail_loud_ausente_sin_sqx(entorno):
    _, ref, out = entorno
    (ref / "B.sqx").unlink()
    with pytest.raises(SystemExit, match="sin .sqx"):
        RP.main(["build", "--proj", "mini", "--dir", str(out), "--reference", str(ref),
                 "--tasks", "MC_Execution_Realism"])


def test_merge_resuelve_ausentes_y_reporta_repetidos(entorno, capsys):
    _, ref, out = entorno
    # el retry computó B (passed); si además hubiera otra ausente D sin computar, se reporta
    m = pd.read_csv(out / "pass_matrix.csv", sep=";")
    m.loc[len(m)] = ["D", "D", None, True, 1, 1]
    m.to_csv(out / "pass_matrix.csv", sep=";", index=False)
    rex = out / "retry_exports"
    rex.mkdir()
    (rex / "databank_Retry_MC_Execution_Realism_out.csv").write_text(
        '"Strategy Name";"Filters result"\n"B";"PASSED"\n', encoding="utf-8-sig")

    rc = RP.main(["merge", "--dir", str(out)])
    assert rc == 0
    s = capsys.readouterr().out
    assert "1 ausentes resueltos" in s and "1 siguen sin computar" in s
    m2 = pd.read_csv(out / "pass_matrix.csv", sep=";").set_index("strategy")
    assert str(m2.loc["B", "pass_MC_Execution_Realism"]) == "True"
    assert pd.isna(m2.loc["D", "pass_MC_Execution_Realism"])
    assert m2.loc["B", "n_absent"] == 0 and m2.loc["D", "n_absent"] == 1
    # backup creado
    assert list(out.glob("pass_matrix.csv.bak_*"))


TASK_XML_PM = TASK_XML.replace("MC_Execution_Realism", "Input PM").replace("High_Precission", "Real_OOS")
CONFIG_XML_PM = CONFIG_XML.replace("MC_Execution_Realism", "Input PM").replace("High_Precission", "Real_OOS")


def test_prueba_con_espacio_sanitizada_y_merge_inverso(tmp_path, monkeypatch):
    """La prueba 'Input PM' genera banco/ficheros Retry_Input_PM (sin espacio)
    y el merge mapea de vuelta a la columna pass_Input PM."""
    projects = tmp_path / "projects"
    monkeypatch.setattr(RP.REG, "PROJECTS", projects)
    mini = projects / "mini"
    mini.mkdir(parents=True)
    with zipfile.ZipFile(mini / "project.cfx", "w") as z:
        z.writestr("config.xml", CONFIG_XML_PM)
        z.writestr("Retest-Task7.xml", TASK_XML_PM)
    ref = tmp_path / "ref"
    ref.mkdir()
    for n in ("A", "B"):
        with zipfile.ZipFile(ref / f"{n}.sqx", "w") as z:
            z.writestr("strategy_Portfolio.xml", f"<portfolio name='{n}'/>")
    out = tmp_path / "out"
    out.mkdir()
    pd.DataFrame({
        "strategy": ["A", "B"], "key": ["A", "B"],
        "pass_Input PM": [True, None], "score": [1, 0], "n_absent": [0, 1],
    }).to_csv(out / "pass_matrix.csv", sep=";", index=False)

    rc = RP.main(["build", "--proj", "mini", "--dir", str(out), "--reference", str(ref),
                  "--tasks", "Input PM"])
    assert rc == 0
    retry = projects / "mini_retry"
    assert (retry / "databanks" / "Retry_Input_PM" / "B.sqx").exists()
    entries = RP.read_cfx("mini_retry")
    assert "Retry-Task-Input_PM.xml" in entries
    task = entries["Retry-Task-Input_PM.xml"]
    assert 'value="Retry_Input_PM"' in task and 'value="Retry_Input_PM_out"' in task

    rex = out / "retry_exports"
    rex.mkdir()
    (rex / "databank_Retry_Input_PM_out.csv").write_text(
        '"Strategy Name";"Filters result"\n"B";"FAILED"\n', encoding="utf-8-sig")
    rc = RP.main(["merge", "--dir", str(out)])
    assert rc == 0
    m2 = pd.read_csv(out / "pass_matrix.csv", sep=";").set_index("strategy")
    assert str(m2.loc["B", "pass_Input PM"]) == "False"


# ---------------------------------------------------------------------------
# cmd_export (R5, guardrail sqx-headless-fallos Fase 1): el motor se detecta por NOMBRE exacto y el cleanup
# solo mata el motor que arrancó este comando (nunca un sqcli ajeno). Motor y HTTP falsos; sin SQX.
# ---------------------------------------------------------------------------

@pytest.fixture()
def export_falso(entorno, tmp_path, monkeypatch):
    """Entorno de export con el motor REAL sustituido por un symlink `sqcli_<uuid>` -> /usr/bin/yes (vive sin
    argumentos, salida a /dev/null) y el HTTP del motor simulado. Devuelve (nombre del motor, dir del export,
    lista de PIDs de motor vistos durante el export)."""
    if not Path("/usr/bin/yes").exists():
        pytest.skip("sin /usr/bin/yes: no hay motor falso sin argumentos")
    _, _, out = entorno
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)
    link = tmp_path / "bin" / name
    link.parent.mkdir()
    link.symlink_to("/usr/bin/yes")
    monkeypatch.setattr(RP, "SQCLI", str(link))
    monkeypatch.setattr(RP.SP, "gui_pids", lambda: [])        # sin depender de una GUI real abierta
    monkeypatch.setattr(RP, "http_ok", lambda: True)
    monkeypatch.setattr(RP, "wait_loaded", lambda *a, **k: None)
    vistos: list[int] = []

    def http_call(cmd, timeout=30):
        vistos.extend(RP.SP.engine_pids())                    # el motor que acaba de arrancar cmd_export
        dst = Path(re.search(r"file=(\S+)", cmd).group(1))
        dst.write_text("Strategy Name;Filters result\nA;PASSED\n", encoding="utf-8")
        return "ok"

    monkeypatch.setattr(RP, "http_call", http_call)
    return name, out, vistos


def test_export_aborta_si_hay_un_sqcli_por_nombre_pero_no_por_cmdline(export_falso, tmp_path, procs):
    name, out, _ = export_falso
    bait = procs.spawn(["bash", "-c", "sleep 60; true # /Resources/sqcli"])    # el patrón ancho antiguo lo casaba
    assert _wait_for(lambda: "Resources/sqcli" in cmdline_of(bait.pid))
    assert RP.main(["export", "--proj", "mini", "--dir", str(out), "--banks", "Retry_X_out"]) == 0
    ajeno = fake_engine(procs, tmp_path / "otro", name)
    with pytest.raises(SystemExit, match="ya hay un sqcli corriendo"):
        RP.main(["export", "--proj", "mini", "--dir", str(out), "--banks", "Retry_X_out"])
    assert pid_alive(ajeno.pid)                                # abortar NO mata al ajeno


def test_export_cleanup_mata_solo_el_motor_que_arranco(export_falso, tmp_path, procs, monkeypatch):
    name, out, vistos = export_falso
    ajeno: dict = {}
    real_http_call = RP.http_call

    def http_call_y_aparece_otro_motor(cmd, timeout=30):
        r = real_http_call(cmd, timeout)
        if "p" not in ajeno:   # un motor AJENO (mismo nombre, otra sesión) aparece mientras corre el export
            ajeno["p"] = fake_engine(procs, tmp_path / "otro", name)
        return r

    monkeypatch.setattr(RP, "http_call", http_call_y_aparece_otro_motor)
    assert RP.main(["export", "--proj", "mini", "--dir", str(out), "--banks", "Retry_X_out"]) == 0
    assert len(vistos) == 1
    assert _wait_for(lambda: not pid_alive(vistos[0]), timeout=15), "el motor que arrancó el export debe morir"
    assert pid_alive(ajeno["p"].pid), "un sqcli ajeno NO se toca (R3/R5)"
    assert (out / "retry_exports" / "databank_Retry_X_out.csv").exists()
