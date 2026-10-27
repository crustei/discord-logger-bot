# Knowledge bases and access tiers

Answers come only from the knowledge bases. Raw Discord messages stay in
bot.db for officer sweeps and are never searched for /ask or @mentions
(unless `answer_from = "knowledge_and_messages"` is set in settings).

## Tiers (config/tiers.toml)
An asker's tier is the highest tier of any Discord role they hold.
Officers and executives share one tier.

| Tier | Roles |
|---|---|
| officer | President, Vice President, Officer, Team Lead (examples; set in config/tiers.toml) |
| member | Member, Alumni |
| non-member | Guest, and any unlisted role |

## Source folders (author here; one entry lives in one folder only)
- `knowledge/public/`  facts anyone may see (public channels, constitution, info night)
- `knowledge/member/`  facts from member-visible channels
- `knowledge/officer/` officer/executive channels, rosters, internal data

`tools_local/kb/build.py` writes extracted entries into `<folder>/generated/`.
An entry's folder is the most restrictive tier among its source channels.

## Role bases (generated at index time, `data/kb/`)
| Asker | Base searched | Contains | Ranked first |
|---|---|---|---|
| non-member | kb_public | public | public |
| member | kb_member | member + public | member |
| officer/exec | kb_officer | officer + member + public | officer |

Each base is a self-contained copy with its own SQLite index (`data/kb/<base>.db`).
A question searches exactly one base.

## Retrieval
Hybrid: keyword FTS (bm25) + Gemini embeddings (`gemini-embedding-001`, 768 dims,
cached by content hash in `data/kb/embeddings.sqlite`), merged by reciprocal-rank
fusion. If embeddings fail, keywords only. At most 2 hits per file and 3 per
source channel; up to 20 whole entries / 15k chars go to the model, each with its
date. Newer entries win conflicts. A second model pass checks the answer is
supported by the entries; if not, the bot replies "I don't know".
