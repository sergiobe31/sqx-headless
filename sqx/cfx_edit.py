#!/usr/bin/env python3
"""Editar la configuración de un custom project de SQX que YA existe (SQX y su GUI CERRADOS).

Cambia, en las tasks que elijas:
  1. la ventana de test (inicio y fin) de las tasks que tienen ventana;
  2. el reparto IS/OOS (rango fuera de muestra) de una task con ventana;
  3. la fitness de un Build: goals y pesos de la Weighted, o una fitness simple de una lista cerrada;
  4. el máximo de estrategias del databank y la condición de parada de un Build.

NO hace (por diseño; se hace en la GUI de SQX): ampliar la ventana más allá del enlace de datos (binding) del
símbolo, tocar ese binding, crear proyectos o tasks, cambiar símbolos, costes, bloques ni condiciones de
aceptación, ni pasar de fitness simple a Weighted. Esta herramienta NO puede ver qué datos tiene descargados
SQX: solo sabe lo que dice el propio `.cfx`.

Sin `--apply` es una SIMULACIÓN: enseña `campo: antes → después` y los avisos, y no escribe nada. La regla
para asistentes de IA: simular, enseñar el resultado al usuario, esperar su OK y solo entonces repetir con
`--apply`.

Escritura segura (`--apply`): pre-check de procesos (`stop_and_persist.pre_gui_check`: motor, GUI,
orquestador, driver, cascada, `--slim` sin restaurar, `.pre_run_task.bak`), ZIP nuevo en un temporal con los
mismos miembros / orden / compresión, verificación independiente del temporal (lector + modelo del árbol XML:
solo cambia lo pedido), segundo pre-check + sha256 del `project.cfx`, copia
`project.cfx.bak_<motivo>_<AAAAMMDD_HHMMSS>` (nunca se pisa) y `os.replace` atómico con comprobación final.

USO:
  python sqx/cfx_edit.py <proyecto> show [--task SEL ...] [--json]
  python sqx/cfx_edit.py <proyecto> set --task SEL [--task SEL ...] <cambios> [--apply] [--motivo TXT]

Código de salida: 0 bien (simulación o escrito) · 1 rechazado (validación, pre-check, task o métrica
inexistente, estructura no reconocida, proyecto cambiado mientras se preparaba) · 2 uso · 3 falló la
verificación interna (no se escribió nada) · 4 escrito, pero el `.cfx` final no coincide con lo verificado
(escritura concurrente: no se restaura solo, el mensaje dice qué backup usar).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

try:  # patrón de persist_before_gui.py: paquete (tests / PYTHONPATH) o script (sys.path[0] = sqx/)
    from sqx import stop_and_persist as SP
    from sqx import bank_plan as BP
except ImportError:
    import stop_and_persist as SP
    import bank_plan as BP

# ----------------------------- constantes y límites ------------------------------------------------------

#: Fitness simples admitidas (H8: las usadas con éxito en builds del repo).
FITNESS_SIMPLES = ("NetProfit", "ReturnDDRatio", "ProfitFactor", "RExpectancy", "AnnualPctReturnDDRatio")
#: Límites de LA HERRAMIENTA (no son reglas de SQX): lo observado en proyectos guardados por la GUI (H7, H9, H10).
LIM_PESO = (1, 100)
LIM_MAX = (1, 25000)
LIM_PASSED = (1, 25000)
LIM_DIAS = (0, 365)
LIM_HORAS = (0, 48)
LIM_MINUTOS = (0, 59)

PIE_BINDING = ("cfx_edit no puede ver qué datos tiene SQX descargados y nunca cambia el enlace de datos del "
               "símbolo (binding).")

LIMITES_AYUDA = f"""\
Límites de la herramienta (NO son reglas de SQX: se ciñen a lo observado en proyectos guardados por su GUI):
  peso de un goal {LIM_PESO[0]}-{LIM_PESO[1]} · al menos 1 goal en uso al final · máximo de estrategias
  {LIM_MAX[0]}-{LIM_MAX[1]} · passed-count {LIM_PASSED[0]}-{LIM_PASSED[1]} · time-limit: días {LIM_DIAS[0]}-{LIM_DIAS[1]},
  horas {LIM_HORAS[0]}-{LIM_HORAS[1]}, minutos {LIM_MINUTOS[0]}-{LIM_MINUTOS[1]} (total > 0) · fitness simple:
  {", ".join(FITNESS_SIMPLES)}.
--oos-last-pct P (entero 1-99): días = fin - inicio; el IS se redondea HACIA ABAJO al día
  (IS = (100-P)*días // 100), así que el OOS puede salir hasta un día más largo que el P % exacto.
La ventana nueva debe caber en el enlace de datos del símbolo (binding): para ampliarlo, cambia Start/End de
esa task en la GUI de SQX y guarda; esta herramienta no lo toca. Solo se edita con SQX y su GUI cerrados."""


class CfxEditError(Exception):
    """Base de los errores de la herramienta. `rc` = código de salida."""
    rc = 1


class Rejected(CfxEditError):
    """Rechazado sin escribir nada: validación, pre-check, selector, estructura no reconocida..."""
    rc = 1


class UsageError(CfxEditError):
    """Combinación de opciones incoherente."""
    rc = 2


class VerifyFailed(CfxEditError):
    """La verificación interna falló: no se escribió nada."""
    rc = 3


class FinalMismatch(CfxEditError):
    """Escrito, pero el `.cfx` final no es el verificado."""
    rc = 4


# ----------------------------- fechas ----------------------------------------------------------------------

_RX_FECHA = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")


def _parse_date(s: str, que: str = "fecha") -> dt.date:
    if not isinstance(s, str) or not _RX_FECHA.match(s):
        raise Rejected(f"{que} «{s}» no tiene el formato AAAA.MM.DD")
    try:
        return dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        raise Rejected(f"{que} «{s}» no es una fecha real")


def _fmt(d: dt.date) -> str:
    return f"{d.year:04d}.{d.month:02d}.{d.day:02d}"


def _ms_to_date(ms: int) -> dt.date:
    """Milisegundos epoch (00:00 UTC) -> fecha UTC."""
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).date()


# ----------------------------- LECTOR (ElementTree, rutas exactas) -----------------------------------------

def _nodes(root: ET.Element) -> dict[str, list[ET.Element]]:
    """Nodos por rutas canónicas (H3-H10). Compartido por el lector y por el MODELO de verificación: el
    modelo nunca usa posiciones ni resultados de los regex del escritor."""
    n: dict[str, list[ET.Element]] = {}
    n["data"] = root.findall("Data")
    n["setups"] = root.findall("Data/Setups")
    n["setup"] = root.findall("Data/Setups/Setup")
    n["chart"] = n["setup"][0].findall("Chart") if n["setup"] else []
    sym = n["chart"][0].get("symbol") if len(n["chart"]) == 1 else None
    n["binding"] = ([s for s in root.findall("Resources/Symbols/Symbol") if s.get("name") == sym]
                    if sym is not None else [])
    n["oos"] = root.findall("Data/OutOfSample")
    n["cross_root"] = root.findall("CrossChecks/RetestOnAdditionalMarkets")
    n["cross_setup"] = root.findall("CrossChecks/RetestOnAdditionalMarkets/Settings/Setups/Setup")
    n["rankings"] = root.findall("Rankings")
    n["max"] = root.findall("Rankings/MaxStrategies")
    n["stop"] = root.findall("Rankings/StopCondition")
    n["ranking"] = root.findall("Rankings/FitnessCriteria/Settings/Ranking")
    return n


def _symbol(n: dict) -> str | None:
    return n["chart"][0].get("symbol") if len(n["chart"]) == 1 else None


def _read_root(root: ET.Element) -> dict:
    if root.tag != "Settings":
        raise Rejected(f"estructura no reconocida: la raíz del XML de la task es <{root.tag}>, no <Settings>")
    n = _nodes(root)
    d: dict = {"counts": {k: len(v) for k, v in n.items()}}
    d["has_data"] = len(n["data"]) == 1
    d["symbol"] = _symbol(n)
    st = n["setup"][0] if n["setup"] else None
    d["window"] = ({"from": st.get("dateFrom"), "to": st.get("dateTo")} if st is not None else None)
    if len(n["binding"]) == 1:
        b = n["binding"][0]
        try:
            d["binding"] = {"from_ms": int(b.get("dateFrom")), "to_ms": int(b.get("dateTo"))}
        except (TypeError, ValueError):
            d["binding"] = None
    else:
        d["binding"] = None
    if n["oos"]:
        o = n["oos"][0]
        d["oos"] = {
            "show_graph": o.get("showGraph"),
            "foreign": sorted({c.tag for c in o if c.tag != "Range"}),
            "ranges": [{"from": r.get("dateFrom"), "to": r.get("dateTo"), "type": r.get("type"),
                        "extra": sorted(set(r.attrib) - {"dateFrom", "dateTo", "type"})}
                       for r in o.findall("Range")],
        }
    else:
        d["oos"] = None
    cross_setups = []
    for s in n["cross_setup"]:
        ch = s.find("Chart")
        mt = s.findall("MainTestValues")      # hijo de CADA <Setup> del cross check (CR1-O1)
        cross_setups.append({"symbol": ch.get("symbol") if ch is not None else None,
                             "from": s.get("dateFrom"), "to": s.get("dateTo"),
                             "main_test_dates": mt[0].get("dates") if len(mt) == 1 else None})
    d["cross"] = {"setups": cross_setups} if n["cross_root"] else None
    if n["rankings"]:
        fit = None
        if n["ranking"]:
            r = n["ranking"][0]
            fit = {"type": r.get("type"), "extra": sorted(set(r.attrib) - {"type"}),
                   "goals": [{"metric": g.get("type"), "use": g.get("use"), "weight": g.get("weight"),
                              "value_type": g.get("valueType"), "target": g.get("target")}
                             for g in r.findall("Goal")]}
        d["rankings"] = {"max_strategies": (n["max"][0].text.strip() if n["max"] and n["max"][0].text else None),
                         "stop": (dict(n["stop"][0].attrib) if n["stop"] else None),
                         "fitness": fit}
    else:
        d["rankings"] = None
    return d


def read_task(xml_bytes: bytes) -> dict:
    """Lee el XML de UNA task y devuelve sus valores (estructura de datos plana, comparable con ==)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise Rejected(f"estructura no reconocida: el XML de la task no se puede leer ({e})")
    return _read_root(root)


# ----------------------------- ESCRITOR (cirugía de texto) -------------------------------------------------

def _set_attr(tag: str, nombre: str, valor: str) -> str:
    """Cambia el valor del atributo `nombre` DENTRO del texto de UN tag ya localizado. Nombre exacto precedido
    de espacio (no toca `xdateFrom`); tiene que casar exactamente una vez."""
    rx = re.compile(r'(?<=\s)' + re.escape(nombre) + r'="[^"]*"')
    if len(rx.findall(tag)) != 1:
        raise Rejected(f"estructura no reconocida: no encuentro exactamente un atributo «{nombre}» en {tag.strip()}")
    return rx.sub(lambda _m: f'{nombre}="{valor}"', tag)


def _w_window(text: str, a: str, b: str) -> str:
    """W1: dateFrom/dateTo del primer <Setup> de <Data><Setups> (adyacencia observada, H3)."""
    if len(re.findall(r"<Data\b", text)) != 1:
        raise Rejected("estructura no reconocida: <Data> no aparece exactamente una vez")
    ms = list(re.finditer(r"(<Data\b[^>]*>\s*<Setups\b[^>]*>\s*)(<Setup\b[^>]*>)", text))
    if len(ms) != 1:
        raise Rejected("estructura no reconocida: no encuentro el <Setup> principal justo dentro de <Data><Setups>")
    m = ms[0]
    tag = _set_attr(_set_attr(m.group(2), "dateFrom", a), "dateTo", b)
    return text[:m.start(2)] + tag + text[m.end(2):]


def _w_oos(text: str, ranges: list[tuple[str, str]]) -> str:
    """W2: reconstruye el <OutOfSample> que sigue al </Setups> de <Data> (H6). Se sustituye SOLO el elemento;
    su cola (fin de línea e indentación posteriores) se conserva byte a byte."""
    if len(re.findall(r"<Data\b", text)) != 1:
        raise Rejected("estructura no reconocida: <Data> no aparece exactamente una vez")
    dm = re.search(r"<Data\b[^>]*>.*?</Data>", text, re.S)
    if not dm:
        raise Rejected("estructura no reconocida: no encuentro el bloque <Data>…</Data>")
    sub = dm.group(0)
    ms = list(re.finditer(r"</Setups>\s*<OutOfSample\b([^>]*?)(/>|>)", sub))
    if len(ms) != 1:
        raise Rejected("estructura no reconocida: no encuentro un único <OutOfSample> justo tras </Setups> en <Data>")
    m = ms[0]
    s_rel = sub.index("<OutOfSample", m.start())
    if m.group(2) == "/>":
        e_rel = m.end()
    else:
        c = sub.find("</OutOfSample>", m.end())
        if c < 0:
            raise Rejected("estructura no reconocida: <OutOfSample> sin cierre")
        e_rel = c + len("</OutOfSample>")
    s_abs = dm.start() + s_rel
    e_abs = dm.start() + e_rel
    ctx = re.search(r"(\r\n|\n)([ \t]*)$", text[:s_abs])
    if not ctx:
        raise Rejected("estructura no reconocida: <OutOfSample> no empieza en su propia línea")
    eol, indent = ctx.group(1), ctx.group(2)
    attrs = m.group(1).rstrip()
    if not ranges:
        nuevo = f"<OutOfSample{attrs} />"
    else:
        hijo = indent + "  "
        nuevo = (f"<OutOfSample{attrs}>"
                 + "".join(f'{eol}{hijo}<Range dateFrom="{a}" dateTo="{b}" />' for a, b in ranges)
                 + f"{eol}{indent}</OutOfSample>")
    return text[:s_abs] + nuevo + text[e_abs:]


def _en_rankings(text: str, fn) -> str:
    """Aplica `fn(bloque)` al texto del único <Rankings …>…</Rankings>."""
    if len(re.findall(r"<Rankings\b", text)) != 1:
        raise Rejected("estructura no reconocida: <Rankings> no aparece exactamente una vez")
    m = re.search(r"<Rankings\b[^>]*>.*?</Rankings>", text, re.S)
    if not m:
        raise Rejected("estructura no reconocida: no encuentro el bloque <Rankings>…</Rankings>")
    return text[:m.start()] + fn(m.group(0)) + text[m.end():]


def _w_max(block: str, n: int) -> str:
    """W3: texto de <MaxStrategies> (H9), exactamente uno en <Rankings>."""
    rx = re.compile(r"<MaxStrategies>[^<]*</MaxStrategies>")
    if len(rx.findall(block)) != 1:
        raise Rejected("estructura no reconocida: <MaxStrategies> no aparece exactamente una vez en <Rankings>")
    return rx.sub(lambda _m: f"<MaxStrategies>{n}</MaxStrategies>", block)


def _w_stop(block: str, attrs: dict[str, str]) -> str:
    """W3: atributos de <StopCondition …/> (H10)."""
    ms = list(re.finditer(r"<StopCondition\b[^>]*>", block))
    if len(ms) != 1:
        raise Rejected("estructura no reconocida: <StopCondition> no aparece exactamente una vez")
    tag = ms[0].group(0)
    for k, v in attrs.items():
        tag = _set_attr(tag, k, v)
    return block[:ms[0].start()] + tag + block[ms[0].end():]


def _span_ranking(block: str) -> tuple[int, int]:
    """Posición del elemento <Ranking …> (autocerrado o con goals) dentro del bloque <Rankings>."""
    if len(re.findall(r"<Ranking\b", block)) != 1:
        raise Rejected("estructura no reconocida: <Ranking> no aparece exactamente una vez en <Rankings>")
    m = re.search(r"<Ranking\b[^>]*?(/>|>)", block)
    if m.group(1) == "/>":
        return m.start(), m.end()
    c = block.find("</Ranking>", m.end())
    if c < 0:
        raise Rejected("estructura no reconocida: <Ranking> sin cierre")
    return m.start(), c + len("</Ranking>")


def _w_goal(block: str, metrica: str, use: str | None, weight: str | None) -> str:
    """W3: atributos use/weight del <Goal type="M" …/> dentro del <Ranking> (H7)."""
    s, e = _span_ranking(block)
    el = block[s:e]
    ms = [m for m in re.finditer(r"<Goal\b[^>]*>", el)
          if re.search(r'(?<=\s)type="' + re.escape(metrica) + '"', m.group(0))]
    if len(ms) != 1:
        raise Rejected(f"estructura no reconocida: el goal «{metrica}» no aparece exactamente una vez")
    tag = ms[0].group(0)
    if use is not None:
        tag = _set_attr(tag, "use", use)
    if weight is not None:
        tag = _set_attr(tag, "weight", weight)
    el = el[:ms[0].start()] + tag + el[ms[0].end():]
    return block[:s] + el + block[e:]


def _w_fitness(block: str, metrica: str) -> str:
    """W3: sustituye el elemento <Ranking> entero por `<Ranking type="M" />` (H7); la cola se conserva."""
    s, e = _span_ranking(block)
    return block[:s] + f'<Ranking type="{metrica}" />' + block[e:]


def _write_task(text: str, ed: dict) -> str:
    """Aplica las ediciones resueltas de una task (ver `_resolve`) al texto del XML."""
    if ed.get("window"):
        text = _w_window(text, *ed["window"])
    if ed.get("oos") is not None:
        text = _w_oos(text, ed["oos"])
    if ed.get("max") is not None:
        text = _en_rankings(text, lambda b: _w_max(b, ed["max"]))
    if ed.get("stop"):
        text = _en_rankings(text, lambda b: _w_stop(b, ed["stop"]))
    for metrica, vals in (ed.get("goals") or {}).items():
        text = _en_rankings(text, lambda b, m=metrica, v=vals: _w_goal(b, m, v.get("use"), v.get("weight")))
    if ed.get("fitness"):
        text = _en_rankings(text, lambda b: _w_fitness(b, ed["fitness"]))
    return text


# ----------------------------- MODELO (ElementTree, independiente del escritor) -----------------------------

def _model(orig_bytes: bytes, ed: dict) -> ET.Element:
    """Aplica las mismas ediciones LÓGICAS sobre el árbol original seleccionando los nodos SOLO por las
    rutas canónicas del lector (`_nodes`), nunca con los regex del escritor."""
    root = ET.fromstring(orig_bytes)
    n = _nodes(root)
    if ed.get("window"):
        st = n["setup"][0]
        st.set("dateFrom", ed["window"][0])
        st.set("dateTo", ed["window"][1])
    if ed.get("oos") is not None:
        o = n["oos"][0]
        for c in list(o):
            o.remove(c)
        for a, b in ed["oos"]:
            ET.SubElement(o, "Range", {"dateFrom": a, "dateTo": b})
    if ed.get("max") is not None:
        n["max"][0].text = str(ed["max"])
    if ed.get("stop"):
        for k, v in ed["stop"].items():
            n["stop"][0].set(k, v)
    for metrica, vals in (ed.get("goals") or {}).items():
        gs = [g for g in n["ranking"][0].findall("Goal") if g.get("type") == metrica]
        g = gs[0]
        if vals.get("use") is not None:
            g.set("use", vals["use"])
        if vals.get("weight") is not None:
            g.set("weight", vals["weight"])
    if ed.get("fitness"):
        r = n["ranking"][0]
        for c in list(r):
            r.remove(c)
        r.attrib.clear()
        r.set("type", ed["fitness"])
        r.text = None
    return root


def _expected(old: dict, ed: dict) -> dict:
    """Lo que debe devolver el lector tras la edición (el `old` del lector con las ediciones aplicadas)."""
    e = copy.deepcopy(old)
    if ed.get("window"):
        e["window"] = {"from": ed["window"][0], "to": ed["window"][1]}
    if ed.get("oos") is not None:
        e["oos"]["ranges"] = [{"from": a, "to": b, "type": None, "extra": []} for a, b in ed["oos"]]
        e["oos"]["foreign"] = []
    if ed.get("max") is not None:
        e["rankings"]["max_strategies"] = str(ed["max"])
    if ed.get("stop"):
        e["rankings"]["stop"].update(ed["stop"])
    for metrica, vals in (ed.get("goals") or {}).items():
        for g in e["rankings"]["fitness"]["goals"]:
            if g["metric"] == metrica:
                if vals.get("use") is not None:
                    g["use"] = vals["use"]
                if vals.get("weight") is not None:
                    g["weight"] = vals["weight"]
    if ed.get("fitness"):
        e["rankings"]["fitness"] = {"type": ed["fitness"], "extra": [], "goals": []}
    return e


_RUTA_OOS = "/Settings/Data/OutOfSample"
_RX_BLANCO = re.compile(r"[ \t\r\n]*")


def _blanco(x: str | None) -> bool:
    """None o solo espacios / tabuladores / fines de línea."""
    return x is None or _RX_BLANCO.fullmatch(x) is not None


def _tree_diff(a: ET.Element, b: ET.Element, path: str = "") -> str | None:
    """Comparación recursiva EXACTA (tag, atributos, texto, cola, hijos; sin recortar espacios). Única
    excepción: el texto interior del <OutOfSample> reconstruido y la cola de sus <Range> (espacios que el
    escritor reconstruye a propósito), y solo si AMBOS lados son None o solo blancos (CR1-C1)."""
    here = f"{path}/{a.tag}"
    if a.tag != b.tag:
        return f"{path}: etiqueta {a.tag!r} ≠ {b.tag!r}"
    if a.attrib != b.attrib:
        return f"{here}: atributos {dict(a.attrib)} ≠ {dict(b.attrib)}"
    if here == _RUTA_OOS:
        if not (_blanco(a.text) and _blanco(b.text)):
            return f"{here}: texto no blanco {a.text!r} ≠ {b.text!r}"
    elif a.text != b.text:
        return f"{here}: texto {a.text!r} ≠ {b.text!r}"
    if here == _RUTA_OOS + "/Range":
        if not (_blanco(a.tail) and _blanco(b.tail)):
            return f"{here}: cola no blanca {a.tail!r} ≠ {b.tail!r}"
    elif a.tail != b.tail:
        return f"{here}: cola {a.tail!r} ≠ {b.tail!r}"
    ca, cb = list(a), list(b)
    if len(ca) != len(cb):
        return f"{here}: {len(ca)} hijos ≠ {len(cb)}"
    for x, y in zip(ca, cb):
        r = _tree_diff(x, y, here)
        if r:
            return r
    return None


_RX_OOS_ANY = re.compile(r"<OutOfSample\b[^>]*?(?:/>|>.*?</OutOfSample>)", re.S)


_RX_RANKING_ANY = re.compile(r"<Ranking\b[^>]*?(?:/>|>.*?</Ranking>)", re.S)


def _sin_oos(t: str, y_ranking: bool = False) -> str:
    """Texto sin el elemento OutOfSample (y, si se pide, sin el <Ranking>: al pasar de Weighted a fitness simple
    desaparecen sus líneas a propósito)."""
    t = _RX_OOS_ANY.sub("", t)
    return _RX_RANKING_ANY.sub("", t) if y_ranking else t


def _eol_counts(t: str) -> tuple[int, int]:
    return t.count("\r\n"), len(re.findall(r"(?<!\r)\n", t))


def _check_eol(orig: str, new: str, oos_cambia: bool, fit_cambia: bool = False) -> str | None:
    """f) fuera del OutOfSample los recuentos de CRLF y LF sueltos no cambian; el bloque reconstruido usa solo
    el fin de línea local. # PLAN-DESVIO (plan v2.1 §4.3.f, CR1-C7): con --fitness (Weighted -> simple) el
    <Ranking> sustituido también se excluye del recuento: sus líneas de goals desaparecen a propósito y el
    elemento nuevo no tiene líneas."""
    a, b = _sin_oos(orig, fit_cambia), _sin_oos(new, fit_cambia)
    if _eol_counts(a) != _eol_counts(b):
        return (f"cambió el recuento de fines de línea fuera del OutOfSample "
                f"({_eol_counts(a)} → {_eol_counts(b)})")
    if oos_cambia:
        dm = re.search(r"<Data\b[^>]*>.*?</Data>", new, re.S)
        m = _RX_OOS_ANY.search(new, dm.start(), dm.end()) if dm else None
        if not m:
            return "no encuentro el OutOfSample reconstruido"
        ctx = re.search(r"(\r\n|\n)[ \t]*$", new[:m.start()])
        if not ctx:
            return "el OutOfSample reconstruido no empieza en su propia línea"
        bloque = m.group(0)
        crlf, lf = _eol_counts(bloque)
        if ctx.group(1) == "\r\n" and lf:
            return "el bloque OutOfSample reconstruido mezcla LF sueltos en un fichero CRLF"
        if ctx.group(1) == "\n" and crlf:
            return "el bloque OutOfSample reconstruido mezcla CRLF en un fichero LF"
    return None


def _decode(b: bytes, que: str) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        raise Rejected(f"estructura no reconocida: {que} no es UTF-8")


# ----------------------------- datos del proyecto ---------------------------------------------------------

@dataclass
class TaskPlan:
    idx: int
    label: str
    xml: str
    type: str
    orig_bytes: bytes
    old: dict
    edits: dict
    new_bytes: bytes
    lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Plan:
    proj: str
    projdir: Path
    cfx: Path
    sha0: str
    members: list  # [(ZipInfo, bytes)] en el orden del ZIP original
    tasks: list[TaskPlan]


@dataclass
class Changes:
    """Cambios pedidos, tal cual llegan de la CLI (cadenas): `plan_edit` los parsea y valida."""
    dates: str | None = None            # "AAAA.MM.DD:AAAA.MM.DD"
    oos: str | None = None              # "AAAA.MM.DD:AAAA.MM.DD" | "none"
    oos_last_pct: int | str | None = None
    goal: list[str] = field(default_factory=list)       # "METRICA=PESO"
    goal_off: list[str] = field(default_factory=list)   # "METRICA"
    goals_only: bool = False
    fitness: str | None = None
    max_strategies: int | str | None = None
    stop: str | None = None             # never | databank-full | passed-count:N | time-limit:D:H:M


class _Ctx:
    """Proyecto leído una sola vez (bytes, sha256, miembros del ZIP y tasks)."""

    def __init__(self, proj: str):
        if (not proj or proj in (".", "..") or "/" in proj or "\\" in proj or os.sep in proj):
            raise Rejected(f"«{proj}» no es un nombre de proyecto (solo nombres bajo la carpeta de proyectos de SQX)")
        self.proj = proj
        self.projdir = SP.projects_dir() / proj
        self.cfx = self.projdir / "project.cfx"
        if not self.projdir.is_dir():
            raise Rejected(f"no existe el proyecto «{proj}» ({self.projdir})")
        if not self.cfx.is_file():
            raise Rejected(f"el proyecto «{proj}» no tiene project.cfx")
        self.data = self.cfx.read_bytes()
        self.sha = hashlib.sha256(self.data).hexdigest()
        try:
            with zipfile.ZipFile(io.BytesIO(self.data)) as z:
                self.members = [(i, z.read(i)) for i in z.infolist()]
            names = [i.filename for i, _ in self.members]
            if len(set(names)) != len(names):
                raise Rejected("estructura no reconocida: el .cfx tiene miembros ZIP con nombre repetido")
            if "config.xml" not in names:
                raise Rejected("estructura no reconocida: el .cfx no tiene config.xml")
            self.tasks = BP._tasks_of_cfx(io.BytesIO(self.data))
        except zipfile.BadZipFile as e:
            raise Rejected(f"el project.cfx no es un ZIP válido: {e}")
        self.by_name = {i.filename: b for i, b in self.members}

    def member(self, name: str) -> bytes:
        if name not in self.by_name:
            raise Rejected(f"estructura no reconocida: {name} no está en el .cfx")
        return self.by_name[name]


def _select(sels: list[str], tasks: list[dict]) -> list[int]:
    """SEL = índice 0-based de `--list`, label exacto (title, si no name) o taskXMLFile exacto. Sin repetir."""
    out: list[int] = []
    for sel in sels:
        sel = str(sel)
        if re.fullmatch(r"\d+", sel):
            i = int(sel)
            if i >= len(tasks):
                raise Rejected(f"no existe la task con índice {i} (hay {len(tasks)}: 0-{len(tasks) - 1})")
            cand = [i]
        else:
            cand = [i for i, t in enumerate(tasks) if t["label"] == sel or t["xml"] == sel]
            if not cand:
                disp = "; ".join(f"{i}: {t['label']}" for i, t in enumerate(tasks))
                raise Rejected(f"ninguna task coincide con «{sel}». Tasks disponibles: {disp}")
            if len(cand) > 1:
                disp = "; ".join(f"{i}: {tasks[i]['label']} ({tasks[i]['xml']})" for i in cand)
                raise Rejected(f"«{sel}» es ambiguo, coincide con varias tasks: {disp}. Usa el índice.")
        if cand[0] not in out:
            out.append(cand[0])
    return out


# ----------------------------- show -----------------------------------------------------------------------

def _direccion(vt: str | None) -> str | None:
    return {"1": "max", "2": "min"}.get(vt or "")


def _describe(d: dict) -> dict:
    """Valores de `read_task` en el esquema (fijo) de `show`."""
    out: dict = {"window": None, "symbol": d["symbol"], "binding": None, "oos": None,
                 "cross_checks": None, "rankings": None}
    if d["window"]:
        out["window"] = {"from": d["window"]["from"], "to": d["window"]["to"]}
    if d["binding"]:
        f, t = d["binding"]["from_ms"], d["binding"]["to_ms"]
        out["binding"] = {"from": _fmt(_ms_to_date(f)), "to": _fmt(_ms_to_date(t)), "from_ms": f, "to_ms": t}
    if d["oos"] is not None:
        out["oos"] = {"show_graph": d["oos"]["show_graph"],
                      "ranges": [{"from": r["from"], "to": r["to"], "type": r["type"]} for r in d["oos"]["ranges"]]}
    if d["cross"] is not None:
        out["cross_checks"] = {"setups": [dict(s) for s in d["cross"]["setups"]]}
    if d["rankings"] is not None:
        rk = d["rankings"]
        fit = None
        if rk["fitness"] is not None:
            fit = {"type": rk["fitness"]["type"],
                   "goals": [{"metric": g["metric"], "weight": g["weight"], "value_type": g["value_type"],
                              "direction": _direccion(g["value_type"])}
                             for g in rk["fitness"]["goals"] if g["use"] == "true"]}
        out["rankings"] = {"fitness": fit, "max_strategies": rk["max_strategies"],
                           "stop": (dict(rk["stop"]) if rk["stop"] is not None else None)}
    return out


def show(proj: str, sels: list[str] | None = None) -> list[dict]:
    """Solo lectura (sin pre-checks): una entrada por task."""
    ctx = _Ctx(proj)
    idxs = _select(sels, ctx.tasks) if sels else list(range(len(ctx.tasks)))
    out = []
    for i in idxs:
        t = ctx.tasks[i]
        e = {"index": i, "label": t["label"], "type": t["type"], "active": t["active"], "file": t["xml"]}
        try:
            e.update(_describe(read_task(ctx.member(t["xml"]))))
        except CfxEditError as ex:
            e["error"] = str(ex)
        out.append(e)
    return out


def _fmt_show(entries: list[dict]) -> str:
    L: list[str] = []
    for e in entries:
        L.append(f"[{e['index']}] {e['label']}  tipo={e['type']}  activa={'sí' if e['active'] else 'no'}  "
                 f"fichero={e['file']}")
        if "error" in e:
            L.append(f"    (no se pudo leer: {e['error']})")
            continue
        if e["window"]:
            L.append(f"    ventana de test: {e['window']['from']} – {e['window']['to']}   símbolo principal: {e['symbol']}")
        if e["binding"]:
            b = e["binding"]
            L.append(f"    binding de datos de {e['symbol']}: {b['from']} – {b['to']}")
        elif e["window"]:
            L.append("    binding de datos del símbolo principal: no encontrado (o repetido)")
        if e["oos"] is not None:
            if e["oos"]["ranges"]:
                for r in e["oos"]["ranges"]:
                    tipo = f" (tipo {r['type']})" if r["type"] else ""
                    L.append(f"    OOS: {r['from']} – {r['to']}{tipo}")
            else:
                L.append("    OOS: ninguno")
        if e["cross_checks"] is not None:
            cc = e["cross_checks"]
            L.append("    cross checks (mercados adicionales):")
            for s in cc["setups"]:
                L.append(f"      {s['symbol']}: {s['from']} – {s['to']}  (MainTestValues dates={s['main_test_dates']})")
        if e["rankings"] is not None:
            rk = e["rankings"]
            if rk["fitness"] is not None:
                f = rk["fitness"]
                if f["type"] == "Weighted":
                    L.append("    fitness: Weighted; goals en uso:")
                    for g in f["goals"]:
                        d = f", {g['direction']}" if g["direction"] else ""
                        L.append(f"      {g['metric']}  peso {g['weight']}  (valueType {g['value_type']}{d})")
                else:
                    L.append(f"    fitness: {f['type']} (simple)")
            L.append(f"    máximo de estrategias: {rk['max_strategies']}")
            if rk["stop"] is not None:
                L.append("    parada: " + " ".join(f'{k}="{v}"' for k, v in rk["stop"].items()))
    L.append("")
    L.append(PIE_BINDING)
    return "\n".join(L)


# ----------------------------- parseo y validación de cambios ----------------------------------------------

_RX_METRICA = re.compile(r"^[A-Za-z0-9_]+$")


def _parse_rango(s: str, flag: str) -> tuple[dt.date, dt.date]:
    partes = str(s).split(":")
    if len(partes) != 2:
        raise UsageError(f"{flag}: formato AAAA.MM.DD:AAAA.MM.DD (recibido «{s}»)")
    a = _parse_date(partes[0], f"{flag}: inicio")
    b = _parse_date(partes[1], f"{flag}: fin")
    if not a < b:
        raise Rejected(f"{flag}: el inicio ({_fmt(a)}) tiene que ser anterior al fin ({_fmt(b)})")
    return a, b


def _int_en(s, nombre: str, lim: tuple[int, int]) -> int:
    if not re.fullmatch(r"\d+", str(s).strip()):
        raise Rejected(f"{nombre}: «{s}» no es un entero")
    v = int(str(s).strip())
    if not lim[0] <= v <= lim[1]:
        raise Rejected(f"{nombre}: {v} fuera del límite de la herramienta ({lim[0]}-{lim[1]})")
    return v


def _parse_changes(ch: Changes) -> dict:
    p: dict = {}
    if ch.dates is not None:
        p["dates"] = _parse_rango(ch.dates, "--dates")
    if ch.oos is not None and ch.oos_last_pct is not None:
        raise UsageError("--oos y --oos-last-pct son excluyentes")
    if ch.oos is not None:
        p["oos"] = "none" if ch.oos == "none" else _parse_rango(ch.oos, "--oos")
    if ch.oos_last_pct is not None:
        p["oos_pct"] = _int_en(ch.oos_last_pct, "--oos-last-pct", (1, 99))
    goals: dict[str, int] = {}
    for g in ch.goal:
        if "=" not in g:
            raise UsageError(f"--goal: formato MÉTRICA=PESO (recibido «{g}»)")
        m, w = g.split("=", 1)
        if not _RX_METRICA.match(m):
            raise Rejected(f"--goal: nombre de métrica no válido «{m}»")
        if m in goals:
            raise UsageError(f"--goal: la métrica {m} está repetida")
        goals[m] = _int_en(w, f"--goal {m}: peso", LIM_PESO)
    off: list[str] = []
    for m in ch.goal_off:
        if not _RX_METRICA.match(m):
            raise Rejected(f"--goal-off: nombre de métrica no válido «{m}»")
        if m in goals or m in off:
            raise UsageError(f"la métrica {m} aparece repetida o a la vez en --goal y --goal-off")
        off.append(m)
    if ch.goals_only and not goals:
        raise UsageError("--goals-only exige al menos un --goal")
    if goals or off:
        p["goals"] = goals
        p["goals_off"] = off
        p["goals_only"] = bool(ch.goals_only)
    if ch.fitness is not None:
        if goals or off or ch.goals_only:
            raise UsageError("--fitness y --goal / --goal-off / --goals-only son excluyentes")
        if ch.fitness not in FITNESS_SIMPLES:
            raise Rejected(f"--fitness: «{ch.fitness}» no está entre las admitidas ({', '.join(FITNESS_SIMPLES)})")
        p["fitness"] = ch.fitness
    if ch.max_strategies is not None:
        p["max"] = _int_en(ch.max_strategies, "--max-strategies", LIM_MAX)
    if ch.stop is not None:
        s = ch.stop
        if s in ("never", "databank-full"):
            p["stop"] = {"type": s}
        elif s.startswith("passed-count:"):
            n = _int_en(s.split(":", 1)[1], "--stop passed-count", LIM_PASSED)
            p["stop"] = {"type": "passed-count", "passedStrategies": str(n)}
        elif s.startswith("time-limit:"):
            partes = s.split(":")
            if len(partes) != 4:
                raise UsageError("--stop time-limit:D:H:M (días:horas:minutos)")
            d = _int_en(partes[1], "--stop time-limit días", LIM_DIAS)
            h = _int_en(partes[2], "--stop time-limit horas", LIM_HORAS)
            m = _int_en(partes[3], "--stop time-limit minutos", LIM_MINUTOS)
            if d + h + m == 0:
                raise Rejected("--stop time-limit: el tiempo total tiene que ser mayor que 0")
            p["stop"] = {"type": "time-limit", "days": str(d), "hours": str(h), "minutes": str(m)}
        else:
            raise UsageError("--stop: never | databank-full | passed-count:N | time-limit:D:H:M")
    if not p:
        raise UsageError("no hay ningún cambio que aplicar (usa --dates, --oos, --goal, --fitness, "
                         "--max-strategies, --stop...)")
    return p


def _estado_uso(g: dict) -> str:
    return f"en uso, peso {g['weight']}" if g["use"] == "true" else f"sin uso, peso {g['weight']}"


def _resolve(t: dict, old: dict, p: dict) -> tuple[dict, list[str], list[str]]:
    """Valida los cambios sobre el estado FINAL de una task y devuelve (ediciones, líneas, avisos)."""
    ed: dict = {}
    lines: list[str] = []
    warns: list[str] = []
    c = old["counts"]
    quiere_data = any(k in p for k in ("dates", "oos", "oos_pct"))
    quiere_build = any(k in p for k in ("goals", "fitness", "max", "stop"))
    if quiere_data and c["data"] > 1:
        raise Rejected(f"estructura no reconocida: <Data> aparece {c['data']} veces")
    if quiere_data and c["data"] != 1:
        raise Rejected(f"la task es de tipo {t['type']} y no tiene ventana ni OOS (no tiene <Data>): "
                       "no hay nada que editar con --dates / --oos / --oos-last-pct")
    if quiere_data and t["type"] not in ("Build", "Retest"):      # CR1-O2 (p. ej. Optimize: <Data useFromPreviousTask>)
        raise Rejected(f"cfx_edit solo cambia la ventana y el OOS de tasks Build y Retest (esta es {t['type']})")
    if quiere_build and t["type"] != "Build":
        raise Rejected(f"--goal / --fitness / --max-strategies / --stop solo se admiten en tasks Build "
                       f"(esta es {t['type']}: su efecto en otros tipos no está establecido)")

    # ---- ventana
    if quiere_data:
        if c["setups"] != 1 or c["setup"] < 1 or c["chart"] != 1 or not old["window"]:
            raise Rejected("estructura no reconocida: no encuentro un único <Setup> principal con su <Chart>")
        ws = _parse_date(old["window"]["from"], "ventana actual: inicio")
        we = _parse_date(old["window"]["to"], "ventana actual: fin")
    if "dates" in p:
        a, b = p["dates"]
        if c["binding"] != 1 or not old["binding"]:
            raise Rejected(f"no encuentro un único enlace de datos (binding) para el símbolo {old['symbol']}: "
                           "no puedo comprobar que la ventana quepa")
        bf, bt = _ms_to_date(old["binding"]["from_ms"]), _ms_to_date(old["binding"]["to_ms"])
        if a < bf or b > bt:
            raise Rejected(f"la ventana se sale de los datos enlazados a esta task ({_fmt(bf)}–{_fmt(bt)}). "
                           "Amplía las fechas en la GUI de SQX (cambia Start/End de esa task y guarda) o elige "
                           f"una ventana dentro de {_fmt(bf)}–{_fmt(bt)}")
        ed["window"] = (_fmt(a), _fmt(b))
        lines.append(f"ventana: {old['window']['from']}–{old['window']['to']} → {_fmt(a)}–{_fmt(b)}")
        ws, we = a, b

    # ---- OOS
    def _oos_txt(rs):
        return "ninguno" if not rs else ", ".join(f"{r[0]}–{r[1]}" for r in rs)

    if "oos" in p or "oos_pct" in p:
        if c["oos"] != 1 or old["oos"] is None:
            raise Rejected("estructura no reconocida: no encuentro un único <OutOfSample> en <Data>")
        o = old["oos"]
        if any(r["type"] is not None for r in o["ranges"]):
            raise Rejected("el OOS actual tiene rangos con tipo (p. ej. isv): esta herramienta no los edita; "
                           "cámbialos en la GUI de SQX")
        if o["foreign"] or any(r["extra"] for r in o["ranges"]):
            raise Rejected("estructura no reconocida: el OOS actual tiene elementos o atributos que la "
                           "herramienta no conoce")
        if len(o["ranges"]) > 1:                                  # CR1-C2
            raise Rejected("estructura no reconocida: el OOS actual tiene más de un Range; esta herramienta "
                           "solo edita un OOS de un rango (o ninguno)")
        for r in o["ranges"]:
            try:
                if not r["from"] or not r["to"]:
                    raise Rejected("falta dateFrom o dateTo")
                _parse_date(r["from"]), _parse_date(r["to"])
            except Rejected as e:
                raise Rejected(f"estructura no reconocida: un Range del OOS actual no es válido ({e})") from None
        if "oos_pct" in p:
            dias = (we - ws).days
            is_dias = (100 - p["oos_pct"]) * dias // 100
            desde = ws + dt.timedelta(days=is_dias)
            if not ws < desde < we:
                raise Rejected(f"--oos-last-pct {p['oos_pct']}: con una ventana de {dias} días no queda IS y OOS "
                               "a la vez; usa --oos con fechas o una ventana más larga")
            nuevo = [(_fmt(desde), _fmt(we))]
        elif p["oos"] == "none":
            nuevo = []
        else:
            a, b = p["oos"]
            if a < ws or b > we:
                raise Rejected(f"--oos {_fmt(a)}:{_fmt(b)} queda fuera de la ventana final ({_fmt(ws)}–{_fmt(we)})")
            if a == ws and b == we:
                raise Rejected("--oos cubre la ventana entera: tiene que quedar algo de IS")
            nuevo = [(_fmt(a), _fmt(b))]
        ed["oos"] = nuevo
        antes = [(r["from"], r["to"]) for r in o["ranges"]]
        lines.append(f"OOS: {_oos_txt(antes)} → {_oos_txt(nuevo)}")
    elif "dates" in p and old["oos"] is not None:
        for r in old["oos"]["ranges"]:
            rf, rt = _parse_date(r["from"], "OOS actual: inicio"), _parse_date(r["to"], "OOS actual: fin")
            if rf < ws or rt > we:
                raise Rejected(f"el OOS actual ({r['from']}–{r['to']}) quedaría fuera de la ventana nueva "
                               f"({_fmt(ws)}–{_fmt(we)}): indica también --oos, --oos-last-pct o --oos none")

    # ---- avisos de cross checks (H4)
    if "dates" in p and old["cross"]:
        for s in old["cross"]["setups"]:
            f_, t_ = s["from"] or "", s["to"] or ""
            if _RX_FECHA.match(f_) and _RX_FECHA.match(t_) and (f_ < _fmt(ws) or t_ > _fmt(we)):
                warns.append(f"el cross check de {s['symbol']} ({f_}–{t_}) queda fuera de la ventana nueva "
                             f"({_fmt(ws)}–{_fmt(we)}); la herramienta no lo toca (cámbialo en la GUI si lo necesitas)")

    # ---- Rankings (solo Build)
    if quiere_build:
        if c["rankings"] != 1 or old["rankings"] is None:
            raise Rejected("estructura no reconocida: no encuentro un único <Rankings>")
        rk = old["rankings"]
        fit = rk["fitness"]
        if ("goals" in p or "fitness" in p):
            if c["ranking"] != 1 or fit is None:
                raise Rejected("estructura no reconocida: no encuentro un único <Ranking> de fitness")
        if "goals" in p:
            if fit["type"] != "Weighted":
                raise Rejected(f"la fitness de esta task es simple ({fit['type']}): no tiene goals. Cámbiala a "
                               "Weighted en la GUI de SQX")
            disp = [g["metric"] for g in fit["goals"]]
            for m in list(p["goals"]) + p["goals_off"]:
                if disp.count(m) != 1:
                    raise Rejected(f"la métrica {m} no existe (o está repetida) como goal en esta task. "
                                   f"Disponibles: {', '.join(disp)}")
            nuevo: dict[str, dict] = {}
            final_uso = {g["metric"]: g["use"] == "true" for g in fit["goals"]}
            if p["goals_only"]:
                for g in fit["goals"]:
                    if g["metric"] not in p["goals"] and g["use"] == "true":
                        nuevo[g["metric"]] = {"use": "false"}
                        final_uso[g["metric"]] = False
            for m, w in p["goals"].items():
                nuevo[m] = {"use": "true", "weight": str(w)}
                final_uso[m] = True
            for m in p["goals_off"]:
                nuevo[m] = {"use": "false"}
                final_uso[m] = False
            if not any(final_uso.values()):
                raise Rejected("tras el cambio no quedaría ningún goal en uso: la fitness Weighted necesita al menos uno")
            ed["goals"] = nuevo
            for g in fit["goals"]:
                if g["metric"] in nuevo:
                    v = nuevo[g["metric"]]
                    despues = {"use": v.get("use", g["use"]), "weight": v.get("weight", g["weight"])}
                    if despues != {"use": g["use"], "weight": g["weight"]}:
                        lines.append(f"goal {g['metric']}: {_estado_uso(g)} → {_estado_uso(despues)}")
        if "fitness" in p:
            if fit["extra"]:
                raise Rejected("estructura no reconocida: <Ranking> tiene atributos además de type")
            ed["fitness"] = p["fitness"]
            lines.append(f"fitness: {fit['type']} → {p['fitness']}")
            if fit["type"] == "Weighted":
                warns.append("se pierde la lista de goals de esta task (queda en la copia de seguridad); volver a "
                             "Weighted solo se puede desde la GUI de SQX")
        if "max" in p:
            if c["max"] != 1 or rk["max_strategies"] is None or not re.fullmatch(r"\d+", rk["max_strategies"]):
                raise Rejected("estructura no reconocida: no encuentro un único <MaxStrategies> numérico en <Rankings>")
            ed["max"] = p["max"]
            lines.append(f"máximo de estrategias: {rk['max_strategies']} → {p['max']}")
        if "stop" in p:
            if c["stop"] != 1 or rk["stop"] is None:
                raise Rejected("estructura no reconocida: no encuentro un único <StopCondition> en <Rankings>")
            falta = [k for k in p["stop"] if k not in rk["stop"]]
            if falta:
                raise Rejected("el <StopCondition> de esta task es de formato antiguo (le faltan los atributos "
                               f"{', '.join(falta)}) y la herramienta no los inventa; usa --stop never o "
                               "--stop databank-full, o cámbialo en la GUI")
            ed["stop"] = dict(p["stop"])
            antes = " ".join(f'{k}="{rk["stop"][k]}"' for k in p["stop"])
            despues = " ".join(f'{k}="{v}"' for k, v in p["stop"].items())
            lines.append(f"parada: {antes} → {despues}")
    return ed, lines, warns


# ----------------------------- verificación de un XML nuevo ------------------------------------------------

def _verify_task_text(tp: TaskPlan, nb: bytes) -> None:
    """c-f del §4.3 para el texto nuevo de una task. Falla con VerifyFailed."""
    pref = f"verificación interna de la task [{tp.idx}] «{tp.label}»"
    try:
        nueva = ET.fromstring(nb)
    except ET.ParseError as e:
        raise VerifyFailed(f"{pref}: el XML nuevo no se puede leer ({e})")
    try:
        leido = read_task(nb)
    except CfxEditError as e:
        raise VerifyFailed(f"{pref}: el lector no entiende el XML nuevo ({e})")
    esperado = _expected(tp.old, tp.edits)
    if leido != esperado:
        difs = [k for k in esperado if esperado[k] != leido.get(k)]
        raise VerifyFailed(f"{pref}: lo que se lee tras escribir no coincide con lo pedido (campos: {', '.join(difs)})")
    try:
        modelo = _model(tp.orig_bytes, tp.edits)
    except Exception as e:  # noqa: BLE001 — cualquier fallo del modelo es un fallo de verificación
        raise VerifyFailed(f"{pref}: no pude construir el modelo ({e})")
    dif = _tree_diff(modelo, nueva)
    if dif:
        raise VerifyFailed(f"{pref}: cambió algo más que lo pedido — {dif}")
    try:
        problema = _check_eol(tp.orig_bytes.decode("utf-8"), nb.decode("utf-8"), tp.edits.get("oos") is not None,
                             bool(tp.edits.get("fitness")))
    except UnicodeDecodeError:
        problema = "el XML nuevo no es UTF-8"
    if problema:
        raise VerifyFailed(f"{pref}: {problema}")


# ----------------------------- plan -----------------------------------------------------------------------

def plan_edit(proj: str, sels: list[str], changes: Changes) -> Plan:
    """Valida los cambios sobre el estado final de cada task, genera los XML nuevos y los verifica en memoria.
    No escribe nada. Cualquier fallo en una task aborta todo el plan."""
    p = _parse_changes(changes)
    if not sels:
        raise UsageError("falta --task (qué task(s) cambiar)")
    ctx = _Ctx(proj)
    idxs = _select(list(sels), ctx.tasks)
    tps: list[TaskPlan] = []
    vistos: set[str] = set()
    for i in idxs:
        t = ctx.tasks[i]
        pref = f"task [{i}] «{t['label']}»"
        try:
            xml = t["xml"]
            m = re.match(r"[A-Za-z]+", xml or "")
            if not xml or not t["type"] or not m or m.group(0) != t["type"]:
                raise Rejected(f"estructura no reconocida: el fichero {xml!r} no corresponde al tipo {t['type']!r}")
            if xml in vistos:
                raise Rejected(f"estructura no reconocida: otra task seleccionada usa el mismo fichero {xml}")
            vistos.add(xml)
            orig = ctx.member(xml)
            old = read_task(orig)
            ed, lines, warns = _resolve(t, old, p)
            nuevo = _write_task(_decode(orig, xml), ed)
            nb = nuevo.encode("utf-8")
            tp = TaskPlan(i, t["label"], xml, t["type"], orig, old, ed, nb, lines, warns)
            _verify_task_text(tp, nb)
        except Rejected as e:       # UsageError y VerifyFailed ya llevan su propio contexto
            raise Rejected(f"{pref}: {e}") from None
        tps.append(tp)
    return Plan(proj, ctx.projdir, ctx.cfx, ctx.sha, ctx.members, tps)


def _fmt_plan(plan: Plan, aplicar: bool) -> str:
    L = ["Cambios " + ("a escribir" if aplicar else "(SIMULACIÓN: no se escribe nada)")
         + f" — proyecto «{plan.proj}»:"]
    for tp in plan.tasks:
        L.append(f"[{tp.idx}] {tp.label} ({tp.type}, {tp.xml})")
        for ln in (tp.lines or ["(sin cambios de valor)"]):
            L.append(f"    {ln}")
        for w in tp.warnings:
            L.append(f"    AVISO: {w}")
    return "\n".join(L)


# ----------------------------- escritura segura -----------------------------------------------------------

def _now() -> dt.datetime:
    return dt.datetime.now()


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _precheck(proj: str) -> None:
    try:
        rep = SP.pre_gui_check(proj)
    except Exception as e:  # noqa: BLE001 — fail-closed
        raise Rejected(f"no pude comprobar si es seguro editar ({e})")
    if not rep["safe"]:
        raise Rejected("NO es seguro editar el proyecto ahora: " + "; ".join(rep["reasons"]))


def _build_zip(tmp: Path, plan: Plan) -> None:
    """ZIP nuevo: mismos miembros, orden, fecha y compresión; solo cambian los XML editados."""
    nuevos = {tp.xml: tp.new_bytes for tp in plan.tasks}
    with zipfile.ZipFile(tmp, "w") as z:
        for info, data in plan.members:
            zi = zipfile.ZipInfo(info.filename, info.date_time)
            zi.compress_type = info.compress_type
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            zi.comment = info.comment
            z.writestr(zi, nuevos.get(info.filename, data))


def _verify_zip(plan: Plan, tmp: Path) -> None:
    """a-c del §4.3 sobre el temporal (d-f se hacen por task con `_verify_task_text`)."""
    try:
        zf = zipfile.ZipFile(tmp)
    except Exception as e:  # noqa: BLE001
        raise VerifyFailed(f"verificación interna: el ZIP temporal no se abre ({e})")
    with zf:
        if zf.testzip() is not None:
            raise VerifyFailed("verificación interna: el ZIP temporal está dañado (testzip)")
        infos = zf.infolist()
        if [i.filename for i in infos] != [i.filename for i, _ in plan.members]:
            raise VerifyFailed("verificación interna: los miembros del ZIP nuevo no coinciden en nombre u orden")
        if [i.compress_type for i in infos] != [i.compress_type for i, _ in plan.members]:
            raise VerifyFailed("verificación interna: cambió la compresión de algún miembro del ZIP")
        editadas = {tp.xml: tp for tp in plan.tasks}
        for (oi, odata), ni in zip(plan.members, infos):
            nd = zf.read(ni)
            tp = editadas.get(ni.filename)
            if tp is None:
                if nd != odata:
                    raise VerifyFailed(f"verificación interna: el miembro no editado {ni.filename} cambió de bytes")
            else:
                if nd != tp.new_bytes:
                    raise VerifyFailed(f"verificación interna: {ni.filename} no tiene los bytes verificados")
                _verify_task_text(tp, nd)


def apply(plan: Plan, motivo: str = "cfx_edit") -> Path:
    """Escribe el plan (pasos 1-7 del §4.3) y devuelve la ruta del backup. Todo `OSError` sale como error
    controlado (CR1-C3): rc 1 antes del `os.replace`, rc 4 después."""
    motivo = re.sub(r"[^A-Za-z0-9_-]", "_", motivo or "") or "cfx_edit"
    cfx = plan.cfx
    tmp = cfx.with_name(f"{cfx.name}.cfx_edit.{os.getpid()}.tmp")           # CR1-C5: temporal con el PID
    backup: Path | None = None
    reemplazado = False
    try:
        _precheck(plan.proj)                                              # 1
        _build_zip(tmp, plan)                                             # 2
        _verify_zip(plan, tmp)                                            # 3
        sha_tmp = _sha_file(tmp)
        _precheck(plan.proj)                                              # 4
        if _sha_file(cfx) != plan.sha0:
            raise Rejected("el proyecto cambió mientras se preparaba la edición; vuelve a simular")
        backup = cfx.with_name(f"{cfx.name}.bak_{motivo}_{_now().strftime('%Y%m%d_%H%M%S')}")   # 5
        if backup.exists():
            raise Rejected(f"ya existe el backup {backup.name}: nunca se pisa; repite dentro de un segundo")
        try:
            shutil.copy2(cfx, backup)
        except OSError:
            _borrar(backup)                                               # backup parcial
            backup = None
            raise
        if _sha_file(backup) != plan.sha0:
            _borrar(backup)
            raise Rejected("la copia de seguridad no coincide con el original (¿cambió el .cfx?); no se escribió "
                           "nada, vuelve a simular")
        try:
            os.replace(tmp, cfx)                                          # 6
        except Exception as e:  # noqa: BLE001
            raise Rejected(f"no pude sustituir el project.cfx ({e}); el original sigue intacto y el backup "
                           f"queda en {backup}")
        reemplazado = True
        if _sha_file(cfx) != sha_tmp:
            raise FinalMismatch(f"el project.cfx escrito no coincide con lo verificado (¿otra escritura "
                                f"concurrente?). NO se restaura solo; el original está en {backup}")
        return backup                                                     # 7
    except OSError as e:
        if reemplazado:
            raise FinalMismatch(f"se sustituyó el project.cfx pero no pude comprobarlo ({e}). NO se restaura "
                                f"solo; el original está en {backup}") from None
        dejado = f" El backup {backup} queda (el original está intacto)." if backup is not None and backup.exists() else ""
        raise Rejected(f"error de disco antes de sustituir el project.cfx ({e}); no se escribió nada.{dejado}") from None
    finally:
        _borrar(tmp)


def _borrar(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


# ----------------------------- CLI ------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cfx_edit.py",
        description=("Edita la configuración de un custom project de SQX que ya existe (ventana de test, OOS, "
                     "fitness, máximo de estrategias, parada). SQX y su GUI tienen que estar cerrados. Sin "
                     "--apply solo simula. No ve los datos de SQX ni toca el binding del símbolo."),
        epilog=LIMITES_AYUDA, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("proyecto", help="nombre del proyecto (carpeta bajo la de proyectos de SQX)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="{show,set}")
    sh = sub.add_parser("show", help="enseña la configuración editable de las tasks (solo lectura)",
                        epilog=PIE_BINDING, formatter_class=argparse.RawDescriptionHelpFormatter)
    sh.add_argument("--task", action="append", default=[], metavar="SEL",
                    help="índice (de run_task_headless.py --list), label exacto o fichero XML; repetible")
    sh.add_argument("--json", action="store_true", help="salida en JSON")
    st = sub.add_parser("set", help="cambia valores (simulación; con --apply escribe)",
                        epilog=LIMITES_AYUDA, formatter_class=argparse.RawDescriptionHelpFormatter)
    st.add_argument("--task", action="append", default=[], metavar="SEL",
                    help="índice (de run_task_headless.py --list), label exacto o fichero XML; repetible: mismos "
                         "cambios en varias tasks, una sola escritura")
    st.add_argument("--dates", metavar="AAAA.MM.DD:AAAA.MM.DD", help="ventana de test (debe caber en el binding)")
    st.add_argument("--oos", metavar="AAAA.MM.DD:AAAA.MM.DD|none", help="rango fuera de muestra, o 'none'")
    st.add_argument("--oos-last-pct", metavar="P",
                    help="último P %% (1-99) de la ventana como OOS; el IS se redondea hacia abajo al día "
                         "(el OOS puede salir hasta un día más largo que el P %% exacto)")
    st.add_argument("--goal", action="append", default=[], metavar="MÉTRICA=PESO",
                    help="pone el goal en uso con ese peso entero (Weighted de un Build); repetible")
    st.add_argument("--goal-off", action="append", default=[], metavar="MÉTRICA", help="apaga ese goal; repetible")
    st.add_argument("--goals-only", action="store_true", help="apaga todos los goals que no estén en --goal")
    st.add_argument("--fitness", metavar="MÉTRICA",
                    help="fitness simple (" + ", ".join(FITNESS_SIMPLES) + "); sustituye a la Weighted")
    st.add_argument("--max-strategies", metavar="N", help="máximo de estrategias del databank (Build)")
    st.add_argument("--stop", metavar="COND", help="never | databank-full | passed-count:N | time-limit:D:H:M")
    st.add_argument("--apply", action="store_true", help="escribe de verdad (sin esto es una simulación)")
    st.add_argument("--motivo", default="cfx_edit", help="texto del nombre del backup (solo A-Z a-z 0-9 _ -)")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        a = _build_parser().parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    try:
        if a.cmd == "show":
            entradas = show(a.proyecto, a.task or None)
            print(json.dumps(entradas, ensure_ascii=False, indent=2) if a.json else _fmt_show(entradas))
            return 0
        ch = Changes(dates=a.dates, oos=a.oos, oos_last_pct=a.oos_last_pct, goal=a.goal, goal_off=a.goal_off,
                     goals_only=a.goals_only, fitness=a.fitness, max_strategies=a.max_strategies, stop=a.stop)
        plan = plan_edit(a.proyecto, a.task, ch)
        print(_fmt_plan(plan, a.apply))
        if not a.apply:
            print("\nNo se ha escrito nada. Si es lo que quieres, repite el mismo comando con --apply "
                  "(con SQX y su GUI cerrados).")
            print(PIE_BINDING)
            return 0
        backup = apply(plan, a.motivo)
        print(f"\nEscrito. Copia de seguridad: {backup}")
        print("Valores escritos: los de arriba. Comprueba en la GUI de SQX antes de lanzar nada.")
        return 0
    except CfxEditError as e:
        print(f"{'ERROR DE USO' if e.rc == 2 else 'RECHAZADO' if e.rc == 1 else 'FALLO'}: {e}", file=sys.stderr)
        return e.rc


if __name__ == "__main__":
    sys.exit(main())
