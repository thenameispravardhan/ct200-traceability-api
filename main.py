import hashlib
import json
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

import llm
from store import Node, Selection, SelectionItem, Version, get_db, init_db, load_generations, now, save_generation
from versioning import change_summary, ingest, latest_version, nodes_of, staleness

init_db()
app = FastAPI(title="CT-200 traceability API")


def summary(n: Node) -> dict:
    return {"id": n.id, "logical_id": n.logical_id, "number": n.number, "heading": n.heading,
            "level": n.level, "change": n.change, "warnings": n.warnings}


def get_version(db: Session, document: str, number: int | None) -> Version:
    v = (db.scalar(select(Version).where(Version.document == document, Version.number == number))
         if number else latest_version(db, document))
    if v is None:
        raise HTTPException(404, f"no version {number or '(any)'} of {document!r}")
    return v


def get_node(db: Session, node_id: int) -> Node:
    n = db.get(Node, node_id)
    if n is None:
        raise HTTPException(404, f"node {node_id} not found")
    return n


# ---- ingestion -------------------------------------------------------------------------------

@app.post("/documents/{document}/versions", status_code=201)
async def upload_version(document: str, request: Request, db: Session = Depends(get_db)):
    """Body: the raw PDF (curl --data-binary @file.pdf). Re-uploading an identical file is a no-op."""
    pdf = await request.body()
    if not pdf.startswith(b"%PDF"):
        raise HTTPException(415, "body must be a PDF file")
    try:
        return ingest(db, document, pdf)
    except ValueError as e:
        raise HTTPException(422, f"could not parse PDF: {e}")


@app.get("/documents/{document}/versions")
def list_versions(document: str, db: Session = Depends(get_db)):
    vs = db.scalars(select(Version).where(Version.document == document).order_by(Version.number))
    return [{"version": v.number, "title": v.title, "created_at": v.created_at, "warnings": v.warnings} for v in vs]


# ---- browse ----------------------------------------------------------------------------------

@app.get("/documents/{document}/sections")
def top_level(document: str, version: int | None = None, db: Session = Depends(get_db)):
    v = get_version(db, document, version)
    return {"version": v.number, "sections": [summary(n) for n in nodes_of(db, v.id) if n.parent_id is None]}


@app.get("/nodes/{node_id}")
def node_detail(node_id: int, db: Session = Depends(get_db)):
    n = get_node(db, node_id)
    children = db.scalars(select(Node).where(Node.parent_id == n.id).order_by(Node.position))
    return {**summary(n), "version": db.get(Version, n.version_id).number, "parent_id": n.parent_id,
            "page": n.page, "body": n.body, "blocks": n.blocks, "content_hash": n.content_hash,
            "matched_by": n.matched_by, "children": [summary(c) for c in children]}


@app.get("/documents/{document}/search")
def search(document: str, q: str, version: int | None = None, db: Session = Depends(get_db)):
    v = get_version(db, document, version)
    like = f"%{q}%"
    hits = db.scalars(select(Node).where(Node.version_id == v.id, or_(Node.heading.ilike(like), Node.body.ilike(like)))
                      .order_by(Node.position))
    return {"version": v.number, "results": [summary(n) for n in hits]}


@app.get("/nodes/{node_id}/history")
def history(node_id: int, db: Session = Depends(get_db)):
    """Has this section changed across versions? One entry per version it appears in, with a diff
    summary against the previous appearance whenever the content hash differs."""
    n = get_node(db, node_id)
    rows = db.execute(select(Node, Version.number).join(Version).where(Node.logical_id == n.logical_id)
                      .order_by(Version.number)).all()
    out, prev = [], None
    for node, ver in rows:
        e = {"version": ver, "node_id": node.id, "number": node.number, "content_hash": node.content_hash}
        if prev is not None and prev.content_hash != node.content_hash:
            e["diff_vs_previous"] = change_summary(prev.text, node.text)
        out.append(e)
        prev = node
    latest = latest_version(db, db.get(Version, n.version_id).document)
    return {"logical_id": n.logical_id, "changed": len({e["content_hash"] for e in out}) > 1,
            "in_latest_version": rows[-1][1] == latest.number, "versions": out}


# ---- selections ------------------------------------------------------------------------------

class SelectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    node_ids: list[int] = Field(min_length=1)
    include_descendants: bool = True


def selection_out(db: Session, sel: Selection, **extra) -> dict:
    nodes = db.scalars(select(Node).join(SelectionItem).where(SelectionItem.selection_id == sel.id)
                       .order_by(Node.position))
    v = db.get(Version, sel.version_id)
    return {"id": sel.id, "name": sel.name, "document": v.document, "version": v.number,
            "created_at": sel.created_at, **extra,
            "items": [{"node_id": n.id, "logical_id": n.logical_id, "number": n.number, "heading": n.heading,
                       "content_hash": n.content_hash, "text": n.text} for n in nodes]}


@app.post("/selections", status_code=201)
def create_selection(body: SelectionIn, db: Session = Depends(get_db)):
    """Node ids are rows of one specific version, so a selection is version-pinned by construction.
    Same name + same resolved node set returns the existing selection."""
    picked = list(db.scalars(select(Node).where(Node.id.in_(body.node_ids))))
    if missing := set(body.node_ids) - {n.id for n in picked}:
        raise HTTPException(404, f"unknown node ids {sorted(missing)}")
    if len({n.version_id for n in picked}) > 1:
        raise HTTPException(422, "all nodes of a selection must come from the same document version")
    ids = {n.id for n in picked}
    if body.include_descendants:
        children: dict[int, list[int]] = {}
        for n in nodes_of(db, picked[0].version_id):
            children.setdefault(n.parent_id, []).append(n.id)
        stack = list(ids)
        while stack:
            for c in children.get(stack.pop(), []):
                if c not in ids:
                    ids.add(c)
                    stack.append(c)
    fp = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
    sel = db.scalar(select(Selection).where(Selection.name == body.name, Selection.fingerprint == fp))
    if sel:
        return selection_out(db, sel, reused=True)
    sel = Selection(name=body.name, fingerprint=fp, version_id=picked[0].version_id)
    db.add(sel)
    db.flush()
    db.add_all(SelectionItem(selection_id=sel.id, node_id=i) for i in ids)
    db.commit()
    return selection_out(db, sel, reused=False)


@app.get("/selections/{selection_id}")
def get_selection(selection_id: int, db: Session = Depends(get_db)):
    sel = db.get(Selection, selection_id)
    if sel is None:
        raise HTTPException(404, f"selection {selection_id} not found")
    return selection_out(db, sel)


# ---- generation & retrieval ------------------------------------------------------------------

@app.post("/selections/{selection_id}/generations", status_code=201)
def generate(selection_id: int, force: bool = False, db: Session = Depends(get_db)):
    """Same selection + same prompt version + same model returns the stored generation
    (reused=true) instead of paying for a different, non-deterministic answer to the same
    question. force=true creates an additional generation; earlier ones are kept."""
    sel = get_selection(selection_id, db)
    sources = [{"node_id": i["node_id"], "logical_id": i["logical_id"], "number": i["number"],
                "heading": i["heading"], "content_hash": i["content_hash"], "text": i["text"]} for i in sel["items"]]
    fp = hashlib.sha256(json.dumps([llm.PROMPT_VERSION, llm.MODEL, sources]).encode()).hexdigest()
    if not force:
        prior = [g for g in load_generations() if g["selection_id"] == selection_id and g["input_fingerprint"] == fp]
        if prior:
            return {**staleness(db, prior[-1]), "reused": True}
    try:
        result, attempts, raw = llm.generate(sources)
    except llm.GenerationFailed as e:
        # Nothing is stored: a half-valid test suite must not show up in retrieval as if it were real.
        raise HTTPException(502, {"message": f"LLM reply failed validation {llm.MAX_ATTEMPTS} times",
                                  "errors": e.errors, "last_raw": (e.last_raw or "")[:2000]})
    gen = {"id": uuid.uuid4().hex, "selection_id": selection_id, "selection_name": sel["name"],
           "document": sel["document"], "version": sel["version"], "created_at": now(),
           "model": llm.MODEL, "prompt_version": llm.PROMPT_VERSION, "input_fingerprint": fp,
           "attempts": attempts, "sources": sources,
           "test_cases": [{"id": f"TC-{k + 1}", **tc.model_dump()} for k, tc in enumerate(result.test_cases)],
           "raw_response": raw}
    save_generation(gen)
    return {**staleness(db, gen), "reused": False}


@app.get("/selections/{selection_id}/generations")
def generations_for_selection(selection_id: int, db: Session = Depends(get_db)):
    return [staleness(db, g) for g in load_generations() if g["selection_id"] == selection_id]


@app.get("/nodes/{node_id}/generations")
def generations_for_node(node_id: int, db: Session = Depends(get_db)):
    """Every generation built from this section in any version (matched by logical id)."""
    lid = get_node(db, node_id).logical_id
    return [staleness(db, g) for g in load_generations() if any(s["logical_id"] == lid for s in g["sources"])]


@app.get("/generations/{generation_id}")
def get_generation(generation_id: str, db: Session = Depends(get_db)):
    for g in load_generations():
        if g["id"] == generation_id:
            return staleness(db, g)
    raise HTTPException(404, f"generation {generation_id} not found")
