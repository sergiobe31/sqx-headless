"""Tests de sqx/builder_registry.py — de momento, task_output_databanks().

Helper compartido (2026-09-27) del monitor (Estado en vivo) y de
run_pipeline_per_task.sh (validación H11 del Output poblado): cruza config.xml
con el "Output databank" de cada *-TaskN.xml, para Build/Retest/Filtering,
indexado por title Y por name (el log usa ambos según la task).

Correr desde la raíz del repo:
    ~/miniconda3/envs/robotrader/bin/python -m pytest tests/sqx/test_builder_registry.py -v
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SQX = ROOT / "sqx"
if str(SQX) not in sys.path:
    sys.path.insert(0, str(SQX))

import builder_registry as REG  # noqa: E402


def _make_cfx(proj_dir: Path) -> None:
    config = (
        '<root>'
        '<Task type="Build" name="Build strategies" title="Build Long TF" '
        'taskXMLFile="Build-Task1.xml" active="true"/>'
        '<Task type="Retest" name="Retest strategies 5" title="Retest MC Rand" '
        'taskXMLFile="Retest-Task5.xml" active="true"/>'
        '<Task type="Filtering" name="Filter strategies" title="Long_TF - Results" '
        'taskXMLFile="Filtering-Task1.xml" active="true"/>'
        '</root>')
    build = '<Task><Databank label="Output databank" value="Long_TF"/></Task>'
    retest = '<Task><Databank label="Output databank" value="MC_Randomize_parameters"/></Task>'
    filt = '<Task><Databank label="Output databank" value="Results"/></Task>'
    with zipfile.ZipFile(proj_dir / "project.cfx", "w") as z:
        z.writestr("config.xml", config)
        z.writestr("Build-Task1.xml", build)
        z.writestr("Retest-Task5.xml", retest)
        z.writestr("Filtering-Task1.xml", filt)


def test_task_output_databanks_all_types_title_and_name(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    _make_cfx(proj_dir)
    monkeypatch.setattr(REG, "PROJECTS", tmp_path)
    outs = REG.task_output_databanks("proj_x")
    assert outs["Build Long TF"] == "Long_TF"
    assert outs["Build strategies"] == "Long_TF"
    assert outs["Retest MC Rand"] == "MC_Randomize_parameters"
    assert outs["Retest strategies 5"] == "MC_Randomize_parameters"
    assert outs["Long_TF - Results"] == "Results"
    assert outs["Filter strategies"] == "Results"


def test_task_output_databanks_missing_cfx_returns_empty(tmp_path, monkeypatch):
    (tmp_path / "proj_x").mkdir()
    monkeypatch.setattr(REG, "PROJECTS", tmp_path)
    assert REG.task_output_databanks("proj_x") == {}
