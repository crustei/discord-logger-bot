# Privacy Policy

**Effective date:** <EFFECTIVE_DATE>
**Operator:** <ORG_NAME>
**Contact:** <OPERATOR_CONTACT>

This is a plain-language template for a self-hosted Discord bot. It is not legal advice. Replace every `<PLACEHOLDER>` before you publish it; the bot does not fill them in. If you change the code or settings, update this page to match.

## What's new

- **Anonymous question log.** Questions asked with `/ask` or a mention are logged with the answer, the sources used, a confidence score, and the time. The log does not record who asked or in which channel.
- **Access tiers.** Answers are limited by the asker's role tier and by which channels they can view.
- **History backfill.** The operator can import a server's older message history, collected with the bot's token through the official bot API.

## What the bot stores

The bot reads messages through Discord's official bot API, in servers where it was invited. It stores each message it can read, except its own posts and channels on the blocklist:

- message text, created and edited times, and a flag if the message was deleted
- the author's Discord user id, username, display name, and role names
- server id, channel id, channel name, and category name
- attachment links (not copies of the files)
- edit history (old text, new text, edit time)

On a schedule it also stores member snapshots (user id, names, roles, join time) and join, leave, role-change, and name-change events. Role snapshots store role names, ids, and member counts.

**History backfill.** The operator can import older messages from channels the bot can view, from exports collected with the bot's own token through the official bot API. Imported messages are stored the same way as live ones and follow the same blocklist and opt-out rules.

Direct messages are not logged.

## Channels that are never stored

The operator keeps a blocklist of channels. Messages there are not written to the database. Adding a channel later deletes rows already stored for it.

## Opting out

Run `/optout` in the server (when the operator leaves it on), or ask <OPERATOR_CONTACT>. Opting out:

- stops storing your messages, edits, snapshots, and events
- deletes the copies the bot already has
- keeps your older messages out of answers
- stops logging your questions in the question log

You can still use `/ask`. Opting out does not delete messages from Discord, or anything an LLM provider already received. Question-log entries made **before** you opted out cannot be traced to you, so they cannot be found or removed by person.

## How answers work

`/ask` answers are short paraphrases, not quotes. The bot searches stored messages and the operator's knowledge files, then sends the question and a few short excerpts to the language-model provider the operator configured (<LLM_PROVIDER>). That provider's own terms apply to what it receives; the operator does not control it. If no provider is configured, no text leaves the operator's machine. Answers can be wrong or incomplete. Email addresses, phone numbers, and similar details are replaced with `[redacted]`.

Some answers wait for an officer to approve them before the model is called.

## Access tiers

The operator defines tiers (for example: guest, member, officer, admin) and maps roles and channels onto them. Your tier is the highest tier among your roles. An answer only uses messages from channels you can view and knowledge files at or below your tier.

## Anonymous question log

Each `/ask` or mention is logged with:

- the question
- the answer
- the sources used (a knowledge file title, or a channel name and date)
- a confidence score
- the time

It does **not** record who asked or which channel it was asked in. Questions from opted-out users are not logged. The operator uses the log to improve the bot's answers and knowledge files.

## Admin commands

`/admin` is limited to people with the **Manage Server** permission. Admin actions (blocklist, tiers, approvals, forget, opt-out) are recorded in an audit log with the time, the admin's user id, and the action.

## Retention and deletion

There is no automatic expiry: <RETENTION_PERIOD>. Rows stay until the operator removes them. To have your stored data deleted, contact <OPERATOR_CONTACT> with your Discord user id. The operator runs `forget`, which deletes your messages, edits, snapshots, events, and approval rows. It does not remove the audit log or anonymous question-log entries.

The data lives in a SQLite file on the operator's machine. It is not sold or shared with advertisers.

## Changes

<ORG_NAME> may update this policy. The effective date at the top changes when it does, and the "What's new" section lists the main changes.

## Contact

Privacy questions and deletion requests: <OPERATOR_CONTACT>.
