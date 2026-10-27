"""Tests de sqx/bank_plan.py (Fase 0 del plan ram-runner-integration).

Aceptación F0 del plan convertida en tests:
  - golden por task del proyecto intraday_nasdaq_future (tabla verificada a mano
    2026-10-06, §Fase 0 del plan). La fixture sintética de abajo REPLICA la
    evidencia extraída del .cfx real (los tests nunca leen el .cfx real);
  - `archive` nunca contiene inputs ni output;
  - input==output (update-in-place) excluido una vez;
  - lectura corrupta del cfx → excepción, nunca plan vacío silencioso (fail-loud,
    el parser NO hereda el `except Exception: pass` de builder_registry);
  - parser type-aware tolerante al orden de atributos;
  - CLI --list / --task-idx / --archive-list / --restore-list;
  - plan del driver: archive = declared − union(inputs ∪ outputs de tasks N..fin).
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

import sqx.bank_plan as bp
import sqx.gui_slim as gs

# Tabla golden (extraída del .cfx real de intraday_nasdaq_future, 2026-10-06):
# (label, type, inputs, output)
GOLDEN = [
    ("Build Long Trend Following", "Build", ["Initial population"], "Long_TF"),
    ("Build Mean Reversion Long", "Build",
     ["Initial population", "Strategies to improve"], "MR_Long"),
    ("Build Mean Reversion Short", "Build",
     ["Initial population", "Strategies to improve"], "MR_Short"),
    ("Long_TF - Results", "Filtering", ["Long_TF"], "Results"),
    ("MR_Long - Results", "Filtering", ["MR_Long"], "Results"),
    ("MR_short- Results", "Filtering", ["MR_Short"], "Results"),
    ("Second_OOS", "Retest", ["Results"], "Second_OOS"),
    ("Different Market and Tiemframes", "Retest", ["Second_OOS"],
     "Different_Markets_and_TimeFrames"),
    ("Retest High_Precission", "Retest", ["Different_Markets_and_TimeFrames"],
     "High_Precission"),
    ("MC_Execution_Realism", "Retest", ["HP_passed_todo"], "MC_Execution_Realism"),
    ("MC_Randomize_parameters", "Retest", ["MC_Execution_Realism"],
     "MC_Randomize_parameters"),
    ("Real_OOS", "Retest", ["Results"], "Real_OOS"),
]

DECLARED = [
    "Results", "Last generation", "Initial population", "Strategies to improve",
    "High_Precission", "MC_Randomize_parameters", "Second_OOS", "Simple_strategies",
    "Different_Markets_and_TimeFrames", "Real_OOS", "Existing portfolio",
    "MC_Execution_Realism", "Long_TF", "MR_Long", "Input PM", "MR_Short",
    "HP_passed_all", "HP_passed_todo",
]


def _db_tag(label: str, value: str, reverse_order: bool = False) -> str:
    if reverse_order:
        return f'<Databank value="{value}" name="X" label="{label}" />'
    return f'<Databank label="{label}" name="X" value="{value}" />'


def make_cfx(projdir: Path, tasks: list[tuple[str, str, list[str], str]],
             declared: list[str] | None = None,
             xml_extra: dict[str, str] | None = None) -> None:
    """Escribe un project.cfx sintético: config.xml + un XML por task."""
    declared = declared if declared is not None else DECLARED
    xml_extra = xml_extra or {}
    cfg_databanks = "".join(f'<Databank name="{b}" />' for b in declared)
    cfg_tasks, members = [], {}
    for i, (label, typ, inputs, output) in enumerate(tasks):
        xmlfile = f"Task-{i}.xml"
        if typ == "Build":
            dbs = _db_tag("Output databank", output)
            dbs += "".join(_db_tag("Input databank", b) for b in inputs)
        elif typ == "Retest":
            dbs = _db_tag("Output databank", output)
            dbs += "".join(_db_tag("Input databank", b) for b in inputs)
        else:  # Filtering: Source/Target, sin tags Input
            dbs = _db_tag("Source databank", inputs[0]) + _db_tag("Target databank", output)
        members[xmlfile] = f"<Task>{dbs}</Task>"
        cfg_tasks.append(
            f'<Task type="{typ}" name="n{i}" taskXMLFile="{xmlfile}" title="{label}" active="true" />')
    members["config.xml"] = f"<root>{cfg_databanks}{''.join(cfg_tasks)}</root>"
    members.update(xml_extra)
    projdir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(projdir / "project.cfx", "w") as z:
        for name, content in members.items():
            z.writestr(name, content)


@pytest.fixture()
def nasdaq_like(tmp_path: Path) -> Path:
    proj = tmp_path / "nasdaq_like"
    make_cfx(proj, GOLDEN)
    return proj


# ----------------------------- golden ------------------------------------------

@pytest.mark.parametrize("idx", range(len(GOLDEN)))
def test_golden_inputs_output(nasdaq_like: Path, idx: int) -> None:
    label, typ, inputs, output = GOLDEN[idx]
    plan = bp.build_plan_at(nasdaq_like, idx)
    assert plan["task"] == label
    assert plan["type"] == typ
    assert plan["inputs"] == inputs
    assert plan["output"] == output
    assert plan["declared"] == DECLARED


@pytest.mark.parametrize("idx", range(len(GOLDEN)))
def test_archive_nunca_contiene_inputs_ni_output(nasdaq_like: Path, idx: int) -> None:
    _, _, inputs, output = GOLDEN[idx]
    plan = bp.build_plan_at(nasdaq_like, idx)
    keep = set(inputs) | {output}
    assert not keep.intersection(plan["archive"])
    # el keep cubre lo que la task toca: archive = declared − keep
    assert set(plan["archive"]) == set(DECLARED) - keep


def test_golden_casos_del_plan(nasdaq_like: Path) -> None:
    """Los casos nombrados en la aceptación F0 del plan, uno a uno."""
    p0 = bp.build_plan_at(nasdaq_like, 0)
    assert p0["output"] == "Long_TF" and p0["inputs"] == ["Initial population"]
    p1 = bp.build_plan_at(nasdaq_like, 1)
    assert p1["output"] == "MR_Long" and len(p1["inputs"]) == 2
    p3 = bp.build_plan_at(nasdaq_like, 3)  # Filtering sin tag Input
    assert p3["type"] == "Filtering" and p3["inputs"] == ["Long_TF"]
    p6 = bp.build_plan_at(nasdaq_like, 6)  # "Retest strategies"
    assert p6["inputs"] == ["Results"] and p6["output"] == "Second_OOS"
    p11 = bp.build_plan_at(nasdaq_like, 11)  # "Retest4"
    assert p11["inputs"] == ["Results"] and p11["output"] == "Real_OOS"


# ----------------------------- reglas type-aware --------------------------------

def test_update_in_place_excluido_una_vez(tmp_path: Path) -> None:
    proj = tmp_path / "uip"
    make_cfx(proj, [("Self retest", "Retest", ["Results"], "Results")],
             declared=["Results", "Other"])
    plan = bp.build_plan_at(proj, 0)
    assert plan["inputs"] == []          # excluido del keep vía output
    assert plan["output"] == "Results"
    assert "Results" not in plan["archive"]  # y por tanto NO se archiva
    assert plan["archive"] == ["Other"]


def test_parser_tolerante_a_orden_de_atributos(tmp_path: Path) -> None:
    proj = tmp_path / "order"
    declared = ["InA", "OutB"]
    cfg = (f'<root><Databank name="InA" /><Databank name="OutB" />'
           f'<Task type="Retest" name="n" taskXMLFile="T.xml" title="T" active="true" /></root>')
    task = ("<Task>" + _db_tag("Output databank", "OutB", reverse_order=True)
            + _db_tag("Input databank", "InA", reverse_order=True) + "</Task>")
    proj.mkdir()
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        z.writestr("config.xml", cfg)
        z.writestr("T.xml", task)
    plan = bp.build_plan_at(proj, 0)
    assert plan["inputs"] == ["InA"] and plan["output"] == "OutB"


@pytest.mark.parametrize("n_inputs", [0, 2])
def test_retest_fail_loud_con_0_o_2_inputs(tmp_path: Path, n_inputs: int) -> None:
    proj = tmp_path / f"retest{n_inputs}"
    declared = ["In1", "In2", "Out"]
    ins = ["In1", "In2"][:n_inputs]
    make_cfx(proj, [("R", "Retest", ins, "Out")], declared=declared)
    with pytest.raises(bp.BankPlanError, match="Input databank"):
        bp.build_plan_at(proj, 0)


def test_output_no_resuelto_fail_loud(tmp_path: Path) -> None:
    proj = tmp_path / "noout"
    declared = ["InA"]
    cfg = ('<root><Databank name="InA" />'
           '<Task type="Retest" name="n" taskXMLFile="T.xml" title="T" active="true" /></root>')
    proj.mkdir()
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        z.writestr("config.xml", cfg)
        z.writestr("T.xml", "<Task>" + _db_tag("Input databank", "InA") + "</Task>")
    with pytest.raises(bp.BankPlanError, match="Output databank"):
        bp.build_plan_at(proj, 0)


def test_input_no_declarado_fail_loud_L1(tmp_path: Path) -> None:
    proj = tmp_path / "undeclared"
    # el input "Fantasma" no está en config.xml → invisible para el motor (L1)
    make_cfx(proj, [("R", "Retest", ["Fantasma"], "Out")], declared=["Out"])
    with pytest.raises(bp.BankPlanError, match="Fantasma.*no declarado"):
        bp.build_plan_at(proj, 0)


def test_build_con_last_generation_como_segundo_output(tmp_path: Path) -> None:
    """Build de la plantilla de la GUI con 2 Output: el principal es el propio y
    "Last generation" (auxiliar del genético) se conserva, nunca se archiva."""
    proj = tmp_path / "build_lastgen"
    declared = ["Results", "Last generation", "B_H4", "B_H1"]
    cfg = ('<root>' + "".join(f'<Databank name="{b}" />' for b in declared)
           + '<Task type="Build" name="a" taskXMLFile="A.xml" title="B_H4" active="true" />'
           + '<Task type="Build" name="b" taskXMLFile="B.xml" title="B_H1" active="true" />'
           + '</root>')
    proj.mkdir()
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        z.writestr("config.xml", cfg)
        for xf, out in (("A.xml", "B_H4"), ("B.xml", "B_H1")):
            z.writestr(xf, "<Task>" + _db_tag("Output databank", out)
                       + _db_tag("Output databank", "Last generation") + "</Task>")
    plan = bp.build_plan_at(proj, 1)
    assert plan["output"] == "B_H1"
    assert "Last generation" in plan["inputs"]


def test_build_con_dos_outputs_sin_last_generation_sigue_fallando(tmp_path: Path) -> None:
    proj = tmp_path / "build_2out"
    cfg = ('<root><Databank name="X" /><Databank name="Y" />'
           '<Task type="Build" name="a" taskXMLFile="A.xml" title="b" active="true" /></root>')
    proj.mkdir()
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        z.writestr("config.xml", cfg)
        z.writestr("A.xml", "<Task>" + _db_tag("Output databank", "X")
                   + _db_tag("Output databank", "Y") + "</Task>")
    with pytest.raises(bp.BankPlanError, match="esperado 1"):
        bp.build_plan_at(proj, 0)


def test_cfx_corrupto_fail_loud(tmp_path: Path) -> None:
    proj = tmp_path / "corrupto"
    proj.mkdir()
    (proj / "project.cfx").write_bytes(b"no es un zip")
    with pytest.raises(bp.BankPlanError):
        bp.build_plan_at(proj, 0)


def test_task_xml_ausente_fail_loud(tmp_path: Path) -> None:
    proj = tmp_path / "missingxml"
    make_cfx(proj, [("R", "Retest", ["InA"], "Out")], declared=["InA", "Out"])
    # romper la referencia: reescribir el cfx sin el task xml
    with zipfile.ZipFile(proj / "project.cfx") as z:
        members = {n: z.read(n) for n in z.namelist() if n != "Task-0.xml"}
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        for n, content in members.items():
            z.writestr(n, content)
    with pytest.raises(bp.BankPlanError, match="no está en el .cfx"):
        bp.build_plan_at(proj, 0)


def test_filtering_sin_source_usa_output_de_activa_anterior(tmp_path: Path) -> None:
    """Fallback de la regla F0 del plan: Filtering sin tag Source → input = output
    de la task activa anterior en el orden del pipeline."""
    proj = tmp_path / "filt_fallback"
    declared = ["A", "B", "Out"]
    cfg = (f'<root><Databank name="A" /><Databank name="B" /><Databank name="Out" />'
           f'<Task type="Build" name="b" taskXMLFile="B.xml" title="Build B" active="true" />'
           f'<Task type="Filtering" name="f" taskXMLFile="F.xml" title="Filtra" active="true" />'
           f'</root>')
    proj.mkdir()
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        z.writestr("config.xml", cfg)
        z.writestr("B.xml", "<Task>" + _db_tag("Output databank", "B") + "</Task>")
        # Filtering SIN Source: solo Target
        z.writestr("F.xml", "<Task>" + _db_tag("Target databank", "Out") + "</Task>")
    plan = bp.build_plan_at(proj, 1)
    assert plan["inputs"] == ["B"] and plan["output"] == "Out"


def test_filtering_con_source_lo_usa_aunque_la_anterior_inmediata_sea_otra(tmp_path: Path) -> None:
    """Evidencia real: 3 Builds seguidas y luego 3 Filterings; el Source de cada
    Filtering NO es el output de su anterior inmediata."""
    proj = tmp_path / "filt_source"
    declared = ["X", "Y", "Res"]
    cfg = (f'<root><Databank name="X" /><Databank name="Y" /><Databank name="Res" />'
           f'<Task type="Build" name="b0" taskXMLFile="X.xml" title="Build X" active="true" />'
           f'<Task type="Build" name="b1" taskXMLFile="Y.xml" title="Build Y" active="true" />'
           f'<Task type="Filtering" name="f" taskXMLFile="F.xml" title="Filtra X" active="true" />'
           f'</root>')
    proj.mkdir()
    with zipfile.ZipFile(proj / "project.cfx", "w") as z:
        z.writestr("config.xml", cfg)
        z.writestr("X.xml", "<Task>" + _db_tag("Output databank", "X") + "</Task>")
        z.writestr("Y.xml", "<Task>" + _db_tag("Output databank", "Y") + "</Task>")
        z.writestr("F.xml", "<Task>" + _db_tag("Source databank", "X")
                   + _db_tag("Target databank", "Res") + "</Task>")
    plan = bp.build_plan_at(proj, 2)
    assert plan["inputs"] == ["X"]   # el Source real, no Y (anterior inmediata)
    assert plan["output"] == "Res"


# ----------------------------- plan del driver ----------------------------------

def test_driver_plan_archive_excluye_union_inputs_y_outputs(nasdaq_like: Path) -> None:
    """archive = declared − union(inputs ∪ outputs de tasks N..fin)."""
    for first_idx in (0, 3, 6, 9, 11):
        plan = bp.driver_plan_at(nasdaq_like, first_idx)
        keep: set[str] = set()
        for _, _, inputs, output in GOLDEN[first_idx:]:
            keep.update(inputs)
            keep.add(output)
        assert set(plan["archive"]) == set(DECLARED) - keep
        # los inputs de la task actual nunca se archivan (L3)
        assert not set(GOLDEN[first_idx][2]).intersection(plan["archive"])
        assert GOLDEN[first_idx][3] not in plan["archive"]


def test_present_solo_bancos_con_ficheros(tmp_path: Path) -> None:
    proj = tmp_path / "present"
    make_cfx(proj, [("R", "Retest", ["InA"], "Out")], declared=["InA", "Out", "Empty"])
    (proj / "databanks" / "InA").mkdir(parents=True)
    (proj / "databanks" / "InA" / "a.sqx").write_bytes(b"x")
    (proj / "databanks" / "Out").mkdir(parents=True)      # dir vacío (L2) → no presente
    plan = bp.build_plan_at(proj, 0)
    assert plan["present"] == ["InA"]


# ----------------------------- CLI ----------------------------------------------

def test_cli_list(capsys: pytest.CaptureFixture, nasdaq_like: Path) -> None:
    assert bp.main([str(nasdaq_like), "--list"]) == 0
    out = capsys.readouterr().out
    assert "Build Long Trend Following" in out
    assert "MC_Execution_Realism" in out
    assert "in=HP_passed_todo" in out.replace(" ", "")


def test_cli_task_idx_json(capsys: pytest.CaptureFixture, nasdaq_like: Path) -> None:
    assert bp.main([str(nasdaq_like), "--task-idx", "9"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["task"] == "MC_Execution_Realism"
    assert plan["inputs"] == ["HP_passed_todo"]


def test_cli_archive_y_restore_list(capsys: pytest.CaptureFixture,
                                    nasdaq_like: Path) -> None:
    # --archive-list filtra por presencia en disco (archive ∩ present): damos
    # ficheros a dos bancos archivables; ni el input ni el output de la task salen
    for bank in ("Simple_strategies", "Last generation", "HP_passed_todo"):
        d = nasdaq_like / "databanks" / bank
        d.mkdir(parents=True)
        (d / "x.sqx").write_bytes(b"x")
    assert bp.main([str(nasdaq_like), "--task-idx", "9", "--archive-list"]) == 0
    arch = capsys.readouterr().out.splitlines()
    assert "HP_passed_todo" not in arch and "MC_Execution_Realism" not in arch
    assert "Simple_strategies" in arch and "Last generation" in arch

    capsys.readouterr()
    assert bp.main([str(nasdaq_like), "--task-idx", "9", "--restore-list"]) == 0
    assert capsys.readouterr().out.split() == ["HP_passed_todo"]


def test_cli_error_exit_1(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    proj = tmp_path / "bad"
    proj.mkdir()
    (proj / "project.cfx").write_bytes(b"basura")
    assert bp.main([str(proj), "--task-idx", "0"]) == 1
    assert "ERROR" in capsys.readouterr().err


# ----------------------------- marcador + ciclo slim ----------------------------

def _fill_banks(proj: Path, banks: dict[str, list[str]]) -> None:
    for bank, files in banks.items():
        d = proj / "databanks" / bank
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_bytes(b"sqx")


def test_marker_roundtrip(tmp_path: Path) -> None:
    proj = tmp_path / "m"
    proj.mkdir()
    assert bp.marker_read(proj) is None
    bp.marker_write(proj, {"run_id": "r1", "pid_driver": 1, "banks": ["A", "B"]})
    assert bp.marker_banks(proj) == ["A", "B"]
    bp.marker_clear(proj)
    assert bp.marker_read(proj) is None


def test_slim_archive_y_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ciclo mínimo: slim_archive archiva solo lo planeado ∩ present y deja
    marcador coherente; slim_restore restaura 1:1 y borra el marcador."""
    monkeypatch.setattr(bp.gui_slim, "live_processes", lambda: [])
    proj = tmp_path / "cycle"
    make_cfx(proj, [("R", "Retest", ["InA"], "Out")], declared=["InA", "Out", "ExtraC"])
    _fill_banks(proj, {"InA": ["a.sqx"], "Out": ["b.sqx"], "ExtraC": ["c.sqx", "d.sqx"]})

    done = bp.slim_archive(proj, 0, "run1", pid=123456)
    assert done == ["ExtraC"]
    assert bp.marker_banks(proj) == ["ExtraC"]
    assert not (proj / "databanks" / "ExtraC").exists()
    assert (proj / "databanks_archive" / "ExtraC" / "c.sqx").exists()
    assert bp.present_banks(proj) == ["InA", "Out"]

    rep = bp.slim_restore(proj)
    assert rep["restored"] == ["ExtraC"]
    assert rep["cleared"] is True
    assert bp.marker_read(proj) is None
    assert sorted(p.name for p in (proj / "databanks").iterdir()) == ["ExtraC", "InA", "Out"]
    assert (proj / "databanks" / "ExtraC" / "d.sqx").exists()


def test_slim_restore_presence_aware_e_idempotente(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """- banco ya presente → skip (no aborta por 'destino existe');
    - banco del marcador sin nada en archive → skip;
    - segunda corrida completa: misma corrección, mismo estado final."""
    monkeypatch.setattr(bp.gui_slim, "live_processes", lambda: [])
    proj = tmp_path / "pa"
    make_cfx(proj, [("R", "Retest", ["InA"], "Out")], declared=["InA", "Out", "ExtraC"])
    _fill_banks(proj, {"InA": ["a.sqx"], "Out": ["b.sqx"], "ExtraC": ["c.sqx"]})
    bp.slim_archive(proj, 0, "run1", pid=123456)

    # alguien devuelve ExtraC a mano (dir con ficheros) antes del restore
    (proj / "databanks_archive" / "ExtraC").rename(proj / "databanks" / "ExtraC")
    rep = bp.slim_restore(proj)
    assert rep["restored"] == [] and "ExtraC" in rep["skipped"]
    assert rep["cleared"] is True

    # segunda corrida slim completa: vuelve a archivar y a restaurar sin errores
    done = bp.slim_archive(proj, 0, "run2", pid=123457)
    assert done == ["ExtraC"]
    assert bp.marker_banks(proj) == ["ExtraC"]
    rep2 = bp.slim_restore(proj)
    assert rep2["restored"] == ["ExtraC"] and rep2["cleared"] is True
    assert (proj / "databanks" / "ExtraC" / "c.sqx").exists()
    assert bp.marker_read(proj) is None


def test_slim_restore_inputs_L3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El driver restaura antes del motor los inputs de SU task si quedaron
    archivados en una iteración anterior de la cadena (punto 1, L3 por task)."""
    monkeypatch.setattr(bp.gui_slim, "live_processes", lambda: [])
    proj = tmp_path / "l3"
    make_cfx(proj, [("T1", "Build", ["InA"], "MidM"),
                    ("T2", "Retest", ["MidM"], "Out")],
             declared=["InA", "MidM", "Out", "ExtraC"])
    _fill_banks(proj, {"InA": ["a.sqx"], "MidM": ["m.sqx"], "Out": ["b.sqx"],
                       "ExtraC": ["c.sqx"]})
    # iteración 1 archiva ExtraC (y sólo eso: MidM es output de T1 → preservado)
    assert bp.slim_archive(proj, 0, "run1", pid=123456) == ["ExtraC"]
    # forzar el estado "input de la task 2 archivado" (guarda defensiva del driver)
    bp.archive_banks(proj, ["MidM"])
    bp.marker_write(proj, {"run_id": "run1", "pid_driver": 123456,
                           "banks": ["ExtraC", "MidM"]})
    rep = bp.slim_restore_inputs(proj, 1)
    assert rep["restored"] == ["MidM"]
    assert bp.marker_banks(proj) == ["ExtraC"]     # sigue archivado lo demás
    assert (proj / "databanks" / "MidM" / "m.sqx").exists()


def test_reconcile_restaura_y_borra_marcador(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bp.gui_slim, "live_processes", lambda: [])
    proj = tmp_path / "rec"
    make_cfx(proj, [("R", "Retest", ["InA"], "Out")], declared=["InA", "Out", "ExtraC"])
    _fill_banks(proj, {"InA": ["a.sqx"], "Out": ["b.sqx"], "ExtraC": ["c.sqx"]})
    bp.slim_archive(proj, 0, "run1", pid=123456)

    rep = bp.reconcile(proj)
    assert rep["restored"] == ["ExtraC"] and rep["cleared"] is True
    assert (proj / "databanks" / "ExtraC" / "c.sqx").exists()
    assert bp.marker_read(proj) is None


def test_reconcile_if_dead_respeta_pid_vivo(tmp_path: Path) -> None:
    proj = tmp_path / "live"
    proj.mkdir()
    bp.marker_write(proj, {"run_id": "r", "pid_driver": os.getpid(), "banks": ["A"]})
    rep = bp.reconcile_if_dead(proj)
    assert rep["action"] == "live"
    assert bp.marker_banks(proj) == ["A"]   # no tocó nada


def test_reconcile_reanudable_si_muere_a_medias(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """(criterio F1c) muerte DURANTE la reconciliación → el siguiente intento la
    reanuda sin abortar (idempotencia): un banco ya restaurado se salta."""
    monkeypatch.setattr(bp.gui_slim, "live_processes", lambda: [])
    proj = tmp_path / "halfrec"
    make_cfx(proj, [("R", "Retest", ["InA"], "Out")],
             declared=["InA", "Out", "B1", "B2"])
    _fill_banks(proj, {"InA": ["a.sqx"], "Out": ["b.sqx"], "B1": ["1.sqx"], "B2": ["2.sqx"]})
    bp.slim_archive(proj, 0, "run1", pid=123456)
    # re-archivar B1 a mano para simular una reconciliación anterior a medias:
    # B1 ya vuelto, B2 aún archivado, marcador con ambos
    (proj / "databanks_archive" / "B1").rename(proj / "databanks" / "B1")
    bp.marker_write(proj, {"run_id": "run1", "pid_driver": 123456, "banks": ["B1", "B2"]})

    rep = bp.reconcile(proj)
    assert rep["restored"] == ["B2"] and "B1" in rep["skipped"]
    assert (proj / "databanks" / "B1" / "1.sqx").exists()
    assert (proj / "databanks" / "B2" / "2.sqx").exists()
