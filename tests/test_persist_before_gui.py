"""Tests de la Fase 2 del guardrail `sqx-headless-fallos` (2026-10-21), F2-j (R4, decisión D1 de Sergio):
`sqx/persist_before_gui.py` YA NO escribe nada: no arranca `sqcli`, no sincroniza; solo COMPRUEBA con
`stop_and_persist.pre_gui_check` (global, solo lectura) si es seguro abrir la GUI.

  F2-j  con un SQCLI falso que deja un testigo si se ejecuta → sin testigo (nunca se arranca el motor) ... test_f2j_nunca_*
        rc 1 con motivo para cada uno: sqcli falso vivo, orquestador falso de OTRO proyecto, driver falso de
        otro proyecto, cascada falsa, marcador slim, bancos archivados, .bak ..................... test_f2j_inseguro_*
        rc 0 en limpio con conteos ................................................................ test_f2j_limpio_*
        sin argumento → rc 2 (y ya no hay proyecto por defecto) ................................... test_f2j_sin_argumento_*
        docstring nuevo (qué cambió y por qué) ................................................... test_f2j_docstring_*

Los casos "inseguro" corren como SUBPROCESO real (la CLI de verdad) y son deterministas aunque la máquina tenga
cosas vivas; el caso "limpio" corre en proceso con las sondas globales parcheadas (la GUI de SQX del usuario puede
estar abierta en la máquina de desarrollo). Procesos falsos de nombre único: jamás se ve ni se toca un sqcli real.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "sqx"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
if str(ROOT / "tests") not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx
    sys.path.append(str(ROOT / "tests"))

import persist_before_gui as PBG  # noqa: E402
from test_slim_hooks import (  # noqa: E402  infraestructura de procesos falsos
    fake_engine, fake_launcher, make_proj, procs, unique_engine_name,
)

SCRIPT = ROOT / "sqx" / "persist_before_gui.py"


@pytest.fixture(autouse=True)
def _nunca_el_sqcli_real(monkeypatch, tmp_path):
    """SQCLI y SQX_APP a rutas inexistentes; nombre de motor único: ningún test ve ni arranca el sqcli real."""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", unique_engine_name())


@pytest.fixture()
def proyecto(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    name = f"pbg{uuid4().hex[:6]}"
    d = make_proj(projects, name, [("T1", "Retest", ["InA"], "Out")], ["InA", "Out"])
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    return name, d


def cli(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)


def sondas_limpias(monkeypatch):
    for fn in ("engine_pids", "gui_pids", "orchestrator_pids", "driver_pids", "cascade_pids"):
        monkeypatch.setattr(PBG.SP, fn, lambda *a, **k: [])


# ----------------------------- F2-j: nunca arranca el motor --------------------------------------

def _sqcli_con_testigo(tmp_path: Path) -> tuple[Path, Path]:
    testigo = tmp_path / "TESTIGO_sqcli_ejecutado"
    fake = tmp_path / "sqcli_falso.sh"
    fake.write_text(f'#!/bin/bash\necho "$@" > "{testigo}"\n')
    fake.chmod(0o755)
    return fake, testigo


def test_f2j_nunca_arranca_el_sqcli_en_proceso_ni_lanza_ningun_proceso(tmp_path, monkeypatch, proyecto):
    """Con el motor MUERTO (el caso en que el script antiguo lanzaba `sqcli -run file=` con save+synctofiles+count)
    ahora no se ejecuta nada: ni el SQCLI falso (testigo) ni ningún subproceso (Popen prohibido)."""
    name, _ = proyecto
    fake, testigo = _sqcli_con_testigo(tmp_path)
    monkeypatch.setenv("SQCLI", str(fake))
    sondas_limpias(monkeypatch)

    def prohibido(*a, **k):
        raise AssertionError("persist_before_gui no debe lanzar ningún proceso")

    monkeypatch.setattr(subprocess, "Popen", prohibido)
    assert PBG.main([name]) == 0
    assert not testigo.exists()


def test_f2j_nunca_arranca_el_sqcli_como_cli_real(tmp_path, proyecto):
    name, _ = proyecto
    fake, testigo = _sqcli_con_testigo(tmp_path)
    r = cli(name, extra_env={"SQCLI": str(fake)})
    assert r.returncode in (0, 1), r.stdout + r.stderr          # (0/1 según lo que haya vivo en esta máquina)
    assert not testigo.exists(), "el script NO puede ejecutar el sqcli: el testigo apareció"
    assert "sqcli -run" not in r.stdout


def test_f2j_el_codigo_ya_no_contiene_el_arranque_ciego():
    src = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]                               # sin el docstring
    for prohibido in ('"-run"', "subprocess", "synctofiles", "action=save", "SQCLI", "tempfile"):
        assert prohibido not in body, f"quedó rastro del modo antiguo: {prohibido}"
    assert "Resources/sqcli" not in src and "pgrep" not in body


# ----------------------------- F2-j: rc 1 con motivo para cada uno --------------------------------

def test_f2j_inseguro_con_un_sqcli_falso_vivo(tmp_path, procs, proyecto):
    name, _ = proyecto
    motor = fake_engine(procs, tmp_path / "bin", os.environ["SQX_ENGINE_PROC_NAME"])
    r = cli(name)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NO abras la GUI todavía" in r.stdout and "hay un sqcli vivo" in r.stdout and str(motor.pid) in r.stdout
    assert "Ya puedes abrir la GUI" not in r.stdout and "stop_and_persist.py" in r.stdout


def test_f2j_inseguro_con_un_orquestador_falso_de_OTRO_proyecto(tmp_path, procs, proyecto):
    """La instancia única es de TODA la máquina: un orquestador de otro proyecto también impide abrir la GUI."""
    name, _ = proyecto
    otro = f"otro{uuid4().hex[:6]}"
    orq = fake_launcher(procs, tmp_path / "o", "run_pipeline_per_task.sh", otro, 0)
    r = cli(name)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "hay un orquestador per-task vivo" in r.stdout and str(orq.pid) in r.stdout
    assert "Ya puedes abrir la GUI" not in r.stdout


def test_f2j_inseguro_con_un_driver_falso_de_otro_proyecto(tmp_path, procs, proyecto):
    name, _ = proyecto
    otro = f"otro{uuid4().hex[:6]}"
    drv = fake_launcher(procs, tmp_path / "d", f"{otro}_driver_AbC123", otro, 5050, "", 0)
    r = cli(name)
    assert r.returncode == 1 and "hay un driver de pipeline vivo" in r.stdout and str(drv.pid) in r.stdout


def test_f2j_inseguro_con_una_cascada_falsa(tmp_path, procs, proyecto):
    name, _ = proyecto
    casc = fake_launcher(procs, tmp_path / "c", "run_cascade_projects.sh", 0, name)
    r = cli(name)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "hay una cascada de proyectos viva" in r.stdout and str(casc.pid) in r.stdout


def test_f2j_inseguro_con_marcador_slim_y_con_bancos_archivados(proyecto):
    name, d = proyecto
    (d / "databanks_archive" / "ExtraC").mkdir(parents=True)
    (d / "databanks_archive" / ".runner_session.json").write_text("{}")
    r = cli(name)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "corrida --slim sin restaurar" in r.stdout and "ExtraC" in r.stdout
    assert "gui_slim.py" in r.stdout and "Ya puedes abrir la GUI" not in r.stdout


def test_f2j_inseguro_con_un_bak_pendiente(proyecto):
    name, d = proyecto
    (d / "project.cfx.pre_run_task.bak").write_bytes(b"x")
    r = cli(name)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "project.cfx.pre_run_task.bak" in r.stdout and "Ya puedes abrir la GUI" not in r.stdout


def test_f2j_proyecto_inexistente_rc1(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    r = cli("no_existe_este_proyecto")
    assert r.returncode == 1 and "No existe el proyecto" in r.stdout


# ----------------------------- F2-j: rc 0 en limpio con conteos -----------------------------------

def test_f2j_limpio_rc0_con_conteos_por_banco(proyecto, monkeypatch, capsys):
    name, d = proyecto
    for extra in ("Extra1.sqx", "Extra2.sqx"):                  # InA con 3 .sqx, Out con 1
        (d / "databanks" / "InA" / extra).write_bytes(b"x")
    (d / "databanks_archive").mkdir()                            # vacío (manifest consumido): no es un motivo
    sondas_limpias(monkeypatch)
    assert PBG.main([name]) == 0
    out = capsys.readouterr().out
    assert "InA: 3" in out and "Out: 1" in out and "Total .sqx en disco: 4" in out
    assert "Ya puedes abrir la GUI" in out and "NO abras la GUI" not in out
    assert "solo lectura" in out


def test_f2j_un_banco_sin_sqx_se_marca_pero_no_bloquea(proyecto, monkeypatch, capsys):
    name, d = proyecto
    for f in (d / "databanks" / "Out").iterdir():
        f.unlink()
    sondas_limpias(monkeypatch)
    assert PBG.main([name]) == 0
    assert "Out: 0" in capsys.readouterr().out


def test_f2j_limpio_como_cli_real_si_la_maquina_lo_permite(proyecto):
    """Misma comprobación como subproceso real. pre_gui_check es global: si la máquina de desarrollo tiene la GUI
    de SQX abierta o procesos del stack vivos, el resultado correcto es rc 1 con el motivo (también se valida)."""
    name, _ = proyecto
    r = cli(name)
    if r.returncode == 0:
        assert "Ya puedes abrir la GUI" in r.stdout and "InA: 1" in r.stdout
    else:
        assert r.returncode == 1 and "NO abras la GUI todavía" in r.stdout


# ----------------------------- F2-j: uso -----------------------------------------------------------

def test_f2j_sin_argumento_uso_y_rc2_ya_no_hay_proyecto_por_defecto(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    (tmp_path / "Esqueleto").mkdir()                             # el antiguo default NO se usa
    r = cli()
    assert r.returncode == 2, r.stdout + r.stderr
    assert "uso: python sqx/persist_before_gui.py <proyecto>" in r.stderr and "solo lectura" in r.stderr
    assert r.stdout == ""
    r = cli("--help")
    assert r.returncode == 0 and "uso:" in r.stdout


def test_f2j_docstring_nuevo_explica_que_cambio_y_por_que():
    doc = PBG.__doc__
    assert "SOLO LECTURA" in doc and "CAMBIÓ" in doc
    assert "no puede AÑADIR" in doc and "carga lazy" in doc and "QUITARLOS" in doc
    assert "stop_and_persist.py" in doc and "pre_gui_check" in doc
