"""stop_graceful reconoce un proyecto que nunca arrancó (status con contadores a cero).

Además (guardrail `sqx-headless-fallos`, Fase 1): `run()` camino directo CON HTTP — barrera de carga
(MM-H10 / A3), re-comprobación del pipeline justo antes de actuar (A6) y camino SIN HTTP con motor ciego (R2),
más la CLI. Motor, procesos y tiempos falsos: nada de esto toca un proceso real (los casos con procesos
reales están en tests/test_sqx_procs.py).

Fase 2 (R1, R6, R9, C4): `persist_bank` con timeout largo, NO matar con un banco sin guardar (F2-c), MODO
PIPELINE de `run` (bandera + una entrega de `stop` por driver, sin persistir desde aquí, A6 → modo pipeline),
sondeos rotos (C4) y el veredicto de la GUI en la CLI (R9). El e2e con orquestador y driver reales está en
tests/test_pipeline_stop.py."""
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

import sqx.stop_and_persist as sp


@pytest.fixture(autouse=True)
def _nunca_el_sqcli_real(monkeypatch, tmp_path):
    """Seguridad de plataforma (incidente de la Fase 1, 2026-10-21: un test arrancó el sqcli REAL de SQX): `SQCLI` y
    `SQX_APP` a rutas inexistentes y un nombre de motor ÚNICO por test. Ningún test de este módulo puede ver, arrancar
    ni matar un sqcli real (los tests que necesitan un motor falso lo fabrican con su propio nombre)."""
    monkeypatch.setenv("SQCLI", str(tmp_path / "no_hay_sqcli_real"))
    monkeypatch.setenv("SQX_APP", str(tmp_path / "NoSQX.app"))
    monkeypatch.setenv("SQX_ENGINE_PROC_NAME", f"sqcli_{uuid4().hex[:8]}")

IDLE = """Status of project X
Strategies generated                             0
Running time so far                          0 ms.
"""
RUNNING = """Status of project X
Strategies generated                          1234
Running time so far                     12345 ms.
"""


def _patch(monkeypatch, status):
    monkeypatch.setattr(sp, "_http", lambda *a, **k: "")
    monkeypatch.setattr(sp, "_status", lambda *a, **k: status)
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)


def test_idle_never_started_counts_as_stopped(monkeypatch):
    _patch(monkeypatch, IDLE)
    assert sp.stop_graceful("X", 5050, max_wait=10) is True


def test_running_without_stop_words_times_out(monkeypatch):
    _patch(monkeypatch, RUNNING)
    assert sp.stop_graceful("X", 5050, max_wait=10) is False


# A9 (aceptación real 2026-10-27): en B144 el status tras un stop sigue igual que en marcha (solo contadores); lo
# que confirma la parada es la respuesta literal al propio stop.
STOP_REPLY = "10:19:17 Stopping project X\n" + "-" * 50 + "\nProject execution stopped.\n" + "-" * 50 + "\n"


def test_a9_la_respuesta_al_stop_basta_aunque_el_status_no_cambie(monkeypatch):
    _patch(monkeypatch, RUNNING)
    monkeypatch.setattr(sp, "_http", lambda *a, **k: STOP_REPLY)
    assert sp.stop_graceful("X", 5050, max_wait=10) is True


LIST_REPLY = ("10:42:45 List of available databanks\n" + "-" * 50 + "\nResults, Records: 101\n"
              "Last generation, Records: 99\nInitial population, Records: 0\nStrategies to improve, Records: 0\n")


def _fake_list_count(listing):
    """Respuestas literales de B144 (2026-10-27): `list` cuenta todos los bancos; `count` corta el nombre en el
    primer espacio."""
    def http(cmd, *a, **k):
        if "action=list" in cmd:
            return listing
        name = cmd.split(" name=", 1)[1]
        if " " in name:
            return f"Error: Databank '{name.split(' ')[0]}' doesn't exist."
        return {"Results": "Number of strategies\n---\nRecords: 101\n", "Out": "Records: 7\n"}.get(
            name, f"Error: Databank '{name}' doesn't exist.")
    return http


def test_a10_los_bancos_con_espacios_se_cuentan_con_la_lista(monkeypatch):
    monkeypatch.setattr(sp, "_http", _fake_list_count(LIST_REPLY))
    got = sp._motor_counts("X", ["Results", "Last generation", "Initial population", "Strategies to improve"])
    assert got == {"Results": 101, "Last generation": 99, "Initial population": 0, "Strategies to improve": 0}


def test_a10_sin_lista_cae_al_count_y_sin_respuesta_queda_desconocido(monkeypatch):
    monkeypatch.setattr(sp, "_http", _fake_list_count("Error: Missing parameter 'project'."))
    got = sp._motor_counts("X", ["Results", "Out", "Last generation"])
    assert got == {"Results": 101, "Out": 7, "Last generation": None}


def test_a10_un_banco_que_no_sale_en_la_lista_usa_count(monkeypatch):
    monkeypatch.setattr(sp, "_http", _fake_list_count(LIST_REPLY))
    assert sp._motor_counts("X", ["Out"]) == {"Out": 7}


def test_a10_nombres_con_comas_y_crlf(monkeypatch):
    """CR5-H2: el nombre llega hasta la ÚLTIMA ", Records:"; CRLF tolerado."""
    listing = "List of available databanks\r\n----\r\nMC, Results, Records: 12\r\nResults, Records: 3\r\n"
    monkeypatch.setattr(sp, "_http", _fake_list_count(listing))
    assert sp._motor_counts("X", ["MC, Results", "Results"]) == {"MC, Results": 12, "Results": 3}


def test_a10_si_la_lista_no_responde_no_se_suman_esperas_por_banco(monkeypatch):
    """CR5-H1: lista sin respuesta (timeout/conexión) → todos None sin probar `count` banco a banco."""
    calls = []

    def http(cmd, port, timeout):
        calls.append((cmd, timeout))
        raise TimeoutError("timed out")
    monkeypatch.setattr(sp, "_http", http)
    assert sp._motor_counts("X", ["Results", "Out"], timeout=6) == {"Results": None, "Out": None}
    assert calls == [("-databank action=list project=X", 6)]


def test_a11_subshell_que_termino_no_es_un_driver_raiz(monkeypatch):
    """A11 (2026-10-27): un `$(…)` del driver que muere entre el pgrep y el `ps` da ppid 0; antes contaba como raíz
    nueva y recibía un segundo stop (`test_f2d` inestable con carga). Un huérfano real (ppid 1) sí es raíz."""
    ppids = {100: 1, 101: 100, 102: 0, 103: 0, 200: 1}   # 100 driver; 101 su subshell; 102 ya muerto; 200 otro driver
    monkeypatch.setattr(sp, "_ppid", lambda pid: ppids.get(pid, 0))
    monkeypatch.setattr(sp, "_pid_exists", lambda pid: pid != 102)   # 103: vivo, pero `ps` no dio su ppid
    assert sp._driver_roots([100, 101, 102]) == [100]
    assert sp._driver_roots([100, 101, 102, 200]) == [100, 200]
    # CR6-A1: ppid 0 con el proceso vivo (falló `ps`) → raíz, como antes: un stop de más es inocuo, uno de menos no
    assert sp._driver_roots([100, 103]) == [100, 103]
    # el ppid de cada pid se lee una vez: si el proceso muere entre dos lecturas, no se reabre la carrera
    reads = []
    vivos = {101: 100}

    def ppid_que_muere(pid):
        reads.append(pid)
        return vivos.pop(pid, 1 if pid == 100 else 0)
    monkeypatch.setattr(sp, "_ppid", ppid_que_muere)
    assert sp._driver_roots([101, 100]) == [100]
    assert reads.count(101) == 1


def test_a9_entrega_del_stop_al_pipeline_una_sola_orden(monkeypatch):
    _patch(monkeypatch, RUNNING)
    sent = []
    monkeypatch.setattr(sp, "_http", lambda cmd, *a, **k: sent.append(cmd) or STOP_REPLY)
    rep = {}
    assert sp._deliver_stop("X", 5050, rep) is True
    assert rep["stop_sent"] == 1 and len(sent) == 1


# ---------------------------------------------------------------------------
# run(): camino directo CON HTTP
# ---------------------------------------------------------------------------

def _status_text(n):
    return f"Status of project X\nStrategies generated  5\nIn databank   {n:,}\n"


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """`run()` con HTTP/motor/procesos simulados. h.events = orden de status/comandos; h.kills = llamadas
    a kill_engine; h.status_values = lecturas de 'In databank' (la última se repite); h.timeouts = timeout con el
    que se llamó cada comando HTTP; h.sync_fail = bancos cuyo synctofiles devuelve error; h.count_fail = bancos
    cuyo count no responde."""
    h = SimpleNamespace(events=[], kills=[], status_values=[5], stops=0, driver_calls=0, timeouts=[],
                        sync_fail=set(), count_fail=set())
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))      # pre_gui_check mira <proyecto>/databanks_archive, .bak
    (tmp_path / "X").mkdir()

    def fake_http(cmd, port=5050, timeout=30):
        h.events.append(("cmd", cmd))
        h.timeouts.append((cmd, timeout))
        bank = cmd.rsplit("name=", 1)[-1]
        if "action=count" in cmd:
            return "Status unknown" if bank in h.count_fail else "Records: 5"
        if "action=synctofiles" in cmd:
            return "Error: boom" if bank in h.sync_fail else "Synchronization started."
        return ""

    def fake_status(proj, port):
        i = min(len([e for e in h.events if e[0] == "status"]), len(h.status_values) - 1)
        h.events.append(("status", h.status_values[i]))
        return _status_text(h.status_values[i])

    def fake_stop(proj, port, max_wait=120):
        h.stops += 1
        return True

    def fake_kill(pids=None, grace=None):
        h.kills.append(pids)
        return True

    monkeypatch.setenv("CARGA_SLEEP", "5")
    monkeypatch.setenv("STABLE_N", "3")
    monkeypatch.setenv("LOAD_WAIT_SECS", "300")
    monkeypatch.setattr(sp, "motor_alive", lambda port=5050: True)
    monkeypatch.setattr(sp, "stop_graceful", fake_stop)
    monkeypatch.setattr(sp, "_http", fake_http)
    monkeypatch.setattr(sp, "_status", fake_status)
    monkeypatch.setattr(sp, "databanks_of", lambda proj: ["Results", "Big Bank"])
    monkeypatch.setattr(sp, "count_on_disk", lambda proj: {"Results": 3})
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "orchestrator_pids", lambda proj=None: [])
    monkeypatch.setattr(sp, "driver_pids", lambda proj=None: [])
    monkeypatch.setattr(sp, "engine_pids", lambda: [])
    monkeypatch.setattr(sp, "gui_pids", lambda: [])          # pre_gui_check (veredicto de la GUI) es global
    monkeypatch.setattr(sp, "cascade_pids", lambda: [])
    # A11: un driver simulado está VIVO (tiene padre); sin esto `_ppid` lanzaba el `ps` real sobre un pid inventado
    monkeypatch.setattr(sp, "_ppid", lambda pid: 1)
    monkeypatch.setattr(sp, "kill_engine", fake_kill)
    return h


def _syncs(h):
    return [i for i, e in enumerate(h.events) if e[0] == "cmd" and "action=synctofiles" in e[1]]


def test_barrera_de_carga_espera_a_que_in_databank_se_estabilice_antes_de_sincronizar(harness):
    h = harness
    h.status_values = [10, 20, 30, 30, 30, 30]       # carga creciente que se estabiliza en 30 (3 lecturas iguales)
    rep = sp.run("X")
    assert rep["load_stable"] is True and "load_unstable" not in rep
    syncs = _syncs(h)
    assert len(syncs) == 1 and rep["banks_persisted"] == ["Results"]
    estados_antes = [e for e in h.events[:syncs[0]] if e[0] == "status"]
    assert [e[1] for e in estados_antes] == [10, 20, 30, 30, 30, 30], \
        "no se sincroniza hasta que 'In databank' lleva 3 lecturas iguales"
    assert h.stops == 1 and h.kills == [None] and rep["motor_killed"] is True
    assert rep["banks_at_risk"] == {"Big Bank": 5}   # con espacios: en riesgo, nunca por HTTP


def test_carga_que_no_se_estabiliza_ni_sincroniza_ni_mata(harness, monkeypatch):
    h = harness
    h.status_values = list(range(1, 200))             # crece siempre
    monkeypatch.setenv("LOAD_WAIT_SECS", "20")
    rep = sp.run("X")
    assert rep["load_unstable"] is True and rep["load_stable"] is False
    assert not _syncs(h) and not [e for e in h.events if e[0] == "cmd" and "action=count" in e[1]]
    assert h.kills == [] and rep["motor_killed"] is False
    assert any("carga no estabilizada" in w and "NO sincronizo" in w for w in rep["warnings"])


def test_carga_sin_estabilizar_con_force_kill_mata_pero_nunca_sincroniza(harness, monkeypatch):
    h = harness
    h.status_values = list(range(1, 200))
    monkeypatch.setenv("LOAD_WAIT_SECS", "20")
    rep = sp.run("X", force_kill=True)
    assert not _syncs(h), "--force-kill solo salta la compuerta del kill: nunca se sincroniza a ciegas"
    assert h.kills == [None] and rep["motor_killed"] is True
    assert any("--force-kill" in w for w in rep["warnings"])
    # y con --no-kill (kill=False) ni siquiera fuerza
    h.kills.clear()
    sp.run("X", kill=False, force_kill=True)
    assert h.kills == []


def test_status_sin_in_databank_no_estabiliza(harness, monkeypatch):
    """Sin la línea 'In databank' no hay forma de saber si terminó de cargar: no se asume que sí."""
    h = harness
    monkeypatch.setenv("LOAD_WAIT_SECS", "10")
    monkeypatch.setattr(sp, "_status", lambda proj, port: "Status of project X\nRunning time so far  0 ms.\n")
    rep = sp.run("X")
    assert rep["load_unstable"] is True and not _syncs(h) and h.kills == []


def test_wait_loaded_devuelve_estable_y_ultimo_valor(monkeypatch):
    vals = iter([1, 2, 2, 2, 2])
    monkeypatch.setattr(sp, "_status", lambda proj, port: _status_text(next(vals)))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    assert sp.wait_loaded("X", 5050, sleep_s=1, stable_n=3, timeout_s=100) == (True, "2")


# ---------------------------------------------------------------------------
# F2-c (R1/R2): persist_bank con timeout largo; NO matar con un banco sin guardar (salvo --force-kill)
# ---------------------------------------------------------------------------

def test_persist_bank_usa_el_timeout_largo_por_defecto_y_es_inyectable(monkeypatch):
    seen = []
    monkeypatch.setattr(sp, "_http", lambda cmd, port=5050, timeout=30: seen.append(timeout) or "Synchronization started.")
    monkeypatch.delenv("SQ_LONG_SECS", raising=False)
    assert sp.persist_bank("X", "Results", 5050) is True
    assert seen[-1] == 1800 == sp.SQ_LONG_SECS, "antes 30 s: cortaba el sync de un banco gordo"
    monkeypatch.setenv("SQ_LONG_SECS", "77")
    sp.persist_bank("X", "Results", 5050)
    assert seen[-1] == 77
    sp.persist_bank("X", "Results", 5050, timeout=5)
    assert seen[-1] == 5


def test_run_sincroniza_cada_banco_con_el_timeout_largo(harness):
    sp.run("X")
    sync_timeouts = [t for c, t in harness.timeouts if "action=synctofiles" in c]
    assert sync_timeouts == [1800]
    assert all(t < 1800 for c, t in harness.timeouts if "action=count" in c), "count sí es una consulta rápida"


def test_run_no_mata_el_motor_si_un_persist_falla_salvo_force_kill(harness):
    h = harness
    h.sync_fail = {"Results"}
    rep = sp.run("X")
    assert rep["persist_failed"] is True and rep["banks_http_failed"] == ["Results"]
    assert h.kills == [] and rep["motor_killed"] is False
    assert any("NO mato el motor" in w and "Results" in w and "--force-kill" in w for w in rep["warnings"])
    # con --force-kill se mata (y lo dice); sin --force-kill nunca
    h.kills.clear()
    rep = sp.run("X", force_kill=True)
    assert h.kills == [None] and rep["motor_killed"] is True and rep["persist_failed"] is False
    assert any("--force-kill" in w and "Results" in w and "se pierde" in w for w in rep["warnings"])


def test_run_no_mata_el_motor_si_un_count_falla_salvo_force_kill(harness):
    h = harness
    h.count_fail = {"Results"}
    rep = sp.run("X")
    assert rep["banks_unknown"] == ["Results"] and rep["persist_failed"] is True
    assert h.kills == [] and rep["motor_killed"] is False
    assert not [e for e in h.events if e[0] == "cmd" and "action=synctofiles" in e[1] and "Results" in e[1]]
    rep = sp.run("X", force_kill=True)
    assert h.kills == [None] and rep["motor_killed"] is True


def test_run_con_no_kill_nunca_mata_ni_marca_persist_failed(harness):
    h = harness
    h.sync_fail = {"Results"}
    rep = sp.run("X", kill=False)
    assert h.kills == [] and rep["persist_failed"] is False      # no se pidió matar: no hay compuerta que fallar
    assert rep["banks_http_failed"] == ["Results"]


# ---------------------------------------------------------------------------
# MODO PIPELINE (R6): bandera + una entrega de `stop` por driver, SIN persistir desde aquí
# ---------------------------------------------------------------------------

@pytest.fixture()
def world(monkeypatch, tmp_path):
    """Mundo simulado para el modo pipeline: PIDs de orquestador/driver vivos, motor HTTP, status del proyecto y
    un `on_poll` que hace evolucionar el mundo en cada sondeo de `orchestrator_pids` (el reloj del bucle)."""
    w = SimpleNamespace(orch=[111], drv=[222], motor_up=True, state="running", cmds=[], polls=0, kills=[],
                        engines=[], cpu=0.1, flag_seen=[], on_poll=lambda w: None, stop_ignored=False,
                        probe_fail_from=None, stop_graceful_called=0)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    (tmp_path / "X").mkdir()
    w.flag = tmp_path / "X" / ".pipeline_stop_request"

    def orchestrator_pids(proj=None):
        w.polls += 1
        w.flag_seen.append(w.flag.exists())
        if w.probe_fail_from is not None and w.polls >= w.probe_fail_from:
            raise sp.ProcProbeError("pgrep roto (test)")
        w.on_poll(w)
        return list(w.orch)

    def status(proj, port):
        if not w.motor_up:
            return ""
        return {"never": "Status of project X\nStrategies generated 0\nRunning time so far  0 ms.\n",
                "running": "Status of project X\nStrategies generated 9\nRunning time so far  5000 ms.\n",
                # EN MARCHA pero con las palabras "stopped"/"finished" en otro contexto: el stop se envía igual
                "running_noisy": "Status of project X\nTasks finished: 0 of 1\nStopped by: nobody\n"
                                 "Strategies generated 9\nRunning time so far  5000 ms.\n",
                "stopped": "Status of project X\nProject execution stopped\n"}[w.state]

    def http(cmd, port=5050, timeout=30):
        w.cmds.append(cmd)
        if "action=stop" in cmd and not w.stop_ignored:
            w.state = "stopped"
        return ""

    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    monkeypatch.setattr(sp, "orchestrator_pids", orchestrator_pids)
    monkeypatch.setattr(sp, "driver_pids", lambda proj=None: list(w.drv))
    monkeypatch.setattr(sp, "motor_alive", lambda port=5050: w.motor_up)
    monkeypatch.setattr(sp, "_status", status)
    monkeypatch.setattr(sp, "_http", http)
    monkeypatch.setattr(sp, "engine_pids", lambda: list(w.engines))
    monkeypatch.setattr(sp, "engine_cpu", lambda pid, **k: w.cpu)
    monkeypatch.setattr(sp, "kill_engine", lambda pids=None, grace=None: w.kills.append(pids) or True)
    monkeypatch.setattr(sp, "gui_pids", lambda: [])
    monkeypatch.setattr(sp, "cascade_pids", lambda: [])
    # A11: los drivers simulados están VIVOS (tienen padre); sin esto `_ppid` lanzaba el `ps` real sobre pids inventados
    monkeypatch.setattr(sp, "_ppid", lambda pid: 1)

    def stop_graceful(*a, **k):
        w.stop_graceful_called += 1
        return True

    monkeypatch.setattr(sp, "stop_graceful", stop_graceful)
    w.stops = lambda: [c for c in w.cmds if "action=stop" in c]
    return w


def _pipeline_sale_en(n):
    def on_poll(w):
        if w.polls >= n:
            w.orch.clear()
            w.drv.clear()
    return on_poll


def test_modo_pipeline_entrega_un_stop_por_driver_sin_persistir_ni_matar_y_borra_la_bandera(world):
    w = world
    w.on_poll = _pipeline_sale_en(5)
    rep = sp.run("X", wait_secs=20)
    assert len(w.stops()) == 1, "UNA entrega efectiva de stop por driver (no se repite tras aceptarse)"
    assert not [c for c in w.cmds if "synctofiles" in c or "action=count" in c], "no persiste desde aquí: lo hace el driver"
    assert w.kills == [] and rep["motor_killed"] is False and w.stop_graceful_called == 0
    assert rep["pipeline_alive"] is True and rep["pipeline_stopped"] is True and rep["pipeline_stopping"] is False
    assert rep["stopped_by"] == "pipeline" and rep["stop_delivered"] is True and rep["stop_sent"] == 1
    assert any(w.flag_seen), "la bandera estuvo puesta durante la espera"
    assert not w.flag.exists(), "sin orquestador vivo la bandera se borra"
    assert rep["safe_for_gui"] is True and rep["reasons"] == []        # (todo simulado como parado)


def test_modo_pipeline_no_envia_stop_antes_del_start_y_lo_envia_cuando_el_proyecto_corre(world):
    """Un stop antes del START (el driver aún carga) no hace nada y se daría por 'entregado' en falso: no se envía
    mientras el status diga que el proyecto no corre (PLAN-DESVIO menor documentado en `_deliver_stop`)."""
    w = world
    w.state = "never"

    def on_poll(w):
        if w.polls == 4:
            w.state = "running"               # el driver manda el START
        if w.polls >= 8:
            w.orch.clear(); w.drv.clear()

    w.on_poll = on_poll
    rep = sp.run("X", wait_secs=30)
    assert len(w.stops()) == 1 and rep["stop_delivered"] is True
    assert w.polls >= 5, "esperó al START antes de entregar el stop"


def test_modo_pipeline_un_status_en_marcha_con_las_palabras_finished_o_stopped_recibe_su_stop(world):
    """El stop solo se omite con un "parado" CLARO (`not running`, `Project execution stopped`, nunca arrancó): un status
    en marcha que contiene "finished"/"stopped" en otro contexto NO debe impedir la parada (si no, nunca se enviaría)."""
    w = world
    w.state = "running_noisy"
    w.on_poll = _pipeline_sale_en(5)
    rep = sp.run("X", wait_secs=20)
    assert len(w.stops()) == 1 and rep["stop_delivered"] is True and rep["pipeline_stopped"] is True


def test_modo_pipeline_motor_sin_respuesta_reintenta_en_la_vuelta_siguiente(world):
    w = world
    w.motor_up = False

    def on_poll(w):
        if w.polls == 4:
            w.motor_up = True
        if w.polls >= 8:
            w.orch.clear(); w.drv.clear()

    w.on_poll = on_poll
    rep = sp.run("X", wait_secs=30)
    assert len(w.stops()) == 1 and rep["pipeline_stopped"] is True


def test_modo_pipeline_tope_agotado_con_orquestador_vivo_deja_la_bandera_y_pipeline_stopping(world):
    w = world
    rep = sp.run("X", wait_secs=6)                 # nunca sale
    assert rep["pipeline_alive"] and rep["pipeline_stopping"] is True and rep["pipeline_stopped"] is False
    assert rep["stopped_by"] is None and rep["stop_delivered"] is True
    assert w.flag.exists(), "con orquestador vivo la bandera se queda: abortará al llegar"
    assert any("parando: el pipeline saldrá al terminar de guardar la task en curso" in x
               and "no lanzará ninguna task más" in x for x in rep["warnings"])
    assert rep["safe_for_gui"] is False and any("orquestador" in r for r in rep["reasons"])


def test_modo_pipeline_solo_driver_al_agotar_el_tope_borra_la_bandera_y_lo_dice(world):
    w = world
    w.orch.clear()
    rep = sp.run("X", wait_secs=6)
    assert rep["pipeline_stopping"] is True and not w.flag.exists()
    assert any("Solo queda el driver" in x for x in rep["warnings"])


def test_modo_pipeline_si_el_stop_no_llega_a_entregarse_lo_avisa(world):
    w = world
    w.motor_up = False
    rep = sp.run("X", wait_secs=6)
    assert rep["pipeline_stopping"] and rep["stop_delivered"] is False and not w.stops()
    assert any("no llegó a entregarse" in x for x in rep["warnings"])


def test_modo_pipeline_stop_no_aceptado_se_reintenta_y_un_driver_nuevo_recibe_el_suyo(world):
    """Si el motor sigue 'running' tras el stop, no se da por entregado y se reintenta; un driver que aparece en la
    espera (carrera de lanzamiento del orquestador) recibe su propio stop."""
    w = world
    w.stop_ignored = True                                  # el motor no deja de 'running'

    def on_poll(w):
        if w.polls == 3:
            w.stop_ignored = False                         # a la 3.ª vuelta el motor ya acepta
        if w.polls == 6:
            w.drv[:] = [333]; w.state = "running"          # el orquestador lanzó otra task: driver NUEVO
        if w.polls >= 10:
            w.orch.clear(); w.drv.clear()

    w.on_poll = on_poll
    rep = sp.run("X", wait_secs=40)
    # stops previos sin efecto (reintentos) + 1 aceptado al driver 222 + 1 al driver nuevo 333
    assert len(w.stops()) == 3
    assert rep["pipeline_stopped"] is True and rep["stop_sent"] == 3


def test_modo_pipeline_sondeo_final_roto_no_afirma_que_paro(world):
    w = world
    w.probe_fail_from = 3                                  # a partir de la 3.ª consulta pgrep falla (también la final)
    rep = sp.run("X", wait_secs=6)
    assert rep["probe_failed"] is True and rep["pipeline_stopped"] is False and rep["pipeline_stopping"] is False
    assert any("no se pudo comprobar: no actúo" in x and "si el pipeline" in x for x in rep["warnings"])


def test_modo_pipeline_queda_un_sqcli_huerfano_camino_directo(world):
    """Sin driver ni orquestador pero con un sqcli vivo (p. ej. el que un driver dejó por un sync sin confirmar):
    camino directo sobre él (motor sin HTTP y CPU baja → zombi)."""
    w = world
    w.engines = [999]

    def on_poll(w):
        if w.polls >= 3:
            w.orch.clear(); w.drv.clear(); w.motor_up = False

    w.on_poll = on_poll
    rep = sp.run("X", wait_secs=20)
    assert rep["pipeline_stopped"] and rep["stopped_by"] == "pipeline"
    assert w.kills == [[999]] and rep["zombi_killed"] is True and rep["nothing_to_stop"] is True


def test_a6_driver_que_aparece_antes_del_stop_entra_en_modo_pipeline_y_no_para_el_motor_a_pelo(world):
    """A6: el pipeline arranca entre el primer chequeo y la decisión de enviar stop: NO se hace el stop graceful
    del camino directo (competiría con el driver): bandera + delegación."""
    w = world
    w.orch[:] = []
    w.drv[:] = []

    def on_poll(w):
        if w.polls == 2:                         # 2.º chequeo (justo antes del stop): ya hay orquestador y driver
            w.orch[:] = [111]; w.drv[:] = [222]
        if w.polls >= 6:
            w.orch.clear(); w.drv.clear()

    w.on_poll = on_poll
    rep = sp.run("X", wait_secs=20)
    assert w.stop_graceful_called == 0, "con un pipeline vivo NO se hace el stop graceful directo"
    assert rep["pipeline_alive"] is True and len(w.stops()) == 1 and w.kills == []
    assert rep["pipeline_stopped"] is True and rep["stopped_by"] == "pipeline"


def test_a6_driver_que_aparece_justo_antes_de_matar_entra_en_modo_pipeline(harness, monkeypatch):
    """Re-comprobación 'justo antes de actuar': 3 chequeos (inicio, antes del stop, antes del kill). Si aparece al
    tercero, ya se había persistido: no se mata y se pasa a modo pipeline (bandera + delegación)."""
    h = harness
    state = {"drv": [], "polls": 0}

    def driver_pids(proj=None):
        h.driver_calls += 1
        if h.driver_calls >= 3:
            state["polls"] += 1
            return [222] if state["polls"] <= 2 else []     # aparece y se va durante la espera del modo pipeline
        return []

    monkeypatch.setattr(sp, "driver_pids", driver_pids)
    monkeypatch.setattr(sp, "_status", lambda proj, port: "Status of project X\nStrategies generated 9\n"
                                                         "In databank 5\nRunning time so far  5 ms.\n")
    rep = sp.run("X", wait_secs=10)
    assert h.kills == [] and rep["motor_killed"] is False
    assert rep["banks_persisted"] == ["Results"], "ya se había persistido (el driver apareció tras el sync)"
    assert rep["pipeline_alive"] is True


# ---------------------------------------------------------------------------
# C4: el sondeo de procesos roto NO se traduce en actuar a ciegas
# ---------------------------------------------------------------------------

def test_c4_pipeline_alive_con_pgrep_roto_no_actua_y_lo_dice(harness, monkeypatch):
    h = harness

    def roto(proj=None):
        raise sp.ProcProbeError("pgrep -f roto (test)")

    monkeypatch.setattr(sp, "orchestrator_pids", roto)
    rep = sp.run("X")
    assert rep["probe_failed"] is True
    assert h.events == [] and h.kills == [] and rep["motor_killed"] is False and h.stops == 0
    assert any("no se pudo comprobar: no actúo" in w and "pipeline vivo" in w for w in rep["warnings"])
    assert rep["safe_for_gui"] is False


def test_c4_engine_pids_roto_en_el_camino_directo_tampoco_mata(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(sp, "motor_alive", lambda port=5050: False)

    def roto():
        raise sp.ProcProbeError("pgrep -x roto (test)")

    monkeypatch.setattr(sp, "engine_pids", roto)
    rep = sp.run("X")
    assert rep["probe_failed"] is True and h.kills == [] and "zombi_killed" not in rep
    assert any("no se pudo comprobar: no actúo" in w and "motor vivo" in w for w in rep["warnings"])


def test_c4_pipeline_alive_es_fail_closed_para_quien_la_llame_a_ciegas(monkeypatch):
    """`_pipeline_alive` captura ProcProbeError: devuelve True (quien haga `if _pipeline_alive(...)` NO actúa) y deja
    `probe_failed` + el motivo en el reporte para quien necesite distinguirlo de 'hay un pipeline vivo'."""
    rep = {"warnings": []}

    def roto(proj=None):
        raise sp.ProcProbeError("pgrep -f roto (test)")

    monkeypatch.setattr(sp, "orchestrator_pids", roto)
    monkeypatch.setattr(sp, "driver_pids", lambda proj=None: [])
    assert sp._pipeline_alive("X", rep) is True
    assert rep["probe_failed"] is True and "pipeline_alive" not in rep       # no es "hay un pipeline vivo"
    assert len(rep["warnings"]) == 1 and "no actúo" in rep["warnings"][0]
    # con el sondeo sano: False sin pipeline; True (y datos) con él
    monkeypatch.setattr(sp, "orchestrator_pids", lambda proj=None: [])
    rep = {"warnings": []}
    assert sp._pipeline_alive("X", rep) is False and not rep.get("probe_failed")
    monkeypatch.setattr(sp, "driver_pids", lambda proj=None: [7])
    assert sp._pipeline_alive("X", rep) is True and rep["pipeline_pids"] == {"orchestrator": [], "driver": [7]}


# ---------------------------------------------------------------------------
# run(): sin HTTP, motor ciego (R2)
# ---------------------------------------------------------------------------

@pytest.fixture()
def ciego(harness, monkeypatch):
    monkeypatch.setattr(sp, "motor_alive", lambda port=5050: False)
    monkeypatch.setattr(sp, "engine_pids", lambda: [999])
    return harness


def test_sin_http_cpu_baja_zombi_se_mata(ciego, monkeypatch):
    monkeypatch.setattr(sp, "engine_cpu", lambda pid, **k: 0.4)
    rep = sp.run("X")
    assert ciego.kills == [[999]] and rep["zombi_killed"] is True and rep["nothing_to_stop"] is True
    assert rep["engine_cpu"] == 0.4 and not rep["warnings"]


def test_sin_http_cpu_alta_no_se_mata_salvo_force_kill(ciego, monkeypatch):
    monkeypatch.setattr(sp, "engine_cpu", lambda pid, **k: 87.5)
    rep = sp.run("X")
    assert ciego.kills == [] and rep["engine_busy"] is True and rep["nothing_to_stop"] is False
    assert "saturado" in rep["warnings"][0] and "--force-kill" in rep["warnings"][0]
    rep = sp.run("X", force_kill=True)
    assert ciego.kills == [[999]] and rep["zombi_killed"] is True
    assert any("--force-kill" in w and "trabajando" in w for w in rep["warnings"])


def test_sin_http_umbral_exacto_5_por_ciento(ciego, monkeypatch):
    monkeypatch.setattr(sp, "engine_cpu", lambda pid, **k: sp.CPU_BUSY_PCT)        # 5,0 % = trabajando
    assert sp.run("X")["engine_busy"] is True
    monkeypatch.setattr(sp, "engine_cpu", lambda pid, **k: sp.CPU_BUSY_PCT - 0.1)  # 4,9 % = colgado
    assert sp.run("X")["zombi_killed"] is True


def test_sin_http_ni_proceso_nada_que_parar(harness, monkeypatch):
    monkeypatch.setattr(sp, "motor_alive", lambda port=5050: False)
    rep = sp.run("X")
    assert rep["nothing_to_stop"] is True and harness.kills == [] and not rep["warnings"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

BASE_REP = {"proj": "X", "nothing_to_stop": False, "stopped": True, "banks_persisted": [], "banks_http_failed": [],
            "banks_empty": [], "banks_at_risk": {}, "banks_unknown": [], "databanks": {}, "motor_killed": True,
            "warnings": []}


def _cli(monkeypatch, capsys, argv, report):
    seen = {}

    def fake_run(proj, port=5050, kill=True, force_kill=False, wait_secs=None):
        seen.update(proj=proj, port=port, kill=kill, force_kill=force_kill)
        if wait_secs is not None:
            seen["wait_secs"] = wait_secs
        return report

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["stop_and_persist.py", *argv])
    rc = sp._cli()
    return rc, seen, capsys.readouterr().out


def test_cli_force_kill_y_no_kill_se_propagan(monkeypatch, capsys):
    rc, seen, out = _cli(monkeypatch, capsys, ["X", "--force-kill"], BASE_REP)
    assert rc == 0 and seen == {"proj": "X", "port": 5050, "kill": True, "force_kill": True}
    rc, seen, _ = _cli(monkeypatch, capsys, ["X", "6060", "--no-kill"], BASE_REP)
    assert seen == {"proj": "X", "port": 6060, "kill": False, "force_kill": False}


def test_cli_zombi_matado_nada_que_parar_y_zombi_que_no_muere(monkeypatch, capsys):
    rc, _, out = _cli(monkeypatch, capsys, ["X"], {**BASE_REP, "nothing_to_stop": True, "zombi_killed": True,
                                                   "motor_killed": False})
    assert rc == 0 and "detectado y matado" in out
    rc, _, out = _cli(monkeypatch, capsys, ["X"], {**BASE_REP, "nothing_to_stop": True, "motor_killed": False})
    assert rc == 0 and "Nada que parar" in out
    rc, _, out = _cli(monkeypatch, capsys, ["X"], {**BASE_REP, "nothing_to_stop": True, "zombi_killed": False,
                                                   "motor_killed": False})
    assert rc == 1 and "NO se pudo matar" in out and "Nada que parar" not in out


@pytest.mark.parametrize("flag", ["engine_busy", "load_unstable", "persist_failed", "probe_failed"])
def test_cli_rc_1_y_motivo_si_no_se_completo_la_parada(monkeypatch, capsys, flag):
    rep = {**BASE_REP, flag: True, "motor_killed": False, "warnings": ["motivo de prueba"]}
    rc, _, out = _cli(monkeypatch, capsys, ["X"], rep)
    assert rc == 1 and "NO completado" in out and "motivo de prueba" in out
    assert "ya puedes abrir la GUI" not in out.lower()


def test_cli_wait_secs_se_propaga_solo_si_se_pide(monkeypatch, capsys):
    _, seen, _ = _cli(monkeypatch, capsys, ["X"], BASE_REP)
    assert "wait_secs" not in seen
    _, seen, _ = _cli(monkeypatch, capsys, ["X", "--wait-secs", "45"], BASE_REP)
    assert seen["wait_secs"] == 45


def test_cli_pipeline_parado_rc0_y_pipeline_parandose_rc1(monkeypatch, capsys):
    base = {**BASE_REP, "pipeline_alive": True, "motor_killed": False}
    rc, _, out = _cli(monkeypatch, capsys, ["X"], {**base, "pipeline_stopped": True, "stopped_by": "pipeline",
                                                    "safe_for_gui": False, "reasons": ["hay un sqcli vivo (pids 7)"]})
    assert rc == 0 and "Pipeline de 'X' parado" in out and "NO completado" not in out
    rc, _, out = _cli(monkeypatch, capsys, ["X"], {**base, "pipeline_stopped": False, "pipeline_stopping": True,
                                                    "warnings": ["parando: el pipeline sigue vivo"],
                                                    "safe_for_gui": False, "reasons": ["hay un orquestador"]})
    assert rc == 1 and "PARÁNDOSE" in out and "parando: el pipeline sigue vivo" in out


@pytest.mark.parametrize("rep_extra", [
    {},                                                                                   # parada directa normal
    {"nothing_to_stop": True},                                                            # nada que parar
    {"pipeline_alive": True, "pipeline_stopped": True, "stopped_by": "pipeline"},        # pipeline parado
])
def test_cli_solo_dice_ya_puedes_abrir_la_gui_con_safe_for_gui(monkeypatch, capsys, rep_extra):
    """R9: el veredicto sale de `safe_for_gui` (= pre_gui_check), no de 'matamos el motor'."""
    seguro = {**BASE_REP, **rep_extra, "safe_for_gui": True, "reasons": []}
    _, _, out = _cli(monkeypatch, capsys, ["X"], seguro)
    assert "Ya puedes abrir la GUI de SQX" in out and "NO abras la GUI" not in out
    inseguro = {**BASE_REP, **rep_extra, "safe_for_gui": False,
                "reasons": ["hay un sqcli vivo (pids 4321)", "queda project.cfx.pre_run_task.bak"]}
    _, _, out = _cli(monkeypatch, capsys, ["X"], inseguro)
    assert "ya puedes abrir la gui" not in out.lower()
    assert "NO abras la GUI todavía" in out and "sqcli vivo (pids 4321)" in out and "pre_run_task.bak" in out


def test_cli_motor_matado_ya_no_dice_ya_puedes_abrir_la_gui_por_si_solo(monkeypatch, capsys):
    """Antes: `motor_killed` ⇒ "ya puedes abrir la GUI". Ahora un motor matado con la GUI NO segura no lo dice."""
    rep = {**BASE_REP, "motor_killed": True, "safe_for_gui": False, "reasons": ["hay un driver de pipeline vivo"]}
    _, _, out = _cli(monkeypatch, capsys, ["X"], rep)
    assert "sqcli matado" in out and "ya puedes abrir la gui" not in out.lower()
    # sin la clave (reporte antiguo) no se afirma nada
    rep = {k: v for k, v in BASE_REP.items()}
    _, _, out = _cli(monkeypatch, capsys, ["X"], rep)
    assert "ya puedes abrir la gui" not in out.lower() and "NO abras la GUI" not in out
