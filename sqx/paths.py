"""Rutas de la instalación de StrategyQuant X — ÚNICA fuente para Python.

Cambiar de versión de SQX (p.ej. B144 -> B145):
  - puntual: exportar ``SQX_APP=/Applications/StrategyQuantXB145.app`` antes de lanzar;
  - permanente: editar el default de ``SQX_APP`` aquí y en ``sqx/paths.sh`` (gemelo
    para shell). Nada más en el repo debe llevar la ruta escrita (guardián:
    ``tests/test_no_hardcoded_sqx_path.py``).

Import desde ``sqx/`` (funciona como paquete y como script con ``sys.path[0]=sqx/``)::

    try:
        from sqx.paths import PROJECTS
    except ImportError:
        from paths import PROJECTS

Resolución de ``SQX_APP`` (2026-10-21, para la copia compartible ``sqx-headless``):
variable de entorno ``SQX_APP`` > la ÚNICA ``StrategyQuantX*.app`` de las carpetas de
``SQX_APP_SEARCH`` (``:``-separadas; por defecto ``/Applications`` y ``~/Applications``) >
el default de abajo (también si hay 0 o varias). Mismo orden en ``sqx/paths.sh``
(test ``tests/test_sqx_paths_autodetect.py``). Comprobar: ``python sqx/paths.py``.

Nota: ``src/risk`` no puede importar ``sqx`` (``tests/test_risk_no_sqx_import.py``).
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SQX_APP = Path("/Applications/StrategyQuantXB144_arm.app")


def _search_dirs() -> list[Path]:
    raw = os.environ.get("SQX_APP_SEARCH") or f"/Applications:{Path.home() / 'Applications'}"
    return [Path(d) for d in raw.split(":") if d]


def resolve_sqx_app() -> tuple[Path, str]:
    """(ruta, origen) con origen ``env`` | ``autodetect`` | ``default``."""
    env = os.environ.get("SQX_APP")
    if env:
        return Path(env), "env"
    found = sorted({p for d in _search_dirs() for p in d.glob("StrategyQuantX*.app") if p.is_dir()})
    if len(found) == 1:
        return found[0], "autodetect"
    return DEFAULT_SQX_APP, "default"


#: Bundle .app de SQX (override con la variable de entorno ``SQX_APP``).
SQX_APP, SQX_APP_ORIGIN = resolve_sqx_app()

SQX_RESOURCES = SQX_APP / "Contents" / "Resources"
SQX_USER = SQX_RESOURCES / "user"
#: Raíz de proyectos (custom projects: ``<proyecto>/project.cfx``, ``databanks/``, ``log/``).
PROJECTS = SQX_USER / "projects"
#: Binario del motor headless (HTTP :5050).
SQCLI = SQX_RESOURCES / "sqcli"
SQX_INTERNAL = SQX_RESOURCES / "internal"
#: Plantillas de estilo del Builder (``<Estilo>.xml``: TrendFollowing, Mean-Reversal...).
SIMPLE_TEMPLATES = SQX_INTERNAL / "web" / "BUILDER" / "simpleTemplates"
#: Código Java de los bloques de señal del Builder (``<carpeta>/<Clave>.java``, con ``@OppositeBlock``).
SNIPPETS_BLOCKS = SQX_INTERNAL / "extend" / "Snippets" / "SQ" / "Blocks"

__all__ = [
    "SQX_APP", "SQX_RESOURCES", "SQX_USER", "PROJECTS", "SQCLI",
    "SQX_INTERNAL", "SIMPLE_TEMPLATES", "SNIPPETS_BLOCKS",
]


if __name__ == "__main__":  # autocomprobación: python sqx/paths.py
    import glob
    import sys

    print(f"SQX_APP  = {SQX_APP}  ({SQX_APP_ORIGIN})")
    ok = True
    for name, p in (("SQCLI", SQCLI), ("PROJECTS", PROJECTS)):
        exists = p.exists()
        ok &= exists
        print(f"{name:8} = {p}  {'OK' if exists else 'NO EXISTE'}")
    if SQX_APP_ORIGIN == "default" and not SQX_APP.exists():
        cands = [c for d in _search_dirs() for c in glob.glob(str(d / "StrategyQuantX*.app"))]
        print("Instalaciones encontradas:", cands or "ninguna",
              "-> exporta SQX_APP=/ruta/a/StrategyQuantX<ver>.app")
    sys.exit(0 if ok else 1)
