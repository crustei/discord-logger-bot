"""bot.log is the one master log: errors and diagnostic results only.
Info lines (login, reconnects, /ask) are dropped; ask_log in the DB tracks questions.
Known reconnect errors are one line, and a reconnect that repeats within 10 min is an error."""
import logging, time

DROP = ("PyNaCl is not installed", "davey is not installed")
KNOWN = ("ClientConnectionResetError", "ConnectionClosed", "session has been invalidated",
         "InvalidSession", "ServerDisconnectedError", "ClientOSError", "TimeoutError", "Session invalidated")
RECONNECT = ("stopped responding to the gateway", "Attempting a reconnect", "session has been invalidated",
             "has successfully RESUMED", "reconnect")
REPEAT_WINDOW = 600


class NoiseFilter(logging.Filter):
    def __init__(self, clock=time.monotonic):
        super().__init__()
        self.clock, self._last_reconnect = clock, None

    def filter(self, record):
        msg = record.getMessage()
        if any(d in msg for d in DROP):
            return False
        if record.exc_info and record.exc_info[1] is not None:
            e = record.exc_info[1]
            if any(k in f"{type(e).__name__} {e}" or k in msg for k in KNOWN):
                record.msg, record.args = f"{msg} [{type(e).__name__}: {str(e)[:120]}]", None
                record.exc_info = record.exc_text = None
                msg = record.msg
        if record.name == "diag" or (record.name == "asyncio" and record.levelno >= logging.ERROR):
            return True
        if any(k.lower() in msg.lower() for k in RECONNECT) and record.levelno < logging.ERROR:
            t, prev = self.clock(), self._last_reconnect
            self._last_reconnect = t
            if prev is None or t - prev > REPEAT_WINDOW:
                return False
            record.levelno, record.levelname = logging.ERROR, "ERROR"
            record.msg, record.args = f"repeated reconnect: {msg}", None
            return True
        return record.levelno >= logging.ERROR


def setup(level=logging.INFO):
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")
    f = NoiseFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(f)
    for name in ("discord.http", "discord.state", "discord.voice_state", "aiohttp", "urllib3", "asyncio", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
