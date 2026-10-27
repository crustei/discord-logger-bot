from watchdog import GatewayWatchdog


class Clock:
    t = 0.0
    def __call__(self): return self.t


def test_exits_after_three_minutes_disconnected():
    c = Clock(); state = {"ok": True}; exits = []
    w = GatewayWatchdog(lambda: state["ok"], clock=c, exit=exits.append)
    c.t = 10; w.beat(); assert w.check() is None
    state["ok"] = False
    c.t = 100; assert w.check() is None
    c.t = 191; assert "not connected" in w.check()


def test_stale_events_trip_even_if_flag_says_connected():
    c = Clock()
    w = GatewayWatchdog(lambda: True, clock=c)
    c.t = 179; assert w.check() is None
    c.t = 200; assert w.check() is None   # last_ok was 179
    c.t = 360; assert "event loop stalled" in w.check()


def test_healthy_traffic_never_trips():
    c = Clock()
    w = GatewayWatchdog(lambda: True, clock=c)
    for i in range(1, 100):
        c.t = i * 30; w.beat(); assert w.check() is None


def test_stale_status_triggers_exit_after_grace():
    from watchdog import GatewayWatchdog
    now = [0.0]
    age = [100.0]
    w = GatewayWatchdog(lambda: True, clock=lambda: now[0], status_age=lambda: age[0])
    w.beat()
    assert w.check() is None
    age[0] = 23 * 60
    assert w.check() is None          # still in startup grace
    now[0] = 22 * 60; w.beat()
    assert "stale" in w.check()
    age[0] = 60
    assert w.check() is None
