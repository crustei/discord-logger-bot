"""Build sample/sample.db, a fictional log for the offline demo.

The private-channel rows are planted on purpose. demo.py and eval.py ignore
them because sample/settings.toml lists that channel. The live bot does not
read that fixture. Run: python sample/build_sample.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import connect, rebuild_messages_fts, upsert_message

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "sample.db")


def row(**kwargs):
    base = dict(
        guild_id="1",
        category="General",
        roles=["Member"],
        edited_timestamp=None,
        attachments=[],
        source="sample",
    )
    base.update(kwargs)
    return base


NORMAL = [
    row(
        id="m-form",
        channel_id="10",
        channel_name="general",
        author_id="1001",
        author_name="ethan",
        display_name="Ethan",
        timestamp="2026-10-18T15:00:00+00:00",
        content="I will bring the purchase order form on Friday.",
    ),
    row(
        id="m-recruit-guess",
        channel_id="10",
        channel_name="general",
        author_id="1001",
        author_name="ethan",
        display_name="Ethan",
        timestamp="2026-10-19T12:00:00+00:00",
        content="I think recruitment is in December.",
    ),
    row(
        id="m-solarium",
        channel_id="10",
        channel_name="general",
        author_id="1002",
        author_name="avery",
        display_name="Avery Chen",
        timestamp="2026-10-18T15:05:00+00:00",
        content="The Sunday session is at 3PM in the Solarium.",
    ),
    row(
        id="m-drive",
        channel_id="20",
        channel_name="internal-announcements",
        category="Internal",
        author_id="1002",
        author_name="avery",
        display_name="Avery Chen",
        timestamp="2026-10-12T18:00:00+00:00",
        content="Donation drive closes next Friday. Sample link: https://example.test/donation-drive",
    ),
    row(
        id="m-fire",
        channel_id="30",
        channel_name="projects",
        category="Projects",
        author_id="1003",
        author_name="riley",
        display_name="Riley Okonkwo",
        timestamp="2026-10-15T16:00:00+00:00",
        content="Static fire is Saturday morning on the pad.",
    ),
    row(
        id="m-quote",
        channel_id="40",
        channel_name="business",
        category="Business",
        author_id="1001",
        author_name="ethan",
        display_name="Ethan",
        timestamp="2026-10-10T17:00:00+00:00",
        content="My vendor quote for nozzle blanks is 500 dollars.",
    ),
    row(
        id="m-receipts",
        channel_id="40",
        channel_name="business",
        category="Business",
        author_id="1002",
        author_name="avery",
        display_name="Avery Chen",
        timestamp="2026-10-11T17:00:00+00:00",
        content="Reimbursement receipts go in the shared drive.",
    ),
]
NORMAL += [
    row(
        id=f"m-note-{n}",
        channel_id="10",
        channel_name="general",
        author_id="1001",
        author_name="ethan",
        display_name="Ethan",
        timestamp=f"2026-10-18T16:{n:02d}:00+00:00",
        content=f"standupnote {n} for the shop checklist",
    )
    for n in range(1, 15)
]

EXCLUDED = [
    row(
        id="m-pin",
        channel_id="50",
        channel_name="private-channel",
        category="Private",
        author_id="1004",
        author_name="jordan",
        display_name="Jordan Hale",
        timestamp="2026-10-16T15:16:00+00:00",
        content="The discretionary fund is 99999 and the budget PIN is 1234. Do not share.",
    ),
    row(
        id="m-short",
        channel_id="50",
        channel_name="private-channel",
        category="Private",
        author_id="1001",
        author_name="ethan",
        display_name="Ethan",
        timestamp="2026-10-16T15:20:00+00:00",
        content="I agree the sponsor shortfall stays in this channel.",
    ),
]


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect(DB_PATH)
    for item in NORMAL:
        if not upsert_message(conn, item):
            raise SystemExit(f"refused to store sample row {item['id']}")
    for item in EXCLUDED:
        if not upsert_message(conn, item, allow_excluded=True):
            raise SystemExit(f"failed to plant excluded row {item['id']}")
    rebuild_messages_fts(conn)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    for suffix in ("-wal", "-shm"):
        extra = DB_PATH + suffix
        if os.path.exists(extra):
            os.remove(extra)
    print(f"wrote {DB_PATH} ({count} messages)")


if __name__ == "__main__":
    main()
