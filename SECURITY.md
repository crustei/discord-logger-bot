# Security

Never commit `.env` or tokens. Bot tokens, API keys, and guild-specific secrets belong only in local files that git ignores.

Report security issues privately to the repository owner via GitHub. Do not open a public issue for a vulnerability or a leaked credential.

The bot is read-only by default. It does not post on its own. It answers only when someone uses `/ask` or mentions it, and channels are logged only where an operator has invited the bot and has not blocklisted the channel.

Operators must publish their own Terms and Privacy links in local config. Put those URLs in `terms_url` and `privacy_url` in `config/settings.local.toml` (see `config/settings.local.example.toml`). The `TERMS.md` and `PRIVACY.md` files in this repository are templates, not published policies.
