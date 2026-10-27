"""Tests de sqx/pass_matrix.py — matriz genérica estrategia × pruebas.

Correr desde la raíz del repo:
    python -m pytest tests/sqx/test_pass_matrix.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQX = ROOT / "sqx"
if str(SQX) not in sys.path:
    sys.path.insert(0, str(SQX))

import pass_matrix as PM  # noqa: E402


def _banco(dir_: Path, banco: str, filas: list[tuple[str, str]]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    cuerpo = "".join(f'"{n}";"{r}";"1.5"\n' for n, r in filas)
    (dir_ / f"databank_{banco}.csv").write_text(
        '"Strategy Name";"Filters result";"Fitness (IS)"\n' + cuerpo, encoding="utf-8-sig")


def test_pool_sin_estilos_clave_cruda(tmp_path):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", ""), ("B", ""), ("C", "")])
    _banco(d, "T1", [("A", "PASSED"), ("B", "FAILED"), ("C", "PASSED")])
    _banco(d, "T2", [("A", "PASSED"), ("B", "PASSED"), ("C", "FAILED")])

    rc = PM.main(["--dir", str(d), "-o", str(d / "m.csv")])
    assert rc == 0
    m = pd.read_csv(d / "m.csv", sep=";")
    assert len(m) == 3
    assert m.loc[m["strategy"] == "A", "score"].item() == 2
    assert m.loc[m["strategy"] == "B", "pass_T1"].item() is False
    assert m.loc[m["strategy"] == "C", "pass_T2"].item() is False
    # Fitness (IS) de Results se arrastra
    assert m["Fitness (IS)"].notna().all()


def test_pool_tf_mr_colision_clave_compuesta(tmp_path):
    d = tmp_path / "out"
    # cada build numera desde 0: los FullRange llevan el MISMO plano 'X' y en
    # Results/bancos de pruebas la copia MR aparece como 'X(1)' (convención casa)
    _banco(d, "FullRange_TF", [("X", "PASSED")])
    _banco(d, "FullRange_MR", [("X", "PASSED")])
    _banco(d, "Results", [("X", ""), ("X(1)", "")])
    _banco(d, "T1", [("X", "PASSED"), ("X(1)", "FAILED")])
    _banco(d, "T2", [("X", "FAILED"), ("X(1)", "PASSED")])

    rc = PM.main(["--dir", str(d), "-o", str(d / "m.csv"),
                  "--exclude", "FullRange_TF,FullRange_MR"])
    assert rc == 0
    m = pd.read_csv(d / "m.csv", sep=";").set_index("strategy")
    # convención notebook: la copia MR canoniza al nombre plano
    assert set(m["key"]) == {"TF|X", "MR|X"}
    assert m.loc["X(1)", "style"] == "MR"
    assert bool(m.loc["X(1)", "pass_T1"]) is False and bool(m.loc["X", "pass_T1"]) is True
    # score por defecto sobre las pruebas (FullRange excluido)
    assert m["score"].sum() == 2


def test_fail_loud_sin_results(tmp_path):
    d = tmp_path / "out"
    _banco(d, "T1", [("A", "PASSED")])
    with pytest.raises(SystemExit, match="Results"):
        PM.main(["--dir", str(d)])


def test_fail_loud_nombres_duplicados(tmp_path):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", ""), ("A", "")])
    with pytest.raises(SystemExit, match="duplicados"):
        PM.main(["--dir", str(d)])


def test_fail_loud_exclude_inexistente(tmp_path):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", "")])
    _banco(d, "T1", [("A", "PASSED")])
    with pytest.raises(SystemExit, match="inexistentes"):
        PM.main(["--dir", str(d), "--exclude", "NoExiste"])


def test_tasks_restringe_score(tmp_path):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", "")])
    _banco(d, "T1", [("A", "PASSED")])
    _banco(d, "T2", [("A", "FAILED")])

    rc = PM.main(["--dir", str(d), "-o", str(d / "m.csv"), "--tasks", "T1"])
    assert rc == 0
    m = pd.read_csv(d / "m.csv", sep=";")
    assert m["score"].item() == 1
    # T2 sigue en el CSV aunque no entre en el score
    assert "pass_T2" in m.columns


def test_ausentes_permitidos_con_aviso(tmp_path, capsys):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", ""), ("B", ""), ("C", "")])
    _banco(d, "T1", [("A", "PASSED"), ("B", "FAILED"), ("C", "PASSED")])
    _banco(d, "T2", [("A", "PASSED"), ("C", "FAILED")])   # B ausente (no llegó)

    rc = PM.main(["--dir", str(d), "-o", str(d / "m.csv")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "T2: 1 estrategias del roster ausentes" in out
    m = pd.read_csv(d / "m.csv", sep=";").set_index("strategy")
    assert pd.isna(m.loc["B", "pass_T2"])          # celda vacía, no False
    assert m.loc["B", "score"] == 0                # el ausente no cuenta
    assert m.loc["B", "n_absent"] == 1
    assert m.loc["A", "n_absent"] == 0 and m.loc["A", "score"] == 2


def test_strict_fail_loud_ausentes(tmp_path):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", ""), ("B", "")])
    _banco(d, "T1", [("A", "PASSED")])   # B ausente
    with pytest.raises(SystemExit, match="ausentes"):
        PM.main(["--dir", str(d), "--strict"])


def test_extras_ajenas_avisadas(tmp_path, capsys):
    d = tmp_path / "out"
    _banco(d, "Results", [("A", ""), ("B", "")])
    _banco(d, "T1", [("A", "PASSED"), ("B", "FAILED"), ("RESTO", "PASSED")])

    rc = PM.main(["--dir", str(d), "-o", str(d / "m.csv")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "T1: 1 estrategias ajenas al roster" in out
    m = pd.read_csv(d / "m.csv", sep=";")
    assert "RESTO" not in set(m["strategy"])
    assert m.loc[m["strategy"] == "A", "score"].item() == 1


def test_roster_flag_sin_results(tmp_path):
    d = tmp_path / "out"
    # sin databank_Results.csv: el roster lo pone Second_OOS
    _banco(d, "Second_OOS", [("A", "PASSED"), ("B", "FAILED")])
    _banco(d, "T1", [("A", "PASSED"), ("B", "PASSED")])

    rc = PM.main(["--dir", str(d), "-o", str(d / "m.csv"),
                  "--roster", "Second_OOS", "--exclude", "Second_OOS"])
    assert rc == 0
    m = pd.read_csv(d / "m.csv", sep=";")
    assert len(m) == 2
    assert m["score"].tolist() == [1, 1]
    # las métricas se arrastran del banco roster
    assert m["Fitness (IS)"].notna().all()