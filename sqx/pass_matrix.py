"""pass_matrix.py — matriz genérica estrategia × pruebas desde exports de databanks.

Lee los ``databank_<banco>.csv`` de un directorio de salida del pipeline (sqcli
``-databank action=export``, ver ``run_pipeline_headless.sh``) y construye la
matriz de paso: qué pruebas ha pasado cada estrategia.

Reglas (convención casa, ver prod_2024oos_nasdaq.ipynb celdas 4-5):
- ``Results`` hace de roster (editable con ``--roster``); cada banco con
  etiquetas PASSED/FAILED en la columna ``Filters result`` es una prueba.
- Celda con 3 estados: PASSED / FAILED / vacía (estrategia del roster AUSENTE
  en el banco: no llegó — fallo de cómputo MC, pipeline interrumpido…). Los
  ausentes se avisan con su count y no cuentan en el score; ``--strict``
  convierte cualquier ausencia en FAIL-LOUD (proyectos con cadena íntegra).
  Las estrategias de un banco ajenas al roster (restos de corridas previas)
  se ignoran con aviso.
- Si hay colisión de nombres crudos Y existen ``FullRange_TF``/``FullRange_MR``,
  se resuelve estilo con la regla de los pools TF/MR: nombre plano compartido →
  TF; su ``'(1)'`` → MR; resto por pertenencia al banco de su estilo. La clave
  pasa a ser ``style|name`` (los builds numeran cada estilo desde 0: el nombre
  solo NO es clave única). Sin colisión ni par FullRange, la clave es el nombre
  crudo (pools sin estilos, caso R4).
- ``score`` = nº de pruebas pasadas (sobre las pruebas incluidas).

Uso:
    python sqx/pass_matrix.py --dir sqx/output/nasdaq_h4_prod_2024oos_20260922
    python sqx/pass_matrix.py --dir ... --exclude FullRange_TF,FullRange_MR,Real_OOS \\
        --tasks Second_OOS,Different_Markets_and_TimeFrames -o matriz.csv

``--exclude`` quita bancos del análisis (se listan pero no generan columna ni
entran en el score). ``--tasks`` restringe EL SCORE a esas pruebas (las columnas
de las demás pruebas siguen en el CSV). El CSV sale por defecto a
``<dir>/pass_matrix.csv``.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

LABELS_VALIDAS = {"PASSED", "FAILED", ""}
SUF = re.compile(r"\(\d+\)$")


def read_bank(path: Path) -> pd.DataFrame:
    """Lee un export de databank (sep=';', utf-8-sig) y valida el esquema mínimo."""
    if not path.exists():
        raise SystemExit(f"FAIL-LOUD: falta {path}")
    d = pd.read_csv(path, sep=";", encoding="utf-8-sig", dtype=str)
    for col in ("Strategy Name", "Filters result"):
        if col not in d.columns:
            raise SystemExit(f"FAIL-LOUD: {path.name} sin columna {col!r} (¿es un export de métricas?)")
    d["Filters result"] = d["Filters result"].fillna("").str.strip()
    bad = set(d["Filters result"].unique()) - LABELS_VALIDAS
    if bad:
        raise SystemExit(f"FAIL-LOUD: {path.name}: etiquetas fuera de {{PASSED, FAILED, vacío}}: {bad}")
    if d["Strategy Name"].duplicated().any():
        dups = sorted(d["Strategy Name"][d["Strategy Name"].duplicated()].unique())[:5]
        raise SystemExit(f"FAIL-LOUD: {path.name} con nombres duplicados; ejemplos: {dups}")
    return d


def discover_banks(dir_csv: Path) -> dict[str, Path]:
    """databank_<banco>.csv → {banco: path}. Sin exports → FAIL-LOUD."""
    banks = {p.name[len("databank_"):-len(".csv")]: p
             for p in sorted(dir_csv.glob("databank_*.csv"))}
    if not banks:
        raise SystemExit(f"FAIL-LOUD: {dir_csv} sin databank_*.csv (¿directorio correcto?)")
    return banks


def build_style_map(tf_names: set[str], mr_names: set[str], nombres: set[str]) -> dict[str, str]:
    """Nombre crudo → clave ``style|name`` (regla TF/MR de los pools con copias).

    Plano compartido entre estilos → TF; su ``'(1)'`` → MR (la copia llega vía
    ``nombres``, típicamente el roster de Results); resto por pertenencia al
    banco de su estilo. Los ``(N)`` intrínsecos no se tocan."""
    compartidos = tf_names & mr_names
    solo_tf, solo_mr = tf_names - mr_names, mr_names - tf_names

    def asignar(nombre: str) -> str:
        if nombre in compartidos:
            return f"TF|{nombre}"
        if nombre not in tf_names | mr_names:
            base = SUF.sub("", nombre).strip()
            if base in compartidos:
                return f"MR|{base}"
        if nombre in solo_tf:
            return f"TF|{nombre}"
        if nombre in solo_mr:
            return f"MR|{nombre}"
        raise SystemExit(f"FAIL-LOUD: nombre sin estilo asignable: {nombre!r}")

    # la regla debe ser inyectiva: cada nombre crudo cae en UNA clave
    claves = {}
    for n in nombres:
        k = asignar(n)
        if k in claves:
            raise SystemExit(f"FAIL-LOUD: clave compuesta repetida {k!r} ({n!r} y {claves[k]!r})")
        claves[n] = k
    return claves


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Matriz estrategia × pruebas desde databank_*.csv (0 SQX).")
    ap.add_argument("--dir", required=True, help="directorio con los databank_*.csv exportados")
    ap.add_argument("--roster", default="Results", help="banco que hace de roster (default: Results)")
    ap.add_argument("--strict", action="store_true",
                    help="FAIL-LOUD si alguna estrategia del roster está ausente en un banco de pruebas")
    ap.add_argument("--exclude", default="", help="bancos a excluir, separados por coma (p.ej. FullRange_TF,Real_OOS)")
    ap.add_argument("--tasks", default="", help="pruebas que entran en el score, separadas por coma (default: todas)")
    ap.add_argument("-o", "--out", default="", help="CSV de salida (default: <dir>/pass_matrix.csv)")
    a = ap.parse_args(argv)

    dir_csv = Path(a.dir)
    excluidos = {s.strip() for s in a.exclude.split(",") if s.strip()}
    banks = discover_banks(dir_csv)
    if a.roster not in banks:
        raise SystemExit(f"FAIL-LOUD: no hay databank_{a.roster}.csv en {dir_csv} (hace de roster)")
    desconocidos = excluidos - set(banks)
    if desconocidos:
        raise SystemExit(f"FAIL-LOUD: --exclude con bancos inexistentes: {sorted(desconocidos)} "
                         f"(disponibles: {sorted(banks)})")

    # --- roster ---
    res = read_bank(banks[a.roster])
    res = res.set_index("Strategy Name")

    # --- pruebas: bancos con etiquetas PASSED/FAILED, fuera roster y excluidos ---
    pruebas, etiquetas_por_banco = [], {}
    for b, p in banks.items():
        if b in {a.roster} | excluidos:
            continue
        d = read_bank(p)
        labels = set(d["Filters result"].unique()) - {""}
        if not labels:
            print(f"aviso: {b} sin etiquetas en 'Filters result' — no es prueba, se omite")
            continue
        pruebas.append(b)
        etiquetas_por_banco[b] = d

    if not pruebas:
        raise SystemExit("FAIL-LOUD: ningún banco con etiquetas PASSED/FAILED (¿export de las tasks?)")

    # --- clave: nombre crudo o compuesta style|name si hay colisión + par FullRange ---
    nombres = set(res.index)
    tiene_fr = "FullRange_TF" in banks and "FullRange_MR" in banks
    colision = None
    if tiene_fr:
        tf_nombres = set(read_bank(banks["FullRange_TF"])["Strategy Name"])
        mr_nombres = set(read_bank(banks["FullRange_MR"])["Strategy Name"])
        colision = tf_nombres & mr_nombres
    if colision:
        style_map = build_style_map(tf_nombres, mr_nombres, nombres)
        claves = {n: style_map[n] for n in nombres}
        print(f"colisión TF/MR detectada ({len(colision)} nombres compartidos): "
              f"clave compuesta style|name (TF={sum(1 for k in claves.values() if k.startswith('TF'))}, "
              f"MR={sum(1 for k in claves.values() if k.startswith('MR'))})")
    else:
        claves = {n: n for n in nombres}
        print(f"clave = nombre crudo ({len(nombres)} estrategias)")

    # --- matriz: filas = estrategias del roster ---
    claves_s = pd.Series(claves)              # índice = nombre crudo, valor = clave
    matriz = pd.DataFrame({"strategy": claves_s.index, "key": claves_s.to_numpy()})
    if colision:
        matriz["style"] = matriz["key"].str.split("|").str[0]

    score_tasks = ([s.strip() for s in a.tasks.split(",") if s.strip()] if a.tasks else pruebas)
    faltan = set(score_tasks) - set(pruebas)
    if faltan:
        raise SystemExit(f"FAIL-LOUD: --tasks con pruebas sin etiquetas/excluidas: {sorted(faltan)}")

    for b in pruebas:
        d = etiquetas_por_banco[b].set_index("Strategy Name")
        extras = set(d.index) - nombres
        if extras:
            print(f"aviso: {b}: {len(extras)} estrategias ajenas al roster (ignoradas)")
        vals = d["Filters result"] == "PASSED"
        vals = vals[vals.index.isin(nombres)]
        if colision:
            vals = vals.copy()
            vals.index = [style_map[n] for n in vals.index]   # clave compuesta
        col = matriz["key"].map(vals)
        n_nan = int(col.isna().sum())
        if n_nan:
            if a.strict:
                raise SystemExit(f"FAIL-LOUD: pass_{b} con {n_nan} estrategias del roster ausentes en el banco")
            print(f"aviso: {b}: {n_nan} estrategias del roster ausentes en el banco (celdas vacías)")
        else:
            col = col.astype(bool)
        matriz[f"pass_{b}"] = col

    cols_score = [f"pass_{b}" for b in score_tasks]
    matriz["score"] = matriz[cols_score].eq(True).sum(axis=1).astype(int)
    matriz["n_absent"] = matriz[cols_score].isna().sum(axis=1).astype(int)

    # --- métricas del roster que existan (fitness/net/PF por sufijo IS/OOS) ---
    for col in res.columns:
        if col != "Filters result" and res[col].notna().any():
            matriz[col] = matriz["strategy"].map(res[col])

    # --- salida ---
    out = Path(a.out) if a.out else dir_csv / "pass_matrix.csv"
    matriz.to_csv(out, sep=";", index=False)
    print(f"\nmatriz: {len(matriz)} estrategias × {len(pruebas)} pruebas → {out}")
    print(f"score sobre: {', '.join(score_tasks)}\n")
    resumen = pd.DataFrame({
        "n_pass": [int(matriz[f"pass_{b}"].eq(True).sum()) for b in pruebas],
        "n_absent": [int(matriz[f"pass_{b}"].isna().sum()) for b in pruebas],
        "tasa": [float(matriz[f"pass_{b}"].eq(True).mean()) for b in pruebas],
    }, index=pruebas)
    print(resumen.to_string(float_format=lambda v: f"{v:.3f}"))
    dist = matriz["score"].value_counts().sort_index()
    print(f"\ndistribución del score ({'/'.join(map(str, dist.index))}): "
          f"{' / '.join(f'{n} estr.' for n in dist.to_numpy())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())