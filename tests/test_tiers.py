from types import SimpleNamespace as N

import tiers
from feedback import record_vote, tally
from db import connect
from asklog import record
from qa import Result

SEED = {"lead": "officer", "crew": "member"}


def role(i, name):
    return N(id=i, name=name)


def guild(gid=1):
    return N(id=gid, roles=[role(10, "Lead"), role(20, "Crew"), role(30, "Other")])


def user(roles, admin=False, gid=1):
    return N(roles=roles, guild=N(id=gid), guild_permissions=N(administrator=admin))


def test_order_and_aliases():
    assert tiers.TIERS == ["public", "member", "officer", "admin"]
    assert tiers.norm_tier("non-member") == "public"
    assert tiers.is_officer("admin") and tiers.is_officer("officer") and not tiers.is_officer("member")


def test_admin_permission_beats_roles():
    assert tiers.member_tier(user([], admin=True)) == "admin"


def test_seed_then_role_ids_and_live_edits(tmp_path):
    g = guild()
    assert tiers.seed_guild(g, names=SEED) == 2
    assert tiers.seed_guild(g, names=SEED) is None  # only once
    assert tiers.member_tier(user([role(10, "Lead")])) == "officer"
    # renaming a role doesn't change access (stored by id)
    assert tiers.member_tier(user([role(10, "Renamed")])) == "officer"
    tiers.set_role(1, 30, "officer")
    assert tiers.member_tier(user([role(30, "Other")])) == "officer"
    assert tiers.remove_role(1, 10) == "officer"
    assert tiers.member_tier(user([role(10, "Lead")])) == "public"
    assert tiers.member_tier(user([role(20, "Crew")])) == "member"
    assert tiers.guild_roles(2) is None


def test_unconfigured_server_uses_name_seed():
    assert tiers.member_tier(user([role(99, "Lead")], gid=5), SEED) == "officer"


def test_set_rejects_admin_and_public():
    import pytest
    for t in ("admin", "public"):
        with pytest.raises(ValueError):
            tiers.set_role(1, 5, t)


def test_admin_can_verify_answers(tmp_path):
    conn = connect(str(tmp_path / "t.db"))
    rid = record(conn, "q", Result(kind="answer", reason="", context_block="", text="a", llm_called=True), asker_id="1")
    assert record_vote(conn, rid, 5, "admin", "yes") == "saved"
    assert tally(conn, rid)["votes"] == 1


def test_bases():
    assert tiers.searchable_bases("admin") == ["officer", "member", "public"]
    assert tiers.searchable_bases("public") == ["public"]
    import kb_bases
    assert kb_bases.base_for("admin") == "kb_officer"
