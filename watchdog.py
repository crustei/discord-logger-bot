"""In-process gateway watchdog. Exits the process so supervise.sh restarts it
when the bot has not been connected and ready (or seen gateway traffic) for too long."""
import faulthandler, logging, os, sys, threading, time

log = logging.getLogger(__name__)


class GatewayWatchdog:
    def __init__(self, is_connected, *, limit=180.0, interval=15.0, clock=time.monotonic, exit=os._exit,
                 status_age=None, stale_after=22 * 60, grace=22 * 60):
        self.is_connected, self.limit, self.interval = is_connected, limit, interval
        self.clock, self._exit = clock, exit
        self.last_ok = clock()
        self.last_event = clock()
        self._stop = threading.Event()
        self.status_age, self.stale_after, self.started = status_age, stale_after, clock()
        self.grace = grace

    def beat(self):
        self.last_event = self.clock()

    def check(self) -> str | None:
        t = self.clock()
        if self.status_age is not None and t - self.started >= self.grace:
            try:
                age = self.status_age()
            except Exception:
                age = None
            if age is not None and age > self.stale_after:
                return f"diag status.json stale ({age:.0f}s > {self.stale_after:.0f}s)"
        try:
            ok = bool(self.is_connected())
        except Exception:
            ok = False
        if ok and t - self.last_event < self.limit:
            self.last_ok = t
            return None
        if t - self.last_ok >= self.limit:
            why = "not connected/ready" if not ok else "event loop stalled"
            return f"{why} for {t - self.last_ok:.0f}s (last loop beat {t - self.last_event:.0f}s ago)"
        return None

    def _run(self):
        while not self._stop.wait(self.interval):
            reason = self.check()
            if reason:
                log.error("watchdog: %s; exiting for supervisor restart", reason)
                try:
                    import counters
                    counters.bump("stale_status_watchdog" if "stale" in reason else "freeze_watchdog", quiet=True)
                except Exception:
                    pass
                try:
                    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    sys.stderr.flush()
                    for h in logging.getLogger().handlers:
                        h.flush()
                except Exception:
                    pass
                self._exit(1)
                return

    def start(self):
        threading.Thread(target=self._run, name="gateway-watchdog", daemon=True).start()

    def stop(self):
        self._stop.set()
