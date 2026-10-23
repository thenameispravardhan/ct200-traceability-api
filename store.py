"""Persistence: SQLite (tree, versions, selections) + a JSON-file store for LLM generations."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

engine = create_engine(os.environ.get("DB_URL", "sqlite:///ct200.db"))
GEN_DIR = Path(os.environ.get("GENERATIONS_DIR", "generations"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Base(DeclarativeBase):
    pass


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("document", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document: Mapped[str]
    number: Mapped[int]
    file_sha256: Mapped[str]
    title: Mapped[str]
    warnings: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=now)


class Node(Base):
    """One section as it appears in one version. Rows are never updated after ingestion,
    so a node id is itself a version-pinned reference."""
    __tablename__ = "nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"), index=True)
    logical_id: Mapped[str] = mapped_column(index=True)  # same section across versions
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"))
    position: Mapped[int]  # document order
    number: Mapped[str]
    heading: Mapped[str]
    level: Mapped[int]
    page: Mapped[int]
    body: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list] = mapped_column(JSON)
    content_hash: Mapped[str]
    warnings: Mapped[list] = mapped_column(JSON)
    change: Mapped[str]  # vs previous version: added | unchanged | modified
    matched_by: Mapped[str | None]  # number+heading | heading | content | None (new)

    @property
    def text(self) -> str:
        # No section number: renumbering alone is not a content change.
        return f"{self.heading}\n{self.body}"


class Selection(Base):
    __tablename__ = "selections"
    __table_args__ = (UniqueConstraint("name", "fingerprint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    fingerprint: Mapped[str]  # sha256 of the sorted node ids
    version_id: Mapped[int] = mapped_column(ForeignKey("versions.id"))
    created_at: Mapped[str] = mapped_column(default=now)


class SelectionItem(Base):
    __tablename__ = "selection_items"
    selection_id: Mapped[int] = mapped_column(ForeignKey("selections.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), primary_key=True)


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    with Session(engine) as s:
        yield s


# Generations are immutable documents written once, read by selection or node, never joined
# against the tree: one JSON file each is enough and needs no server.
def save_generation(gen: dict):
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GEN_DIR / f"{gen['id']}.json.tmp"
    tmp.write_text(json.dumps(gen, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, GEN_DIR / f"{gen['id']}.json")  # atomic: readers never see half a file


def load_generations() -> list[dict]:
    # ponytail: full directory scan per query; fine for hundreds, move to MongoDB with an index on
    # selection_id / sources.logical_id when it isn't.
    if not GEN_DIR.exists():
        return []
    gens = [json.loads(p.read_text(encoding="utf-8")) for p in GEN_DIR.glob("*.json")]
    return sorted(gens, key=lambda g: g["created_at"])
