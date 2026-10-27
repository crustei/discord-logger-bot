"""In-bot uptime check every 10 min (no LLM). The hourly functional test is the external
tools_local/synthetic_test.py harness.
Each run overwrites diag/status.json (state for heartbeat_check.sh) and logs one line to bot.log."""
import asyncio, datetime as dt, json, logging, math, os, shutil, sqlite3, time
from pathlib import Path

log = logging.getLogger("diag")
ROOT = Path(__file__).resolve().parent
DIAG_DIR = ROOT / "diag"
LIGHT_EVERY, FULL_EVERY = 600, 3600
TICK = 15
PAUSE_GAP = 30
RUN_TIMEOUT = 120      # one whole check pass
STALE_AFTER = 22 * 60  # status.json older than this = checks stalled
GATEWAY_DEAD_LIMIT = 2


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _res(ok, detail=""):
    return {"ok": bool(ok), "detail": str(detail)[:200]}


async def _guard(fn, *a):
    t = time.monotonic()
    try:
        out = fn(*a)
        if asyncio.iscoroutine(out):
            out = await asyncio.wait_for(out, 60)
    except Exception as e:  # a check that raises is a failed check
        out = _res(False, f"{type(e).__name__}: {e}")
    out = dict(out)
    out["secs"] = round(time.monotonic() - t, 3)
    return out


# ---- light checks -------------------------------------------------------
def check_gateway(client):
    lat = getattr(client, "latency", float("inf"))
    ready, closed = client.is_ready(), client.is_closed()
    ok = ready and not closed and lat is not None and math.isfinite(lat) and lat < 10
    return _res(ok, f"ready={ready} closed={closed} latency={lat if lat is None or not math.isfinite(lat) else round(lat, 3)}")


async def check_loop(limit=1.0):
    t = time.monotonic()
    await asyncio.sleep(0)
    lag = time.monotonic() - t
    return _res(lag < limit, f"lag={lag:.3f}s")


def check_db_write(db_path):
    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS diag_probe (id INTEGER PRIMARY KEY, ts TEXT)")
        conn.execute("INSERT OR REPLACE INTO diag_probe (id, ts) VALUES (1, ?)", (_now(),))
        conn.commit()
        return _res(conn.execute("SELECT ts FROM diag_probe WHERE id=1").fetchone() is not None, "write ok")
    finally:
        conn.close()


def check_kb_search(db_path, docs_dir, query="Aurora"):
    from db import connect
    from retrieve import search_docs
    conn = connect(db_path)
    try:
        hits = search_docs(conn, query, docs_dir, asker_tier="non-member")
    finally:
        conn.close()
    return _res(len(hits) > 0, f"{len(hits)} hit(s) for {query!r}")


def check_log_writable(log_path):
    with open(log_path, "a"):
        pass
    return _res(os.access(log_path, os.W_OK), str(Path(log_path).name))


# ---- full-only checks ---------------------------------------------------
def check_gemini(llm):
    complete = getattr(llm, "_complete", None)
    if complete is None:
        return _res(False, f"provider {getattr(llm, 'name', '?')} has no model endpoint")
    reply = (complete("Reply with exactly the word PONG and nothing else.", "ping") or "").strip()
    return _res("PONG" in reply.upper(), f"reply={reply[:20]!r}")


class _StubLLM:
    """Expansion and re-rank raise (to exercise fallbacks); answer copies a fact."""
    name = "diag-stub"

    def __init__(self, answer):
        self._answer = answer

    def expand_query(self, q):
        raise RuntimeError("stub")

    def rerank(self, q, listing):
        raise RuntimeError("stub")

    def answer(self, q, block):
        return self._answer


def check_pipeline(db_path, docs_dir, question="Who is Jamie?",
                   answer="Jamie Taylor is an ACME team lead for 2026-27.", tiers=("non-member", "member", "officer")):
    from answerstyle import expand_terms
    from kb_bases import base_for
    from qa import answer_in_new_connection
    from retrieve import expanded_question
    problems = []
    if expanded_question("how heavy is it", _StubLLM("")) != expanded_question("how heavy is it", None):
        problems.append("expansion fallback")
    if "mass" not in expand_terms("how heavy is Aurora"):
        problems.append("synonyms")
    bases = {t: base_for(t) for t in tiers}
    if len(set(bases.values())) != len(tiers):
        problems.append(f"tier routing {bases}")
    for t in tiers:
        r = answer_in_new_connection(
            db_path, question=question, asker_id="0", asker_name="diag", guild_id=None, channel_id="",
            channel_name=None, visible=set(), public_channels=set(), here=set(), docs_dir=docs_dir,
            llm=_StubLLM(answer), asker_tier=t)
        text = r.text or ""
        body, _, foot = text.partition("\n\nBased on: ")
        if r.kind != "answer" or not body.startswith(answer[:20]):
            problems.append(f"{t}: kind={r.kind}")
        elif not foot or "\n" in foot.strip() or "http" not in foot:
            problems.append(f"{t}: footer")
    # grounding: an unsupported answer must not pass
    r = answer_in_new_connection(
        db_path, question=question, asker_id="0", asker_name="diag", guild_id=None, channel_id="",
        channel_name=None, visible=set(), public_channels=set(), here=set(), docs_dir=docs_dir,
        llm=_StubLLM("Zorblax Quinn won the 1987 lunar marathon in Quebec."), asker_tier="officer")
    if r.kind == "answer" and "Zorblax" in (r.text or ""):
        problems.append("grounding")
    return _res(not problems, "; ".join(problems) or f"3 tiers ok, routes {sorted(set(bases.values()))}")


async def check_rest_guild(client, guild_ids):
    gid = next(iter(guild_ids or ()), None) or (client.guilds[0].id if client.guilds else None)
    if gid is None:
        return _res(False, "no guild")
    g = await client.fetch_guild(int(gid))
    return _res(g is not None, "guild fetched")


async def check_commands(client, guild_ids):
    import discord
    gid = next(iter(guild_ids or ()), None)
    cmds = await client.tree.fetch_commands(guild=discord.Object(id=int(gid)) if gid else None)
    names = {c.name for c in cmds}
    return _res("ask" in names, f"{len(names)} commands")


def check_catchup(last_ok, max_age=6 * 3600):
    if not last_ok:
        return _res(False, "never succeeded")
    age = time.time() - last_ok
    return _res(age < max_age, f"last success {int(age)}s ago")


def check_source_links(path):
    data = json.loads(Path(path).read_text())
    return _res(isinstance(data, dict) and len(data) > 0, f"{len(data)} links")


def check_disk(path, min_free=500 * 2**20):
    free = shutil.disk_usage(path).free
    return _res(free > min_free, f"free={free // 2**20}MB")


DISK_WARN_PCT = 80


def disk_warning(path, warn_pct=DISK_WARN_PCT):
    """Disk-usage line for status.json; a warning (not a failure, so it never triggers a restart)."""
    u = shutil.disk_usage(path)
    pct = 100 * u.used / u.total
    return {"pct": round(pct, 1), "free_mb": u.free // 2**20, "warn": pct >= warn_pct}


def check_db_size(db_path, max_bytes=2 * 2**30):
    size = sum(os.path.getsize(p) for p in (db_path, db_path + "-wal") if os.path.exists(p))
    return _res(size < max_bytes, f"size={size // 2**20}MB")


# ---- runner -------------------------------------------------------------
class Diagnostics:
    def __init__(self, client, *, db_path, docs_dir, log_path, guild_ids=(), llm=None,
                 out_dir=DIAG_DIR, exit=os._exit, source_links=ROOT / "config" / "source_links.json"):
        self.client, self.db_path, self.docs_dir, self.log_path = client, db_path, docs_dir, log_path
        self.guild_ids, self.llm, self.out_dir, self._exit = guild_ids, llm, Path(out_dir), exit
        self.source_links = source_links
        self.gateway_fails = 0
        self.last_catchup_ok = None
        self.last_full = None

    async def run(self, level="light"):
        c = {}
        c["gateway"] = await _guard(check_gateway, self.client)
        c["event_loop"] = await _guard(check_loop)
        c["db_write"] = await _guard(asyncio.to_thread, check_db_write, self.db_path)
        c["kb_search"] = await _guard(asyncio.to_thread, check_kb_search, self.db_path, self.docs_dir)
        c["log_writable"] = await _guard(check_log_writable, self.log_path)
        ok = all(v["ok"] for v in c.values())
        try:
            disk = disk_warning(os.path.dirname(os.path.abspath(self.db_path)) or ".")
        except Exception:
            disk = None
        took = " ".join(f"{k}={v.get('secs', 0):.2f}s" for k, v in c.items())
        status = {"timestamp": _now(), "level": level, "ok": ok, "checks": c, "disk": disk}
        if disk and disk["warn"]:
            log.warning("diag disk %.0f%% used (%d MB free) - run DB maintenance or grow the disk", disk["pct"], disk["free_mb"])
        self.write(status)
        failed = [k for k, v in c.items() if not v["ok"]]
        if failed:
            log.error("diag %s FAIL %d/%d: %s (%s)", level, len(failed), len(c),
                      "; ".join(f"{k}: {c[k]['detail']}" for k in failed), took)
        else:
            log.info("diag %s ok %d/%d (%s)", level, len(c), len(c), took)
        self.gateway_fails = 0 if c["gateway"]["ok"] else self.gateway_fails + 1
        if self.gateway_fails >= GATEWAY_DEAD_LIMIT:
            log.error("diag: gateway dead for %d consecutive checks (%s); exiting for supervisor restart",
                      self.gateway_fails, c["gateway"]["detail"])
            for h in logging.getLogger().handlers:
                h.flush()
            self._exit(3)
        return status

    def write(self, status):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        try:  # keep the external harness's section
            prev = json.loads((self.out_dir / "status.json").read_text())
            if "synthetic" in prev:
                status["synthetic"] = prev["synthetic"]
        except Exception:
            pass
        try:
            import counters
            status["events"] = {k: v for k, v in counters.load(self.out_dir / "counters.json").items() if k != "boot_id"}
        except Exception:
            pass
        tmp = self.out_dir / f"status.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(status, indent=1))
        os.replace(tmp, self.out_dir / "status.json")

    async def run_safe(self, level="light", timeout=RUN_TIMEOUT):
        """One pass that can never kill or hang the loop: a timeout or error is a logged failure."""
        t = time.monotonic()
        try:
            return await asyncio.wait_for(self.run(level), timeout)
        except asyncio.TimeoutError:
            why = f"timed out after {timeout}s"
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
        log.error("diag %s FAIL: check pass %s (%.1fs)", level, why, time.monotonic() - t)
        try:
            self.write({"timestamp": _now(), "level": level, "ok": False,
                        "checks": {"run": _res(False, why)}})
        except Exception:
            log.exception("diag: could not write status.json")
        return None

    async def loop(self, first=60, every=LIGHT_EVERY, tick=TICK, wall=time.time):
        """Scheduled off a wall-clock deadline in short ticks. asyncio sleeps use the monotonic clock,
        which stops while the box is suspended, so one long sleep can overrun by the whole freeze."""
        import counters
        due = wall() + first
        while True:
            while (left := due - wall()) > 0:
                w0, m0 = wall(), time.monotonic()
                await asyncio.sleep(min(tick, left))
                gap = (wall() - w0) - (time.monotonic() - m0)
                if gap > PAUSE_GAP:  # wall time moved but the process clocks did not: host froze us
                    try:
                        counters.bump("box_pauses", path=self.out_dir / "counters.json", detail=
                                      f"box paused ~{gap:.0f}s (host suspend, not a bot fault)")
                    except Exception:
                        log.exception("diag: counter write failed")
            await self.run_safe("light")
            due = wall() + every

    def start(self, first=60, every=LIGHT_EVERY):
        """Start the check loop and keep a reference; if it ever ends, log why and restart it."""
        self._task = asyncio.get_running_loop().create_task(self.loop(first, every), name="diag-loop")
        self._task.add_done_callback(lambda t: self._ended(t, every))
        return self._task

    def _ended(self, task, every):
        if task.cancelled():  # only happens at shutdown; not a fault
            return
        else:
            e = task.exception()
            why = f"{type(e).__name__}: {e}" if e else "returned"
        if getattr(self.client, "is_closed", lambda: False)():
            return
        self.restarts = getattr(self, "restarts", 0) + 1
        log.error("diag: check loop ended (%s); restarting it (restart #%d)", why, self.restarts)
        try:
            import counters; counters.bump("check_task_restarts", why, path=self.out_dir / "counters.json", quiet=True)
        except Exception:
            pass
        self.start(first=5, every=every)

    def status_age(self):
        try:
            ts = json.loads((self.out_dir / "status.json").read_text())["timestamp"]
            return time.time() - dt.datetime.fromisoformat(ts).timestamp()
        except Exception:
            return None
