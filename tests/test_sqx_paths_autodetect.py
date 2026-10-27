"""sqx/paths.py y sqx/paths.sh resuelven SQX_APP igual (2026-10-21, copia compartible sqx-headless).

Orden: SQX_APP del entorno > la única StrategyQuantX*.app de SQX_APP_SEARCH > default.
Se ejecutan en subproceso para que cada caso lea su propio entorno.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SQX = ROOT / "sqx"
PY = ("import sys; sys.path.insert(0, sys.argv[1]); import paths; "
      "print(paths.SQX_APP); print(paths.SQX_APP_ORIGIN); print(paths.SQCLI); "
      "print(paths.DEFAULT_SQX_APP)")
# set -euo pipefail + args posicionales: el source no puede abortar ni pisar $1/$2 del driver.
SH = ('set -euo pipefail; set -- uno dos; source "$0"; '
      'echo "$SQX_APP"; echo "$SQCLI"; echo "$1 $2"')


def _env(search: str, app: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("SQX_APP", "SQCLI", "PROJECTS_DIR",
                                                             "SQX_RESOURCES")}
    env["SQX_APP_SEARCH"] = search
    if app is not None:
        env["SQX_APP"] = app
    return env


def _resolve(env: dict[str, str]) -> tuple[list[str], list[str]]:
    py = subprocess.run([sys.executable, "-c", PY, str(SQX)], env=env,
                        capture_output=True, text=True, check=True).stdout.splitlines()
    sh = subprocess.run(["bash", "-c", SH, str(SQX / "paths.sh")], env=env,
                        capture_output=True, text=True, check=True).stdout.splitlines()
    return py, sh


def _apps(d: Path, *names: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).mkdir()


@pytest.mark.parametrize("names, origin", [
    (("StrategyQuantXB145.app",), "autodetect"),
    ((), "default"),
    (("StrategyQuantXB144.app", "StrategyQuantXB145.app"), "default"),
])
def test_py_y_sh_coinciden(tmp_path, names, origin):
    _apps(tmp_path / "Apps", *names)
    (tmp_path / "Apps" / "OtraCosa.app").mkdir()  # no casa el patrón
    py, sh = _resolve(_env(f"{tmp_path / 'Apps'}:{tmp_path / 'vacia'}"))
    assert py[1] == origin
    if origin == "autodetect":
        assert py[0] == str(tmp_path / "Apps" / names[0])
    else:
        assert py[0] == py[3]
    assert sh[0] == py[0] and sh[1] == py[2]
    assert sh[2] == "uno dos"


def test_env_manda_sobre_autodeteccion(tmp_path):
    _apps(tmp_path / "Apps", "StrategyQuantXB145.app")
    elegida = str(tmp_path / "Mia.app")
    py, sh = _resolve(_env(str(tmp_path / "Apps"), app=elegida))
    assert py[:2] == [elegida, "env"]
    assert sh[0] == elegida and sh[1] == f"{elegida}/Contents/Resources/sqcli"


def test_misma_app_en_dos_carpetas_cuenta_una(tmp_path):
    _apps(tmp_path / "Apps", "StrategyQuantXB145.app")
    d = str(tmp_path / "Apps")
    py, sh = _resolve(_env(f"{d}:{d}"))
    assert py[1] == "autodetect" and sh[0] == py[0]
