# Discord Q&A Bot

A self-hosted Discord bot that answers members' questions from your server's message history and a folder of Markdown knowledge pages.

## Features

- `/ask` and @mention Q&A with SQLite FTS5 retrieval; source priority (knowledge pages > pinned/dated pages > chat), OUTDATED lines rank lower, optional page boosts
- Router with built-in replies (identity, greeting, "who am I", privacy decline for personal questions, off-topic redirect, opt-out info) so the LLM is only called when useful; never returns an empty reply
- Access tiers (non-member / member / reviewer) per role, channel allow/exclude lists, sensitive-topic blocklist
- Feedback: ✅/➖/❌ buttons and reviewer reaction ratings, one vote per person, stored for quality reports
- LLM via any OpenAI-compatible API (Gemini free tier works), with extractive fallback when the LLM is down
- Member opt-out (`/optout`), anonymous ask log, catch-up after reconnect, digests
- Self-test/diagnostics, watchdog, systemd unit, optional GCP VM deploy scripts
- Knowledge push + reindex without restarting

See [SETUP.md](SETUP.md), [TERMS.md](TERMS.md), [PRIVACY.md](PRIVACY.md). License: see LICENSE.

The display title, the bot name, the org name, and the voice are config, not code. Shared defaults live in `config/settings.toml` and `config/persona.md`. Values for one deployment go in `config/settings.local.toml`, which is gitignored. Copy `config/settings.local.example.toml` to start that file.

Terms and privacy files are plain-language templates, not legal advice. Replace the bracketed placeholders before you publish them. Host each page at its own public URL, then paste those URLs into the Discord Developer Portal and into `terms_url` and `privacy_url` so `/help` can link to them.

## Architecture

```
Discord messages
    -> bot.py (skips the bot's own posts and blocklisted channels)
    -> SQLite messages + FTS5

/ask or @mention
    -> router.py (mention markup stripped; identity, greeting, privacy, off-topic)
    -> knowledge files in DOCS_DIR, then messages the asker can see
    -> fact pages pinned; OUTDATED lines rank lower; boost pages get +0.5
    -> at most max_context_snippets (default 12)
    -> short paraphrased answer, or a held request for a reviewer
    -> channels the asker cannot see: dropped

reviewer ✅ / ❌ reaction or ✅/➖/❌ buttons
    -> one vote per person per answer (custom_id prefix qav:)
```

The model writes a short reply from the context block `retrieve.py` already built. It does not search the database. With no model, or when the model is down for a reason other than a free-tier rate limit, the bot paraphrases locally. A free-tier rate limit gets a short busy reply and does not call a second key.

| Piece | Role |
| --- | --- |
| `bot.py` | Logger, `/ask`, mentions, `/admin`, reaction ratings |
| `router.py` | Pre-LLM routes and fact-page ranking |
| `db.py` | SQLite, FTS5, approvals, audit log |
| `retrieve.py` | BM25 search, knowledge files, and the context block |
| `permissions.py` | Visibility, allowlist, blocklist |
| `qa.py` | Answer, hold, refusal, never-empty reply |
| `feedback.py` | Reviewer votes, buttons, ✅/❌ reactions |
| `voice.py` | Loads `config/persona.md`, `config/settings.toml`, and `config/settings.local.toml` |
| `llm.py` | OpenAI-compatible chat completions, or a local fallback |
| `admin.py` | Reviewer CLI: list, show, approve, deny, the blocklist, opt-out, and `forget` |
| `demo.py` / `eval.py` | Offline asker and the canned checks |

SQLite tables include `messages`, `message_edits`, `member_snapshots`, `approvals`, `opt_outs`, `admin_audit`, `ask_log`, `ask_feedback`, plus FTS tables.

## Voice and settings

Non-coders change how the bot talks, what it is called, and which channels it ignores by editing config. No Python change is required.

`config/settings.toml` (override any key in `config/settings.local.toml`):

| Key | Default when omitted | Meaning |
| --- | --- | --- |
| `display_title` | `Discord bot` | Digest title and the startup log line |
| `org_name` | `this server` | Slash-command text and the `{org_name}` placeholder |
| `bot_name` | the display title | Identity replies and self-reference matching |
| `org_short_name` | empty | Short token added to stopwords, such as an acronym |
| `maintainer` | `the maintainer` | Named in the built-in "who built you" reply |
| `info_channel_hint` | `#announcements` | Where the "where do I find info" reply points |
| `domain_terms` | meeting, rocket, team, … | Regex fragments that keep a question on-topic |
| `boost_pages` | lore, project-specs, … | Fact-page file stems that outrank chat |
| `meeting_page` | `meeting-times` | Dated page pinned for meeting questions |
| `typo_aliases` | cheif → chief | Extra search words for common typos |
| `max_answer_chars` | 600 | Cap on every posted reply (80–2000) |
| `max_context_snippets` | 12 | How many snippets the model sees |
| `excluded_channels` | empty | Global blocklist. Names or channel ids |
| `allowed_channels` | empty | Where `/ask` and mentions answer. Empty means everywhere not blocklisted |
| `officer_role` | `an officer` | Reviewer label used when the bot does not know |
| `sensitive_topics` | grades, contact details, and similar | Questions that mention one of these wait for a reviewer |

Built-in router replies live under `[replies]` and accept `{bot_name}`, `{org_name}`, `{maintainer}`, `{info_channel_hint}`, and `{reviewer}`. Leave a key out to keep the built-in wording.

Per-server blocklist entries use a guild id placeholder:

```toml
[blocklist.guilds]
"YOUR_GUILD_ID" = ["private-channel", "YOUR_CHANNEL_ID"]
```

`config/persona.md` is the voice. `{max_answer_chars}`, `{org_name}`, and `{display_title}` are filled from settings. The bot re-reads persona and settings when their contents change. `/admin bot reload` reads them immediately.

```bash
cp config/settings.local.example.toml config/settings.local.toml
```

## Knowledge directory

`DOCS_DIR` (default `./docs`, or `./knowledge` for tiered bases) is operator-maintained markdown or text. The bot indexes `.md`, `.markdown`, and `.txt`, and skips `README.md`.

Tiered layouts use `knowledge/public/`, `knowledge/member/`, and `knowledge/officer/`. Hand-written fact pages belong in `knowledge/<tier>/docs/<page>.md`. A meeting question pins `meeting-times.md`. Pages listed in `boost_pages` get a rank bonus. A line containing `OUTDATED` ranks lower.

`python -m tools_local.kb.build` turns local JSON extractions (not committed) into `knowledge/<tier>/generated/`. Set `GUILD_ID` and `BOT_DB`. See [docs/knowledge-bases.md](docs/knowledge-bases.md).

A file may start with front matter (`title`, `source`, `modified`, `visibility`). `docs_visibility = "public"` answers from these files directly. `approval` holds every file that does not say `visibility: public`.

## Model

The provider is OpenAI-compatible and is configured only by environment variables:

| Variable | Example |
| --- | --- |
| `LLM_BASE_URL` | `https://api.openai.com/v1` or `http://127.0.0.1:11434/v1` |
| `LLM_API_KEY` | API key. Any non-empty placeholder works for a local server |
| `LLM_MODEL` | model name the server expects |

All three must be set. If any is missing, the bot skips the network and uses the local fallback. Optional `PAID_KEY_SHA256` (comma-separated sha256 hex digests) and `GEMINI_PAID_API_KEY` are refused at runtime so a paid key is never used by mistake. The API key is never written into the prompt or the logs.

## Discord setup

1. Create an application at <https://discord.com/developers/applications>.
2. Bot tab: reset the token and put it in `.env` as `DISCORD_BOT_TOKEN`. Turn **Public Bot** off.
3. Enable the privileged intents **Server Members Intent** and **Message Content Intent**. The bot also uses the guilds, guild messages, and guild reactions intents.
4. Invite with View Channels, Send Messages, Embed Links, Add Reactions, Read Message History, and Use Application Commands:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=2147568704&scope=bot%20applications.commands
```

`2147568704` is Add Reactions + View Channels + Send Messages + Embed Links + Read Message History + Use Application Commands. Re-invite if an older invite is missing Add Reactions; reviewer ✅/❌ ratings need that permission.

5. Optional: set `GUILD_IDS` to one or more server ids, separated by commas, so slash commands appear in those servers immediately. Without it, the bot watches every server it is invited to.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
./run.sh
```

Background: `./start_nohup.sh` writes `bot.log` and `bot.pid`. Stop with `kill $(cat bot.pid)`.

systemd user unit (edit the paths if the checkout is not under your home directory):

```bash
cp discord-qa-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now discord-qa-bot
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `BOT_DB` | `bot.db` | Live SQLite file |
| `DOCS_DIR` | `docs` | Notes folder |
| `SNAPSHOT_HOURS` | `6` | How often to snapshot members and roles |
| `GUILD_IDS` | empty | Comma-separated guilds to watch and to receive slash commands |
| `CATCHUP_SKIP_CHANNELS` | empty | Comma-separated channel names catch-up never reads |
| `MATCH_MIN` | `0.4` | Minimum token overlap before the bot declines a weak match |

The live SQLite file contains private server history. Keep it off git and off shared drives.

## Asking

- `/ask question: When is the general meeting?`
- `@Bot when is the general meeting?`

Outside `allowed_channels`, `/ask` gets a private notice and a mention is ignored. In a blocklisted channel, `/ask` gets a private refusal and a mention is ignored.

What gets posted:

- A public knowledge page, or a message the asker can see: answered, with a sources footer when citations are on.
- A question about someone's private information: stored in `approvals` and the asker is told the request is held.
- No match, or a match only in a blocklisted or invisible channel: the refusal from `config/persona.md`.
- Identity, greetings, "who am I", opt-out list, and off-topic remarks: a configured reply, with no model call.

## Approval queue

Reviewers use the machine that has the SQLite file:

```bash
.venv/bin/python admin.py list
.venv/bin/python admin.py show 3
.venv/bin/python admin.py approve 3
.venv/bin/python admin.py deny 3 --reason "internal only"
.venv/bin/python admin.py forget YOUR_USER_ID
```

`approve` rebuilds the answer from the stored snippets, then posts it in the original channel. `deny` posts the **Denied** persona line. A second approve or deny on the same id fails.

## Opting out

`/optout` removes the caller's messages from the index and stops further collection. It is on unless `optout_command` is false. Opting back in is an operator action. The bot will not say who is on the opt-out list.

## Admin commands in Discord

`/admin` is shown only to server Administrators, and the bot checks that permission again on every use. Every reply is ephemeral.

Access tiers are public < member < officer < admin. The officer tier is the reviewer tier. `/admin roles show`, `set`, and `remove` map roles. Mappings are stored by role id in `data/role_tiers.json` and seeded from `config/tiers.toml`.

| Command | What it does |
| --- | --- |
| `/admin blocklist add` / `remove` / `list` | Edit the local blocklist |
| `/admin approvals list` / `approve` / `deny` | The same queue as `admin.py` |
| `/admin optout add` / `remove` / `list` | Opt a member out, or allow logging again |
| `/admin member forget` | Delete one member's stored rows after a confirm button |
| `/admin bot status` / `reload` / `set-status` | Uptime, settings reload, presence text |
| `/admin roles show` / `set` / `remove` | Map roles onto the member or reviewer tier |

Reviewers who use `/ask` get a private ✅/➖/❌ prompt. @mention answers get ✅ and ❌ reactions. One vote per person; the latest vote wins, and removing the reaction removes that vote. Buttons use the `qav:` custom id prefix.

## Demo and tests

No Discord account and no API key. The sample log is fictional. Sample docs live in `sample/docs/`.

```bash
.venv/bin/python sample/build_sample.py
.venv/bin/python demo.py --dry-run --as ethan \
  --channels general,business,projects,internal-announcements \
  --in general "When did I say I would bring the purchase order form?"
.venv/bin/python eval.py
.venv/bin/python -m pytest
```

Do not point the live bot at `sample/sample.db`.

## Optional: GCP VM

Set `PROJECT_ID`, `VM_NAME`, and `ZONE`, then follow [deploy/gcp/README.md](deploy/gcp/README.md) and [SETUP.md](SETUP.md). Knowledge push uses `deploy/gcp/push_knowledge.sh` (add/update, then reindex, no restart) over IAP SSH. Keep `.env` on the VM only.
