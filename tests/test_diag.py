import asyncio, json, logging, math, sys
from types import SimpleNamespace as N
import pytest
import diag
from diag import Diagnostics


class FakeClient:
    def __init__(self, ready=True, closed=False, latency=0.1):
        self._r, self._c, self.latency, self.guilds = ready, closed, latency, []
    def is_ready(self): return self._r
    def is_closed(self): return self._c


def test_gateway_check():
    assert diag.check_gateway(FakeClient())["ok"]
    assert not diag.check_gateway(FakeClient(ready=False))["ok"]
    assert not diag.check_gateway(FakeClient(closed=True))["ok"]
    assert not diag.check_gateway(FakeClient(latency=math.inf))["ok"]
    assert not diag.check_gateway(FakeClient(latency=12))["ok"]


def test_db_write_disk_size_links_catchup(tmp_path):
    db = str(tmp_path / "x.db")
    assert diag.check_db_write(db)["ok"]
    assert diag.check_db_size(db)["ok"]
    assert diag.check_disk(str(tmp_path))["ok"]
    (tmp_path / "l.json").write_text(json.dumps({"a": "https://x"}))
    assert diag.check_source_links(tmp_path / "l.json")["ok"]
    assert not diag.check_catchup(None)["ok"]
    assert diag.check_catchup(diag.time.time() - 5)["ok"]


def test_gemini_ping_stub():
    assert diag.check_gemini(N(_complete=lambda s, u: " PONG "))["ok"]
    assert not diag.check_gemini(N(_complete=lambda s, u: "hello"))["ok"]
    assert not diag.check_gemini(N(name="extractive"))["ok"]


def _diag(tmp_path, client, monkeypatch, exits):
    for name in ("check_db_write", "check_kb_search", "check_log_writable"):
        monkeypatch.setattr(diag, name, lambda *a: diag._res(True))
    return Diagnostics(client, db_path="x", docs_dir="x", log_path="x", out_dir=tmp_path, exit=exits.append)


def test_light_run_writes_status_and_log(tmp_path, monkeypatch):
    exits = []
    d = _diag(tmp_path, FakeClient(), monkeypatch, exits)
    st = asyncio.run(d.run("light"))
    saved = json.loads((tmp_path / "status.json").read_text())
    assert saved["ok"] and saved["level"] == "light" and "gateway" in saved["checks"] and "timestamp" in saved
    assert not (tmp_path / "diagnostics.log").exists() and not exits


def test_status_keeps_synthetic_section(tmp_path, monkeypatch):
    (tmp_path / "status.json").write_text(json.dumps({"synthetic": {"ok": True}}))
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    asyncio.run(d.run("light"))
    assert json.loads((tmp_path / "status.json").read_text())["synthetic"] == {"ok": True}


def test_raising_check_is_a_failure(tmp_path, monkeypatch):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    def boom(*a): raise RuntimeError("nope")
    monkeypatch.setattr(diag, "check_kb_search", boom)
    st = asyncio.run(d.run("light"))
    assert not st["ok"] and "nope" in st["checks"]["kb_search"]["detail"]


def test_gateway_dead_twice_exits(tmp_path, monkeypatch):
    exits = []
    d = _diag(tmp_path, FakeClient(ready=False), monkeypatch, exits)
    asyncio.run(d.run("light"))
    assert exits == []
    asyncio.run(d.run("light"))
    assert exits == [3]


def test_pipeline_stub_checks(monkeypatch):
    import qa
    def fake(db, *, asker_tier, llm, **k):
        a = llm.answer("q", "")
        if "Zorblax" in a:
            return N(kind="empty", text="I don't know.")
        return N(kind="answer", text=a + "\n\nBased on: https://docs.google.com/x")
    monkeypatch.setattr(qa, "answer_in_new_connection", fake)
    assert diag.check_pipeline("x", "x")["ok"]
    monkeypatch.setattr(qa, "answer_in_new_connection",
                        lambda db, **k: N(kind="answer", text=k["llm"].answer("q", "") + "\n\nBased on: https://a"))
    r = diag.check_pipeline("x", "x")
    assert not r["ok"] and "grounding" in r["detail"]


def test_noise_filter():
    from logfilter import NoiseFilter
    f = NoiseFilter()
    rec = logging.LogRecord("discord.client", logging.WARNING, "", 0, "PyNaCl is not installed, voice will NOT be supported", None, None)
    assert not f.filter(rec)
    try:
        raise ConnectionResetError("x")
    except ConnectionResetError:
        class ClientConnectionResetError(Exception): pass
        rec = logging.LogRecord("discord.client", logging.ERROR, "", 0, "Attempting a reconnect in %.2fs", (1.0,),
                                (ClientConnectionResetError, ClientConnectionResetError("reset"), None))
    assert f.filter(rec) and rec.exc_info is None and "ClientConnectionResetError" in rec.getMessage()


def test_single_instance_lock(tmp_path):
    import subprocess
    from bot import acquire_single_instance_lock
    p = str(tmp_path / "bot.lock")
    assert acquire_single_instance_lock(p)
    code = f"import sys; sys.path.insert(0, {str(diag.ROOT)!r}); from bot import acquire_single_instance_lock as a; print(a({p!r}))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == "False"


def test_master_log_keeps_errors_and_diag_only():
    from logfilter import NoiseFilter
    t = [0.0]
    f = NoiseFilter(clock=lambda: t[0])
    R = lambda name, lvl, msg: logging.LogRecord(name, lvl, "", 0, msg, None, None)
    assert not f.filter(R("qa_bot", logging.INFO, "Logged in as X"))
    assert not f.filter(R("qa_bot", logging.INFO, "ask id=1 tier=member outcome=answer"))
    assert f.filter(R("diag", logging.INFO, "diag light ok 5/5"))
    assert f.filter(R("qa_bot", logging.ERROR, "ask failed"))
    assert not f.filter(R("discord.gateway", logging.WARNING, "Shard ID None has stopped responding to the gateway. Closing"))
    t[0] = 100
    rec = R("discord.gateway", logging.WARNING, "Shard ID None has stopped responding to the gateway. Closing")
    assert f.filter(rec) and rec.levelname == "ERROR"


def test_interval_is_10_min_and_stale_22():
    assert diag.LIGHT_EVERY == 600 and diag.STALE_AFTER == 22 * 60 and diag.RUN_TIMEOUT == 120


def test_durations_in_status_and_log(tmp_path, monkeypatch, caplog):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    with caplog.at_level(logging.INFO, logger="diag"):
        st = asyncio.run(d.run("light"))
    assert all("secs" in v for v in st["checks"].values())
    assert "kb_search=" in caplog.text and "s)" in caplog.text


def test_run_safe_timeout_is_logged_failure(tmp_path, monkeypatch, caplog):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    async def slow(level="light"): await asyncio.sleep(5)
    monkeypatch.setattr(d, "run", slow)
    with caplog.at_level(logging.ERROR, logger="diag"):
        assert asyncio.run(d.run_safe("light", timeout=0.05)) is None
    assert "timed out" in caplog.text
    saved = json.loads((tmp_path / "status.json").read_text())
    assert not saved["ok"] and "timed out" in saved["checks"]["run"]["detail"]


def test_run_safe_error_does_not_stop_loop(tmp_path, monkeypatch, caplog):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    calls = []
    async def flaky(level="light"):
        calls.append(1)
        if len(calls) == 1: raise RuntimeError("boom")
    monkeypatch.setattr(d, "run", flaky)
    async def go():
        t = asyncio.create_task(d.loop(first=0, every=0.01))
        await asyncio.sleep(0.1); t.cancel()
    with caplog.at_level(logging.ERROR, logger="diag"):
        asyncio.run(go())
    assert len(calls) >= 2 and "boom" in caplog.text


def test_loop_task_restarts_when_it_ends(tmp_path, monkeypatch, caplog):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    n = []
    async def dies(first=60, every=600):
        n.append(1)
        if len(n) == 1: raise RuntimeError("dead")
        await asyncio.sleep(10)
    monkeypatch.setattr(d, "loop", dies)
    async def go():
        d.start(); await asyncio.sleep(0.05); d._task.cancel()
    with caplog.at_level(logging.ERROR, logger="diag"):
        asyncio.run(go())
    assert len(n) >= 2 and "check loop ended (RuntimeError: dead)" in caplog.text


def test_status_age(tmp_path, monkeypatch):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    assert d.status_age() is None
    asyncio.run(d.run("light"))
    assert 0 <= d.status_age() < 5


def test_asyncio_errors_pass_log_filter():
    from logfilter import NoiseFilter
    f = NoiseFilter()
    rec = logging.LogRecord("asyncio", logging.ERROR, "x", 1, "Task exception was never retrieved", None, None)
    assert f.filter(rec)


def test_loop_uses_wall_clock_deadline_after_suspend(tmp_path, monkeypatch):
    """Simulate a box freeze: wall clock jumps far past the deadline; the check must run on the next tick."""
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    now = [1000.0]; runs = []
    async def rec(level="light"): runs.append(now[0])
    monkeypatch.setattr(d, "run_safe", rec)
    async def go():
        t = asyncio.create_task(d.loop(first=600, every=600, tick=0.01, wall=lambda: now[0]))
        await asyncio.sleep(0.03); assert runs == []
        now[0] += 3000                       # suspended for 50 min; monotonic did not advance
        await asyncio.sleep(0.03); assert len(runs) == 1
        await asyncio.sleep(0.03); assert len(runs) == 1   # next one waits a full interval
        now[0] += 600
        await asyncio.sleep(0.03); assert len(runs) == 2
        t.cancel()
    asyncio.run(go())


def test_disk_warning_does_not_fail_status(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(diag, "disk_warning", lambda p, warn_pct=80: {"pct": 85.0, "free_mb": 100, "warn": True})
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    with caplog.at_level(logging.WARNING, logger="diag"):
        st = asyncio.run(d.run("light"))
    assert st["ok"] and st["disk"]["warn"] and "disk 85% used" in caplog.text
    assert diag.disk_warning(str(tmp_path))["pct"] > 0
