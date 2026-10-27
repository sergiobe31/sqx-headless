"""Tests de sqx/run_task_headless.py — de momento, la primitiva restore().

restore() debe copiar el .bak sobre el .cfx Y ELIMINAR el .bak: un .bak que sobrevive
a un restore exitoso es indistinguible de un .bak huérfano (lanzamiento muerto a mitad)
y haría saltar la alerta 'restore pendiente' del monitor para siempre (falso positivo
tras cada lanzamiento sano — cazado en el experimento smoke_p2, 2026-07-19).

Correr desde la raíz del repo:
    ~/miniconda3/envs/robotrader/bin/python -m pytest tests/sqx/test_run_task_headless.py -v
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

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

import run_task_headless as RT  # noqa: E402
from test_slim_hooks import (  # noqa: E402,F401  infraestructura de procesos falsos
    _wait_for, fake_engine, pid_alive, procs, unique_engine_name,
)

from uuid import uuid4  # noqa: E402


@pytest.fixture(autouse=True)
def _nunca_el_sqcli_real(monkeypatch, tmp_path):
    """Seguridad de plataforma (incidente de la Fase 1, 2026-10-21: un test arrancó el sqcli REAL de SQX): `SQCLI` y
    `SQX_APP` a rutas inexistentes y un nombre de motor ÚNICO por test. Ningún test de este módulo puede ver, arrancar
    ni matar un sqcli real (los tests que necesitan un motor falso lo fabrican con su propio nombre)."""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", f"sqcli_{uuid4().hex[:8]}")


def test_restore_copies_and_deletes_bak(tmp_path, monkeypatch):
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    monkeypatch.setattr(RT.REG, "PROJECTS", tmp_path)
    cfx = proj_dir / "project.cfx"
    bak = proj_dir / "project.cfx.pre_run_task.bak"
    cfx.write_bytes(b"cfx-mutado")
    bak.write_bytes(b"cfx-original")

    assert RT.restore("proj_x", bak) is True
    assert cfx.read_bytes() == b"cfx-original"
    assert not bak.exists(), "el .bak debe eliminarse tras un restore exitoso"


def test_restore_without_bak_returns_false(tmp_path, monkeypatch):
    (tmp_path / "proj_x").mkdir()
    monkeypatch.setattr(RT.REG, "PROJECTS", tmp_path)
    assert RT.restore("proj_x", tmp_path / "proj_x" / "project.cfx.pre_run_task.bak") is False


# ---------------------------------------------------------------------------
# mutate_active: aborta si hay .bak huérfano (H8, debate 2026-09-27)
# ---------------------------------------------------------------------------

def _make_cfx(proj_dir, n_tasks=1):
    import zipfile
    tasks = [
        ('<Task type="Retest" name="Retest strategies 4" title="Real_OOS" '
         'taskXMLFile="Retest-Task2.xml" active="true"/>'),
        ('<Task type="Retest" name="Retest strategies 3" title="Input PM" '
         'taskXMLFile="Retest-Task3.xml" active="true"/>'),
    ]
    with zipfile.ZipFile(proj_dir / "project.cfx", "w") as z:
        z.writestr("config.xml", "<root>" + "".join(tasks[:n_tasks]) + "</root>")
        for xml in ("Retest-Task2.xml", "Retest-Task3.xml")[:n_tasks]:
            z.writestr(xml, "<Task/>")


def test_mutate_active_aborts_on_orphan_bak(tmp_path, monkeypatch):
    """Un .bak huérfano + 'if not bak.exists()' = la siguiente mutación NO respalda
    el estado actual y el restore posterior pisa el .cfx con un backup VIEJO.
    mutate_active debe ABORTAR (SystemExit) y dejar el .cfx intacto."""
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    _make_cfx(proj_dir)
    (proj_dir / "project.cfx.pre_run_task.bak").write_bytes(b"backup-viejo")
    monkeypatch.setattr(RT.REG, "PROJECTS", tmp_path)
    cfx_before = (proj_dir / "project.cfx").read_bytes()

    with pytest.raises(SystemExit):
        RT.mutate_active("proj_x", "Retest-Task2.xml", "only")
    assert (proj_dir / "project.cfx").read_bytes() == cfx_before


def test_mutate_active_only_disables_other_tasks(tmp_path, monkeypatch):
    """Camino sano: sin .bak, mode=only deja activa solo la task seleccionada."""
    import re
    import zipfile
    proj_dir = tmp_path / "proj_x"
    proj_dir.mkdir()
    _make_cfx(proj_dir, n_tasks=2)
    monkeypatch.setattr(RT.REG, "PROJECTS", tmp_path)

    bak = RT.mutate_active("proj_x", "Retest-Task2.xml", "only")
    assert bak.exists()
    with zipfile.ZipFile(proj_dir / "project.cfx") as z:
        cfg = z.read("config.xml").decode()
    tags = re.findall(r"<Task\b[^>]*>", cfg)
    assert 'active="true"' in tags[0]
    assert 'active="false"' in tags[1]


# ---------------------------------------------------------------------------
# run(): arranque NO confirmado → driver y motor MATADOS (H1), pero solo el motor de ESTE driver
# (R3/R5, guardrail sqx-headless-fallos Fase 1). Antes: `pkill -f` global sobre cualquier sqcli.
# ---------------------------------------------------------------------------

def test_run_arranque_no_confirmado_mata_solo_el_motor_de_su_driver(tmp_path, procs, monkeypatch):
    proj_dir = tmp_path / "projects" / "proj_x"
    proj_dir.mkdir(parents=True)
    _make_cfx(proj_dir)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("SLIM_TESTING", "1")                    # mocks: sin guardas de motor/GUI reales
    monkeypatch.delenv("RUNNER_PIPELINE", raising=False)
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)

    # dos motores falsos: el del driver (su pgid == el PID del driver, que nace con start_new_session) y uno
    # AJENO (otra sesión: p.ej. el sqcli que hizo abortar al driver por instancia única)
    propio = fake_engine(procs, tmp_path / "bin", name)
    ajeno = fake_engine(procs, tmp_path / "bin", name)
    assert propio.pid != ajeno.pid

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = propio.pid                              # "el driver" cuyo grupo contiene al motor propio

        def terminate(self):
            pass

    def _sin_motor(*a, **k):
        raise OSError("sin motor (test)")

    monkeypatch.setattr(RT, "subprocess", SimpleNamespace(Popen=FakePopen, STDOUT=subprocess.STDOUT,
                                                          run=subprocess.run))
    monkeypatch.setattr(RT, "time", SimpleNamespace(sleep=lambda s: None, time=time.time))
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port=5050: False)   # el start nunca se confirma
    monkeypatch.setattr(RT.SP, "_http", _sin_motor)
    monkeypatch.setattr(RT, "gui_open", lambda: False)

    rep = RT.run("proj_x", "Real_OOS", "only", wait_start=5)
    assert rep["ok"] is False and "MATADOS" in rep["msg"]
    assert _wait_for(lambda: not pid_alive(propio.pid), timeout=15), "el motor del driver debe morir"
    assert pid_alive(ajeno.pid), "un sqcli AJENO no se toca"
    # .cfx restaurado (el .bak desaparece)
    assert not (proj_dir / "project.cfx.pre_run_task.bak").exists()


def test_own_engines_filtra_por_grupo_de_procesos(tmp_path, procs, monkeypatch):
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)
    a = fake_engine(procs, tmp_path / "bin", name)
    b = fake_engine(procs, tmp_path / "bin", name)
    assert RT.own_engines(a.pid) == [a.pid]                    # nueva sesión: pgid == pid
    assert RT.own_engines(b.pid) == [b.pid]
    assert RT.own_engines(2 ** 22 + 12345) == []


# ---------------------------------------------------------------------------
# Fase 2 (R6): bandera de parada del pipeline en el lanzador (F2-g, F2-e) y gui_open fail-closed (C5)
# ---------------------------------------------------------------------------

import re  # noqa: E402
import zipfile  # noqa: E402
from uuid import uuid4  # noqa: E402

import bank_plan as BP  # noqa: E402
from test_slim_hooks import no_live_procs  # noqa: E402


class _Lanzamientos:
    """FakePopen que registra los lanzamientos del driver (el test nunca arranca un driver real)."""
    launched: list = []

    def __init__(self, cmd, **kwargs):
        _Lanzamientos.launched.append(cmd)
        self.pid = 424242

    def terminate(self):
        pass


@pytest.fixture()
def lanzador_base(tmp_path, monkeypatch):
    """Proyecto fixture (2 tasks) + lanzador sin procesos reales: Popen falso, sin sleeps, motor 'arrancado' y NINGÚN
    proceso vivo. NO parchea `gui_open` (los tests C5 ejercitan el real)."""
    _Lanzamientos.launched = []
    name = f"rtp{uuid4().hex[:6]}"
    projects = tmp_path / "projects"
    d = projects / name
    d.mkdir(parents=True)
    _make_cfx(d, n_tasks=2)
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    monkeypatch.setenv("SLIM_TESTING", "1")
    monkeypatch.delenv("RUNNER_PIPELINE", raising=False)
    monkeypatch.delenv("SLIM_IN_PIPELINE", raising=False)
    no_live_procs(monkeypatch)
    monkeypatch.setattr(RT, "subprocess", SimpleNamespace(Popen=_Lanzamientos, STDOUT=subprocess.STDOUT,
                                                          run=subprocess.run))
    monkeypatch.setattr(RT, "time", SimpleNamespace(sleep=lambda s: None, time=time.time))
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port=5050: True)
    monkeypatch.setattr(RT.SP, "_http", lambda cmd, port=5050, timeout=30: "Project running")
    yield SimpleNamespace(name=name, dir=d, cfx=d / "project.cfx", bak=d / "project.cfx.pre_run_task.bak",
                          flag=d / ".pipeline_stop_request")
    for f in Path("/tmp").glob(f"{name}_task*.log"):
        f.unlink(missing_ok=True)


@pytest.fixture()
def lanzador(lanzador_base, monkeypatch):
    monkeypatch.setattr(RT, "gui_open", lambda: False)
    return lanzador_base


def _active_flags(cfx: Path) -> list[str]:
    with zipfile.ZipFile(cfx) as z:
        cfg = z.read("config.xml").decode()
    return re.findall(r'active="(\w+)"', cfg)


def test_f2g_en_pipeline_con_bandera_antes_de_mutar_no_lanza_ni_toca_el_cfx(lanzador, monkeypatch):
    monkeypatch.setenv("RUNNER_PIPELINE", "1")             # lo exporta el orquestador a sus hijos
    lanzador.flag.write_text("ts=x\norigin=stop_and_persist\n")
    antes = lanzador.cfx.read_bytes()
    rep = RT.run(lanzador.name, "Real_OOS", "only")
    assert rep["ok"] is False and rep["stop_requested"] is True and "Parada pedida por el usuario" in rep["msg"]
    assert lanzador.cfx.read_bytes() == antes and not lanzador.bak.exists(), "no se mutó el .cfx"
    assert _Lanzamientos.launched == []
    assert lanzador.flag.exists(), "la bandera la borra el orquestador, no el lanzador"


def test_f2g_bandera_entre_mutar_y_popen_restaura_el_cfx_y_no_lanza(lanzador, monkeypatch):
    """Doble cierre (DS-H2): la bandera aparece DESPUÉS de la 1.ª comprobación y de mutar el .cfx, y ANTES del
    Popen. El lanzador la ve en la 2.ª comprobación, restaura el .cfx (no queda .bak) y no lanza."""
    monkeypatch.setenv("RUNNER_PIPELINE", "1")
    antes = lanzador.cfx.read_bytes()
    visto = {"calls": 0, "mutado_en_la_2a": None}
    real = RT.stop_requested

    def inyectada(proj):
        visto["calls"] += 1
        if visto["calls"] == 2:                            # entre mutar y el Popen
            visto["mutado_en_la_2a"] = "false" in _active_flags(lanzador.cfx) and lanzador.bak.exists()
            lanzador.flag.write_text("ts=x\norigin=test\n")
        return real(proj)

    monkeypatch.setattr(RT, "stop_requested", inyectada)
    rep = RT.run(lanzador.name, "Real_OOS", "only")
    assert visto["calls"] == 2 and visto["mutado_en_la_2a"] is True, "el .cfx SÍ estaba mutado en la 2.ª comprobación"
    assert rep["ok"] is False and rep["stop_requested"] is True and "justo antes de lanzar el driver" in rep["msg"]
    assert _Lanzamientos.launched == [], "no se lanzó el driver"
    assert lanzador.cfx.read_bytes() == antes and not lanzador.bak.exists(), ".cfx restaurado y .bak consumido"
    assert not list(Path("/tmp").glob(f"{lanzador.name}_task*.log")), "no deja el log del lanzamiento"


def test_f2g_sin_bandera_en_pipeline_lanza_con_normalidad(lanzador, monkeypatch):
    monkeypatch.setenv("RUNNER_PIPELINE", "1")
    rep = RT.run(lanzador.name, "Real_OOS", "only")
    assert rep["ok"] is True and len(_Lanzamientos.launched) == 1 and not lanzador.bak.exists()
    assert "stop_requested" not in rep


def test_f2e_lanzamiento_suelto_borra_la_bandera_rancia_y_lo_dice(lanzador):
    lanzador.flag.write_text("ts=2026-01-01 00:00:00\norigin=stop_and_persist\n")
    rep = RT.run(lanzador.name, "Real_OOS", "only")            # sin RUNNER_PIPELINE: lanzamiento suelto
    assert rep["ok"] is True and rep["stale_stop_flag_removed"] is True
    assert "bandera de parada rancia" in rep["msg"] and "decisión explícita" in rep["msg"]
    assert not lanzador.flag.exists() and len(_Lanzamientos.launched) == 1


def test_f2e_lanzamiento_suelto_con_orquestador_vivo_se_rechaza_sin_tocar_la_bandera(lanzador, monkeypatch):
    """Una bandera con un orquestador del proyecto VIVO no es rancia (es una parada en curso): el lanzamiento se
    rechaza (R7) antes de mirar la bandera y esta se queda."""
    monkeypatch.setattr(RT.SP, "orchestrator_pids", lambda proj=None: [123])
    lanzador.flag.write_text("ts=x\norigin=stop_and_persist\n")
    rep = RT.run(lanzador.name, "Real_OOS", "only")
    assert rep["ok"] is False and "orquestador" in rep["msg"]
    assert lanzador.flag.exists() and _Lanzamientos.launched == []


def _gui_roto(monkeypatch):
    def roto():
        raise RT.SP.ProcProbeError("pgrep -f roto (test)")

    monkeypatch.setattr(RT.SP, "gui_pids", roto)


def test_c5_gui_open_con_pgrep_roto_es_fail_closed_y_no_lanza_con_motivo(lanzador_base, monkeypatch):
    """C5: `gui_open` no capturaba ProcProbeError (excepción sin tratar en el lanzador). Ahora: True (no se lanza)
    y el motivo dice que no se pudo comprobar (no que la GUI esté abierta)."""
    lz = lanzador_base
    monkeypatch.delenv("SLIM_TESTING", raising=False)          # el chequeo de la GUI actúa
    monkeypatch.setattr(RT.SP, "motor_alive", lambda port=5050: False)
    _gui_roto(monkeypatch)
    assert RT.gui_open() is True
    assert RT.gui_block_msg().startswith("No pude comprobar si la GUI de SQX está abierta")
    rep = RT.run(lz.name, "Real_OOS", "only")
    assert rep["ok"] is False and "No pude comprobar si la GUI" in rep["msg"] and "no lanzo por seguridad" in rep["msg"]
    assert "abierta (corrompería" not in rep["msg"]
    assert _Lanzamientos.launched == [] and not lz.bak.exists()
    rep = RT.run_pipeline_per_task(lz.name, 0)
    assert rep["ok"] is False and "No pude comprobar si la GUI" in rep["msg"]
    assert _Lanzamientos.launched == []
    # con la GUI realmente abierta el mensaje es el de siempre
    monkeypatch.setattr(RT.SP, "gui_pids", lambda: [999])
    assert RT.gui_open() is True and RT.gui_block_msg().startswith("La GUI de SQX está abierta")


def test_c5_reconcile_con_pgrep_roto_bloquea_con_motivo_y_no_restaura(lanzador_base, monkeypatch):
    lz = lanzador_base
    monkeypatch.delenv("SLIM_TESTING", raising=False)
    BP.marker_write(lz.dir, {"run_id": "r", "pid_driver": 999999, "pipeline": False, "banks": ["InA"]})
    _gui_roto(monkeypatch)
    rec = RT.reconcile_at_startup(lz.name)
    assert rec["action"] == "blocked" and "no pude comprobar si la GUI" in rec["msg"]
    assert BP.marker_path(lz.dir).exists(), "con el sondeo roto no se restaura nada"
