"""Redundancy counters: how often each recovery layer fired. Kept in diag/counters.json
(survives restarts), copied into status.json["events"], one bot.log line per event."""
import datetime as dt, json, logging, os, sys
from pathlib import Path

PATH = Path(__file__).resolve().parent / "diag" / "counters.json"
log = logging.getLogger("diag")
NAMES = ("bot_starts", "restart_loop_restarts", "freeze_watchdog", "stale_status_watchdog",
         "check_task_restarts", "heartbeat_restarts", "box_pauses", "box_resets")


def load(path=PATH):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def bump(name, detail="", path=PATH, quiet=False):
    path = Path(path)
    c = load(path)
    c[name] = int(c.get(name, 0)) + 1
    c["last_" + name] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"counters.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(c, indent=1))
    os.replace(tmp, path)
    if not quiet:
        log.warning("event %s #%d%s", name, c[name], f": {detail}" if detail else "")
    return c[name]


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return ""


def on_start(path=PATH, bid=None):
    """Called once per bot start: counts the start, and a box reset if the kernel boot id changed."""
    bid = bid if bid is not None else boot_id()
    c = load(path)
    prev = c.get("boot_id")
    starts = bump("bot_starts", path=path, quiet=True)
    if prev and bid and prev != bid:
        bump("box_resets", "kernel boot id changed (box was reset)", path=path)
    elif starts > 1:
        bump("restart_loop_restarts", "bot process restarted", path=path)
    c = load(path); c["boot_id"] = bid
    Path(path).write_text(json.dumps(c, indent=1))


if __name__ == "__main__":  # shell use: python3 counters.py heartbeat_restarts "detail"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    bump(sys.argv[1], " ".join(sys.argv[2:]))
