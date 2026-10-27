import asyncio, json, logging
import counters, diag
from tests.test_diag import FakeClient, _diag


def test_bump_and_on_start(tmp_path):
    p = tmp_path / "counters.json"
    counters.on_start(p, bid="a")
    counters.on_start(p, bid="a")
    counters.on_start(p, bid="b")
    c = json.loads(p.read_text())
    assert c["bot_starts"] == 3 and c["restart_loop_restarts"] == 1 and c["box_resets"] == 1 and c["boot_id"] == "b"
    assert counters.bump("heartbeat_restarts", path=p) == 1


def test_pause_detected_and_counted(tmp_path, monkeypatch, caplog):
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    now = [1000.0]
    async def noop(level="light"): pass
    monkeypatch.setattr(d, "run_safe", noop)
    async def go():
        t = asyncio.create_task(d.loop(first=600, every=600, tick=0.01, wall=lambda: now[0]))
        await asyncio.sleep(0.02)
        now[0] += 120                     # wall jumps, monotonic barely moves = box frozen
        await asyncio.sleep(0.03)
        t.cancel()
    with caplog.at_level(logging.WARNING):
        asyncio.run(go())
    assert json.loads((tmp_path / "counters.json").read_text())["box_pauses"] == 1
    assert "box paused" in caplog.text


def test_status_includes_events(tmp_path, monkeypatch):
    (tmp_path / "counters.json").write_text(json.dumps({"box_pauses": 2, "boot_id": "x"}))
    d = _diag(tmp_path, FakeClient(), monkeypatch, [])
    st = asyncio.run(d.run("light"))
    saved = json.loads((tmp_path / "status.json").read_text())
    assert saved["events"] == {"box_pauses": 2}


def test_tests_do_not_touch_live_counters():
    import pathlib
    live = pathlib.Path(counters.__file__).resolve().parent / "diag" / "counters.json"
    before = live.read_text() if live.exists() else None
    counters.on_start(); counters.bump("box_pauses")
    assert (live.read_text() if live.exists() else None) == before
