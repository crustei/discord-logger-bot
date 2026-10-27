# Privacy Policy

**Effective date:** {effective_date}

**Operator:** {org_name}

**Contact:** {operator_contact}

This is a plain-language description of what this self-hosted Discord bot stores and sends. It is not legal advice. Replace `{org_name}`, `{operator_contact}`, and `{effective_date}` before you publish it. The bot does not fill those placeholders in.

{org_name} runs the bot and decides where the database lives. This page matches the bot's current behavior. If the operator changes the code, they should update this page.

## What gets logged

In servers the bot is watching, it stores each message it can read, except its own posts and channels on the blocklist.

For each stored message the database keeps:

- message text
- the author's Discord user id, username, and display name
- role names on that message
- guild id, channel id, channel name, and category name
- created and edited timestamps
- attachment URLs (the link Discord gives, not a copy of the file)
- a flag if the message was later deleted on Discord

Edits are stored too: the old text, the new text, and the edit time.

On a schedule (every six hours unless the operator sets `SNAPSHOT_HOURS`), the bot stores a snapshot of members it can see: Discord user id, username, display name, role names, join time, and whether the account is a bot. It also stores join, leave, role-change, and display-name-change events for members. Separate role snapshots store role names, role ids, and how many members have each role. Those role rows are not a list of people.

Direct messages are not logged. Servers left out of `GUILD_IDS`, when that variable is set, are not logged. The bot's own messages are not logged.

## Questions asked to the bot

Questions sent with /ask or a mention, and the bot's answers, are logged anonymously to improve answers. Each entry keeps the question, the answer, the sources used, a confidence score, and the time. It does not record who asked or in which channel. Questions from opted-out members are not logged.

## Opting out

You can opt out of logging. The list of opted-out Discord user ids is kept in the operator's database. It is not part of the published settings file.

When the operator leaves it enabled (the default), you can run `/optout` in the server. Only you see the confirmation. That command opts you out and deletes the copies the bot already stored: your messages, edit history, member snapshots, member events, and approval rows that quoted your messages.

If that command is turned off, or you cannot use it, ask {operator_contact}. The operator runs `admin.py optout add` with your numeric Discord user id, which does the same thing.

After you opt out:

- New messages, edits, snapshots, and events for your account are not stored.
- Your older messages are not used when someone asks a question, including you.
- You can still use `/ask` and @mention the bot. A question you submit can be saved in the approval queue so an officer can review it. That queue entry does not include excerpts from your old messages.
- Role snapshots stay, because they are role names and counts, not a list of people.

Opting out does not delete your messages from Discord, and it does not delete a copy a language-model provider already received. To be logged again, ask {operator_contact}. The operator can undo the opt-out. That does not restore the deleted copies. `/optout` itself does not undo an opt-out.

## Channels that are never stored

The blocklist is a list of channel names or channel ids, optionally limited to one server. Messages in those channels are not written to the database. If a channel is added to the blocklist later, rows already stored for it are deleted, including edit history. Taking a channel off the blocklist does not bring those rows back. The committed default list is empty, so a new install logs every channel it can read until the operator adds entries.

## Where it is stored

The log is a SQLite file on the operator's machine, usually `bot.db` in the bot directory, or whatever path `BOT_DB` names. Search uses an index in that same file. Approval requests are rows in that same file.

The operator's own notes, if they put `.md` or `.txt` files in the notes folder, are indexed there too. Those files are not Discord messages. With the default `docs_visibility` of `public`, a general question can be answered from those files directly. A file marked `visibility: approval`, or a deployment that sets `docs_visibility` to `approval`, waits for an officer first. Keep private notes out of that folder, because an answer can be based on them.

Posted answers are short paraphrases. The bot strips long copies of the source text and replaces email addresses, phone numbers, street addresses, and similar patterns with `[redacted]`. A citation can name a doc title or a channel and a date. It does not include a link to the file.

Nothing in this bot sells data or sends the log to an advertiser. The operator's copy stays on their machine except for the two cases below: an answer posted back to Discord, and excerpts sent to the language-model server the operator configured.

## When text is sent to a language model

Ordinary messages are only stored. They are not sent to a model.

A model is called only when someone uses `/ask` or @mentions the bot, and only to write that one reply:

- If the reply can be made from the asker's own messages in the channel they asked in, or in a channel the operator marked public, the bot sends the question and a few short excerpts to the model immediately. By default that is at most four excerpts, each trimmed to a few hundred characters.
- If the reply would use someone else's messages, the asker's messages from another channel, or an operator note, the bot does not call the model yet. It saves the question and the excerpts in an approval queue and tells the asker the request is waiting. The Discord notice does not include the excerpts. An officer later approves or denies it. Approve sends those stored excerpts and the question to the model, then posts the reply in the channel. Deny posts a short refusal and does not call the model.

Each model request contains the bot's instructions, the question, and the excerpts (channel, date, author display name, and text). It goes to the server named by `LLM_BASE_URL`, with the model named by `LLM_MODEL`. That server is whoever the operator chose (for example the operator's own machine, or a hosted API). {org_name} does not control what that provider does with the request after receiving it. If `LLM_BASE_URL`, `LLM_API_KEY`, or `LLM_MODEL` is unset, the bot does not call the network. It quotes the excerpts itself.

The reply is posted in the Discord channel, so people who can see that channel can see it. Discord already hosts the server's messages. The bot does not post a reply on its own.

The process log records that a question was asked and which channel it was in. It does not record the question text or the message log.

## How long it is kept

There is no automatic expiry. Rows stay until the operator removes them.

- Deleting a message in Discord marks the row deleted and drops it from search. The text and edit history stay in the database.
- Adding a channel to the blocklist deletes stored rows for that channel.
- `python admin.py forget DISCORD_USER_ID`, or `/admin member forget` after the confirm button, deletes that member's messages, edit history, search-index rows, member snapshots, member events, and approval rows they asked for or that stored their message text. Role snapshots are left in place. Audit rows are left in place.
- Deleting the SQLite file, or a backup of it, removes whatever was in that file.

`forget` does not delete the message from Discord. It does not delete a copy an LLM provider already received. If the operator ran the digest tool, files in `digests/` can contain message text, and `forget` does not edit those files. The operator deletes those by hand.

`/optout`, or `admin.py optout add`, deletes the same stored rows as `forget` and also stops future logging. Ask {operator_contact} if you cannot run the command yourself. The operator uses your numeric Discord user id.

## Admin actions

When someone uses an admin command, the bot stores a row in the same SQLite file: the time, that person's Discord user id and username, the server id, the action name, and a short detail such as a channel id or an approval id. The same line is appended to `bot.log` on the operator's machine.

That audit log stays in the database until the operator deletes the file. `forget` and opt-out do not remove it. There is no automatic expiry. The language-model request is built from the question and the retrieved notes, and this audit row is not part of that request.

## Sharing

{org_name} does not sell this log and does not give it to other organizations for their own use.

People who can read a channel may see an answer posted there. An officer who reviews the queue can read the stored excerpts for that request. The configured language-model server receives the question and excerpts when a reply is generated, as described above. Discord receives the reply the bot posts, the same way it receives any other message.

## Contact

Privacy questions and deletion requests: {operator_contact}.
