"""Three self-contained role knowledge bases built from three source folders.

Authoring (each entry lives in exactly one source folder, never duplicated):
  knowledge/public/   public facts
  knowledge/member/   member-only facts
  knowledge/officer/  officer + executive facts (rosters, internal data)

Build/index time generates one base per role, each with its own index:
  kb_public  = public
  kb_member  = member + public
  kb_officer = officer + member + public

A question searches exactly ONE base, chosen by the asker's tier.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

from tiers import norm_tier

SOURCES = ("public", "member", "officer")
ROLE_BASE = {"public": "kb_public", "member": "kb_member", "officer": "kb_officer", "admin": "kb_officer"}
CONTENTS = {"kb_public": ["public"], "kb_member": ["member", "public"],
            "kb_officer": ["officer", "member", "public"]}
OWN_SOURCE = {"kb_public": "public", "kb_member": "member", "kb_officer": "officer"}


def has_sources(knowledge_dir: str) -> bool:
    return bool(knowledge_dir) and any((Path(knowledge_dir) / s).is_dir() for s in SOURCES)


def base_for(asker_tier: str) -> str:
    return ROLE_BASE[norm_tier(asker_tier)]


def out_root(knowledge_dir: str) -> Path:
    return Path(os.environ.get("KB_BUILD_DIR") or Path(knowledge_dir).resolve().parent / "data" / "kb")


def _files(folder: Path):
    if folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not any(p.startswith(".") for p in path.relative_to(folder).parts):
                yield path


def _signature(knowledge_dir: str, base: str) -> str:
    digest = hashlib.sha256()
    for source in CONTENTS[base]:
        for path in _files(Path(knowledge_dir) / source):
            st = path.stat()
            digest.update(f"{source}/{path.relative_to(Path(knowledge_dir) / source)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def build_base(knowledge_dir: str, base: str) -> Path:
    """Materialize one base folder (a generated copy, rebuilt only when its sources change)."""
    root = out_root(knowledge_dir) / base
    stamp = root / ".signature"
    sig = _signature(knowledge_dir, base)
    if stamp.is_file() and stamp.read_text() == sig:
        return root
    tmp = root.with_name(base + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    for source in CONTENTS[base]:
        src = Path(knowledge_dir) / source
        for path in _files(src):
            dest = tmp / source / path.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / ".signature").write_text(sig)
    shutil.rmtree(root, ignore_errors=True)
    tmp.rename(root)
    return root


def build_all(knowledge_dir: str) -> dict[str, Path]:
    return {base: build_base(knowledge_dir, base) for base in CONTENTS}


def index_conn(knowledge_dir: str, base: str) -> sqlite3.Connection:
    """Each base has its own SQLite index file (its own docs_fts)."""
    from db import connect

    path = out_root(knowledge_dir) / f"{base}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return connect(str(path))
