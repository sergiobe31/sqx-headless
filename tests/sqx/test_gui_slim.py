"""Tests de sqx/gui_slim.py sobre un proyecto falso en tmp_path (in-process).

Inyección de la guarda de procesos: `monkeypatch` sobre `gui_slim.live_processes`
(seam declarado) — los tests son independientes del estado real de la máquina
sin necesitar el flag `--no-process-check`, que desde la revisión del
orquestador está RESTRINGIDO a `status` (enforcement probado en
`test_no_process_check_rejected_for_archive_restore`).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

import sqx.gui_slim as gs

_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS) not in sys.path:   # AL FINAL: delante, `import sqx` resolvería a tests/sqx
    sys.path.append(str(_TESTS))
from test_slim_hooks import _wait_for, cmdline_of, fake_engine, procs, unique_engine_name  # noqa: E402,F401

BANKS = {
    "Long_TF": ["a.sqx", "b.sqx"],
    "MR_Long": ["c.sqx"],
    "MR_Short": ["d.sqx", "e.sqx", "f.sqx"],
    "Results": ["g.sqx"],
    "Small": ["h.sqx"],
}


def make_project(tmp_path: Path, name: str = "intraday_nasdaq_future") -> Path:
    proj = tmp_path / name
    for bank, files in BANKS.items():
        d = proj / "databanks" / bank
        d.mkdir(parents=True)
        for f in files:
            (d / f).write_bytes(b"PK fake sqx")
    (proj / "project.cfx").write_bytes(b"fake cfx")
    return proj


def run_main(monkeypatch: pytest.MonkeyPatch, *argv: str,
             live: list[str] | None = None) -> str | None:
    """Ejecuta gs.main(argv). Devuelve None si sale bien, o el mensaje de fail()."""
    monkeypatch.setattr(gs, "live_processes", lambda: [] if live is None else live)
    try:
        gs.main(list(argv))
    except SystemExit as exc:
        return None if exc.code in (0, None) else str(exc.code)
    return None


def run_ok(monkeypatch: pytest.MonkeyPatch, *argv: str,
           live: list[str] | None = None) -> None:
    msg = run_main(monkeypatch, *argv, live=live)
    assert msg is None, f"{' '.join(argv)}\nfail: {msg}"


def counts(proj: Path, sub: str, bank: str) -> int:
    d = proj / sub / bank
    return sum(1 for p in d.rglob("*") if p.is_file()) if d.is_dir() else -1


def test_archive_default_banks_status_restore_roundtrip(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    run_ok(monkeypatch, str(proj), "archive")
    for bank in ("Long_TF", "MR_Long", "MR_Short", "Results"):
        assert counts(proj, "databanks", bank) == -1
        assert counts(proj, "databanks_archive", bank) == len(BANKS[bank])
    assert counts(proj, "databanks", "Small") == 1  # no tocado
    assert (proj / f"project.cfx.bak_guislim_{dt.date.today():%Y%m%d}").exists()

    manifest = json.loads((proj / "databanks_archive" / "manifest.json").read_text())
    assert manifest["banks"] == {b: len(f) for b, f in BANKS.items() if b != "Small"}

    run_ok(monkeypatch, str(proj), "status")  # status SÍ admite --no-process-check
    run_ok(monkeypatch, str(proj), "status", "--no-process-check")

    run_ok(monkeypatch, str(proj), "restore")
    for bank, files in BANKS.items():
        assert counts(proj, "databanks", bank) == len(files)
        assert counts(proj, "databanks_archive", bank) == -1
    assert not (proj / "databanks_archive" / "manifest.json").exists()


def test_process_guard_fails_archive_and_restore(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    msg = run_main(monkeypatch, str(proj), "archive", live=["1234"])
    assert msg and "1234" in msg
    assert counts(proj, "databanks", "Long_TF") == 2  # nada movido
    run_ok(monkeypatch, str(proj), "archive")
    msg = run_main(monkeypatch, str(proj), "restore", live=["1234"])
    assert msg and "1234" in msg
    assert counts(proj, "databanks", "Long_TF") == -1  # nada restaurado


def test_no_process_check_rejected_for_archive_restore(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    msg = run_main(monkeypatch, str(proj), "archive", "--no-process-check")
    assert msg and "--no-process-check" in msg and "status" in msg
    assert counts(proj, "databanks", "Long_TF") == 2  # enforcement, no bypassó


def test_archive_fail_loud_destination_exists_empty(tmp_path, monkeypatch) -> None:
    """Dir destino EXISTENTE Y VACÍO: shutil.move anidaría src dentro (Results/Results/)."""
    proj = make_project(tmp_path)
    (proj / "databanks_archive" / "Results").mkdir(parents=True)
    msg = run_main(monkeypatch, str(proj), "archive", "--banks", "Results")
    assert msg and "ya existe" in msg and "anido" in msg
    assert counts(proj, "databanks", "Results") == 1  # fuente intacta
    assert not (proj / "databanks_archive" / "Results" / "Results").exists()


def test_restore_fail_loud_destination_exists_empty(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    run_ok(monkeypatch, str(proj), "archive", "--banks", "Small")
    (proj / "databanks" / "Small").mkdir()  # GUI/motor recrea la carpeta vacía
    msg = run_main(monkeypatch, str(proj), "restore")
    assert msg and "ya existe" in msg and "anido" in msg
    assert counts(proj, "databanks_archive", "Small") == 1  # nada movido
    assert counts(proj, "databanks", "Small") == 0


def test_archive_unknown_project_requires_banks(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path, name="proyecto_desconocido")
    msg = run_main(monkeypatch, str(proj), "archive")
    assert msg and "--banks" in msg
    run_ok(monkeypatch, str(proj), "archive", "--banks", "Small")
    assert counts(proj, "databanks_archive", "Small") == 1


def test_archive_fail_loud_missing_bank(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    msg = run_main(monkeypatch, str(proj), "archive", "--banks", "NoExiste")
    assert msg and "NoExiste" in msg
    assert counts(proj, "databanks", "Long_TF") == 2  # nada movido


def test_archive_fail_loud_destination_not_empty(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    dst = proj / "databanks_archive" / "Long_TF"
    dst.mkdir(parents=True)
    (dst / "ocupante.sqx").write_bytes(b"x")
    msg = run_main(monkeypatch, str(proj), "archive", "--banks", "Long_TF")
    assert msg and "ya existe" in msg
    assert counts(proj, "databanks", "Long_TF") == 2  # fuente intacta
    assert (dst / "ocupante.sqx").exists()


def test_restore_fail_loud_manifest_mismatch(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    run_ok(monkeypatch, str(proj), "archive", "--banks", "Long_TF,Small")
    # borrar a mano un fichero del archive -> descuadre 1:1
    next((proj / "databanks_archive" / "Long_TF").glob("*.sqx")).unlink()
    msg = run_main(monkeypatch, str(proj), "restore")
    assert msg and "descuadre" in msg
    # no restaura a medias: nada vuelve a databanks/
    assert counts(proj, "databanks", "Long_TF") == -1
    assert counts(proj, "databanks", "Small") == -1
    assert (proj / "databanks_archive" / "manifest.json").exists()


def test_restore_fail_loud_without_manifest(tmp_path, monkeypatch) -> None:
    proj = make_project(tmp_path)
    run_ok(monkeypatch, str(proj), "archive", "--banks", "Small")
    (proj / "databanks_archive" / "manifest.json").unlink()
    msg = run_main(monkeypatch, str(proj), "restore")
    assert msg and "manifiesto" in msg


def test_status_unknown_project_fail_loud(tmp_path, monkeypatch) -> None:
    msg = run_main(monkeypatch, str(tmp_path / "no_existe"), "status")
    assert msg and "no encuentro" in msg


# ---------------------------------------------------------------------------
# live_processes (R5, guardrail sqx-headless-fallos Fase 1): motor por NOMBRE EXACTO de proceso
# ---------------------------------------------------------------------------

def test_live_processes_motor_por_nombre_exacto_no_por_cmdline(tmp_path, procs, monkeypatch) -> None:
    """La guarda de GUI/motor vivos veía como "motor" a cualquier proceso cuyo cmdline contuviera el texto del
    bundle del motor (un shell de vigilancia): ahora solo cuenta el proceso LLAMADO como el motor."""
    monkeypatch.delenv("SLIM_TESTING", raising=False)
    name = unique_engine_name()
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", name)
    monkeypatch.setattr(gs.SP, "gui_pids", lambda: [])         # sin depender de una GUI real abierta
    # cebo: un shell cuyo cmdline contiene el texto del bundle (el patrón ancho antiguo lo casaba)
    bait = procs.spawn(["bash", "-c", "sleep 60; true # /Resources/sqcli"])
    assert _wait_for(lambda: "Resources/sqcli" in cmdline_of(bait.pid))
    assert gs.live_processes() == []                           # el cebo NO es un motor
    motor = fake_engine(procs, tmp_path / "bin", name)
    assert gs.live_processes() == [str(motor.pid)]
    # y la guarda de archive/restore aborta por el motor, citando su PID
    proj = make_project(tmp_path)
    with pytest.raises(SystemExit) as exc:
        gs.main([str(proj), "archive"])
    assert str(motor.pid) in str(exc.value.code)
    assert counts(proj, "databanks", "Long_TF") == 2           # nada movido


def test_live_processes_gui_por_cmdline_y_slim_testing_la_anula(monkeypatch) -> None:
    monkeypatch.delenv("SLIM_TESTING", raising=False)
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", unique_engine_name())
    monkeypatch.setattr(gs.SP, "gui_pids", lambda: [4321])
    assert gs.live_processes() == ["4321"]
    monkeypatch.setenv("SLIM_TESTING", "1")                    # solo tests con mocks
    assert gs.live_processes() == []
