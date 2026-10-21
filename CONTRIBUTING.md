# Contributing

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
cp config/settings.local.example.toml config/settings.local.toml
python sample/build_sample.py
```

Put `DISCORD_BOT_TOKEN` and any `LLM_*` values in `.env` only. That file is gitignored.

## Tests

```bash
python -m pytest
python eval.py
```

`pytest` covers retrieval, the blocklist, approvals, and the demo. `eval.py` runs the canned questions against the fictional sample log. Build `sample/sample.db` first with `python sample/build_sample.py` if it is missing. Both commands should exit 0.

## Deployment-specific config

Keep this server's config out of the repository. Do not commit `.env`, `config/settings.local.toml`, `*.db`, `bot.log`, `digests/`, `knowledge/`, or `backups/`. Shared defaults stay in `config/settings.toml` and `config/persona.md`. Title, org name, channel lists, and Terms and Privacy URLs go in `config/settings.local.toml`.
