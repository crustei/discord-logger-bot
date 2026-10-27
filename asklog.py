"""Anonymous question log.

Stores only the question, the answer, the decision, a confidence score, the
sources used, and a UTC timestamp. It never stores who asked or where they
asked. The operator reviews it to improve the knowledge files.
"""
from __future__ import annotations

import datetime as dt
import json

from db import connect, is_opted_out

SCHEMA = """
CREATE TABLE IF NOT EXISTS ask_log(
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('answered','dont_know','pending','error')),
  confidence REAL NOT NULL,
  sources TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS ask_log_ts ON ask_log(ts);
"""

DECISIONS = {"answer": "answered", "pending": "pending", "empty": "dont_know", "error": "error"}


def ensure_table(conn) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='ask_log'").fetchone()
    if row and "'error'" not in (row[0] or ""):
        # Older table: widen the decision check to allow 'error'; keep every row.
        conn.executescript(
            "ALTER TABLE ask_log RENAME TO ask_log_old; DROP INDEX IF EXISTS ask_log_ts;"
            + SCHEMA
            + "INSERT INTO ask_log(id, ts, question, answer, decision, confidence, sources)"
            " SELECT id, ts, question, answer, decision, confidence, sources FROM ask_log_old;"
            "DROP TABLE ask_log_old;"
        )


def _source(hit) -> dict:
    if getattr(hit, "kind", "") == "doc":
        return {"type": "doc", "title": hit.title or "note", "date": (hit.modified or "")[:10] or None}
    return {"type": "message", "channel": hit.channel_name or None, "date": (hit.timestamp or "")[:10] or None}


def sources_for(result) -> list[dict]:
    seen, out = set(), []
    for hit in result.snippets or []:
        item = _source(hit)
        key = json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def confidence_for(result) -> float:
    """0-1. Mixes the best retrieval score, how many distinct sources agree, and the decision."""
    decision = DECISIONS.get(result.kind, "dont_know")
    hits = list(result.snippets or [])
    if decision == "error":
        return 0.0
    if decision == "dont_know" or not hits:
        return 0.0 if not hits else 0.1
    scores = [max(0.0, float(getattr(h, "score", 0.0) or 0.0)) for h in hits]
    top = max(scores)
    retrieval = top / (top + 1.0) if top > 0 else 0.3  # squash unbounded scores into 0-1
    agreeing = min(len(sources_for(result)), 3) / 3.0
    value = 0.6 * retrieval + 0.4 * agreeing
    if decision == "pending":
        value *= 0.5
    if not result.llm_called:
        value *= 0.9
    return round(max(0.0, min(1.0, value)), 3)


def record(conn, question: str, result, *, asker_id: str | None = None) -> int | None:
    """Write one row. asker_id is used only for the opt-out check and is never stored."""
    if getattr(result, "dry_run", False) or not (question or "").strip():
        return None
    if asker_id is not None and is_opted_out(conn, asker_id):
        return None
    ensure_table(conn)
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    cur = conn.execute(
        "INSERT INTO ask_log(ts, question, answer, decision, confidence, sources) VALUES(?,?,?,?,?,?)",
        (
            ts,
            question.strip()[:500],
            result.text or "",
            DECISIONS.get(result.kind, "dont_know"),
            confidence_for(result),
            json.dumps(sources_for(result)),
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_in_new_connection(db_path: str, question: str, result, *, asker_id: str | None = None) -> int | None:
    conn = connect(db_path)
    try:
        return record(conn, question, result, asker_id=asker_id)
    finally:
        conn.close()
