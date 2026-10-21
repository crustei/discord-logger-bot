# Discord logger and Q&A bot

A read-only Discord logger and Q&A bot for any server. It records messages into SQLite, and it answers only when someone uses `/ask` or @mentions it. It never posts on its own. `/ask` acknowledges the command before it reloads settings or purges the blocklist. Message inserts, that purge, member snapshots, and `/optout` deletion run off the Discord event loop.

Answers are one to three plain sentences, paraphrased from the knowledge files and from channels that person can see. Channels on the blocklist are never logged and never quoted. The committed blocklist is empty: a channel is excluded only when you add its name or id. A question about someone's private information waits for an officer. General questions about dates, events, how-tos, and team structure are answered directly.

The display title, the org name, and the voice are config, not code. Shared defaults live in `config/settings.toml` and `config/persona.md`. Values for one deployment go in `config/settings.local.toml`, which is gitignored. Copy `config/settings.local.example.toml` to start that file.

Terms and privacy templates are [TERMS.md](TERMS.md) and [PRIVACY.md](PRIVACY.md). They are plain-language starting points, not legal advice. Replace `{org_name}`, `{operator_contact}`, and `{effective_date}` before you publish them. The bot does not fill those placeholders in. Discord's App Directory asks for a Terms of Service URL and a Privacy Policy URL. Committing these files does not publish them. Host each page at its own public URL, then paste those URLs into the Developer Portal. Put the same URLs in `terms_url` and `privacy_url` so `/help` can link to them.

## Architecture

```
Discord messages
    -> bot.py (skips the bot's own posts and blocklisted channels)
    -> SQLite messages + FTS5

/ask or @mention
    -> knowledge files in DOCS_DIR, then messages the asker can see
    -> minus the blocklist and opted-out members
    -> BM25 plus a few extra search words, facts.md and other docs ranked above chat
    -> at most max_context_snippets (default 12, 6000 characters)
    -> public knowledge and general questions: a short paraphrased answer
    -> a question about someone's private information: hold for approval
    -> channels the asker cannot see: dropped

admin.py approve
    -> re-check the stored snippets
    -> call the model with that one context block
    -> post the answer
```

The model writes a short reply from the context block `retrieve.py` already built. It does not search the database. A configured model can also suggest a few extra search words. With no model, the bot still paraphrases locally and redacts contact-detail patterns.

General questions use knowledge files ahead of loose messages, including the asker's own messages. A question about what a specific person said, or a topic on `sensitive_topics`, waits for an officer.

| Piece | Role |
| --- | --- |
| `bot.py` | Logger, `/ask`, mentions, `/admin`, member snapshots |
| `db.py` | SQLite, FTS5, approvals, audit log, purge of blocklisted channels |
| `retrieve.py` | BM25 search, knowledge files, and the context block |
| `permissions.py` | Visibility, allowlist, blocklist, auto-post rule |
| `qa.py` | Answer, hold, or refusal |
| `voice.py` | Loads `config/persona.md`, `config/settings.toml`, and `config/settings.local.toml` |
| `llm.py` | OpenAI-compatible chat completions, or a local quote fallback |
| `admin.py` | Officer CLI: list, show, approve, deny, the blocklist, opt-out, and `forget` |
| `demo.py` / `eval.py` | Offline asker and the 10 canned checks |
| `backfill.py` | DiscordChatExporter JSON import |
| `digest.py` | Keyword digest of the last N hours |

SQLite tables: `messages`, `message_edits`, `member_snapshots`, `member_events`, `role_snapshots`, `meta`, `approvals`, `opt_outs`, `admin_audit`, plus FTS tables `messages_fts` and `docs_fts`.

## Voice and settings

Non-coders change how the bot talks, what it is called, and which channels it ignores by editing two files. No Python change is required.

`config/settings.toml`:

| Key | Default when omitted | Meaning |
| --- | --- | --- |
| `display_title` | `Discord bot` | Digest title and the startup log line |
| `org_name` | `this server` | Slash-command text, and the `{org_name}` placeholder |
| `max_answer_chars` | 600 | Cap on every posted reply (80–2000) |
| `max_context_snippets` | 12 | How many snippets the model sees (1–12) |
| `excluded_channels` | empty | Global blocklist. Names or channel ids. Empty excludes nothing |
| `allowed_channels` | empty | Where `/ask` and mentions answer. Empty means every channel not on the blocklist |
| `public_channels` | empty | Treated as visible to every member, still minus the blocklist |
| `optout_command` | true | Whether members can run `/optout` on themselves |
| `status_text` | `Always listening... \| /help` | Custom status under the bot's name, up to 128 characters |
| `terms_url` | empty | Public Terms page linked from `/help` |
| `privacy_url` | empty | Public Privacy page linked from `/help` |
| `admin_roles` | empty | Role names or role ids required for `/admin`, in addition to Manage Server. Empty means Manage Server is enough |
| `tiers` | `non-member`, `member`, `officer`, `executive` | Access levels, lowest first. A person's tier is the highest tier among their roles |
| `tier_roles` | empty | Role names or role ids for each tier. Set this server's roles in `settings.local.toml` |
| `channel_tiers` | empty | Channel name or id to a tier. A channel with no entry takes the lowest tier among roles that can view it |
| `cite_sources` | true | Append `Based on: <title or #channel>, <date>`. No links |
| `officer_role` | `an officer` | Who to ask when the bot does not know |
| `officer_channel` | empty | Channel name in that sentence, without a link |
| `sensitive_topics` | grades, contact details, and similar | Questions that mention one of these wait for an officer |

Per-server blocklist entries use a guild id:

```toml
[blocklist.guilds]
"123456789012345678" = ["officers", "998877665544332211"]
```

A name in `excluded_channels` matches every server. A name under `[blocklist.guilds]` matches only that server. A numeric channel id matches wherever it is listed, because Discord ids are unique. Put an id next to a name when a rename should not bypass the filter.

`config/persona.md` is the voice. Headings:

- **System prompt** — tone, length, citations. `{max_answer_chars}`, `{org_name}`, and `{display_title}` are filled from settings.
- **Refusal** — nothing matched in the channels the asker can see.
- **Held for approval** — posted while a request waits. `{request_id}` becomes the approval id.
- **Held for approval preview** — same idea for a dry run, before an id exists.
- **Empty question**, **Excluded channel**, **Denied**, **No model** — the other fixed replies.

`config/settings.toml` ships with `display_title` `Discord bot`, `org_name` `My Org`, and an empty blocklist. That empty list means the bot logs every channel it can read. Put this server's title, org name, and blocklist in `config/settings.local.toml` instead of editing the committed file. Keys in the local file replace the same keys in `settings.toml`. `[blocklist.guilds]` entries are merged by guild id, so a guild listed only in one of the files is kept.

```bash
cp config/settings.local.example.toml config/settings.local.toml
```

The example lists `private-channel` so you can see the shape. Change it to the channels this server should skip.

The bot re-reads the persona, `settings.toml`, and `settings.local.toml` when their contents change, before the next question. `/admin bot reload` reads them immediately and replies with the cap, the snippet limit, and the channel lists. That command is limited to admins. It works even when `allowed_channels` would block `/ask`, including in a blocklisted channel, and only the admin sees the reply. Slash-command wording uses `org_name` from startup, so a title or org-name change is picked up the next time the process starts. In the offline demo, `:reload` does the same read.

Officers can edit the blocklist without opening the file. `add` and `remove` write `config/settings.local.toml`. `list` prints the effective list (committed file plus the local file) unless you pass `--settings`.

```bash
.venv/bin/python admin.py blocklist list
.venv/bin/python admin.py blocklist add private-channel
.venv/bin/python admin.py blocklist add --guild 123456789012345678 officers
.venv/bin/python admin.py blocklist add --guild 123456789012345678 998877665544332211
.venv/bin/python admin.py blocklist remove private-channel
.venv/bin/python admin.py blocklist remove --guild 123456789012345678 officers
```

`add` deletes matching rows already in the database. `remove` stops excluding that channel. It does not restore rows that were purged. A running bot purges again when it next sees the file change (the next message, `/ask`, or `/admin bot reload`). Because the local file replaces `excluded_channels`, keep the full global list there. The committed list stays empty.

The same blocklist edits are available in Discord as `/admin blocklist add`, `remove`, and `list`. See [Admin commands in Discord](#admin-commands-in-discord).

## Knowledge directory

`DOCS_DIR` (default `./docs`) is operator-maintained markdown or text. Export notes from a shared drive or write them by hand. The bot indexes `.md`, `.markdown`, and `.txt`, and skips `README.md`. Bullet lists and `facts.md` are indexed one line per fact, so a later line is not cut out of a long paragraph. For a general question these files are searched together with message history. Exact words in the line or the title rank above a loose message. A question that uses part of a fact-line name, such as "When is the Compton fair?" for a line that starts `Compton Air and Space Fair:`, matches that line ahead of a note that only says one of the words. `facts.md` still ranks first when the match is otherwise equal. A "What is X" or "Who is X" question puts a chunk whose title is X, or whose line starts with `X:`, ahead of a note that only mentions X. Snippet windows stay on word boundaries, so the context never starts or ends in the middle of a word.

A file may start with a short front matter block:

```markdown
---
title: Recruitment
source: Officer notes
modified: 2026-10-21
tier: non-member
---

Recruitment applications open on 1 November 2026. As of 2026-10-21.
```

`title` is the name shown in `Based on:`. `source` is a note for the operator. `modified` is a `YYYY-MM-DD` date. If it is missing, the newest `As of YYYY-MM-DD` line in the file is used. `tier` is one of the names in `tiers`. It limits who can hear that file. With no `tier` line, the file is the lowest tier. An older `visibility: public` line means that same lowest tier. `visibility: approval` is read as `officer` when that tier exists, and otherwise as the highest tier.

`/ask` uses a file only when its tier is at or below the asker's tier, and a message only when that is true for its channel and the asker can view the channel. The citation names the title and the date. It does not include a path or a link.

A model reply keeps the words it wrote. If it copies a long stretch of a snippet, the bot asks the model once to rephrase, and uses the don't-know line when that second reply still copies. Words are not cut out of the model text to hide a quote. With no model, matching notes are paraphrased locally, and a raw snippet is not posted when nothing relevant matched. Source labels such as a title stuck to the front of a line are removed from the answer body. Emails, phone numbers, street addresses, and similar patterns are replaced with `[redacted]`. If nothing relevant matches, or the model request fails, the reply says it does not know and names `officer_role` and, when set, `#officer_channel`.

## Model

The provider is OpenAI-compatible and is configured only by environment variables:

| Variable | Example |
| --- | --- |
| `LLM_BASE_URL` | `https://api.openai.com/v1` or `http://127.0.0.1:11434/v1` |
| `LLM_API_KEY` | API key. Any non-empty placeholder works for a local server |
| `LLM_MODEL` | model name the server expects |

All three must be set. The client POSTs `{LLM_BASE_URL}/chat/completions` (a base that already ends in `/chat/completions` is used as-is). The system message is the System prompt section of `config/persona.md`. The user message is the question plus the context block.

If any of the three is unset, the bot skips the network and quotes the context block, using the **No model** section of the persona file. The same fallback is used when the HTTP call fails. The API key is never written into the prompt or the logs.

## Discord setup

1. Create an application at <https://discord.com/developers/applications>.
2. Bot tab: reset the token and put it in `.env` as `DISCORD_BOT_TOKEN`. Turn **Public Bot** off.
3. Enable the privileged intents **Server Members Intent** and **Message Content Intent**. The bot also uses the guilds and guild messages intents.
4. Invite with View Channels, Send Messages, Read Message History, and Use Application Commands only:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=2147552256&scope=bot%20applications.commands
```

`2147552256` is `1024 + 2048 + 65536 + 2147483648` (View Channels, Send Messages, Read Message History, Use Application Commands). Re-invite with that URL if an older invite granted extra permissions. Remove extra permissions from the bot's role in Server Settings.

5. Optional: set `GUILD_IDS` to one or more server ids, separated by commas, so slash commands appear in those servers immediately and the bot only logs them. Without it, the bot watches every server it is invited to, and Discord's global command sync can take up to an hour.

When `GUILD_IDS` is set, startup copies commands into those servers and then clears the global command set, so an earlier global sync does not leave a second copy in the picker. When `GUILD_IDS` is unset, startup publishes the global set and clears guild commands left over from a previous guild sync. The database remembers that scope (`command_scope` and `command_guilds` in `meta`). The first global startup after an upgrade clears guild commands for the servers the bot can see, once. Later global startups skip that clear. Restart the bot after this change so Discord drops the duplicates. Guild command updates are rate-limited, so a busy restart may need a minute before the picker matches.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
# fill DISCORD_BOT_TOKEN, and LLM_BASE_URL / LLM_API_KEY / LLM_MODEL if you want a model
./run.sh
```

Background: `./start_nohup.sh` writes `bot.log` and `bot.pid`. Stop with `kill $(cat bot.pid)`.

systemd user unit (edit the paths if the checkout is not under your home directory):

```bash
cp discord-qa-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-qa-bot
```

Other environment variables, all optional:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOT_DB` | `bot.db` | Live SQLite file |
| `DOCS_DIR` | `docs` | Notes folder |
| `SNAPSHOT_HOURS` | `6` | How often to snapshot members and roles |
| `GUILD_IDS` | empty | Comma-separated guilds to watch and to receive slash commands. `GUILD_ID` is a single-id alias when `GUILD_IDS` is unset |
| `EXPORTS_DIR` | `./exports` | DiscordChatExporter folder used by `backfill.py` when no path is passed |

The database file is `bot.db` unless `BOT_DB` points somewhere else.

Channel lists, the character cap, the display title, and the org name live in `config/settings.toml` and `config/settings.local.toml`, not in the environment.

Import old history from DiscordChatExporter JSON (blocklisted channels are skipped, then purged). With no path, this reads `./exports`, or `EXPORTS_DIR` when that is set. Export with a **bot** token only. Running an exporter with a personal user token is self-botting under Discord's Terms and can get that account suspended.

```bash
.venv/bin/python backfill.py
```

Or pass a path:

```bash
.venv/bin/python backfill.py path/to/exports
```

A keyword digest of recent logged messages. The title is `display_title` from settings:

```bash
.venv/bin/python digest.py --hours 168
```

The live SQLite file contains private server history. Keep it off git and off shared drives. The sample digest in `digests/` is a trimmed log with blocklisted channels removed.

## Asking

- `/ask question: When is the general meeting?`
- `@Bot when is the general meeting?`

Outside `allowed_channels`, `/ask` gets a private notice and a mention is ignored. In a blocklisted channel, `/ask` gets a private refusal and a mention is ignored. With `GUILD_IDS` set, other servers are ignored entirely.

What gets posted:

- The asker's own messages in the channel they asked in (a thread counts as its parent) or in a `public_channels` entry: answered immediately, with a Sources footer.
- Another member's messages, the asker's messages from a different visible channel, or a file in `docs/`: stored in `approvals` and the asker is told the request is held. The pending Discord message does not include the snippets.
- No match, or a match only in a blocklisted or invisible channel: the refusal from `config/persona.md`.

Notes belong in `docs/` as `.md`, `.markdown`, or `.txt`. `README.md` is skipped. Docs are visible to anyone who can ask, once an officer approves, so keep private notes out of that folder. Files over 200KB are skipped.

## Query log

Every `/ask` or mention is written to an anonymous `ask_log` table in the bot database (see `asklog.py`): the question, the answer, the sources used, a confidence score, and the time. It never records who asked or the channel, and it skips users who opted out. The write runs off the event loop and a failure there never blocks an answer. Use it to find unanswered or low-confidence questions and improve your knowledge files. `python demo.py --log --db <copy of bot.db>` logs demo questions the same way.

## Approval queue

Officers review holds on the machine that has the SQLite file and `DISCORD_BOT_TOKEN`:

```bash
.venv/bin/python admin.py list
.venv/bin/python admin.py show 3
.venv/bin/python admin.py approve 3
.venv/bin/python admin.py deny 3 --reason "internal only"
```

`approve` rebuilds the answer from the stored snippets after dropping anything now blocklisted or outside the stored scope, then posts a paraphrased reply in the original channel. The model runs at approve time. `deny` posts the **Denied** persona line. The reason stays in the database and is not posted. A second approve or deny on the same id fails. `/admin approvals list`, `approve`, and `deny` use this same queue.

Delete one member's stored rows (messages, edits, snapshots, events, and approval rows that quote them). This does not delete the Discord messages, and it does not reach a model provider that already received a snippet. There is no automatic expiry.

```bash
.venv/bin/python admin.py forget 123456789012345678
```

## Opting out

A member's Discord user id can be opted out. The bot then skips their messages, edits, member snapshots, and member events, and it will not use their old messages in answers. They can still use `/ask`. The id list lives in the local SQLite file, not in committed settings.

`/optout` does this for the member who runs it and replies ephemerally. It is on unless `optout_command` is set to false in `config/settings.local.toml`. Turning it off takes effect on the next question or `/admin bot reload`. Opting back in is an operator action. `remove` does not restore rows that were deleted. `/admin optout` does the same for a member an admin picks.

`/help` replies ephemerally with how to ask, what the bot can see, the approval queue, and `/optout`. Admins also see the `/admin` commands. It adds the Terms and Privacy links when those URLs are set. The custom status is applied when the bot is ready and again on `/admin bot reload`. Restart once so Discord registers new commands.

## Admin commands in Discord

`/admin` is a command group. Discord shows it to members with Manage Server. The bot checks that permission again, and when `admin_roles` lists role names or role ids the member needs one of those roles too. Every reply is ephemeral.

| Command | What it does |
| --- | --- |
| `/admin blocklist add` | Channel picker and an optional name. Writes `settings.local.toml` and deletes stored rows |
| `/admin blocklist remove` | Drops that channel from the local file. Deleted rows stay deleted |
| `/admin blocklist list` | Effective blocklist, committed file plus the local file |
| `/admin approvals list` | Pending requests |
| `/admin approvals approve` | Same as `admin.py approve`, posted in the original channel |
| `/admin approvals deny` | Same as `admin.py deny` |
| `/admin optout add` | Opt out the picked member and delete stored rows |
| `/admin optout remove` | Allow logging again. Does not restore rows |
| `/admin optout list` | Opted-out user ids |
| `/admin member forget` | Delete one member's stored rows after a confirm button |
| `/admin bot status` | Uptime, messages stored, last message, model, blocklist size, pending approvals |
| `/admin bot reload` | Re-read persona and settings, then refresh presence |
| `/admin bot set-status` | Save `status_text` in the local file and update presence |
| `/admin tier set-channel` | Map a channel to a tier in `settings.local.toml` |
| `/admin tier set-role` | Map a role to a tier in `settings.local.toml` |
| `/admin tier show` | Effective tier list, role map, and channel map |

A channel picked in the menu is stored by id on the global blocklist. A typed name is stored for that server. There is no top-level `/reload`.

Each use is inserted into `admin_audit` (time, user id, username, server id, action, short detail) and appended to `bot.log`. `forget` and opt-out do not remove those audit rows.

```bash
.venv/bin/python admin.py optout list
.venv/bin/python admin.py optout add 123456789012345678
.venv/bin/python admin.py optout remove 123456789012345678
```

## Demo in Cursor

No Discord account and no API key. The sample log is fictional (`sample/sample.db`): Ethan, Avery, Riley, and Jordan, in `#general`, `#business`, `#projects`, `#internal-announcements`, and a planted `#private-channel`. `demo.py` and `eval.py` ignore that planted channel because `sample/settings.toml` lists it. The live bot does not read that fixture, and the committed blocklist stays empty. Sample docs live in `sample/docs/`.

```bash
.venv/bin/python sample/build_sample.py   # rebuild the fictional log
.venv/bin/python demo.py --dry-run --as ethan \
  --channels general,business,projects,internal-announcements \
  --in general "When did I say I would bring the purchase order form?"
```

The report has three sections:

1. **Context** — the exact block that would be sent to the model, with the source of each snippet.
2. **Decision** — answered, or held for approval, and why.
3. **Answer** — the reply and its length.

`--dry-run` skips the model call. Omit the question for a REPL (`:as`, `:channels`, `:in`, `:dry`, `:reload`, `:quit`). The default asker is Ethan and the default channels are general, business, projects, and internal-announcements.

Each demo copies the sample to `sample/demo-session.db` so the committed file stays put. `--db path/to/bot.db` reads that file in place and does not apply `sample/settings.toml`. `--docs docs` points at the live notes folder. `--provider extractive` forces the quote fallback. `--provider openai` requires the three `LLM_*` variables.

```bash
.venv/bin/python eval.py
```

Canned questions, including a recruitment date from `sample/docs/facts.md`, a Friday GM time taken from a later bullet in `sample/docs/schedule.md`, a check that an unrelated note is not pasted when nothing relevant matches, a failed model call that says it does not know, a "What is X" question that ranks the `X:` fact line ahead of a note that only mentions X, a scripted model reply that is sent back once when it copies a snippet and is posted with its words intact, a check that the doc outranks a newer chat message, a no-verbatim check, a redacted email, the blocklisted sample channel, and a vendor quote that waits for an officer. Each line is PASS or FAIL. Exit status is 1 if any case fails.

Do not point the live bot at `sample/sample.db`. Startup purge deletes blocklisted rows, and the sample plants those rows on purpose so the probes have something to refuse.

## Tests

```bash
.venv/bin/python -m pytest
```

Covers FTS retrieval, the blocklist (including a channel you add yourself), the visibility filter, the approval path, persona hot reload, the OpenAI-compatible request, the demo report, and `eval.py`.

## Migrating an existing checkout

`DISCORD_BOT_TOKEN`, `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL` do not change.

Use `BOT_DB`, `DOCS_DIR`, `SNAPSHOT_HOURS`, and `GUILD_IDS`. Older checkouts used different environment variable names for the database, the docs folder, the snapshot interval, and a single guild id. Those names are not read. Point `BOT_DB` at the existing SQLite file if it is not named `bot.db`. `GUILD_ID` is a single-id alias when `GUILD_IDS` is unset. An empty `GUILD_IDS` means every server the bot is in.

The committed blocklist is empty. Put this server's skipped channels, title, and org name in `config/settings.local.toml` (see `config/settings.local.example.toml`). `admin.py blocklist` writes that local file. Per-server blocks go under `[blocklist.guilds]`.

The systemd example is `discord-qa-bot.service`. Edit the paths in that file to your checkout. If a previous unit is still enabled, disable it before enabling this one.

## Where to tweak

- Voice and the hold/refusal wording: `config/persona.md`.
- Title, org name, caps, and channel lists: `config/settings.toml` for shared defaults, `config/settings.local.toml` for this deployment, or `admin.py blocklist` for the blocklist.
- How many FTS hits are considered before the snippet cap, and how long each snippet is: `CANDIDATE_LIMIT`, `SNIPPET_CHARS`, and `CONTEXT_CHAR_BUDGET` in `retrieve.py`.
- What may be posted without an officer: `split_for_answer` in `qa.py`, `tiers`, and `sensitive_topics`.
- Another model server: set `LLM_BASE_URL`, `LLM_API_KEY`, and `LLM_MODEL`. A new provider subclasses `LLMProvider` in `llm.py` and is added to `PROVIDERS`.
