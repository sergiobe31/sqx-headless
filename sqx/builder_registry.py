"""Builder config registry — DRAFT DE SCHEMA, ronda 3 (2026-06-28, post-prueba GUI de Sergio).

Fuente única de verdad concepto→XML del Builder de SQX. Reemplaza el mapeo que hoy se re-deriva a
mano en ~8 scripts make_*_cfx.py. El emisor y el auditor (aún por escribir) IMPORTARÁN este módulo.

CAMBIOS RONDA 3 (decodificados con la prueba GUI gui_decode_test, diff verificado):
  - SL/PT NO es un mode-switch → son TOGGLES INDEPENDIENTES (SLFixedPips/SLATR/SLPercent/SLIndicatorBased),
    varios pueden estar ON a la vez; SLValueType NO es el selector autoritativo. → fuera `ModeSwitch`.
    Cada toggle gateado por SLRequired; cada rango gateado por (SLRequired AND su toggle) = gates compuestos
    (tuple de Gate = AND, ya soportado).
  - Vocabulario de métricas aprendido (clases de Column-Value) → METRICS.
  - RRR vive en LimitSLPTRRR/…From/…To (persiste bien, verificado). SeparatedSettings=false → PT espeja SL.

4 primitivas: Knob, Gate (tuple=AND), MutexGroup (XOR sin selector, ej Method use=true de MM),
Constraint (relación cross-campo que falla en silencio).

Uso (read-side grounding):  python sqx/builder_registry.py [proyecto=miner_eurusd_d1_mr]
"""
from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

try:  # ruta de instalación SQX: única fuente en sqx/paths.py
    from sqx.paths import PROJECTS
except ImportError:  # ejecutado como script (sys.path[0] = sqx/)
    from paths import PROJECTS
BASES = Path(__file__).resolve().parent / "bases"   # fixtures versionadas (seed del emit, git-tracked)
REFERENCE = "miner_eurusd_d1_mr"


def task_output_databanks(proj: str) -> dict:
    """{nombre de la task en el log: databank destino}, leído del `.cfx`.

    Cruza config.xml (cada <Task> → taskXMLFile, con `title` y `name`) con el
    `<Databank label="Output databank" ... value="..."/>` de cada *-TaskN.xml.
    Vale para Build, Retest y Filtering: todos declaran "Output databank"
    (verificado 2026-09-27 en gold_h4_prod_oos).
    El log escribe el TÍTULO de la task ("Build Mean Reversion L/S") cuando existe
    y el `name` cuando no → se indexan AMBAS claves al mismo databank.

    Home compartido (2026-09-27): lo consumen el monitor (Estado en vivo) y
    run_pipeline_per_task.sh (validación de Output poblado, H11) — sin Streamlit.
    """
    cfx = PROJECTS / proj / "project.cfx"
    out = {}
    try:
        with zipfile.ZipFile(cfx) as z:
            cfg = z.read("config.xml").decode("utf-8", "replace")
            for m in re.finditer(r"<Task\b[^>]*>", cfg):
                tag = m.group(0)
                xf = re.search(r'taskXMLFile="([^"]*)"', tag)
                if not xf or xf.group(1) not in z.namelist():
                    continue
                task = z.read(xf.group(1)).decode("utf-8", "replace")
                db = re.search(r'<Databank[^>]*label="Output databank"[^>]*value="([^"]*)"', task)
                bank = db.group(1) if db else None
                for attr in ("title", "name"):
                    am = re.search(rf'{attr}="([^"]*)"', tag)
                    if am:
                        out[am.group(1)] = bank
    except Exception:
        pass
    return out


def resolve_base(name):
    """Dir del proyecto a usar como base: la fixture del repo (sqx/bases/<name>/) si existe,
    si no el proyecto VIVO de SQX. Hace el toolkit self-contained: el emit reseed-ea del repo aunque
    se borren los proyectos de SQX (y la GUI no puede corromper una fixture en git). Ver §9.3."""
    repo = BASES / name
    return repo if (repo / "project.cfx").exists() else PROJECTS / name

# Vocabulario de métricas (atributo `class` de <Column-Value>), decodificado de la prueba GUI + ranking.
METRICS = {
    "NetProfit": "Net profit", "NumberOfTrades": "# of trades", "DrawdownPct": "Max DD %",
    "ReturnDDRatio": "Ret/DD Ratio", "SharpeRatio": "Sharpe Ratio", "ProfitFactor": "Profit factor",
    "Stability": "Stability", "Symmetry": "Symmetry",
}
# sampleType: 10=IS, 20=OOS, 127=SampleTypes.FullSample (IS+OOS; también el lado derecho "main" de
# condiciones relativas % del original). Verificado en SQTradingLib B144, 2026-10-12.

# Catálogo de goals de la fitness Weighted (B143). Extraído de un .cfx vivo escrito por la
# GUI el 2026-09-14 (eurusd_h2_exp_lados_separados); las fixtures viejas de sqx/bases/ solo
# traen 67 (serialización antigua con target=, en desuso). Referencia completa:
# docs/referencia/fitness_goals_catalogo.md. valueType: 1=maximizar, 2=minimizar.
FITNESS_GOALS = (
    # rentabilidad
    "NetProfit", "NetProfitInPct", "NetProfitInPips", "GrossProfit", "GrossLoss",
    "AnnualPctReturn", "AvgPctProfitPerYear", "AvgProfitPerDay", "AvgProfitPerMonth",
    "AvgProfitPerYear", "CAGR", "MaxProfit", "MaxLoss", "WorstYearProfit",
    "ProfitableMonths", "ProfitableMonthsPct", "Commission", "SlippageInMoney",
    # ratios de calidad
    "ProfitFactor", "ReturnDDRatio", "ReturnOpenDDRatio", "AnnualPctReturnDDRatio",
    "RecoveryFactor", "CalmarRatio", "SharpeRatio", "SQN", "SQNScore", "RExpectancy",
    "RExpectancyScore", "Expectancy", "WinLossRatio", "PayoutRatio", "WinningPct",
    "KellyFormula", "EdgeRatioInPips", "Efficiency", "UlcerIndex",
    "UlcerPerformanceIndex", "ZScore", "ZProbability", "AHPR",
    # drawdown
    "Drawdown", "DrawdownPct", "DrawdownPips", "ActualDD", "ActualDrawdownPct",
    "AvgDrawdown", "AvgPctDrawdown", "MaxIntradayDrawdown", "MaxTSIntradayDrawdown",
    "OpenDrawdown", "OpenDrawdownPct",
    # estabilidad de curva
    "Stability", "StabilitySQ3", "RSquared", "EquityAngle", "EquitySlope", "Symmetry",
    "NSymmetry", "TradesSymmetry", "Stagnation", "StagnationPct", "MaxNewHighDuration",
    "StandardDev", "AvgParametersStability", "WorstParametersStability", "TSIndex",
    "TSWinLossRatio",
    # exposición y tiempos
    "Exposure", "ExposurePosition", "AvgBarsInTrade", "LongestTrade",
    "AvgTradesPerDay", "AvgTradesPerMonth", "AvgTradesPerYear",
    "TotalTradingDays", "TotalTradingMonths", "TotalTradingYears",
    "TotalDataDays", "TotalDataMonths", "TotalDataYears", "TotalMFE", "BiggestMAE",
    "AvgTrStddevRatio",
    # robustez y estadística de trades
    "DegreesOfFreedom", "Complexity", "AmbiguousTrades", "AmbiguousTradesPct",
    "Outlier", "Outlier2", "AvgConsecWins", "AvgConsecLosses", "MaxConsecWins",
    "MaxConsecLosses", "AvgWin", "AvgLoss", "AvgTrade", "AvgAbsTrade", "AvgBarsWin",
    "AvgBarsLoss", "NumberOfTrades", "NumberOfProfits", "NumberOfLosses",
    "NumberOfCanceled", "ExitQuality", "DateGenerated", "DateLastModified",
    "InitialDeposit",
)

# Fitness canónica de producción (goals activos, confirmado Sergio 2026-09-09).
# (type, weight, valueType) — ver docs/referencia/fitness_goals_catalogo.md §configs canónicas.
FITNESS_CANONICAL_TF = (
    ("ReturnDDRatio", "10", "1"), ("Stability", "5", "1"), ("SQNScore", "5", "1"),
    ("ProfitableMonthsPct", "5", "1"), ("StagnationPct", "5", "2"),
)
FITNESS_CANONICAL_MR = FITNESS_CANONICAL_TF + (("Exposure", "5", "2"),)


def fitness_goals(root):
    """Goals activos (use=true) del <Ranking type="Weighted"> del build.

    Devuelve [(type, weight, valueType)]; valueType 1=maximizar, 2=minimizar.
    Serialización antigua (fixtures pre-B143): sin valueType → cae al attr target.
    """
    rk = next(iter(root.iter("Ranking")), None)
    if rk is None or rk.get("type") != "Weighted":
        return []
    out = []
    for g in rk.iter("Goal"):
        if g.get("use") == "true":
            out.append((g.get("type"), g.get("weight"), g.get("valueType") or g.get("target")))
    return out

# AutomaticDismissal ("Configure automatic filter"): bitflag code → (snake, etiqueta GUI).
# Decodificado de internal/web/RESULTS2/result2.js (29-jun) + verificado contra disco (256+512 ON = los 2 toggles
# encendidos en la GUI de Sergio). Atributo `warnings` de <AutomaticDismissal> = "Add automatic checks warnings to
# strategy even when not used for dismissal" (opcional; AUSENTE == false en la referencia y la plantilla oficial →
# NO se modela como knob para no romper el read-side grounding; setearlo es un solo .set('warnings','true')).
DISMISSAL_CODES = {
    1:    ("no_trades",            "no trades"),
    2:    ("too_little_trades",    "too little trades"),
    4:    ("zero_pl_trades",       "zero PL trades"),
    8:    ("too_many_same_bar",    "too many trades closing at the same bar"),
    16:   ("zero_duration_trades", "zero duration trades"),
    32:   ("too_many_open",        "too many open trades"),
    64:   ("unfinished_trades",    "unfinished trades"),
    256:  ("no_filled_trades",     "no filled trades"),
    512:  ("outlier_trade",        "outlier trade - one exceptionally big trade"),
    1024: ("too_many_ambiguous",   "too many ambiguous trades"),
}

# ---------------------------------------------------------------------------
# Navegación
# ---------------------------------------------------------------------------

def complexity_chart(root):
    for ch in root.iter("Chart"):
        if ch.get("name") == "Main chart" and ch.get("minConditions") is not None:
            return ch
    return None

def buildmode(root):
    return next(iter(root.iter("BuildMode")), None)

def bm_child(root, tag):
    bm = buildmode(root)
    return bm.find(tag) if bm is not None else None

def slpt(root, tag):
    for s in root.iter("SLPTOptions"):
        el = s.find(tag)
        if el is not None:
            return el
    return None

def main_test_values(root):
    return next(iter(root.iter("MainTestValues")), None)

def stop_condition(root):
    return next(iter(root.iter("StopCondition")), None)

def crosschecks(root):
    return next(iter(root.iter("CrossChecks")), None)

def _main_setup(root):
    """El Setup PRINCIPAL (no el de RetestOnAdditionalMarkets). review GLM #5a."""
    pmap = {c: p for p in root.iter() for c in p}
    for s in root.iter("Setup"):
        anc, p = [], pmap.get(s)
        while p is not None:
            anc.append(p.tag); p = pmap.get(p)
        if "RetestOnAdditionalMarkets" not in anc:
            return s
    return next(iter(root.iter("Setup")), None)

def _main_chart(root):
    s = _main_setup(root)
    return s.find("Chart") if s is not None else None

def main_commission_method(root):
    s = _main_setup(root)                        # setup-aware, no "primer Commissions" (review GLM #5a)
    comm = s.find("Commissions") if s is not None else None
    return comm.find("Method") if comm is not None else None

def mm_methods(root):
    out = []
    for mm in root.iter("MoneyManagement"):
        for m in mm.findall("Method"):
            out.append((m.get("type"), m))
    return out

def tparam(root, key):
    """<Param key=...> dentro de <BuildTradingOptions> (Trading options tab)."""
    bto = next(iter(root.iter("BuildTradingOptions")), None)
    if bto is None:
        return None
    return next((p for p in bto.iter("Param") if p.get("key") == key), None)

def dismissal_problems(root):
    """<AutomaticDismissal>: el filtro automático ("Configure automatic filter", 10 <Problem code=> bitflags)."""
    return next(iter(root.iter("AutomaticDismissal")), None)

def _problem(code):
    """find ejecutable del <Problem code="N"> bajo AutomaticDismissal (factory para los knobs dismiss.*)."""
    def find(root):
        ad = dismissal_problems(root)
        return next((p for p in ad.findall("Problem") if p.get("code") == str(code)), None) if ad is not None else None
    return find

# ---------------------------------------------------------------------------
# Modelo (4 primitivas)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    find: Callable
    attr: Optional[str]
    required: str
    why: str
    def evaluate(self, root):
        el = self.find(root)
        val = (el.get(self.attr) if self.attr else (el.text or "").strip()) if el is not None else None
        return val == self.required, val

@dataclass(frozen=True)
class Knob:
    concept: str
    gui: str
    find: Callable
    attr: Optional[str]
    encoding: str
    default: str
    valid: str = ""
    gates: tuple = ()          # tuple[Gate] — semántica AND: activo si TODOS pasan
    status: str = "read"
    notes: str = ""
    def read(self, root):
        el = self.find(root)
        if el is None:
            return None
        return el.get(self.attr) if self.attr else (el.text or "").strip()
    def active(self, root):
        return all(g.evaluate(root)[0] for g in self.gates)

@dataclass(frozen=True)
class MutexGroup:
    concept: str
    gui: str
    members: Callable
    use_attr: str = "use"
    notes: str = ""
    def state(self, root):
        allm = self.members(root)
        on = [(n, el) for n, el in allm if el.get(self.use_attr) == "true"]
        return on, allm

@dataclass(frozen=True)
class Constraint:
    name: str
    check: Callable
    why: str

# ---------------------------------------------------------------------------
# GATES
# ---------------------------------------------------------------------------

GATE_MTV_SWAP = Gate(main_test_values, "swap", "true",
                     "doc §7: <Swap use=true> inerte si MainTestValues swap=false")
GATE_MTV_COMM = Gate(main_test_values, "commissions", "true",
                     "doc §7: <Commissions use=true> inerte si MainTestValues commissions=false (gemelo del swap)")
GATE_STOP_PASSED = Gate(stop_condition, "type", "passed-count",
                        "doc §9.1: passedStrategies inerte salvo type=passed-count")
GATE_CC_GLOBAL = Gate(crosschecks, "use", "true",
                      "doc §12: sub-checks inertes si CrossChecks.use=false")
GATE_GENETIC = Gate(buildmode, "generationType", "genetic-evolution",
                    "hiperparámetros genéticos inertes si generationType=random-generation")
GATE_STAGNATION = Gate(lambda r: bm_child(r, "EvoRestartOnStagnation"), "status", "true",
                       "generations inerte si EvoRestartOnStagnation status=false")
GATE_FRESHBLOOD = Gate(lambda r: bm_child(r, "FreshBloodReplaceWeakest"), None, "true",
                       "FreshBloodWeakestPct/Generations inertes si FreshBloodReplaceWeakest=false "
                       "(interruptor del reemplazo del N% más débil; bug cazado en GUI 29-jun)")
GATE_MDFM = Gate(lambda r: tparam(r, "MaxDistanceFromMarket"), None, "true",
                 "MaxDistancePct inerte si MaxDistanceFromMarket=false")
# SL/PT (decodificados de la prueba GUI)
GATE_SL_REQ = Gate(lambda r: slpt(r, "SLRequired"), None, "true", "sin SLRequired no hay SL → todos los toggles SL inertes")
GATE_PT_REQ = Gate(lambda r: slpt(r, "PTRequired"), None, "true", "sin PTRequired no hay PT")
GATE_SL_PIPS = Gate(lambda r: slpt(r, "SLFixedPips"), None, "true", "rango pips SL inerte si SLFixedPips=false")
GATE_SL_ATR = Gate(lambda r: slpt(r, "SLATR"), None, "true", "rango ATR SL inerte si SLATR=false")
GATE_PT_LIMIT_RRR = Gate(lambda r: slpt(r, "LimitSLPTRRR"), None, "true", "RRR From/To inerte si LimitSLPTRRR=false (verificado prueba GUI)")
GATE_TIME_RANGE = Gate(lambda r: tparam(r, "LimitTimeRange"), None, "true",
                       "SignalTimeRangeFrom/To inertes si LimitTimeRange=false")
GATE_EOD = Gate(lambda r: tparam(r, "ExitAtEndOfDay"), None, "true",
                "EODExitTime inerte si ExitAtEndOfDay=false")

# ---------------------------------------------------------------------------
# KNOBS
# ---------------------------------------------------------------------------

_GENETIC_HYPER = {"genetic.population", "genetic.maxGenerations", "genetic.crossover",
                  "genetic.mutation", "genetic.islands", "genetic.migrationModulo",
                  "genetic.migrationRate", "genetic.decimationCoef", "genetic.restartOnFinish",
                  "genetic.inSampleRatio", "genetic.stagnationOn", "genetic.stagnationGenerations",
                  "genetic.freshBloodReplaceWeakest", "genetic.freshBloodReplaceSimilar",
                  "genetic.freshBloodPct", "genetic.freshBloodGenerations"}

_KNOBS_BASE = [
    # --- genético ---
    Knob("genetic.generationType", "What to build · Builder mode", buildmode, "generationType",
         "enum", "genetic-evolution", valid="genetic-evolution|random-generation", status="behavioral"),
    Knob("genetic.population", "What to build · Population", lambda r: bm_child(r, "PopulationSize"), None, "int", "80"),
    Knob("genetic.maxGenerations", "What to build · Max generations", lambda r: bm_child(r, "MaxGenerations"), None, "int", "100"),
    Knob("genetic.crossover", "What to build · Crossover %", lambda r: bm_child(r, "CrossoverProbability"), None, "int", "75"),
    Knob("genetic.mutation", "What to build · Mutation %", lambda r: bm_child(r, "MutationProbability"), None, "int", "35"),
    Knob("genetic.islands", "What to build · Islands", lambda r: bm_child(r, "Islands"), None, "int", "3"),
    Knob("genetic.migrationModulo", "What to build · Migrate every N", lambda r: bm_child(r, "MigrationModulo"), None, "int", "20"),
    Knob("genetic.migrationRate", "What to build · Migration rate %", lambda r: bm_child(r, "MigrationRate"), None, "int", "10"),
    Knob("genetic.decimationCoef", "What to build · Decimation", lambda r: bm_child(r, "DecimationCoef"), None, "int", "2"),
    Knob("genetic.restartOnFinish", "What to build · Restart when finished", lambda r: bm_child(r, "EvoRestartOnFinish"), "status", "bool", "false"),
    # Sin efecto en B144: ningún jar lee GPSettings.trainingValidationRatio (verificado 2026-10-12).
    Knob("genetic.inSampleRatio", "What to build · In-sample %", lambda r: bm_child(r, "EvoInSamplePeriod"), "ratio", "int", "75"),

    # --- complejidad ---
    Knob("complexity.minConditions", "What to build · Min entry cond", complexity_chart, "minConditions", "int", "2"),
    Knob("complexity.maxConditions", "What to build · Max entry cond", complexity_chart, "maxConditions", "int", "2", status="behavioral"),
    Knob("complexity.minExitConditions", "What to build · Min exit cond", complexity_chart, "minExitConditions", "int", "2"),
    Knob("complexity.maxExitConditions", "What to build · Max exit cond", complexity_chart, "maxExitConditions", "int", "2"),
    Knob("complexity.minPeriod", "What to build · Min period", complexity_chart, "minPeriod", "int", "15"),
    Knob("complexity.maxPeriod", "What to build · Max period", complexity_chart, "maxPeriod", "int", "80"),

    # --- market sides ---
    Knob("sides.type", "What to build · Market sides", lambda r: next(iter(r.iter("MarketSides")), None), "type", "enum", "both", valid="both|longonly|shortonly"),
    Knob("sides.entrySymmetry", "What to build · Entry symmetry", lambda r: next((m.find("EntrySymmetry") for m in r.iter("MarketSides")), None), None, "bool", "true"),
    Knob("sides.exitSymmetry", "What to build · Exit symmetry", lambda r: next((m.find("ExitSymmetry") for m in r.iter("MarketSides")), None), None, "bool", "true"),

    # --- SL/PT = TOGGLES INDEPENDIENTES (ronda 3, decodificado prueba GUI) ---
    Knob("sl.required", "What to build · SL required", lambda r: slpt(r, "SLRequired"), None, "bool", "true"),
    Knob("sl.fixedPips", "What to build · SL Fixed pips ON", lambda r: slpt(r, "SLFixedPips"), None, "bool", "false", gates=(GATE_SL_REQ,)),
    Knob("sl.atr", "What to build · SL ATR-based ON", lambda r: slpt(r, "SLATR"), None, "bool", "true", gates=(GATE_SL_REQ,)),
    Knob("sl.percent", "What to build · SL Percent ON", lambda r: slpt(r, "SLPercent"), None, "bool", "false", gates=(GATE_SL_REQ,)),
    Knob("sl.pips.min", "What to build · SL pips Min", lambda r: slpt(r, "MinSLInPips"), None, "int", "40", gates=(GATE_SL_REQ, GATE_SL_PIPS),
         notes="⚠ el GUI espeja este valor en MinSLInMoney al guardar"),
    Knob("sl.pips.max", "What to build · SL pips Max", lambda r: slpt(r, "MaxSLInPips"), None, "int", "200", gates=(GATE_SL_REQ, GATE_SL_PIPS)),
    Knob("sl.atr.multMin", "What to build · SL ATR mult Min", lambda r: slpt(r, "MinSLATRMultiple"), None, "float", "2", gates=(GATE_SL_REQ, GATE_SL_ATR)),
    Knob("sl.atr.multMax", "What to build · SL ATR mult Max", lambda r: slpt(r, "MaxSLATRMultiple"), None, "float", "4", gates=(GATE_SL_REQ, GATE_SL_ATR)),
    Knob("sl.atr.periodMin", "What to build · SL ATR period Min", lambda r: slpt(r, "MinSLATRPeriod"), None, "int", "20", gates=(GATE_SL_REQ, GATE_SL_ATR)),
    Knob("sl.atr.periodMax", "What to build · SL ATR period Max", lambda r: slpt(r, "MaxSLATRPeriod"), None, "int", "20", gates=(GATE_SL_REQ, GATE_SL_ATR)),
    Knob("pt.required", "What to build · PT required", lambda r: slpt(r, "PTRequired"), None, "bool", "true"),
    Knob("pt.separatedSettings", "What to build · PT separate from SL", lambda r: slpt(r, "SeparatedSettings"), None, "bool", "false",
         notes="false = PT usa los rangos de SL (el GUI espeja SL→PT); true = PT independiente"),
    Knob("pt.fixedPips", "What to build · PT Fixed pips ON", lambda r: slpt(r, "PTFixedPips"), None, "bool", "false", gates=(GATE_PT_REQ,)),
    Knob("pt.atr", "What to build · PT ATR-based ON", lambda r: slpt(r, "PTATR"), None, "bool", "true", gates=(GATE_PT_REQ,)),
    Knob("pt.limitRRR", "What to build · Limit RRR (SL vs PT)", lambda r: slpt(r, "LimitSLPTRRR"), None, "bool", "false"),
    Knob("pt.rrrFrom", "What to build · RRR from %", lambda r: slpt(r, "LimitSLPTRRRFrom"), None, "int", "50", gates=(GATE_PT_LIMIT_RRR,)),
    Knob("pt.rrrTo", "What to build · RRR to %", lambda r: slpt(r, "LimitSLPTRRRTo"), None, "int", "80", gates=(GATE_PT_LIMIT_RRR,)),

    # --- DATA tab (Setup principal + Chart) — pestaña señalada 2026-06-28 ---
    Knob("data.symbol", "What to test · Symbol", _main_chart, "symbol", "enum", "EURUSD_axi"),
    Knob("data.timeframe", "What to test · Timeframe", _main_chart, "timeframe", "enum", "D1"),
    Knob("data.spread", "What to test · Spread (pips)", _main_chart, "spread", "float", "0.5"),
    Knob("data.dateFrom", "What to test · Start day", _main_setup, "dateFrom", "date", "2015.06.01"),
    Knob("data.dateTo", "What to test · End day", _main_setup, "dateTo", "date", "2023.06.01"),
    Knob("data.engine", "What to test · Trading engine", _main_setup, "engine", "enum", "MetaTrader5 (hedged)"),
    Knob("data.precision", "What to test · Precision", _main_setup, "testPrecision", "enum", "1", notes="1=Selected timeframe only"),
    Knob("data.slippage", "What to test · Slippage (pips)", _main_setup, "slippage", "int", "0"),
    Knob("data.minDist", "What to test · Min distance (pips)", _main_setup, "minDist", "int", "0"),

    # --- Genético: stagnation + fresh blood (completar la pestaña) ---
    Knob("genetic.stagnationOn", "What to build · Restart if stagnates", lambda r: bm_child(r, "EvoRestartOnStagnation"), "status", "bool", "true"),
    Knob("genetic.stagnationGenerations", "What to build · Stagnation gens", lambda r: bm_child(r, "EvoRestartOnStagnation"), "generations", "int", "5", gates=(GATE_STAGNATION,)),
    Knob("genetic.freshBloodReplaceWeakest", "What to build · Replace weakest N% (toggle)", lambda r: bm_child(r, "FreshBloodReplaceWeakest"), None, "bool", "false",
         notes="INTERRUPTOR del reemplazo periódico del N% más débil; sin él freshBloodPct/Generations son inertes"),
    Knob("genetic.freshBloodReplaceSimilar", "What to build · Replace similar/duplicates", lambda r: bm_child(r, "FreshBloodReplaceSimilar"), None, "bool", "true",
         notes="diversidad por des-duplicación (independiente del weakest); ON en el fixture"),
    Knob("genetic.freshBloodPct", "What to build · Fresh blood %", lambda r: bm_child(r, "FreshBloodWeakestPct"), None, "int", "15", gates=(GATE_FRESHBLOOD,)),
    Knob("genetic.freshBloodGenerations", "What to build · Fresh blood every N", lambda r: bm_child(r, "FreshBloodWeakestGenerations"), None, "int", "15", gates=(GATE_FRESHBLOOD,)),

    # --- TRADING OPTIONS (pestaña señalada 2026-06-28; toggles + gates) ---
    Knob("trading.dontTradeWeekends", "Trading options · Don't trade on weekends", lambda r: tparam(r, "DontTradeOnWeekends"), None, "bool", "false"),
    Knob("trading.exitOnFriday", "Trading options · Exit on Friday", lambda r: tparam(r, "ExitOnFriday"), None, "bool", "true"),
    Knob("trading.exitEndOfDay", "Trading options · Exit at end of day", lambda r: tparam(r, "ExitAtEndOfDay"), None, "bool", "false"),
    Knob("trading.eodExitTime", "Trading options · EOD exit time", lambda r: tparam(r, "EODExitTime"), None, "secofday", "2355",
         gates=(GATE_EOD,), notes="crudo; canon GUI B143 = segundos del día (83040=23:04), fixtures viejas en HHMM (2355=23:55)"),
    Knob("trading.limitTimeRange", "Trading options · Limit time range", lambda r: tparam(r, "LimitTimeRange"), None, "bool", "false"),
    Knob("trading.timeRangeFrom", "Trading options · Time range from", lambda r: tparam(r, "SignalTimeRangeFrom"), None, "secofday", "800",
         gates=(GATE_TIME_RANGE,), notes="crudo; canon GUI B143 = segundos del día (36000=10:00), fixtures viejas en HHMM (800=08:00)"),
    Knob("trading.timeRangeTo", "Trading options · Time range to", lambda r: tparam(r, "SignalTimeRangeTo"), None, "secofday", "1600",
         gates=(GATE_TIME_RANGE,), notes="crudo; canon GUI B143 = segundos del día (72000=20:00), fixtures viejas en HHMM (1600=16:00)"),
    Knob("trading.maxDistFromMarket", "Trading options · Max distance from market", lambda r: tparam(r, "MaxDistanceFromMarket"), None, "bool", "false"),
    Knob("trading.maxDistPct", "Trading options · Max distance %", lambda r: tparam(r, "MaxDistancePct"), None, "int", "6", gates=(GATE_MDFM,)),
    Knob("trading.maxTradesPerDay", "Trading options · Max trades per day", lambda r: tparam(r, "MaxTradesPerDay"), None, "int", "0"),
    # Tiempos de Trading Options (RE-DECODIFICADO 23-jul contra GUI B143 + disco): los campos
    #   HORA-DEL-DÍA (SignalTimeRangeFrom/To, EODExitTime) se guardan como SEGUNDOS desde medianoche
    #   (36000=10:00, 72000=20:00, 83040=23:04); la GUI muestra HH:MM. La nota anterior ("HHMM 0..2359
    #   step 30") quedó refutada por 3 proyectos escritos por la GUI B143 (Builder, NASDAQ_4H, eurusd_2h).
    #   ⚠ Fixtures viejas (sqx/bases/miner_*) aún llevan HHMM (800=08:00, 1600=16:00) — la GUI los
    #   migra a segundos al re-guardar. Los knobs reportan el valor CRUDO; al EMITIR usar segundos.
    #   FridayCloseTime NO modelado: nuestro canon usa salida 1-barra (ExitAfterBars=1), no exits por
    #   hora; además en disco es ambiguo (2300 tanto en fixtures HHMM como en proyectos nuevos).

    # --- AUTOMATIC FILTER ("Configure automatic filter"; decodificado 29-jun, ver DISMISSAL_CODES) ---
    #   10 toggles independientes (sin gate global; "Select all" del diálogo es bulk, no co-dep). default=todos OFF.
    *[Knob(f"dismiss.{snake}", f"Auto filter · {label}", _problem(code), "dismiss", "bool", "false")
      for code, (snake, label) in sorted(DISMISSAL_CODES.items())],

    # --- RANKING (pestaña señalada 2026-06-28) ---
    Knob("ranking.maxStrategies", "Ranking · Max strategies to store", lambda r: next(iter(r.iter("MaxStrategies")), None), None, "int", "3000"),
    Knob("ranking.fitness", "Ranking · Strategy quality fitness", lambda r: next(iter(r.iter("Ranking")), None), "type", "enum", "ReturnDDRatio",
         valid="NetProfit|ReturnDDRatio|ProfitFactor|RExpectancy|AnnualPctReturnDDRatio|Weighted",
         notes="enum versionado de valores verificados en casa (ProfitFactor validado en A3, 2026-09-11); "
               "el enum completo del desplegable vive en el plugin Java SettingsRankings (no extraíble). "
               "Catálogo de goals Weighted: FITNESS_GOALS + docs/referencia/fitness_goals_catalogo.md"),

    # --- portadores de gate (coste / stop / cross-check) ---
    Knob("cost.commission", "What to test · Commission",
         lambda r: next((p for p in main_commission_method(r).iter("Param") if p.get("key") == "Commission"), None) if main_commission_method(r) is not None else None,
         None, "float", "4.5", gates=(GATE_MTV_COMM,), status="behavioral"),
    Knob("cost.swap.long", "What to test · Swap long", lambda r: (_main_setup(r).find("Swap") if _main_setup(r) is not None else None), "long", "float", "-6.93", gates=(GATE_MTV_SWAP,), status="behavioral"),
    Knob("stop.passedStrategies", "Ranking · Stop after N passed", stop_condition, "passedStrategies", "int", "1000", gates=(GATE_STOP_PASSED,), status="behavioral"),
    Knob("crosschecks.m1", "Cross checks · Retest M1", lambda r: crosschecks(r).find("RetestWithHigherPrecision") if crosschecks(r) is not None else None, "use", "bool", "true", gates=(GATE_CC_GLOBAL,)),
]

KNOBS = [replace(k, gates=k.gates + (GATE_GENETIC,)) if k.concept in _GENETIC_HYPER else k for k in _KNOBS_BASE]

MUTEX_GROUPS = [
    MutexGroup("mm.method", "Money management · Sizing method", mm_methods,
               notes="XOR sin selector; modo = qué Method tiene use=true. LotsIfNoMM = fallback si ninguno on."),
]

def _migration_fires(root):
    bm = buildmode(root)
    if bm is None or bm.get("generationType") != "genetic-evolution":
        return True   # en random-generation no hay migración → la relación es inerte (no aplica)
    mm = bm_child(root, "MigrationModulo"); mg = bm_child(root, "MaxGenerations")
    try:
        return int((mm.text or "0")) <= int((mg.text or "0"))
    except (AttributeError, ValueError):
        return True

CONSTRAINTS = [
    Constraint("migration_fires", _migration_fires, "doc §9.1: MigrationModulo > MaxGenerations → nunca migra"),
]

# ---------------------------------------------------------------------------
# Read-side grounding (NO tautológico: correr contra OTROS proyectos)
# ---------------------------------------------------------------------------

def _load(proj):
    with zipfile.ZipFile(resolve_base(proj) / "project.cfx") as z:
        bt = [n for n in z.namelist() if n.startswith("Build-Task") and n.endswith(".xml")][0]
        return ET.fromstring(z.read(bt).decode("utf-8", "surrogateescape"))


def validate_against(proj):
    root = _load(proj)
    print(f"=== READ-SIDE GROUNDING contra '{proj}' ===\n")
    ok = miss = drift = 0
    print(f"{'concepto':26} {'leído':>12}  {'def(ref)':>10}  {'activo?':<8} estado")
    print("-" * 78)
    for k in KNOBS:
        cur = k.read(root)
        act = "" if not k.gates else ("ACTIVO" if k.active(root) else "⚠INERTE")
        if cur is None:
            tag = "❌ NO RESUELVE"; miss += 1
        elif proj == REFERENCE and cur != k.default:
            tag = "⚠ default stale"; drift += 1
        else:
            tag = "✓"; ok += 1
        print(f"{k.concept:26} {str(cur):>12}  {k.default:>10}  {act:<8} {tag}")

    print("\n--- Fitness Weighted: goals activos (type w weight valueType) ---")
    goals = fitness_goals(root)
    if not goals:
        print("  (no es Weighted o sin goals activos)")
    else:
        vt = {"1": "max", "2": "min", None: "?"}
        for t, w, v in goals:
            tag = "" if t in FITNESS_GOALS else "⚠ FUERA DEL CATÁLOGO"
            print(f"  {t:25} w={w:>3} {vt.get(v, v):<4} {tag}")

    print("\n--- MutexGroups (exactamente-uno-on) ---")
    for mx in MUTEX_GROUPS:
        on, allm = mx.state(root)
        names = ",".join(n for n, _ in on) or "NINGUNO"
        print(f"  {mx.concept:14} on={names} ({len(on)}/{len(allm)})  {'✓' if len(on) == 1 else '⚠ NO es 1'}")

    print("\n--- Constraints ---")
    for c in CONSTRAINTS:
        print(f"  {c.name:18} {'✓ válido' if c.check(root) else '⚠ VIOLADO'}")

    print(f"\n=== {ok} resuelven, {miss} no-resuelven, {drift} default-stale ===")
    return miss == 0


if __name__ == "__main__":
    proj = sys.argv[1] if len(sys.argv) > 1 else REFERENCE
    sys.exit(0 if validate_against(proj) else 1)
