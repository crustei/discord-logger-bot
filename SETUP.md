# Setup
1. **Discord app**: discord.com/developers → New Application → Bot → copy token. Enable Privileged Intents: Message Content, Server Members. Invite with scopes `bot applications.commands` and permissions: View Channels, Read Message History, Send Messages, Embed Links, Add Reactions, Use Slash Commands.
2. **Gemini key** (free): aistudio.google.com → Get API key.
3. `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
4. `cp .env.example .env` and fill placeholders. Copy `config/settings.local.example.toml` → `config/settings.local.toml`; set `bot_name`, `org_name`, guild/channel/role IDs, reviewer role, excluded channels.
5. Put Markdown pages in `knowledge/`, then `python -m tools_local.kb.build` (or `./run.sh reindex`).
6. Run: `./run.sh` · Tests: `pytest -q`
## Optional: GCP VM
Set PROJECT_ID, VM_NAME, ZONE in your shell, then `deploy/gcp/provision.sh` and `deploy/gcp/create_vm.sh`. Push knowledge with `deploy/gcp/push_knowledge.sh` (adds/updates then reindexes, no restart). Uses IAP SSH. Keep `.env` on the VM only.
