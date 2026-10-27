#!/usr/bin/env python3
"""Hourly synthetic test harness, run OUTSIDE the bot process.

Each run:
  a) one Gemini call writes ~5 realistic ACME command invocations (falls back to a
     built-in batch if the call fails); fixed coverage cases are always added;
  b) every registered slash command and subcommand runs through the bot's real
     handlers with fake interactions against a temp copy of bot.db (never the
     real one). Up to 2 /ask runs use the real Gemini pipeline, the rest a stub.
     Each is graded: no error, reply format (one-line sources footer or exactly
     "I don't know."), tier gating, opt-out persisted/honored/removed;
  c) read-only Discord REST checks (gateway, bot user, guild, registered
     commands match the code) and the bot's status.json (gateway ready, fresh).
Never posts or sends anything to Discord. Budget: <= 7 Gemini calls per run.
Results: one line (+ failure detail) appended to bot.log and status.json["synthetic"].

  python tools_local/synthetic_test.py           # one run
  python tools_local/synthetic_test.py --loop    # every hour
  python tools_local/synthetic_test.py --no-llm  # no Gemini at all (tests)
"""
from __future__ import annotations

import argparse, asyncio, datetime as dt, json, os, random, re, sqlite3, sys, tempfile, time, traceback
import urllib.request
from pathlib import Path
from types import SimpleNamespace as N

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

STATUS = ROOT / "diag" / "status.json"
BOT_LOG = ROOT / "bot.log"
IDK = "I don't know."
MAX_REAL_ASKS = 2
STATUS_MAX_AGE = 1320
RUN_LIMIT = 900  # one harness run
TIER_RANK = {"non-member": 0, "member": 1, "officer": 2}
TIER_ROLE = {"non-member": "Guest", "member": "Member", "officer": "Team Lead"}
FALLBACK_BATCH = [
    {"cmd": "ask", "tier": "non-member", "question": "What is ACME?"},
    {"cmd": "ask", "tier": "member", "question": "How heavy is Aurora?"},
    {"cmd": "ask", "tier": "officer", "question": "Who are the current team leads?"},
    {"cmd": "ask", "tier": "member", "question": "When are general meetings?"},
    {"cmd": "ask", "tier": "non-member", "question": "Who is Jamie?"},
]


# ---------------------------------------------------------------- fakes
class Recorder:
    """Stands in for CommandTree: collects the commands the code registers."""

    def __init__(self):
        self.cmds = {}

    def command(self, name, description, **kw):
        from discord import app_commands

        def deco(fn):
            cmd = app_commands.Command(name=name, description=description, callback=fn)
            self.cmds[name] = cmd
            return cmd
        return deco

    def add_command(self, cmd):
        self.cmds[cmd.name] = cmd

    def leaves(self):
        """(path, command) for every invocable command or subcommand."""
        from discord import app_commands
        out = []

        def walk(cmd, prefix):
            path = f"{prefix} {cmd.name}".strip()
            if isinstance(cmd, app_commands.Group):
                for sub in cmd.commands:
                    walk(sub, path)
            else:
                out.append((path, cmd))
        for c in self.cmds.values():
            walk(c, "")
        return out


class Response:
    def __init__(self):
        self.done, self.sent = False, []

    def is_done(self):
        return self.done

    async def send_message(self, body, **kw):
        self.done = True
        self.sent.append(str(body))

    async def defer(self, **kw):
        self.done = True


class Followup:
    def __init__(self, sink):
        self.sink = sink

    async def send(self, body=None, **kw):
        body = kw.get("content") if body is None else body
        if kw.get("view") is not None:  # private rating prompt: kept apart from the visible answer
            self.prompts = getattr(self, "prompts", []) + [(str(body), kw.get("view"), kw.get("ephemeral"))]
            return N(id=random.randrange(10**17, 10**18))
        self.sink.append(str(body))
        return N(id=random.randrange(10**17, 10**18))


class Perms:
    def __init__(self, view=True, manage=False):
        self.view_channel = view
        self.read_message_history = view
        self.manage_guild = manage
        self.administrator = manage


class FakeChannel:
    def __init__(self, cid, tier, guild):
        self.id, self.tier, self.guild = int(cid), tier, guild
        self.name = f"chan-{str(cid)[-4:]}"  # never use real channel names
        self.category = None
        self.parent = None
        self.type = 0
        self._history = []

    def permissions_for(self, who):
        t = getattr(who, "tier", "officer")
        return Perms(view=TIER_RANK[t] >= TIER_RANK[self.tier])

    async def archived_threads(self, limit=None):
        return
        yield

    async def history(self, after=None, limit=None, oldest_first=True):
        for m in self._history:
            yield m


class FakeRole:
    def __init__(self, name):
        self.name, self.id = name, abs(hash(name)) % 10**17


class FakeUser:
    def __init__(self, uid, tier, admin=False):
        self.id, self.tier = int(uid), tier
        self.name = self.display_name = f"synthetic-{str(uid)[-4:]}"
        self.roles = [FakeRole(TIER_ROLE[tier])]
        self.bot = False
        self._admin = admin


class FakeGuild:
    def __init__(self, gid, channels):
        self.id = int(gid)
        self.channels, self.threads = [], []
        self.me = FakeUser(1, "officer")
        for cid, tier in channels:
            self.channels.append(FakeChannel(cid, tier, self))
        self.text_channels = list(self.channels)


class FakeInteraction:
    def __init__(self, user, guild, channel, admin=False):
        self.user, self.guild, self.channel = user, guild, channel
        self.permissions = Perms(manage=admin)
        self.response = Response()
        self.out = self.response.sent
        self.followup = Followup(self.out)
        self.guild_id = guild.id if guild else None
        self.channel_id = channel.id if channel else None


class FakeMessage:
    def __init__(self, mid, channel, author, content):
        self.id, self.channel, self.author, self.guild = mid, channel, author, channel.guild
        self.content = content
        self.created_at = dt.datetime.now(dt.timezone.utc)
        self.edited_at = None
        self.attachments = []


class StubLLM:
    name = "synthetic-stub"

    def expand_query(self, q):
        raise RuntimeError("stub: exercise the expansion fallback")

    def rerank(self, q, listing):
        raise RuntimeError("stub: exercise the re-rank fallback")

    def answer(self, question, block):
        # Restate the first entry's fact so grounding can pass; else refuse.
        for line in (block or "").splitlines():
            m = re.match(r"\s*\[\d+\][^:]*:\s*(.+?)(\s*\(as of.*)?$", line)
            if m and len(m.group(1)) > 10:
                return m.group(1).strip()
        return IDK


class CountingLLM:
    """Wraps the real provider and counts model calls."""

    def __init__(self, inner):
        self.inner, self.calls, self.name = inner, 0, getattr(inner, "name", "real")
        orig = inner._complete

        def counted(system, user, *a, **k):
            self.calls += 1
            return orig(system, user, *a, **k)
        inner._complete = counted

    def __getattr__(self, k):
        return getattr(self.inner, k)


# ---------------------------------------------------------------- harness
def make_bot(db_path, llm, guild):
    """A QABot whose handlers are real, but whose tree is a Recorder, whose DB is the
    temp copy, and which cannot reach Discord (no channel lookups, no presence)."""
    import bot as botmod
    from dataclasses import replace
    import threading

    class SyntheticBot(botmod.QABot):
        def __init__(self):  # deliberately skip discord.Client.__init__: no gateway, no HTTP
            from db import connect
            self.cfg = replace(botmod.load_config(), db_path=db_path)
            self.started_at = dt.datetime.now(dt.timezone.utc)
            self.tree = Recorder()
            self.db = connect(db_path)
            self._block_fp = None
            self._db_lock = threading.Lock()
            self._catchup_lock = asyncio.Lock()
            self._catchup_task = None
            self.llm = llm
            self.presence = []
            self._synthetic_guilds = [guild]
            self._register_commands()

        @property
        def guilds(self):
            return self._synthetic_guilds

        @property
        def user(self):
            return N(id=1, name="synthetic-bot")

        def get_channel(self, *_):
            return None

        async def fetch_channel(self, *_):
            raise RuntimeError("synthetic harness never fetches or posts to channels")

        async def change_presence(self, **kw):
            self.presence.append(kw)

        def _watching(self, gid):
            return True

    return SyntheticBot()


async def invoke(cmd, inter, **args):
    if getattr(cmd, "binding", None) is not None:
        await cmd.callback(cmd.binding, inter, **args)
    else:
        await cmd.callback(inter, **args)
    return "\n".join(inter.out)


def reply_format_ok(text):
    """Exactly "I don't know.", or a body plus one "Based on:" line of links."""
    text = (text or "").strip()
    tag = "\n-# answered without AI (backup mode)"
    if text.endswith(tag):  # backup-mode subtext line is allowed after the footer
        text = text[: -len(tag)].rstrip()
    if text == IDK:
        return True, ""
    body, sep, foot = text.partition("\n\nBased on: ")
    if not body.strip():
        return False, "empty body"
    if sep and ("\n" in foot.strip() or "http" not in foot):
        return False, "sources footer is not one line of links"
    if not sep and "Based on" in text:
        return False, "malformed sources footer"
    return True, ""


def gemini_batch(llm, n=5):
    prompt = (
        f"Write {n} realistic Discord slash command invocations a member of ACME (a university rocketry "
        "club: rockets Aurora, Comet and Skylark, team leads, general meetings, testing) might "
        "send to the club's Q&A bot. Mostly /ask questions. Reply with ONLY a JSON list of objects "
        'like {"cmd":"ask","tier":"member","question":"..."}; tier is one of non-member, member, officer.'
    )
    raw = llm._complete("You write test data. Output JSON only.", prompt)
    m = re.search(r"\[.*\]", raw or "", re.S)
    items = json.loads(m.group(0)) if m else []
    out = []
    for it in items[:n]:
        q = str(it.get("question", "")).strip()[:300]
        t = it.get("tier") if it.get("tier") in TIER_RANK else random.choice(list(TIER_RANK))
        if q:
            out.append({"cmd": "ask", "tier": t, "question": q})
    return out


def real_ids(conn):
    ids = set()
    for sql in ("SELECT DISTINCT author_id FROM messages", "SELECT DISTINCT user_id FROM member_snapshots"):
        try:
            ids.update(str(r[0]) for r in conn.execute(sql))
        except sqlite3.Error:
            pass
    return ids


def fake_user_id(taken):
    while True:
        uid = str(random.randint(10**17, 10**18 - 1))
        if uid not in taken:
            return uid


def _channel_tiers():
    p = ROOT / "data" / "channel_tiers.json"
    if p.exists():
        return json.loads(p.read_text())
    # Fictional fallback so the harness runs on a fresh checkout.
    return {"1001": {"name": "general", "tier": "non-member"},
            "1002": {"name": "projects", "tier": "member"},
            "1003": {"name": "leads", "tier": "officer"}}


def pick_channels(guild_id):
    from catchup import ALWAYS_SKIP
    from permissions import is_excluded
    from voice import get_runtime
    data = _channel_tiers()
    bl = get_runtime().blocklist
    out = []
    for cid, info in data.items():
        if info.get("name") in ALWAYS_SKIP or is_excluded(cid, info.get("name"), bl, str(guild_id)):
            continue
        if info.get("tier") in TIER_RANK:
            out.append((cid, info["tier"]))
    return out


class Grader:
    def __init__(self):
        self.results = []

    def add(self, name, ok, detail=""):
        self.results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

    async def run(self, name, coro_fn):
        try:
            r = await coro_fn()
            if isinstance(r, tuple):
                self.add(name, *r)
            else:
                self.add(name, True)
        except Exception as e:
            self.add(name, False, f"{type(e).__name__}: {e} @ {traceback.extract_tb(e.__traceback__)[-1].lineno}")


async def functional(db_path, real_llm, batch, grader, guild_id):
    import bot as botmod
    from catchup import run_catchup
    from db import connect, is_opted_out

    channels = pick_channels(guild_id)
    guild = FakeGuild(guild_id, random.sample(channels, min(len(channels), 40)))
    by_tier = {t: [c for c in guild.channels if c.tier == t] for t in TIER_RANK}
    stub = StubLLM()
    b = make_bot(db_path, stub, guild)
    leaves = dict(b.tree.leaves())
    used = set()
    taken = real_ids(b.db)

    def chan(max_tier="officer"):
        pool = [c for t, cs in by_tier.items() if TIER_RANK[t] <= TIER_RANK[max_tier] for c in cs]
        return random.choice(pool or guild.channels)

    def admin_user():
        return FakeUser(fake_user_id(taken), "officer", admin=True)

    # capture every /ask Result for gating
    captured = []
    orig_answer = botmod.answer_in_new_connection

    def capture(*a, **k):
        r = orig_answer(*a, **k)
        captured.append((k.get("asker_tier"), r))
        return r
    botmod.answer_in_new_connection = capture
    ct = _channel_tiers()

    def gating(tier, result):
        from tiers import base_of
        for h in getattr(result, "snippets", None) or []:
            if getattr(h, "kind", "") == "doc":
                base = base_of(getattr(h, "path", "") or "") or "public"
                need = {"public": "non-member", "member": "member", "officer": "officer"}.get(base, "officer")
            else:
                need = (ct.get(str(getattr(h, "channel_id", "")), {}) or {}).get("tier", "officer")
            if TIER_RANK[need] > TIER_RANK[tier]:
                return False, f"{tier} saw a {need} entry"
        return True, ""

    # ---- /ask (generated batch; first MAX_REAL_ASKS use the real pipeline)
    real_left = MAX_REAL_ASKS if real_llm is not None else 0
    for i, item in enumerate(batch):
        tier, q = item["tier"], item["question"]
        use_real = real_left > 0
        real_left -= use_real
        label = f"ask[{tier}{' real' if use_real else ''}] {q[:60]}"

        async def one(tier=tier, q=q, use_real=use_real):
            b.llm = real_llm if use_real else stub
            u = FakeUser(fake_user_id(taken), tier)
            inter = FakeInteraction(u, guild, chan(tier))
            captured.clear()
            text = await invoke(leaves["ask"], inter, question=q)
            used.add("ask")
            if "Something went wrong" in text:
                return False, "handler error: " + text
            ok, why = reply_format_ok(text)
            if not ok:
                return False, why
            if captured:
                return gating(tier, captured[-1][1])
            return True, ""
        await grader.run(label, one)
    b.llm = stub

    # ---- tier gating: an officer-only fact must not reach lower tiers
    async def tier_map():
        from tiers import member_tier
        got = {t: member_tier(FakeUser(1, t)) for t in TIER_RANK}
        from tiers import norm_tier
        return got == {t: norm_tier(t) for t in TIER_RANK}, json.dumps(got)
    await grader.run("tier mapping", tier_map)

    # ---- opt-out lifecycle (admin) with a fake user id
    target = FakeUser(fake_user_id(taken), "member")
    ch = chan("member")
    st = {}

    async def optout_add():
        inter = FakeInteraction(admin_user(), guild, ch, admin=True)
        t0 = time.monotonic()
        text = await invoke(leaves["admin optout add"], inter, member=target)
        used.add("admin optout add")
        secs = time.monotonic() - t0
        conn = connect(db_path)
        try:
            saved = is_opted_out(conn, target.id)
        finally:
            conn.close()
        if not saved:
            return False, f"not persisted; reply={text[:120]!r}"
        if not inter.response.done:
            return False, "no reply"
        return secs < 10, f"{secs:.2f}s"
    await grader.run("admin optout add (persisted)", optout_add)

    async def honored():
        # live ingestion
        m1 = FakeMessage(random.randint(10**18, 2 * 10**18), ch, target, "synthetic opted-out message")
        await asyncio.to_thread(b._record_message, botmod.msg_row(m1))
        # catch-up (dry against the temp DB, fake channel history)
        conn = connect(db_path)
        try:
            newest = conn.execute("SELECT MAX(CAST(id AS INTEGER)) FROM messages").fetchone()[0] or 1
        finally:
            conn.close()
        m2 = FakeMessage(int(newest) + 7, ch, target, "synthetic catch-up message")
        ch._history = [m2]
        stats = await run_catchup(N(guilds=[guild]), db_path, botmod.msg_row, __import__("voice").get_runtime().blocklist)
        ch._history = []
        conn = connect(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM messages WHERE author_id=?", (str(target.id),)).fetchone()[0]
        finally:
            conn.close()
        return n == 0, f"{n} stored for opted-out user; catch-up fetched {stats['fetched']}"
    await grader.run("opt-out honored (ingest + catch-up)", honored)

    async def status_and_list():
        inter = FakeInteraction(admin_user(), guild, ch, admin=True)
        text = await invoke(leaves["admin optout status"], inter, member=target)
        used.add("admin optout status")
        inter2 = FakeInteraction(admin_user(), guild, ch, admin=True)
        listed = await invoke(leaves["admin optout list"], inter2)
        used.add("admin optout list")
        return ("is opted out" in text and str(target.id) in listed), text[:120]
    await grader.run("admin optout status + list", status_and_list)

    async def optout_remove():
        inter = FakeInteraction(admin_user(), guild, ch, admin=True)
        text = await invoke(leaves["admin optout remove"], inter, member=target)
        used.add("admin optout remove")
        inter2 = FakeInteraction(admin_user(), guild, ch, admin=True)
        st2 = await invoke(leaves["admin optout status"], inter2, member=target)
        m = FakeMessage(random.randint(2 * 10**18, 3 * 10**18), ch, target, "synthetic after opt-in")
        await asyncio.to_thread(b._record_message, botmod.msg_row(m))
        conn = connect(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM messages WHERE author_id=?", (str(target.id),)).fetchone()[0]
        finally:
            conn.close()
        return ("Removed" in text and "not opted out" in st2 and n == 1), f"{text[:80]!r} / stored after={n}"
    await grader.run("admin optout remove (removed, ingest resumes)", optout_remove)

    async def self_optout():
        u = FakeUser(fake_user_id(taken), "member")
        inter = FakeInteraction(u, guild, ch)
        text = await invoke(leaves["optout"], inter)
        used.add("optout")
        conn = connect(db_path)
        try:
            saved = is_opted_out(conn, u.id)
        finally:
            conn.close()
        return saved or "turned off" in text, text[:100]
    await grader.run("/optout (self)", self_optout)

    # ---- the rest of the commands
    async def simple(path, admin=True, expect=None, **args):
        inter = FakeInteraction(admin_user() if admin else FakeUser(fake_user_id(taken), "member"), guild, ch, admin=admin)
        text = await invoke(leaves[path], inter, **args)
        used.add(path)
        if not text:
            return False, "no reply"
        if expect and not re.search(expect, text, re.I):
            return False, f"unexpected reply {text[:120]!r}"
        return True, ""

    nobody = FakeUser(fake_user_id(taken), "member")
    cases = [
        ("help", dict(admin=False, expect=r"/ask")),
        ("admin bot status", dict(expect=r"up|uptime|model")),
        ("admin approvals list", dict(expect=r".")),
        ("admin approvals approve", dict(expect=r"No request", request_id=999999999)),
        ("admin approvals deny", dict(expect=r"No request", request_id=999999999, reason="synthetic")),
        ("admin blocklist list", dict(expect=r"blocklist")),
        # state-changing settings commands: permission path only (non-admin), nothing written
        ("admin blocklist add", dict(admin=False, expect=r"Administrator", channel=None, name="synthetic")),
        ("admin blocklist remove", dict(admin=False, expect=r"Administrator", channel=None, name="synthetic")),
        ("admin bot set-status", dict(admin=False, expect=r"Administrator", text="synthetic")),
        ("admin bot reload", dict(admin=False, expect=r"Administrator")),
        ("admin member forget", dict(expect=r"confirm", member=nobody)),
        ("admin roles show", dict(expect=r"Administrators")),
        # role writers: permission path only (non-admin), the role store is never touched
        ("admin roles set", dict(admin=False, expect=r"Administrator", tier=N(value="officer"), role=FakeRole("synthetic"))),
        ("admin roles remove", dict(admin=False, expect=r"Administrator", role=FakeRole("synthetic"))),
    ]
    for path, kw in cases:
        if path in leaves:
            await grader.run(f"/{path}", lambda path=path, kw=kw: simple(path, **kw))

    # settings writers' core logic, on temp copies of the settings file
    async def settings_logic():
        from slashadmin import mutate_blocklist, write_status_text
        from voice import get_runtime
        src = Path(get_runtime().local_settings_path)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "settings.local.toml"
            if src.exists():
                p.write_text(src.read_text())
            mutate_blocklist(p, action="add", channel_id="1", name=None, guild_id=str(guild_id))
            mutate_blocklist(p, action="remove", channel_id="1", name=None, guild_id=str(guild_id))
            write_status_text(p, "synthetic")
        return True, ""
    await grader.run("blocklist/set-status logic (temp file)", settings_logic)

    # ---- officer verification: private prompt for officer askers only, context menu, votes, badge rule
    async def officer_verify():
        import re
        import feedback
        from db import connect as dbc
        b.llm = stub
        prompts, sent_ids = {}, {}
        for tier in ("officer", "member"):
            inter = FakeInteraction(FakeUser(fake_user_id(taken), tier), guild, chan(tier))
            text = await invoke(leaves["ask"], inter, question="When are general meetings?")
            prompts[tier] = getattr(inter.followup, "prompts", [])
            if feedback.PROMPT in text:
                return False, "rating prompt leaked into the public answer"
        if prompts["member"]:
            return False, "member asker got a rating prompt"
        if len(prompts["officer"]) != 1 or prompts["officer"][0][2] is not True:
            return False, "officer asker missing an ephemeral rating prompt"
        view = prompts["officer"][0][1]
        rid = int(re.fullmatch(feedback.CUSTOM_ID_RE, view.children[0].custom_id)["ask_id"])
        conn = dbc(db_path)
        try:
            linked = conn.execute("SELECT message_id FROM ask_message WHERE ask_id=?", (rid,)).fetchone()
            if not linked:
                return False, "answer message not linked to ask_log"
            if feedback.record_vote(conn, rid, fake_user_id(taken), "member", "yes") != "not_officer":
                return False, "member vote accepted"
            oid = fake_user_id(taken)
            ok = (feedback.record_vote(conn, rid, oid, "officer", "yes") == "saved"
                  and feedback.record_vote(conn, rid, oid, "officer", "no", "synthetic") == "changed"
                  and feedback.tally(conn, rid)["votes"] == 1
                  and feedback.is_verified({"yes": 2, "no": 0}, 2, 0)
                  and not feedback.is_verified({"yes": 3, "no": 1}, 2, 0))
            msg_id = linked[0]
        finally:
            conn.close()
        if not ok:
            return False, "vote/tally/threshold mismatch"
        # context menu: officer on a bot answer gets the private prompt; member and foreign messages are refused
        cm = leaves["Verify answer"]
        msg = N(id=int(msg_id), author=N(id=b.user.id), content="x")
        oi = FakeInteraction(FakeUser(fake_user_id(taken), "officer"), guild, chan())
        await cm.callback(oi, msg)
        mi = FakeInteraction(FakeUser(fake_user_id(taken), "member"), guild, chan("member"))
        await cm.callback(mi, msg)
        fi = FakeInteraction(FakeUser(fake_user_id(taken), "officer"), guild, chan())
        await cm.callback(fi, N(id=5, author=N(id=999), content="x"))
        used.add("Verify answer")
        if feedback.PROMPT not in "".join(oi.out):
            return False, "context menu did not prompt the officer"
        if mi.out != [feedback.NOT_OFFICER]:
            return False, "context menu let a member in"
        if "only works on my answers" not in "".join(fi.out):
            return False, "context menu accepted a non-bot message"
        return True, ""
    await grader.run("officer verification (prompt, context menu, votes)", officer_verify)

    missing = [p for p in leaves if p not in used]
    grader.add("every command exercised", not missing, ",".join(missing))
    botmod.answer_in_new_connection = orig_answer
    b.db.close()
    return sorted(leaves)


def rest(path, token):
    req = urllib.request.Request("https://discord.com/api/v10" + path, method="GET",
                                 headers={"Authorization": "Bot " + token, "User-Agent": "DiscordBot (bot-synthetic, 1)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def connectivity(grader, code_paths, guild_id):
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    def g(name, fn):
        try:
            r = fn()
            grader.add(name, *(r if isinstance(r, tuple) else (True, "")))
        except Exception as e:
            grader.add(name, False, f"{type(e).__name__}: {e}")
    g("REST gateway/bot", lambda: (bool(rest("/gateway/bot", token).get("url")), ""))
    me = {}
    g("REST bot user", lambda: (bool(me.update(rest("/users/@me", token)) or me.get("id")), ""))
    g("REST guild", lambda: (str(rest(f"/guilds/{guild_id}", token).get("id")) == str(guild_id), ""))

    def cmds():
        app = rest("/oauth2/applications/@me", token)["id"]
        remote = set()
        def walk(c, prefix):
            path = f"{prefix} {c['name']}".strip()
            subs = [o for o in c.get("options", []) or [] if o.get("type") in (1, 2)]
            if not subs:
                remote.add(path)
            for o in subs:
                walk(o, path)
        for c in rest(f"/applications/{app}/guilds/{guild_id}/commands", token):
            walk(c, "")
        code = set(code_paths)
        if remote != code:
            return False, f"only in code: {sorted(code - remote)}; only in Discord: {sorted(remote - code)}"
        return True, f"{len(code)} match"
    g("registered commands match code", cmds)

    def status():
        s = json.loads(STATUS.read_text())
        age = time.time() - dt.datetime.fromisoformat(s["timestamp"]).timestamp()
        gw = s.get("checks", {}).get("gateway", {})
        return (age < STATUS_MAX_AGE and gw.get("ok")), f"age={int(age)}s gateway={gw.get('detail')}"
    g("bot status.json fresh + gateway ready", status)


def write_results(summary):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads(STATUS.read_text())
    except Exception:
        cur = {}
    cur["synthetic"] = summary
    tmp = STATUS.with_name(f"status.json.syn{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cur, indent=1))
    os.replace(tmp, STATUS)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    fails = [r for r in summary["results"] if not r["ok"]]
    line = (f"{stamp} INFO synthetic ok {summary['passed']}/{summary['total']} gemini_calls={summary['gemini_calls']}"
            if not fails else
            f"{stamp} ERROR synthetic FAIL {summary['passed']}/{summary['total']} gemini_calls={summary['gemini_calls']}: "
            + "; ".join(f"{r['name']}: {r['detail']}" for r in fails))
    with open(BOT_LOG, "a") as f:
        f.write(line[:4000] + "\n")
    return line


def guild_id_from_config():
    from config import configured_guild_ids
    ids = [g for g in configured_guild_ids() if str(g).isdigit()]
    return int(ids[0]) if ids else None


def run_once(use_llm=True, connectivity_checks=True, db_src=None):
    grader = Grader()
    llm = None
    if use_llm:
        from llm import provider_from_env
        p = provider_from_env()
        llm = CountingLLM(p) if hasattr(p, "_complete") else None
    batch = []
    if llm is not None:
        try:
            batch = gemini_batch(llm)
        except Exception as e:
            grader.add("gemini batch generation", False, f"{type(e).__name__}: {e}")
    gen = bool(batch)
    batch = batch or [dict(x) for x in random.sample(FALLBACK_BATCH, 5)]
    if llm is not None:
        grader.add("gemini batch generation", gen, f"{len(batch)} cases")
    gid = guild_id_from_config()
    with tempfile.TemporaryDirectory(prefix="bot-synth-") as d:
        tmp_db = os.path.join(d, "bot.db")
        src = sqlite3.connect(db_src or str(ROOT / "bot.db"))
        dst = sqlite3.connect(tmp_db)
        src.backup(dst)
        src.close(); dst.close()
        paths = asyncio.run(functional(tmp_db, llm, batch, grader, gid))
    if connectivity_checks:
        connectivity(grader, paths, gid)
    res = grader.results
    summary = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ok": all(r["ok"] for r in res),
        "passed": sum(r["ok"] for r in res),
        "total": len(res),
        "gemini_calls": llm.calls if llm else 0,
        "results": res,
    }
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run every hour")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--every", type=int, default=3600)
    a = ap.parse_args(argv)
    import logging
    logging.disable(logging.CRITICAL)  # handler noise stays out; results go to bot.log below
    import signal

    def _too_long(*_):
        raise TimeoutError(f"harness run exceeded {RUN_LIMIT}s")
    signal.signal(signal.SIGALRM, _too_long)
    while True:
        try:
            signal.alarm(RUN_LIMIT)  # a hung run is reported as a failure instead of silently skipping hours
            try:
                s = run_once(use_llm=not a.no_llm)
            finally:
                signal.alarm(0)
            print(write_results(s), flush=True)
        except Exception as e:
            s = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "ok": False,
                 "passed": 0, "total": 1, "gemini_calls": 0,
                 "results": [{"name": "harness", "ok": False, "detail": f"{type(e).__name__}: {e}"}]}
            print(write_results(s), flush=True)
        if not a.loop:
            return 0 if s["ok"] else 1
        due = time.time() + a.every  # wall-clock deadline: time.sleep stalls while the box is suspended
        while time.time() < due:
            time.sleep(min(30, max(0.0, due - time.time())))


if __name__ == "__main__":
    sys.exit(main())
