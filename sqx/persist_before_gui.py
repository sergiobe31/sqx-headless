#!/usr/bin/env python3
"""¿Es SEGURO abrir la GUI de SQX ahora? Comprobación de SOLO LECTURA.

Evita el LANDMINE: abrir la GUI con sqcli corriendo (o sin persistir) corrompe los
databanks — la GUI lee/escribe un estado parcial y se pierden estrategias (caso real:
Results pasó 4000→136 al solapar GUI sobre sqcli vivo, 2026-07-10).

CAMBIÓ (guardrail `sqx-headless-fallos`, R4, 2026-10-21; decisión de Sergio D1): este script ya NO
arranca un `sqcli -run` ni sincroniza nada. Antes, con el motor muerto, lanzaba `save` + `synctofiles` +
`count` por banco sobre un motor NUEVO. Eso no puede AÑADIR datos a disco (las estrategias que vivían en la
RAM del motor anterior ya no existen: o se guardaron antes de matarlo o se perdieron) y, con la carga lazy
de databanks (`sqcli -run` no espera a que el motor termine de cargar), PUEDE QUITARLOS: un `synctofiles`
sobre un banco a medio cargar borra del disco lo que el motor aún no tiene en RAM. Decía "ya puedes abrir la
GUI" tras una escritura ciega. Ahora solo MIRA, con `stop_and_persist.pre_gui_check` (global: la instancia
única es de toda la máquina):

  inseguro si hay un `sqcli` vivo, la GUI ya abierta, un orquestador / driver / cascada vivos (de CUALQUIER
  proyecto), una corrida `--slim` sin restaurar (marcador `databanks_archive/.runner_session.json` o bancos en
  `databanks_archive/`) o un `project.cfx.pre_run_task.bak` pendiente (el .cfx puede tener tasks desactivadas).

Con el motor VIVO el orden correcto es guardar y luego cerrar: `python sqx/stop_and_persist.py <proyecto>`
(persiste y libera el motor); este script solo lo comprueba después.

USO:
  python sqx/persist_before_gui.py <proyecto>

Código de salida: 0 = seguro abrir la GUI · 1 = NO es seguro (o el proyecto no existe) · 2 = falta el proyecto.
"""
from __future__ import annotations

import sys

try:  # ruta de instalación SQX: única fuente en sqx/paths.py
    from sqx import stop_and_persist as SP
except ImportError:  # ejecutado como script (sys.path[0] = sqx/)
    import stop_and_persist as SP

USAGE = ("uso: python sqx/persist_before_gui.py <proyecto>\n"
         "  Comprueba (solo lectura) si es seguro abrir la GUI de SQX. No arranca el motor ni sincroniza.\n"
         "  Con el motor vivo: python sqx/stop_and_persist.py <proyecto> (persiste y libera) y luego esto.")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if not argv:                      # ya no hay proyecto por defecto ("Esqueleto"): hay que decirlo
        print(USAGE, file=sys.stderr)
        return 2
    proj = argv[0]
    projdir = SP.projects_dir() / proj
    if not projdir.is_dir():
        print(f"⛔ No existe el proyecto '{proj}' ({projdir}).")
        return 1

    rep = SP.pre_gui_check(proj)
    print(f"Comprobación previa a abrir la GUI de SQX — proyecto '{proj}' (solo lectura: no arranca el "
          "motor ni sincroniza nada)")
    print("\n=== .sqx en disco por databank ===")
    total = 0
    for name, n in sorted(rep["databanks"].items()):
        total += n
        print(f"  {name}: {n}{'  ⚠️ 0' if n == 0 else ''}")
    print(f"\nTotal .sqx en disco: {total}")

    if rep["safe"]:
        print("\n✓ Ya puedes abrir la GUI de SQX: no hay motor, GUI, pipeline ni cascada vivos, ni bancos "
              "apartados por --slim, ni .bak pendiente.")
        return 0
    print("\n⛔ NO abras la GUI todavía:")
    for r in rep["reasons"]:
        print(f"  · {r}")
    print("\nQué hacer: con el motor o un pipeline vivos → `python sqx/stop_and_persist.py "
          f"{proj}` (guarda y libera); con una corrida --slim sin restaurar → relanza la task/pipeline (reconcilia "
          f"al arrancar) o `python sqx/gui_slim.py {proj} restore`; con un .bak pendiente → restaura el .cfx "
          "desde el botón 'Restaurar .cfx original' del monitor.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
