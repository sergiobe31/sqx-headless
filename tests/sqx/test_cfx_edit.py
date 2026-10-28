"""Tests de sqx/cfx_edit.py (guardrail `cfx-edit-publico`, Fase 1, 2026-10-27): tests-de-plan T1-T25.

Todo con `.cfx` SINTÉTICOS escritos a mano aquí (nombres de elementos según los hechos H1-H11 del plan; ningún
contenido de SQX ni de proyectos reales). Nunca se arranca un proceso (T21: `subprocess.Popen` parcheado), el
`sqcli` y la app de SQX apuntan a rutas inexistentes y las sondas de procesos de `stop_and_persist` están a [].

  T1  show/--json/pie ............................ test_t01_*          T14 pre-check (9 motivos) ...... test_t14_*
  T2  --dates cirugía de 1 línea ................. test_t02_*          T15 ZIP: orden/compresión ...... test_t15_*
  T3  binding + goldens epoch .................... test_t03_*          T16 sin daños colaterales ...... test_t16*_*
  T4  OOS golden CRLF + ida y vuelta ............. test_t04_*          T17 verificación que falla ..... test_t17_*
  T5  OOS inválidos / isv ........................ test_t05_*          T18 varias tasks ............... test_t18_*
  T6  --dates deja fuera el OOS .................. test_t06_*          T19 fin de línea ............... test_t19_*
  T7  goldens --oos-last-pct ..................... test_t07_*          T20 task sin <Data> ............ test_t20_*
  T8  goals ...................................... test_t08_*          T21 ningún proceso ............. test_t21_*
  T9  fitness simple ............................. test_t09_*          T22 cambio externo en disco .... test_t22_*
  T10 máximo de estrategias ...................... test_t10_*          T23 os.replace deja otros bytes  test_t23_*
  T11 parada ..................................... test_t11_*          T24 aviso de cross checks ...... test_t24_*
  T12 simulación no escribe ...................... test_t12_*          T25 prefijo ≠ type ............. test_t25_*
  T13 backup ..................................... test_t13_*
"""
from __future__ import annotations

import calendar
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import sqx.bank_plan as BP
import sqx.cfx_edit as CE

# ----------------------------- fixture sintética -----------------------------------------------------------


def _ms(y, m, d):
    return calendar.timegm((y, m, d, 0, 0, 0)) * 1000


B0, B1 = _ms(2010, 1, 1), _ms(2024, 12, 31)       # binding de SYM / RSYM: 2010.01.01 – 2024.12.31

BUILD1 = f"""<Settings>
  <Data>
    <Setups>
      <Setup dateFrom="2015.01.01" dateTo="2020.12.31" xdateFrom="1999.01.01">
        <Chart symbol="SYM" timeframe="H1" />
      </Setup>
    </Setups>
    <OutOfSample showGraph="true" />
  </Data>
  <Resources>
    <Symbols>
      <Symbol name="OTRO" dateFrom="{_ms(2012, 1, 1)}" dateTo="{_ms(2019, 1, 1)}" />
      <Symbol name="SYM" dateFrom="{B0}" dateTo="{B1}" />
    </Symbols>
  </Resources>
  <CrossChecks>
    <RetestOnAdditionalMarkets use="true">
      <Settings>
        <Setups>
          <Setup dateFrom="2012.03.01" dateTo="2018.06.30">
            <Chart symbol="OTRO" timeframe="H1" />
            <MainTestValues timeframe="H1" dates="true" />
          </Setup>
        </Setups>
      </Settings>
    </RetestOnAdditionalMarkets>
  </CrossChecks>
  <Rankings use="true">
    <MaxStrategies>1000</MaxStrategies>
    <StopCondition type="time-limit" passedStrategies="1000" restartCount="0" days="0" hours="2" minutes="25" />
    <FitnessCriteria>
      <Settings>
        <Ranking type="Weighted">
          <Goal use="true" type="ReturnDDRatio" weight="10" valueType="1" target="0" />
          <Goal use="true" type="DrawdownPct" weight="5" valueType="2" target="0" />
          <Goal use="false" type="ProfitFactor" weight="3" valueType="1" target="0" />
          <Goal use="false" type="NumberOfTrades" weight="1" valueType="3" target="0" />
        </Ranking>
      </Settings>
    </FitnessCriteria>
  </Rankings>
</Settings>
""".replace("\n", "\r\n")

RETEST1 = f"""<Settings>
  <Data>
    <Setups>
      <Setup dateFrom="2015.01.01" dateTo="2020.12.31">
        <Chart symbol="RSYM" timeframe="H1" />
      </Setup>
    </Setups>
    <OutOfSample showGraph="true">
      <Range dateFrom="2019.06.01" dateTo="2020.12.31" />
    </OutOfSample>
  </Data>
  <Resources>
    <Symbols>
      <Symbol name="RSYM" dateFrom="{B0}" dateTo="{B1}" />
    </Symbols>
  </Resources>
  <Rankings>
    <MaxStrategies>500</MaxStrategies>
    <StopCondition type="never" passedStrategies="0" restartCount="0" days="0" hours="0" minutes="0" />
    <FitnessCriteria>
      <Settings>
        <Ranking type="NetProfit" />
      </Settings>
    </FitnessCriteria>
  </Rankings>
</Settings>
"""

FILTERING1 = """<Settings>
  <Filtering>
    <MaxStrategies>0</MaxStrategies>
  </Filtering>
</Settings>
""".replace("\n", "\r\n")

BUILD2 = f"""<Settings>
  <Data>
    <Setups>
      <Setup dateFrom="2014.01.01" dateTo="2019.12.31">
        <Chart symbol="SYM2" timeframe="H4" />
      </Setup>
    </Setups>
    <OutOfSample showGraph="false" />
  </Data>
  <Resources>
    <Symbols>
      <Symbol name="SYM2" dateFrom="{B0}" dateTo="{B1}" />
    </Symbols>
  </Resources>
  <Rankings>
    <MaxStrategies>700</MaxStrategies>
    <StopCondition type="databank-full" />
    <FitnessCriteria>
      <Settings>
        <Ranking type="NetProfit" />
      </Settings>
    </FitnessCriteria>
  </Rankings>
</Settings>
""".replace("\n", "\r\n")

CONFIG = """<Project name="t"><Tasks>
<Task type="Build" name="Build 1" title="Build principal" taskXMLFile="Build-Task1.xml" active="true" />
<Task type="Filtering" name="Filtro" taskXMLFile="Filtering-Task1.xml" active="true" />
<Task type="Retest" name="Retest A" title="Retest A" taskXMLFile="Retest-Task1.xml" />
<Task type="Build" name="Build 2" title="Build antiguo" taskXMLFile="Build-Task2.xml" active="false" />
<Task type="Retest" name="Retest B" title="Retest A" taskXMLFile="Retest-Task2.xml" active="true" />
</Tasks></Project>
"""

#: nombre -> (contenido, compress_type). `config.xml` NO va primero; Retest-Task1 va ZIP_STORED.
MEMBERS = {
    "Build-Task1.xml": (BUILD1, zipfile.ZIP_DEFLATED),
    "Retest-Task1.xml": (RETEST1, zipfile.ZIP_STORED),
    "config.xml": (CONFIG, zipfile.ZIP_DEFLATED),
    "Filtering-Task1.xml": (FILTERING1, zipfile.ZIP_DEFLATED),
    "Build-Task2.xml": (BUILD2, zipfile.ZIP_DEFLATED),
    "Retest-Task2.xml": (RETEST1, zipfile.ZIP_DEFLATED),
}
SYM_DUP = "Retest A"            # label repetido (tasks 2 y 4)


def write_cfx(path: Path, members: dict) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, (txt, comp) in members.items():
            zi = zipfile.ZipInfo(name, (2026, 1, 2, 3, 4, 6))
            zi.compress_type = comp
            z.writestr(zi, txt.encode("utf-8") if isinstance(txt, str) else txt)


def make_project(root: Path, name: str = "proj", **cambios) -> Path:
    """Crea root/<name>/project.cfx con MEMBERS (con `cambios` = {miembro: texto}). Devuelve la carpeta."""
    members = dict(MEMBERS)
    for k, v in cambios.items():
        k = k.replace("__", "-").replace("_xml", ".xml")
        members[k] = (v, members.get(k, (None, zipfile.ZIP_DEFLATED))[1])
    d = root / name
    d.mkdir(parents=True)
    write_cfx(d / "project.cfx", members)
    return d


@pytest.fixture(autouse=True)
def entorno(monkeypatch, tmp_path):
    """Aislamiento: nunca el sqcli ni la app reales, proyectos en tmp, sondas de procesos a [], Popen prohibido."""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", "sqcli_que_no_existe_cfx_edit")
    for fn in ("engine_pids", "gui_pids", "orchestrator_pids", "driver_pids", "cascade_pids"):
        monkeypatch.setattr(CE.SP, fn, lambda *a, **k: [])
    llamadas = []

    def boom(*a, **k):
        llamadas.append(a)
        raise AssertionError("cfx_edit no debe arrancar ningún proceso")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.popen_llamadas = llamadas      # lo lee T21


@pytest.fixture()
def pdir(tmp_path):
    return make_project(tmp_path / "projects")


def go(capsys, *args):
    rc = CE.main(list(args))
    out, err = capsys.readouterr()
    return rc, out, err


def member(pdir: Path, name: str) -> bytes:
    with zipfile.ZipFile(pdir / "project.cfx") as z:
        return z.read(name)


def sha(pdir: Path) -> bytes:
    return hashlib.sha256((pdir / "project.cfx").read_bytes()).hexdigest().encode()


def sin_efectos(pdir: Path, sha0: bytes):
    """El .cfx no cambió y no hay backups ni temporales."""
    assert sha(pdir) == sha0
    assert not list(pdir.glob("project.cfx.bak_*"))
    assert not list(pdir.glob("*.cfx_edit.*.tmp"))


def n_dif(a: bytes, b: bytes) -> tuple[int, int]:
    """(líneas quitadas, líneas añadidas) entre dos textos."""
    d = list(difflib.unified_diff(a.decode().splitlines(True), b.decode().splitlines(True), n=0))
    quitadas = [x for x in d if x.startswith("-") and not x.startswith("---")]
    anadidas = [x for x in d if x.startswith("+") and not x.startswith("+++")]
    return len(quitadas), len(anadidas)


# ----------------------------- T1 ---------------------------------------------------------------------------

def test_t01_show_mismos_indices_y_labels_que_bank_plan(pdir):
    esperado = [(i, t["label"]) for i, t in enumerate(BP._tasks_of_cfx(pdir / "project.cfx"))]
    got = CE.show("proj")
    assert [(e["index"], e["label"]) for e in got] == esperado
    assert [e["type"] for e in got] == ["Build", "Filtering", "Retest", "Build", "Retest"]
    assert [e["active"] for e in got] == [True, True, True, False, True]
    assert [e["index"] for e in CE.show("proj", ["2", "Build antiguo"])] == [2, 3]


def test_t01_show_json_esquema_fijo(pdir, capsys):
    rc, out, _ = go(capsys, "proj", "show", "--json")
    assert rc == 0
    datos = json.loads(out)
    clave = {"index", "label", "type", "active", "file", "window", "symbol", "binding", "oos",
             "cross_checks", "rankings"}
    assert all(set(e) == clave for e in datos)
    b = datos[0]
    assert b["window"] == {"from": "2015.01.01", "to": "2020.12.31"} and b["symbol"] == "SYM"
    assert b["binding"] == {"from": "2010.01.01", "to": "2024.12.31", "from_ms": B0, "to_ms": B1}
    assert b["oos"] == {"show_graph": "true", "ranges": []}
    assert b["cross_checks"] == {"setups": [{"symbol": "OTRO", "from": "2012.03.01", "to": "2018.06.30",
                                             "main_test_dates": "true"}]}
    assert b["rankings"]["max_strategies"] == "1000"
    assert b["rankings"]["stop"]["type"] == "time-limit"
    assert b["rankings"]["fitness"] == {"type": "Weighted", "goals": [
        {"metric": "ReturnDDRatio", "weight": "10", "value_type": "1", "direction": "max"},
        {"metric": "DrawdownPct", "weight": "5", "value_type": "2", "direction": "min"}]}
    # Filtering: sin ventana ni rankings; Retest: OOS con rango, fitness simple
    assert datos[1]["window"] is None and datos[1]["rankings"] is None
    assert datos[2]["oos"]["ranges"] == [{"from": "2019.06.01", "to": "2020.12.31", "type": None}]
    assert datos[2]["rankings"]["fitness"] == {"type": "NetProfit", "goals": []}


def test_t01_show_texto_con_pie_del_binding_y_filtro_de_tasks(pdir, capsys):
    rc, out, _ = go(capsys, "proj", "show", "--task", "0")
    assert rc == 0
    assert "[0] Build principal" in out and "[1]" not in out
    assert "binding de datos de SYM: 2010.01.01 – 2024.12.31" in out
    assert "cfx_edit no puede ver qué datos tiene SQX descargados y nunca cambia el enlace de datos del símbolo" in out


# ----------------------------- T2 ---------------------------------------------------------------------------

def test_t02_dates_cambia_solo_la_ventana_principal(pdir, capsys):
    orig = member(pdir, "Build-Task1.xml")
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2016.01.01:2019.12.31", "--apply")
    assert rc == 0, err
    nuevo = member(pdir, "Build-Task1.xml")
    assert n_dif(orig, nuevo) == (1, 1)
    assert b'<Setup dateFrom="2016.01.01" dateTo="2019.12.31" xdateFrom="1999.01.01">' in nuevo
    assert b'<Setup dateFrom="2012.03.01" dateTo="2018.06.30">' in nuevo          # señuelo del cross check
    assert f'dateFrom="{B0}" dateTo="{B1}"'.encode() in nuevo                     # binding intacto
    assert nuevo.count(b"\r\n") == orig.count(b"\r\n")


# ----------------------------- T3 ---------------------------------------------------------------------------

def test_t03_goldens_epoch_a_fecha():
    assert CE._ms_to_date(1356998400000) == dt.date(2013, 1, 1)
    assert CE._ms_to_date(1790726400000) == dt.date(2026, 9, 30)


@pytest.mark.parametrize("dates", ["2009.12.31:2020.01.01", "2015.01.01:2025.01.01"])
def test_t03_ventana_fuera_del_binding_rechaza(pdir, capsys, dates):
    s0 = sha(pdir)
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--dates", dates, "--apply")
    assert rc == 1
    assert "se sale de los datos enlazados" in err and "2010.01.01–2024.12.31" in err and "GUI de SQX" in err
    sin_efectos(pdir, s0)


def test_t03_ventana_igual_al_binding_vale_extremos_incluidos(pdir, capsys):
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2010.01.01:2024.12.31", "--apply")
    assert rc == 0, err
    assert b'dateFrom="2010.01.01" dateTo="2024.12.31"' in member(pdir, "Build-Task1.xml")


# ----------------------------- T4 ---------------------------------------------------------------------------

def test_t04_oos_golden_crlf(pdir, capsys):
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--oos", "2019.01.01:2020.12.31", "--apply",
                    "--motivo", "uno")
    assert rc == 0, err
    nuevo = member(pdir, "Build-Task1.xml")
    esperado = (b'    <OutOfSample showGraph="true">\r\n      <Range dateFrom="2019.01.01" dateTo="2020.12.31" />'
                b'\r\n    </OutOfSample>')
    assert esperado in nuevo


def test_t04_oos_none_devuelve_los_bytes_originales(pdir, capsys, monkeypatch):
    orig = member(pdir, "Build-Task1.xml")
    tiempos = iter([dt.datetime(2026, 1, 1, 0, 0, 1), dt.datetime(2026, 1, 1, 0, 0, 2)])
    monkeypatch.setattr(CE, "_now", lambda: next(tiempos))
    assert go(capsys, "proj", "set", "--task", "0", "--oos", "2019.01.01:2020.12.31", "--apply")[0] == 0
    assert member(pdir, "Build-Task1.xml") != orig
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--oos", "none", "--apply")
    assert rc == 0, err
    assert member(pdir, "Build-Task1.xml") == orig


# ----------------------------- T5 ---------------------------------------------------------------------------

@pytest.mark.parametrize("oos", [
    "2014.01.01:2020.01.01",     # empieza antes de la ventana
    "2019.01.01:2021.06.01",     # acaba después
    "2019.01.01:2018.01.01",     # invertido
    "2015.01.01:2020.12.31",     # cubre la ventana entera
])
def test_t05_oos_invalido_rechaza(pdir, capsys, oos):
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--oos", oos, "--apply")
    assert rc == 1, err
    sin_efectos(pdir, s0)


def test_t05_oos_con_rango_isv_rechaza_cualquier_cambio(tmp_path, capsys, monkeypatch):
    isv = RETEST1.replace('    </OutOfSample>', '      <Range dateFrom="2016.01.01" dateTo="2017.01.01" type="isv" />\n    </OutOfSample>')
    d = make_project(tmp_path / "otro", "isvp", Retest__Task1_xml=isv)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "otro"))
    s0 = sha(d)
    for args in (["--oos", "none"], ["--oos", "2019.01.01:2020.01.01"], ["--oos-last-pct", "30"]):
        rc, _, err = go(capsys, "isvp", "set", "--task", "2", *args, "--apply")
        assert rc == 1 and "tipo" in err, args
    sin_efectos(d, s0)


# ----------------------------- T6 ---------------------------------------------------------------------------

def test_t06_dates_que_deja_fuera_el_oos_rechaza_salvo_que_se_redefina(pdir, capsys):
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "2", "--dates", "2016.01.01:2018.12.31", "--apply")
    assert rc == 1 and "--oos" in err
    sin_efectos(pdir, s0)
    rc, _, err = go(capsys, "proj", "set", "--task", "2", "--dates", "2016.01.01:2018.12.31",
                    "--oos-last-pct", "30", "--apply")
    assert rc == 0, err
    t = CE.read_task(member(pdir, "Retest-Task1.xml"))
    assert t["window"] == {"from": "2016.01.01", "to": "2018.12.31"}
    assert t["oos"]["ranges"][0]["to"] == "2018.12.31"


# ----------------------------- T7 ---------------------------------------------------------------------------

@pytest.mark.parametrize("dates,pct,esperado", [
    ("2010.01.01:2020.01.01", 30, ("2016.12.31", "2020.01.01")),
    ("2010.01.01:2020.01.01", 25, ("2017.07.02", "2020.01.01")),
    ("2010.01.01:2020.01.01", 99, ("2010.02.06", "2020.01.01")),
    ("2020.01.01:2020.01.12", 30, ("2020.01.08", "2020.01.12")),
])
def test_t07_goldens_oos_last_pct(pdir, dates, pct, esperado):
    plan = CE.plan_edit("proj", ["0"], CE.Changes(dates=dates, oos_last_pct=pct))
    assert plan.tasks[0].edits["oos"] == [esperado]


def test_t07_ventana_de_un_dia_con_50_rechaza(pdir, capsys):
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2020.01.01:2020.01.02",
                    "--oos-last-pct", "50", "--apply")
    assert rc == 1, err
    sin_efectos(pdir, s0)


@pytest.mark.parametrize("pct", ["0", "100", "x"])
def test_t07_pct_fuera_de_1_99_rechaza(pdir, capsys, pct):
    assert go(capsys, "proj", "set", "--task", "0", "--oos-last-pct", pct)[0] == 1


# ----------------------------- T8 ---------------------------------------------------------------------------

def test_t08_goal_pone_uso_y_peso_sin_tocar_valuetype_ni_target(pdir, capsys):
    orig = member(pdir, "Build-Task1.xml")
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--goal", "ProfitFactor=7", "--apply")
    assert rc == 0, err
    nuevo = member(pdir, "Build-Task1.xml")
    assert b'<Goal use="true" type="ProfitFactor" weight="7" valueType="1" target="0" />' in nuevo
    assert n_dif(orig, nuevo) == (1, 1)


def test_t08_goals_only_apaga_los_demas(pdir, capsys):
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--goal", "ProfitFactor=7", "--goals-only", "--apply")
    assert rc == 0, err
    g = {x["metric"]: (x["use"], x["weight"]) for x in
         CE.read_task(member(pdir, "Build-Task1.xml"))["rankings"]["fitness"]["goals"]}
    assert g == {"ReturnDDRatio": ("false", "10"), "DrawdownPct": ("false", "5"),
                 "ProfitFactor": ("true", "7"), "NumberOfTrades": ("false", "1")}


def test_t08_goal_off(pdir, capsys):
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--goal-off", "DrawdownPct", "--apply")
    assert rc == 0, err
    g = {x["metric"]: x["use"] for x in CE.read_task(member(pdir, "Build-Task1.xml"))["rankings"]["fitness"]["goals"]}
    assert g["DrawdownPct"] == "false" and g["ReturnDDRatio"] == "true"


def test_t08_rechazos(pdir, capsys):
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--goal", "NoExiste=5")
    assert rc == 1 and "ReturnDDRatio" in err and "NumberOfTrades" in err          # lista las disponibles
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--goal-off", "ReturnDDRatio", "--goal-off", "DrawdownPct")
    assert rc == 1 and "ningún goal en uso" in err
    for w in ("0", "101"):
        assert go(capsys, "proj", "set", "--task", "0", "--goal", f"ProfitFactor={w}")[0] == 1
    assert go(capsys, "proj", "set", "--task", "0", "--goal", "ProfitFactor=x")[0] == 1
    rc, _, err = go(capsys, "proj", "set", "--task", "3", "--goal", "ProfitFactor=5")      # fitness simple
    assert rc == 1 and "simple" in err and "Weighted" in err
    sin_efectos(pdir, s0)


def test_t08_goals_only_sin_goal_es_error_de_uso(pdir, capsys):
    assert go(capsys, "proj", "set", "--task", "0", "--goals-only")[0] == 2


# ----------------------------- T9 ---------------------------------------------------------------------------

def test_t09_fitness_simple_sustituye_el_weighted_y_avisa(pdir, capsys):
    orig = member(pdir, "Build-Task1.xml")
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--fitness", "ProfitFactor")
    assert rc == 0 and "AVISO" in out and "se pierde la lista de goals" in out
    assert member(pdir, "Build-Task1.xml") == orig                                  # simulación
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--fitness", "ProfitFactor", "--apply")
    assert rc == 0, err
    nuevo = member(pdir, "Build-Task1.xml")
    assert b'<Ranking type="ProfitFactor" />\r\n      </Settings>' in nuevo        # cola conservada
    assert b"<Goal" not in nuevo
    assert len(list(pdir.glob("project.cfx.bak_*"))) == 1


def test_t09_rechazos_y_exclusion(pdir, capsys):
    s0 = sha(pdir)
    assert go(capsys, "proj", "set", "--task", "0", "--fitness", "Stability")[0] == 1
    assert go(capsys, "proj", "set", "--task", "0", "--fitness", "ProfitFactor", "--goal", "ProfitFactor=5")[0] == 2
    assert go(capsys, "proj", "set", "--task", "2", "--fitness", "ProfitFactor")[0] == 1      # Retest
    sin_efectos(pdir, s0)


# ----------------------------- T10 --------------------------------------------------------------------------

def test_t10_max_strategies_solo_el_de_rankings(pdir, capsys):
    orig = member(pdir, "Build-Task1.xml")
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")
    assert rc == 0, err
    nuevo = member(pdir, "Build-Task1.xml")
    assert b"<MaxStrategies>1500</MaxStrategies>" in nuevo and n_dif(orig, nuevo) == (1, 1)
    assert go(capsys, "proj", "set", "--task", "0", "--max-strategies", "25000")[0] == 0


@pytest.mark.parametrize("n", ["0", "25001", "abc", "-5"])
def test_t10_valores_fuera_de_limite(pdir, capsys, n):
    assert go(capsys, "proj", "set", "--task", "0", "--max-strategies", n)[0] == 1


def test_t10_filtering_y_retest_rechazan(pdir, capsys):
    s0 = sha(pdir)
    for t in ("1", "2"):
        rc, _, err = go(capsys, "proj", "set", "--task", t, "--max-strategies", "100", "--apply")
        assert rc == 1, err
    sin_efectos(pdir, s0)
    assert b"<MaxStrategies>0</MaxStrategies>" in member(pdir, "Filtering-Task1.xml")


# ----------------------------- T11 --------------------------------------------------------------------------

def _stop(pdir):
    return CE.read_task(member(pdir, "Build-Task1.xml"))["rankings"]["stop"]


def test_t11_time_limit(pdir, capsys):
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--stop", "time-limit:0:5:30", "--apply")
    assert rc == 0, err
    assert _stop(pdir) == {"type": "time-limit", "passedStrategies": "1000", "restartCount": "0",
                           "days": "0", "hours": "5", "minutes": "30"}


def test_t11_passed_count(pdir, capsys):
    assert go(capsys, "proj", "set", "--task", "0", "--stop", "passed-count:500", "--apply")[0] == 0
    assert _stop(pdir) == {"type": "passed-count", "passedStrategies": "500", "restartCount": "0",
                           "days": "0", "hours": "2", "minutes": "25"}


@pytest.mark.parametrize("cond", ["never", "databank-full"])
def test_t11_never_y_databank_full_solo_cambian_type(pdir, capsys, cond):
    assert go(capsys, "proj", "set", "--task", "0", "--stop", cond, "--apply")[0] == 0
    assert _stop(pdir) == {"type": cond, "passedStrategies": "1000", "restartCount": "0",
                           "days": "0", "hours": "2", "minutes": "25"}


def test_t11_stopcondition_antiguo(pdir, capsys):
    s0 = sha(pdir)
    for cond in ("time-limit:0:1:0", "passed-count:100"):
        rc, _, err = go(capsys, "proj", "set", "--task", "3", "--stop", cond, "--apply")
        assert rc == 1 and "formato antiguo" in err, cond
    sin_efectos(pdir, s0)
    assert go(capsys, "proj", "set", "--task", "3", "--stop", "never", "--apply")[0] == 0
    assert b'<StopCondition type="never" />' in member(pdir, "Build-Task2.xml")


@pytest.mark.parametrize("cond", ["time-limit:0:0:0", "time-limit:0:49:0", "time-limit:0:0:60",
                                  "time-limit:366:0:0", "passed-count:0", "passed-count:25001"])
def test_t11_fuera_de_limites(pdir, capsys, cond):
    assert go(capsys, "proj", "set", "--task", "0", "--stop", cond)[0] == 1


def test_t11_formato_de_stop_invalido_es_error_de_uso(pdir, capsys):
    assert go(capsys, "proj", "set", "--task", "0", "--stop", "time-limit:1:2")[0] == 2
    assert go(capsys, "proj", "set", "--task", "0", "--stop", "otra-cosa")[0] == 2


# ----------------------------- T12 --------------------------------------------------------------------------

def test_t12_simulacion_no_escribe_y_enseña_antes_despues(pdir, capsys):
    s0 = sha(pdir)
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2016.01.01:2019.12.31",
                      "--oos-last-pct", "30", "--max-strategies", "1500")
    assert rc == 0, err
    assert "SIMULACIÓN" in out and "No se ha escrito nada" in out
    assert "ventana: 2015.01.01–2020.12.31 → 2016.01.01–2019.12.31" in out
    assert "OOS: ninguno →" in out
    assert "máximo de estrategias: 1000 → 1500" in out
    sin_efectos(pdir, s0)


# ----------------------------- T13 --------------------------------------------------------------------------

def test_t13_backup_con_los_bytes_originales_y_nombre(pdir, capsys, monkeypatch):
    orig = (pdir / "project.cfx").read_bytes()
    monkeypatch.setattr(CE, "_now", lambda: dt.datetime(2026, 10, 27, 12, 34, 56))
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply",
                      "--motivo", "prueba")
    assert rc == 0, err
    bak = pdir / "project.cfx.bak_prueba_20261027_123456"
    assert bak.read_bytes() == orig and str(bak) in out
    assert (pdir / "project.cfx").read_bytes() != orig


def test_t13_motivo_se_sanea(pdir, capsys):
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply",
                    "--motivo", "a b/../c!ñ")
    assert rc == 0, err
    nombres = [p.name for p in pdir.glob("project.cfx.bak_*")]
    assert len(nombres) == 1 and re.fullmatch(r"project\.cfx\.bak_[A-Za-z0-9_-]+_\d{8}_\d{6}", nombres[0])
    assert "/" not in nombres[0] and " " not in nombres[0]


def test_t13_backup_existente_no_se_pisa(pdir, capsys, monkeypatch):
    monkeypatch.setattr(CE, "_now", lambda: dt.datetime(2026, 1, 1, 0, 0, 0))
    previo = pdir / "project.cfx.bak_x_20260101_000000"
    previo.write_bytes(b"importante")
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply", "--motivo", "x")
    assert rc == 1 and "ya existe el backup" in err
    assert previo.read_bytes() == b"importante" and sha(pdir) == s0
    assert not list(pdir.glob("*.cfx_edit.*.tmp"))


# ----------------------------- T14 --------------------------------------------------------------------------

def _inseguro_proceso(fn):
    def prep(monkeypatch, pdir):
        monkeypatch.setattr(CE.SP, fn, lambda *a, **k: [4242])
    return prep


def _inseguro_probe(monkeypatch, pdir):
    def falla(*a, **k):
        raise CE.SP.ProcProbeError("pgrep no disponible")
    monkeypatch.setattr(CE.SP, "engine_pids", falla)


def _inseguro_marcador(monkeypatch, pdir):
    (pdir / "databanks_archive").mkdir()
    (pdir / "databanks_archive" / ".runner_session.json").write_text("{}")


def _inseguro_bancos(monkeypatch, pdir):
    (pdir / "databanks_archive" / "Results").mkdir(parents=True)


def _inseguro_bak(monkeypatch, pdir):
    (pdir / "project.cfx.pre_run_task.bak").write_bytes(b"x")


@pytest.mark.parametrize("prep", [
    _inseguro_proceso("engine_pids"), _inseguro_proceso("gui_pids"), _inseguro_proceso("orchestrator_pids"),
    _inseguro_proceso("driver_pids"), _inseguro_proceso("cascade_pids"), _inseguro_probe,
    _inseguro_marcador, _inseguro_bancos, _inseguro_bak,
], ids=["motor", "gui", "orquestador", "driver", "cascada", "ProcProbeError", "slim-marcador",
        "bancos-archivados", "pre_run_task.bak"])
def test_t14_precheck_rechaza_y_no_escribe(pdir, capsys, monkeypatch, prep):
    prep(monkeypatch, pdir)
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")
    assert rc == 1 and "NO es seguro editar" in err
    sin_efectos(pdir, s0)


def test_t14_la_simulacion_no_exige_precheck(pdir, capsys, monkeypatch):
    monkeypatch.setattr(CE.SP, "gui_pids", lambda *a, **k: [1])
    assert go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500")[0] == 0
    assert go(capsys, "proj", "show")[0] == 0


# ----------------------------- T15 --------------------------------------------------------------------------

def test_t15_zip_mismo_orden_compresion_y_miembros_intactos(pdir, capsys):
    ant = zipfile.ZipFile(pdir / "project.cfx")
    ant_info = [(i.filename, i.compress_type, i.date_time) for i in ant.infolist()]
    ant_datos = {i.filename: ant.read(i) for i in ant.infolist()}
    ant.close()
    assert ant_info[0][0] != "config.xml" and any(c == zipfile.ZIP_STORED for _, c, _ in ant_info)
    assert go(capsys, "proj", "set", "--task", "0", "--task", "2", "--dates", "2016.01.01:2019.12.31",
              "--oos-last-pct", "30", "--apply")[0] == 0
    with zipfile.ZipFile(pdir / "project.cfx") as z:
        assert z.testzip() is None
        assert [(i.filename, i.compress_type, i.date_time) for i in z.infolist()] == ant_info   # incl. STORED
        for i in z.infolist():
            if i.filename not in ("Build-Task1.xml", "Retest-Task1.xml"):
                assert z.read(i) == ant_datos[i.filename], i.filename
            else:
                assert z.read(i) != ant_datos[i.filename]


def test_t15_verificacion_del_temporal_detecta_zip_corrupto(pdir, capsys, monkeypatch):
    """El temporal con un miembro no editado distinto -> rc 3, sin temporal ni backup, original intacto."""
    real = CE._build_zip

    def corrupto(tmp, plan):
        real(tmp, plan)
        datos = tmp.read_bytes()
        tmp.unlink()
        with zipfile.ZipFile(tmp, "w") as z:
            for i, b in plan.members:
                zi = zipfile.ZipInfo(i.filename, i.date_time)
                zi.compress_type = i.compress_type
                z.writestr(zi, b + b" " if i.filename == "config.xml" else
                           {t.xml: t.new_bytes for t in plan.tasks}.get(i.filename, b))
    monkeypatch.setattr(CE, "_build_zip", corrupto)
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")
    assert rc == 3 and "config.xml" in err
    sin_efectos(pdir, s0)


# ----------------------------- T16 --------------------------------------------------------------------------

REJILLA = [
    ("0", dict(dates="2016.01.01:2019.12.31", oos_last_pct=30)),
    ("0", dict(oos="2019.01.01:2020.12.31")),
    ("0", dict(goal=["ProfitFactor=7"], goals_only=True)),
    ("0", dict(goal=["ProfitFactor=7", "NumberOfTrades=2"], goal_off=["DrawdownPct"])),
    ("0", dict(fitness="RExpectancy")),
    ("0", dict(max_strategies=1500, stop="time-limit:0:5:30")),
    ("0", dict(max_strategies=2500, stop="passed-count:500")),
    ("0", dict(dates="2011.01.01:2023.01.01", oos="2020.01.01:2023.01.01", goal=["ProfitFactor=9"],
               max_strategies=3000, stop="never")),
    ("2", dict(dates="2016.01.01:2020.01.01", oos="none")),
    ("2", dict(oos_last_pct=40)),
    ("3", dict(oos="2018.01.01:2019.12.31", stop="never", max_strategies=900)),
    ("3", dict(fitness="ProfitFactor", dates="2012.01.01:2022.01.01", oos_last_pct=10)),
    ("4", dict(dates="2010.01.01:2024.12.31", oos="none")),
]


@pytest.mark.parametrize("task,kw", REJILLA)
def test_t16a_el_modelo_coincide_con_el_texto_nuevo(pdir, task, kw):
    plan = CE.plan_edit("proj", [task], CE.Changes(**kw))         # ya verifica en memoria
    tp = plan.tasks[0]
    assert tp.new_bytes != tp.orig_bytes
    assert CE._tree_diff(CE._model(tp.orig_bytes, tp.edits), ET.fromstring(tp.new_bytes)) is None
    assert CE.read_task(tp.new_bytes) == CE._expected(tp.old, tp.edits)


def _parcheado(monkeypatch, capsys, pdir, escritor):
    monkeypatch.setattr(CE, "_w_window", escritor)
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2016.01.01:2019.12.31", "--apply")
    assert rc == 3, err
    sin_efectos(pdir, s0)
    return err


def test_t16b_escritor_que_toca_otro_atributo_falla(pdir, capsys, monkeypatch):
    real = CE._w_window
    err = _parcheado(monkeypatch, capsys, pdir,
                     lambda t, a, b: real(t, a, b).replace('xdateFrom="1999.01.01"', 'xdateFrom="1998.01.01"'))
    assert "xdateFrom" in err


def test_t16c_escritor_anclado_en_el_setup_senuelo_del_cross_check_falla(pdir, capsys, monkeypatch):
    def mal(text, a, b):       # escribe la ventana en el <Setup> del cross check, no en el principal
        return text.replace('<Setup dateFrom="2012.03.01" dateTo="2018.06.30">',
                            f'<Setup dateFrom="{a}" dateTo="{b}">')
    err = _parcheado(monkeypatch, capsys, pdir, mal)
    assert "se lee tras escribir" in err or "cambió algo más" in err


def test_t16d_escritor_que_mete_un_espacio_en_un_texto_ajeno_falla(pdir, capsys, monkeypatch):
    real = CE._w_window
    err = _parcheado(monkeypatch, capsys, pdir,
                     lambda t, a, b: real(t, a, b).replace("</Rankings>", " </Rankings>"))
    assert "cambió algo más" in err


# ----------------------------- T17 --------------------------------------------------------------------------

def test_t17_lector_que_devuelve_otro_valor_falla(pdir, capsys, monkeypatch):
    real = CE.read_task

    def mentiroso(b):
        d = real(b)
        if d["window"] and d["window"]["from"] == "2016.01.01":
            d["window"]["from"] = "1999.01.01"
        return d
    monkeypatch.setattr(CE, "read_task", mentiroso)
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2016.01.01:2019.12.31", "--apply")
    assert rc == 3 and "window" in err
    sin_efectos(pdir, s0)


# ----------------------------- T18 --------------------------------------------------------------------------

def test_t18_varias_tasks_una_escritura_un_backup(pdir, capsys):
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--task", "Build antiguo",
                      "--dates", "2012.01.01:2021.01.01", "--oos-last-pct", "30", "--apply")
    assert rc == 0, err
    assert len(list(pdir.glob("project.cfx.bak_*"))) == 1
    for n in ("Build-Task1.xml", "Build-Task2.xml"):
        assert CE.read_task(member(pdir, n))["window"] == {"from": "2012.01.01", "to": "2021.01.01"}


def test_t18_misma_task_dos_veces_cuenta_una(pdir):
    plan = CE.plan_edit("proj", ["0", "Build principal", "Build-Task1.xml", "0"], CE.Changes(max_strategies=900))
    assert [t.idx for t in plan.tasks] == [0]


def test_t18_selector_ambiguo_lista_candidatos(pdir, capsys):
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", SYM_DUP, "--oos", "none", "--apply")
    assert rc == 1 and "ambiguo" in err and "Retest-Task1.xml" in err and "Retest-Task2.xml" in err
    sin_efectos(pdir, s0)


def test_t18_selectores_inexistentes(pdir, capsys):
    assert go(capsys, "proj", "set", "--task", "99", "--oos", "none")[0] == 1
    rc, _, err = go(capsys, "proj", "set", "--task", "No existe", "--oos", "none")
    assert rc == 1 and "Build principal" in err


def test_t18_un_fallo_en_una_task_no_escribe_ninguna(pdir, capsys):
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--task", "1", "--dates", "2016.01.01:2019.12.31", "--apply")
    assert rc == 1 and "Filtro" in err
    sin_efectos(pdir, s0)


# ----------------------------- T19 --------------------------------------------------------------------------

def test_t19_crlf_no_gana_lf_sueltos(pdir, capsys):
    assert go(capsys, "proj", "set", "--task", "0", "--oos", "2019.01.01:2020.12.31", "--apply")[0] == 0
    nuevo = member(pdir, "Build-Task1.xml")
    assert nuevo.count(b"\n") == nuevo.count(b"\r\n")


def test_t19_lf_sigue_lf(pdir, capsys):
    assert go(capsys, "proj", "set", "--task", "2", "--oos-last-pct", "30", "--apply")[0] == 0
    nuevo = member(pdir, "Retest-Task1.xml")
    assert b"\r" not in nuevo


def test_t19_finales_mezclados_conservan_sus_recuentos(tmp_path, capsys, monkeypatch):
    # fichero CRLF cuyo bloque OutOfSample está en una zona LF y con otras líneas LF sueltas más abajo
    mix = BUILD1.replace('</Setups>\r\n    <OutOfSample showGraph="true" />\r\n',
                         '</Setups>\n    <OutOfSample showGraph="true" />\r\n')
    mix = mix.replace("</CrossChecks>\r\n", "</CrossChecks>\n")
    assert mix.count("\n") != mix.count("\r\n")
    d = make_project(tmp_path / "m", "mix", Build__Task1_xml=mix)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "m"))
    antes = member(d, "Build-Task1.xml").decode()
    rc, _, err = go(capsys, "mix", "set", "--task", "0", "--oos", "2019.01.01:2020.12.31", "--apply")
    assert rc == 0, err
    despues = member(d, "Build-Task1.xml").decode()
    assert CE._eol_counts(CE._sin_oos(antes)) == CE._eol_counts(CE._sin_oos(despues))
    # el contexto local del bloque es LF: el bloque reconstruido usa solo LF
    m = re.search(r"<OutOfSample.*</OutOfSample>", despues, re.S)
    assert "\r" not in m.group(0) and m.group(0).count("\n") == 2


# ----------------------------- T20 --------------------------------------------------------------------------

def test_t20_task_sin_data_rechaza_dates_y_oos(pdir, capsys):
    s0 = sha(pdir)
    for args in (["--dates", "2016.01.01:2019.12.31"], ["--oos", "none"], ["--oos-last-pct", "30"]):
        rc, _, err = go(capsys, "proj", "set", "--task", "1", *args, "--apply")
        assert rc == 1 and "no tiene ventana" in err, args
    sin_efectos(pdir, s0)


# ----------------------------- T21 --------------------------------------------------------------------------

def test_t21_ningun_proceso_arrancado(pdir, capsys, monkeypatch):
    assert go(capsys, "proj", "show")[0] == 0
    assert go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")[0] == 0
    assert go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500")[0] == 0
    assert monkeypatch.popen_llamadas == []


# ----------------------------- T22 --------------------------------------------------------------------------

def test_t22_cambio_externo_entre_verificacion_y_backup(pdir, capsys, monkeypatch):
    real = CE.SP.pre_gui_check
    n = {"c": 0}

    def gancho(proj, *a, **k):
        n["c"] += 1
        if n["c"] == 2:          # segundo pre-check: otro proceso reescribe el .cfx
            write_cfx(pdir / "project.cfx", {**MEMBERS, "Retest-Task2.xml": (RETEST1 + "<!-- externo -->", zipfile.ZIP_DEFLATED)})
        return real(proj, *a, **k)
    monkeypatch.setattr(CE.SP, "pre_gui_check", gancho)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")
    assert rc == 1 and "cambió mientras se preparaba" in err
    assert b"externo" in member(pdir, "Retest-Task2.xml")           # el cambio externo se conserva
    assert not list(pdir.glob("project.cfx.bak_*")) and not list(pdir.glob("*.cfx_edit.*.tmp"))
    assert n["c"] == 2


# ----------------------------- T23 --------------------------------------------------------------------------

def test_t23_replace_que_deja_otros_bytes_da_rc4_con_el_backup(pdir, capsys, monkeypatch):
    real = CE.os.replace

    def replace_malo(src, dst):
        real(src, dst)
        Path(dst).write_bytes(b"otro escritor")
    monkeypatch.setattr(CE.os, "replace", replace_malo)
    orig = (pdir / "project.cfx").read_bytes()
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply", "--motivo", "r4")
    assert rc == 4
    bak = next(pdir.glob("project.cfx.bak_r4_*"))
    assert str(bak) in err and bak.read_bytes() == orig and "NO se restaura solo" in err
    assert (pdir / "project.cfx").read_bytes() == b"otro escritor"


# ----------------------------- T24 --------------------------------------------------------------------------

def test_t24_cross_check_fuera_de_la_ventana_avisa_pero_no_bloquea(pdir, capsys):
    s0 = sha(pdir)
    rc, out, err = go(capsys, "proj", "set", "--task", "0", "--dates", "2019.01.01:2020.12.31")
    assert rc == 0, err
    assert "AVISO" in out and "cross check de OTRO" in out and "2012.03.01–2018.06.30" in out
    sin_efectos(pdir, s0)
    # con una ventana que contiene al cross check no hay aviso
    rc, out, _ = go(capsys, "proj", "set", "--task", "0", "--dates", "2011.01.01:2020.12.31")
    assert rc == 0 and "AVISO" not in out


# ----------------------------- T25 --------------------------------------------------------------------------

def test_t25_prefijo_del_fichero_distinto_del_type_rechaza(tmp_path, capsys, monkeypatch):
    cfg = CONFIG.replace('<Task type="Retest" name="Retest A" title="Retest A" taskXMLFile="Retest-Task1.xml" />',
                         '<Task type="Retest" name="Retest A" title="Retest A" taskXMLFile="Build-Task1.xml" />')
    assert cfg != CONFIG
    d = make_project(tmp_path / "x", "malo", config_xml=cfg)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "x"))
    s0 = sha(d)
    rc, _, err = go(capsys, "malo", "set", "--task", "2", "--max-strategies", "100", "--apply")
    assert rc == 1 and "estructura no reconocida" in err
    sin_efectos(d, s0)


# ----------------------------- extra: escritor, lector, CLI ------------------------------------------------

def test_set_attr_nombre_exacto_y_una_sola_vez():
    tag = '<Setup dateFrom="a" dateTo="b" xdateFrom="c">'
    assert CE._set_attr(tag, "dateFrom", "Z") == '<Setup dateFrom="Z" dateTo="b" xdateFrom="c">'
    for t, n in ((tag, "noexiste"), ('<S a="1" a="2">', "a")):
        with pytest.raises(CE.Rejected):
            CE._set_attr(t, n, "v")


def test_lector_estructura_ambigua_rechaza_la_edicion(tmp_path, capsys, monkeypatch):
    doble = BUILD1.replace("</Settings>\r\n", "  <Data>\r\n  </Data>\r\n</Settings>\r\n")
    d = make_project(tmp_path / "a", "amb", Build__Task1_xml=doble)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "a"))
    s0 = sha(d)
    rc, _, err = go(capsys, "amb", "set", "--task", "0", "--dates", "2016.01.01:2019.12.31", "--apply")
    assert rc == 1 and "estructura no reconocida" in err
    sin_efectos(d, s0)
    assert go(capsys, "amb", "show")[0] == 0              # show no se rompe


def test_proyecto_inexistente_y_nombres_peligrosos(pdir, capsys):
    assert go(capsys, "no_existe", "show")[0] == 1
    assert go(capsys, "../proj", "show")[0] == 1
    assert go(capsys, "proj", "set", "--oos", "none")[0] == 2           # falta --task
    assert go(capsys, "proj", "set", "--task", "0")[0] == 2             # ningún cambio
    assert go(capsys, "proj", "set", "--task", "0", "--oos", "none", "--oos-last-pct", "30")[0] == 2


def test_ayuda_incluye_limites_y_redondeo(capsys):
    rc = CE.main(["-h"])
    out = capsys.readouterr().out
    assert rc == 0 and "NO son reglas de SQX" in out and "HACIA ABAJO" in out and "25000" in out
    rc = CE.main(["p", "set", "-h"])
    out = capsys.readouterr().out
    assert rc == 0 and "--oos-last-pct" in out and "hacia abajo" in out


def test_api_importable(pdir):
    assert CE.show("proj")[0]["label"] == "Build principal"
    plan = CE.plan_edit("proj", ["0"], CE.Changes(max_strategies=1500))
    bak = CE.apply(plan, "api")
    assert bak.name.startswith("project.cfx.bak_api_") and bak.is_file()
    assert CE.read_task(member(pdir, "Build-Task1.xml"))["rankings"]["max_strategies"] == "1500"


# ----------------------------- CR1: correcciones de la revisión del código ----------------------------------

def _oos_nuevo():
    return ["proj", "set", "--task", "0", "--oos", "2019.01.01:2020.12.31", "--apply"]


def test_cr1_c1_tree_diff_solo_perdona_blancos_en_el_oos():
    a = ET.fromstring("<Settings><Data><OutOfSample><Range/></OutOfSample></Data></Settings>")
    ok = ET.fromstring("<Settings><Data><OutOfSample>\n  <Range/>\n</OutOfSample></Data></Settings>")
    texto = ET.fromstring("<Settings><Data><OutOfSample>x<Range/></OutOfSample></Data></Settings>")
    cola = ET.fromstring("<Settings><Data><OutOfSample><Range/>x</OutOfSample></Data></Settings>")
    assert CE._tree_diff(a, ok) is None
    assert "texto no blanco" in CE._tree_diff(a, texto)
    assert "cola no blanca" in CE._tree_diff(a, cola)
    # un OOS original con texto no blanco tampoco se perdona aunque el nuevo sea blanco
    assert CE._tree_diff(texto, ok) is not None


def test_cr1_c6a_w_oos_con_texto_no_blanco_dentro_del_oos_falla(pdir, capsys, monkeypatch):
    real = CE._w_oos
    monkeypatch.setattr(CE, "_w_oos", lambda t, r: real(t, r).replace(
        '<OutOfSample showGraph="true">', '<OutOfSample showGraph="true">x', 1))
    s0 = sha(pdir)
    rc, _, err = go(capsys, *_oos_nuevo())
    assert rc == 3 and "texto no blanco" in err
    sin_efectos(pdir, s0)


def test_cr1_c6b_w_oos_con_texto_no_blanco_en_la_cola_de_un_range_falla(pdir, capsys, monkeypatch):
    real = CE._w_oos
    monkeypatch.setattr(CE, "_w_oos", lambda t, r: real(t, r).replace('\r\n    </OutOfSample>',
                                                                        'x\r\n    </OutOfSample>', 1))
    s0 = sha(pdir)
    rc, _, err = go(capsys, *_oos_nuevo())
    assert rc == 3 and "cola no blanca" in err
    sin_efectos(pdir, s0)


def test_cr1_c6c_w_fitness_que_cambia_un_crlf_por_lf_fuera_del_ranking_falla(pdir, capsys, monkeypatch):
    real = CE._w_fitness
    monkeypatch.setattr(CE, "_w_fitness", lambda b, m: real(b, m).replace("\r\n", "\n", 1))   # antes del <Ranking>
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--fitness", "ProfitFactor", "--apply")
    assert rc == 3 and "fines de línea" in err
    sin_efectos(pdir, s0)


def test_cr1_c6d_check_eol_con_finales_mezclados_recuentos_a_mano():
    assert CE._eol_counts("a\r\nb\nc\r\n") == (2, 1)
    assert CE._eol_counts("a\n\nb\r\n") == (1, 2)
    base = 'x\r\n<Data>\r\n  <Setups/>\n  <OutOfSample showGraph="true" />\r\n</Data>\ny\n'
    # sin cambios y con un OOS reconstruido en contexto LF (el bloque usa LF): bien
    assert CE._check_eol(base, base, False) is None
    lf = base.replace('<OutOfSample showGraph="true" />',
                      '<OutOfSample showGraph="true">\n    <Range dateFrom="a" dateTo="b" />\n  </OutOfSample>')
    assert CE._check_eol(base, lf, True) is None
    # el mismo bloque con CRLF en contexto LF: mal
    crlf = lf.replace("\n    <Range", "\r\n    <Range").replace("\n  </OutOfSample>", "\r\n  </OutOfSample>")
    assert "mezcla CRLF" in CE._check_eol(base, crlf, True)
    # un CRLF -> LF fuera del OOS (recuentos (3, 3) -> (2, 4) a mano): mal
    roto = base.replace("x\r\n", "x\n")
    assert CE._eol_counts(base) == (3, 3) and CE._eol_counts(roto) == (2, 4)
    assert "recuento" in CE._check_eol(base, roto, False)
    # un LF -> CRLF fuera del OOS: mal
    roto2 = base.replace("y\n", "y\r\n")
    assert "recuento" in CE._check_eol(base, roto2, False)


def test_cr1_c7_fitness_sobre_ranking_con_finales_mezclados_vale(tmp_path, capsys, monkeypatch):
    mix = BUILD1.replace('<Ranking type="Weighted">\r\n          <Goal use="true" type="ReturnDDRatio"',
                         '<Ranking type="Weighted">\n          <Goal use="true" type="ReturnDDRatio"')
    mix = mix.replace('weight="5" valueType="2" target="0" />\r\n', 'weight="5" valueType="2" target="0" />\n')
    assert mix.count("\r\n") == BUILD1.count("\r\n") - 2 and mix.count("\n") == BUILD1.count("\n")
    d = make_project(tmp_path / "m2", "mix2", Build__Task1_xml=mix)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "m2"))
    rc, _, err = go(capsys, "mix2", "set", "--task", "0", "--fitness", "ProfitFactor", "--apply")
    assert rc == 0, err
    nuevo = member(d, "Build-Task1.xml").decode()
    assert '<Ranking type="ProfitFactor" />' in nuevo and "<Goal" not in nuevo
    # fuera del Ranking los recuentos de finales de línea no cambian
    assert CE._eol_counts(CE._sin_oos(mix, True)) == CE._eol_counts(CE._sin_oos(nuevo, True))


def test_cr1_c2_oos_con_mas_de_un_range_rechaza_cualquier_cambio(tmp_path, capsys, monkeypatch):
    dos = RETEST1.replace('    </OutOfSample>',
                          '      <Range dateFrom="2018.01.01" dateTo="2018.06.01" />\n    </OutOfSample>')
    assert dos != RETEST1
    d = make_project(tmp_path / "r2", "dos", Retest__Task1_xml=dos)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "r2"))
    s0 = sha(d)
    for args in (["--oos", "2019.01.01:2020.01.01"], ["--oos", "none"], ["--oos-last-pct", "30"]):
        rc, _, err = go(capsys, "dos", "set", "--task", "2", *args, "--apply")
        assert rc == 1 and "estructura no reconocida" in err and "más de un Range" in err, args
    sin_efectos(d, s0)


@pytest.mark.parametrize("rango", ['<Range dateFrom="2019.06.01" />', '<Range dateTo="2020.12.31" />',
                                   '<Range dateFrom="2019.13.45" dateTo="2020.12.31" />',
                                   '<Range dateFrom="ayer" dateTo="2020.12.31" />'])
def test_cr1_c2_range_sin_fechas_validas_rechaza(tmp_path, capsys, monkeypatch, rango):
    roto = RETEST1.replace('<Range dateFrom="2019.06.01" dateTo="2020.12.31" />', rango)
    assert roto != RETEST1
    d = make_project(tmp_path / "r3", "roto", Retest__Task1_xml=roto)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "r3"))
    s0 = sha(d)
    rc, _, err = go(capsys, "roto", "set", "--task", "2", "--oos", "2019.01.01:2020.01.01", "--apply")
    assert rc == 1 and "estructura no reconocida" in err
    sin_efectos(d, s0)


def test_cr1_c3_copy2_que_falla_a_medias_borra_el_backup_parcial(pdir, capsys, monkeypatch):
    def copy_roto(src, dst, *a, **k):
        Path(dst).write_bytes(b"parcial")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(CE.shutil, "copy2", copy_roto)
    s0 = sha(pdir)
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")
    assert rc == 1 and "error de disco" in err and "Traceback" not in err
    sin_efectos(pdir, s0)


@pytest.mark.parametrize("n,rc_esperado", [(1, 1), (2, 1), (3, 1), (4, 4)])
def test_cr1_c3_oserror_al_calcular_sha(pdir, capsys, monkeypatch, n, rc_esperado):
    """Llamadas a _sha_file en apply: 1 temporal, 2 .cfx actual, 3 backup, 4 .cfx final."""
    real = CE._sha_file
    llamadas = {"n": 0}

    def sha_roto(p):
        llamadas["n"] += 1
        if llamadas["n"] == n:
            raise OSError(5, "Input/output error")
        return real(p)
    monkeypatch.setattr(CE, "_sha_file", sha_roto)
    s0 = sha(pdir)
    orig = (pdir / "project.cfx").read_bytes()
    rc, _, err = go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")
    assert rc == rc_esperado and "Traceback" not in err
    assert not list(pdir.glob("*.cfx_edit.*.tmp"))
    if rc_esperado == 1:
        assert sha(pdir) == s0
        for b in pdir.glob("project.cfx.bak_*"):      # solo puede quedar un backup completo
            assert b.read_bytes() == orig
    else:
        assert "NO se restaura solo" in err and next(pdir.glob("project.cfx.bak_*")).read_bytes() == orig


def test_cr1_c5_el_temporal_lleva_el_pid_y_no_queda(pdir, capsys, monkeypatch):
    nombres = []
    real = CE._build_zip

    def espia(tmp, plan):
        nombres.append(tmp.name)
        real(tmp, plan)
    monkeypatch.setattr(CE, "_build_zip", espia)
    assert go(capsys, "proj", "set", "--task", "0", "--max-strategies", "1500", "--apply")[0] == 0
    assert nombres == [f"project.cfx.cfx_edit.{os.getpid()}.tmp"]
    assert not list(pdir.glob("*.cfx_edit.*.tmp")) and not list(pdir.glob("*.tmp"))


def test_cr1_o1_main_test_dates_por_setup_del_cross_check(tmp_path, capsys, monkeypatch):
    dos = BUILD1.replace('<MainTestValues timeframe="H1" dates="true" />', '<MainTestValues timeframe="H1" dates="false" />')
    dos = dos.replace('        </Setups>\r\n      </Settings>\r\n    </RetestOnAdditionalMarkets>',
                      '          <Setup dateFrom="2013.01.01" dateTo="2017.01.01">\r\n'
                      '            <Chart symbol="OTRO" timeframe="H1" />\r\n'
                      '          </Setup>\r\n        </Setups>\r\n      </Settings>\r\n    </RetestOnAdditionalMarkets>')
    assert dos.count("<Setup ") == BUILD1.count("<Setup ") + 1
    make_project(tmp_path / "c", "cc", Build__Task1_xml=dos)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "c"))
    e = CE.show("cc", ["0"])[0]
    assert "main_test_dates" not in e["cross_checks"]
    assert [s["main_test_dates"] for s in e["cross_checks"]["setups"]] == ["false", None]
    assert go(capsys, "cc", "show", "--task", "0")[1].count("MainTestValues dates=") == 2


OPTIMIZE = f"""<Settings>
  <Data useFromPreviousTask="false">
    <Setups>
      <Setup dateFrom="2015.01.01" dateTo="2020.12.31">
        <Chart symbol="SYM" timeframe="H1" />
      </Setup>
    </Setups>
    <OutOfSample showGraph="true" />
  </Data>
  <Resources>
    <Symbols>
      <Symbol name="SYM" dateFrom="{B0}" dateTo="{B1}" />
    </Symbols>
  </Resources>
</Settings>
"""


def test_cr1_o2_task_optimize_rechaza_ventana_y_oos(tmp_path, capsys, monkeypatch):
    cfg = CONFIG.replace("</Tasks>", '<Task type="Optimize" name="Opt" taskXMLFile="Optimize-Task1.xml" />\n</Tasks>')
    d = make_project(tmp_path / "o", "opt", config_xml=cfg, Optimize__Task1_xml=OPTIMIZE)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "o"))
    assert CE.show("opt", ["5"])[0]["type"] == "Optimize"
    s0 = sha(d)
    for args in (["--dates", "2016.01.01:2019.12.31"], ["--oos", "none"], ["--oos-last-pct", "30"]):
        rc, _, err = go(capsys, "opt", "set", "--task", "5", *args, "--apply")
        assert rc == 1 and "cfx_edit solo cambia la ventana y el OOS de tasks Build y Retest" in err, args
    sin_efectos(d, s0)
