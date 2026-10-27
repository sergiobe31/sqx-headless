#!/usr/bin/env python3
"""Adelgaza la GUI de StrategyQuant X archivando databanks pesados lateralmente.

La GUI de SQX carga vistas de TODOS los databanks del proyecto al abrirlo y se
vuelve inusable con bancos de miles de ficheros. Esta utilidad MUEVE (rename en
la misma unidad, nada se borra) los bancos indicados de
`<proj>/databanks/<Banco>/` a `<proj>/databanks_archive/<Banco>/` y los
restaura después, con manifiesto y verificación 1:1 de conteos.

⚠ REQUISITO OPERATIVO: NUNCA lanzar el motor/cascada con bancos en archive.
Las tasks leen los databanks de DISCO; con el banco archivado el input aparece
vacío (landmine de carga: `No strategies to retest`). `archive` imprime este
aviso en cada ejecución.

Guardas (todas fail-loud, exit != 0):
  - Aborta si hay GUI o motor vivos (GUI: `pgrep -f` con el patrón de GUI de
    `stop_and_persist.GUI_PATTERN`; motor: `pgrep -x sqcli`, nombre EXACTO — nunca
    texto de cmdline, R5). Bypass `--no-process-check` válido SOLO
    para `status` (lectura con la GUI abierta); en archive/restore el propio
    CLI aborta si se pasa el flag (enforceado en main(), no solo documentado).
  - Aborta si el destino YA EXISTE, aunque esté vacío: shutil.move con un dir
    destino existente anida src DENTRO (`databanks/Results/Results/`) — el
    motor/GUI recrea carpetas de banco vacías, caso real.
  - Aborta si se pide archivar un banco que no existe en `databanks/`.
  - Backup diario de `project.cfx` antes del primer archive del día
    (`.bak_guislim_<AAAAMMDD>`, no se sobrescribe).
  - Restore: verificación 1:1 contra el manifiesto (nº de ficheros por banco);
    si falta o sobra algo NO restaura nada.

Uso:
  python sqx/gui_slim.py <proyecto> <archive|restore|status> [--banks A,B,C] [--no-process-check]
  <proyecto> = nombre de carpeta bajo PROJECTS o ruta directa a la carpeta del proyecto.
  Sin --banks, archive usa DEFAULT_ARCHIVE[proyecto]; para proyectos sin entrada
  exige --banks explícito (fail-loud, no adivinar).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

try:  # ruta de instalación SQX: única fuente en sqx/paths.py
    from sqx.paths import PROJECTS
except ImportError:  # ejecutado como script (sys.path[0] = sqx/)
    from paths import PROJECTS

try:  # primitivas de procesos (fuente única del stack): engine_pids / gui_pids
    from sqx import stop_and_persist as SP
except ImportError:  # ejecutado como script (sys.path[0] = sqx/)
    import stop_and_persist as SP

# Bancos pesados por defecto a archivar, por nombre de proyecto.
DEFAULT_ARCHIVE: dict[str, list[str]] = {
    "intraday_nasdaq_future": ["Long_TF", "MR_Long", "MR_Short", "Results"],
}

# Patrón de la GUI (electron StrategyQuantX_ui / lanzador StrategyQuantX_nocheck / binario del
# bundle de CUALQUIER versión instalada): vive en stop_and_persist y se re-exporta aquí por
# compatibilidad. El MOTOR ya no va en un patrón de cmdline: se detecta por nombre exacto
# (`SP.engine_pids()`), porque el patrón casaba con cualquier shell que contuviera el texto (R5).
PROCESS_PATTERN = SP.GUI_PATTERN

ENGINE_WARNING = ("WARNING: con bancos en archive NO lances motor/cascada — "
                  "las tasks leen los databanks de disco y verían el input vacío.")


def fail(msg: str) -> "None":
    sys.exit(f"ERROR: {msg}")


def resolve_project(arg: str) -> Path:
    p = Path(arg).expanduser()
    if p.is_dir():
        return p
    p = PROJECTS / arg
    if p.is_dir():
        return p
    fail(f"no encuentro la carpeta del proyecto '{arg}' (ni ruta directa ni en {PROJECTS})")


def live_processes() -> list[str]:
    # SLIM_TESTING=1: entorno de test con mocks (no hay procesos SQX reales y SÍ
    # puede haberlos en la máquina — p.ej. un motor de otra corrida); la guarda
    # queda anulada SOLO en ese modo, nunca en producción.
    if os.environ.get("SLIM_TESTING"):
        return []
    # GUI por cmdline (no tiene nombre de proceso estable) ∪ motores por nombre EXACTO de proceso.
    return [str(p) for p in dict.fromkeys([*SP.gui_pids(), *SP.engine_pids()])]


def guard_no_processes() -> None:
    pids = live_processes()
    if pids:
        fail(f"hay GUI/motor SQX vivos (pids {', '.join(pids)}). Ciérralos antes "
             f"(la GUI sincroniza su RAM a disco y puede destruir .sqx). "
             f"Solo `status` admite --no-process-check.")


def count_files(d: Path) -> int:
    return sum(1 for p in d.rglob("*") if p.is_file())


def parse_banks(s: str | None) -> list[str] | None:
    if s is None:
        return None
    banks = [b.strip() for b in s.split(",") if b.strip()]
    return banks or None


def load_manifest(archive_dir: Path) -> dict:
    m = archive_dir / "manifest.json"
    if not m.is_file():
        return {}
    return json.loads(m.read_text(encoding="utf-8"))


def write_manifest(archive_dir: Path, data: dict) -> None:
    (archive_dir / "manifest.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup_cfx(proj: Path) -> None:
    cfx = proj / "project.cfx"
    if not cfx.is_file():
        fail(f"no existe {cfx} — ¿carpeta de proyecto correcta?")
    bak = proj / f"project.cfx.bak_guislim_{dt.date.today():%Y%m%d}"
    if not bak.exists():
        shutil.copy2(cfx, bak)
        print(f"Backup: {bak.name}")
    else:
        print(f"Backup del día ya existente: {bak.name} (no se toca)")


def cmd_archive(proj: Path, banks: list[str]) -> None:
    guard_no_processes()
    dbs = proj / "databanks"
    arch = proj / "databanks_archive"
    if not dbs.is_dir():
        fail(f"no existe {dbs}")

    # chequeos previos SIN mover nada (fail-loud, no archive a medias)
    plan: list[tuple[Path, Path, int]] = []
    for bank in banks:
        src = dbs / bank
        dst = arch / bank
        if not src.is_dir():
            fail(f"el banco '{bank}' no existe en {dbs} (status para ver los disponibles)")
        if dst.exists():
            fail(f"el destino {dst} ya existe (aunque esté vacío) — no machaco ni anido: "
                 f"shutil.move metería el banco DENTRO de ese dir. Elimínalo a mano tras verificar.")
        plan.append((src, dst, count_files(src)))

    backup_cfx(proj)
    arch.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(arch)
    manifest["date"] = dt.datetime.now().isoformat(timespec="seconds")
    manifest["project"] = proj.name
    manifest.setdefault("banks", {})
    for src, dst, n in plan:
        shutil.move(str(src), str(dst))
        manifest["banks"][dst.name] = n
        print(f"Archivado {dst.name}: {n} ficheros")
    write_manifest(arch, manifest)
    print(f"Manifiesto: {arch / 'manifest.json'}")
    print(ENGINE_WARNING)


def cmd_restore(proj: Path, banks: list[str] | None) -> None:
    guard_no_processes()
    dbs = proj / "databanks"
    arch = proj / "databanks_archive"
    if not arch.is_dir():
        fail(f"no existe {arch} — nada que restaurar")
    manifest = load_manifest(arch)
    mbanks: dict[str, int] = manifest.get("banks") or {}
    if not mbanks:
        fail(f"{arch / 'manifest.json'} no existe o no tiene bancos — "
             f"no restauro sin manifiesto (verificación 1:1 imposible)")
    todo = banks if banks is not None else sorted(mbanks)
    unknown = [b for b in todo if b not in mbanks]
    if unknown:
        fail(f"bancos pedidos no constan en el manifiesto: {unknown} (archivados: {sorted(mbanks)})")

    # verificación 1:1 ANTES de mover nada
    plan: list[tuple[Path, Path, int]] = []
    for bank in todo:
        src = arch / bank
        dst = dbs / bank
        expected = mbanks[bank]
        if not src.is_dir():
            fail(f"en archive falta la carpeta del banco '{bank}' (manifiesto dice {expected} ficheros)")
        n = count_files(src)
        if n != expected:
            fail(f"descuadre 1:1 en '{bank}': manifiesto={expected} ficheros, "
                 f"disco={n}. No se restaura nada (corrige a mano o re-archiva).")
        if dst.exists():
            fail(f"el destino {dst} ya existe (aunque esté vacío) — no machaco ni anido: "
                 f"shutil.move metería el banco DENTRO de ese dir. Elimínalo a mano tras verificar.")
        plan.append((src, dst, n))

    dbs.mkdir(parents=True, exist_ok=True)
    for src, dst, n in plan:
        shutil.move(str(src), str(dst))
        print(f"Restaurado {dst.name}: {n} ficheros")
        del mbanks[dst.name]
    if mbanks:
        manifest["banks"] = mbanks
        write_manifest(arch, manifest)
        print(f"Quedan archivados: {sorted(mbanks)}")
    else:
        (arch / "manifest.json").unlink()
        print("Manifiesto consumido y eliminado (archive vacío).")


def cmd_status(proj: Path) -> None:
    for label, base in (("databanks", proj / "databanks"),
                        ("databanks_archive", proj / "databanks_archive")):
        print(f"{label}/ ({base}):")
        if not base.is_dir():
            print("  (no existe)")
            continue
        entries = sorted(p for p in base.iterdir() if p.is_dir())
        if not entries:
            print("  (vacío)")
        for e in entries:
            print(f"  {e.name}: {count_files(e)} ficheros")
        if (base / "manifest.json").is_file():
            m = load_manifest(base)
            print(f"  manifest.json: fecha={m.get('date')} bancos={m.get('banks')}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project", help="nombre bajo PROJECTS o ruta directa a la carpeta del proyecto")
    ap.add_argument("action", choices=["archive", "restore", "status"])
    ap.add_argument("--banks", help="coma-separados; sin él archive usa DEFAULT_ARCHIVE (fail-loud si no hay)")
    ap.add_argument("--no-process-check", action="store_true",
                    help="bypass de la guarda de GUI/motor vivos. Válido SOLO para `status` "
                         "(lectura con la GUI abierta); con archive/restore el CLI aborta.")
    args = ap.parse_args(argv)

    if args.no_process_check and args.action != "status":
        fail("--no-process-check solo es válido para 'status' (lectura). archive/restore "
             "mueven ficheros del proyecto y exigen GUI/motor cerrados — enforcement, "
             "no solo documentación.")

    proj = resolve_project(args.project)
    banks = parse_banks(args.banks)

    if args.action == "status":
        cmd_status(proj)
    elif args.action == "archive":
        if banks is None:
            banks = DEFAULT_ARCHIVE.get(proj.name)
            if banks is None:
                fail(f"proyecto '{proj.name}' sin DEFAULT_ARCHIVE — pasa --banks A,B,C explícito "
                     f"(no adivino qué bancos pesan)")
        cmd_archive(proj, banks)
    else:  # restore
        cmd_restore(proj, banks)


if __name__ == "__main__":
    main()
