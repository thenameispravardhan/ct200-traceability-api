"""Ingest a version, match its sections to the previous version, and judge how much text changed."""
import difflib
import hashlib
import re
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from pdf_tree import parse
from store import Node, Version

# Tokens whose change alters what a test must check: numbers with units, codes like E3,
# and words that flip or bound a requirement. Everything else counts as wording.
FACT = re.compile(
    r"[A-Za-z]*\d+(?:[.,]\d+)*\s*(?:%|mmHg|bpm|seconds?|minutes?|hours?|days?|years?|cm|V)?"
    r"|\b(?:not|no|never|cannot|must|shall|only|except|unless|before|after|within|above|below)\b",
    re.I,
)


def latest_version(db: Session, document: str) -> Version | None:
    return db.scalar(select(Version).where(Version.document == document).order_by(Version.number.desc()))


def nodes_of(db: Session, version_id: int) -> list[Node]:
    return list(db.scalars(select(Node).where(Node.version_id == version_id).order_by(Node.position)))


def ingest(db: Session, document: str, pdf: bytes) -> dict:
    sha = hashlib.sha256(pdf).hexdigest()
    same = db.scalar(select(Version).where(Version.document == document, Version.file_sha256 == sha))
    if same:  # re-uploading an identical file is a no-op, not a new version
        return {"version": same.number, "reused": True}

    title, parsed, warnings = parse(pdf)
    if not parsed:
        raise ValueError("no numbered sections found")
    prev = latest_version(db, document)
    old = nodes_of(db, prev.id) if prev else []
    matches = match(old, parsed)

    # LIMIT: version numbers race under concurrent ingests of the same document; the unique
    # constraint turns that into an error rather than corruption. Serialize ingests if that matters.
    ver = Version(document=document, number=prev.number + 1 if prev else 1, file_sha256=sha,
                  title=title, warnings=warnings)
    db.add(ver)
    db.flush()

    rows, report = [], {"added": [], "modified": [], "unchanged": 0}
    for i, (p, (old_node, rule)) in enumerate(zip(parsed, matches)):
        change = "added" if old_node is None else "unchanged" if old_node.content_hash == p.content_hash else "modified"
        row = Node(version_id=ver.id, logical_id=old_node.logical_id if old_node else uuid.uuid4().hex[:12],
                   parent_id=rows[p.parent].id if p.parent is not None else None, position=i,
                   number=p.number, heading=p.heading, level=p.level, page=p.page, body=p.body,
                   blocks=p.blocks, content_hash=p.content_hash, warnings=p.warnings,
                   change=change, matched_by=rule)
        db.add(row)
        db.flush()
        rows.append(row)
        if change == "unchanged":
            report["unchanged"] += 1
        else:
            entry = {"node_id": row.id, "number": row.number, "heading": row.heading}
            if old_node is not None:
                entry |= {"matched_by": rule, **change_summary(old_node.text, row.text)}
            report[change].append(entry)
    matched = {o.id for o, _ in matches if o}
    report["removed"] = [{"node_id": o.id, "number": o.number, "heading": o.heading} for o in old if o.id not in matched]
    db.commit()
    return {"version": ver.number, "title": title, "warnings": warnings, "nodes": len(rows), **report}


def match(old: list[Node], new: list) -> list[tuple[Node | None, str | None]]:
    """For each newly parsed node, the previous-version node it continues (or None) and the rule used.

    1. same section number + heading  (normal case; disambiguates the duplicate "Error Codes")
    2. same heading under the same matched parent  (section renumbered)
    3. identical content  (section renamed/moved, text untouched)
    Anything else is a new logical node, and the unmatched old node is reported as removed.
    """
    free = list(old)
    out: list = [(None, None)] * len(new)

    def take(i, pred, rule):
        for o in free:
            if pred(o):
                free.remove(o)
                out[i] = (o, rule)
                return

    for i, n in enumerate(new):
        take(i, lambda o: (o.number, o.heading.casefold()) == (n.number, n.heading.casefold()), "number+heading")
    for i, n in enumerate(new):
        if out[i][0] is None:
            parent_old = out[n.parent][0] if n.parent is not None else None
            take(i, lambda o: o.heading.casefold() == n.heading.casefold()
                 and o.parent_id == (parent_old.id if parent_old else None), "heading")
    for i, n in enumerate(new):
        if out[i][0] is None:
            take(i, lambda o: o.content_hash == n.content_hash, "content")
    return out


def change_summary(old: str, new: str) -> dict:
    """Lightweight diff. severity is 'material' when a number, unit, code or requirement word
    was added or removed, else 'wording'. Heuristic: see APPROACH.md for what it misses."""
    fo, fn = _facts(old), _facts(new)
    removed, added = sorted((fo - fn).elements()), sorted((fn - fo).elements())
    os_, ns = _sentences(old), _sentences(new)
    diff = [l for l in difflib.unified_diff(os_, ns, lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return {
        "severity": "material" if removed or added else "wording",
        "facts_removed": removed,
        "facts_added": added,
        "invalidated_facts": _invalidated(os_, ns),
        "similarity": round(difflib.SequenceMatcher(None, old.split(), new.split()).ratio(), 3),
        "diff": diff,
    }


def staleness(db: Session, gen: dict) -> dict:
    """Compare the text a generation was made from with the same logical nodes in the latest version.

    Per source node: current (hash equal) | changed (+ diff summary) | removed (no longer matched).
    Per test case, over the nodes it cites:
      stale   - a cited node was removed, or a fact this test itself asserts (value, code,
                requirement word) was dropped from its sentence by a rewrite or deletion;
      review  - a cited node changed, but not in anything this test asserts;
      current - every cited node is byte-for-byte the same text (hash equal).
    Nothing is auto-cleared: the token heuristic can miss a meaning change without numbers.
    """
    latest = latest_version(db, gen["document"])
    cur = {n.logical_id: n for n in nodes_of(db, latest.id)}
    by_id = {}
    for s in gen["sources"]:
        n = cur.get(s["logical_id"])
        if n is None:
            st = {"status": "removed"}
        elif n.content_hash == s["content_hash"]:
            st = {"status": "current", "current_node_id": n.id}
        else:
            st = {"status": "changed", "current_node_id": n.id, **change_summary(s["text"], n.text)}
        by_id[s["node_id"]] = {"node_id": s["node_id"], "number": s["number"], "heading": s["heading"], **st}

    cases = []
    for tc in gen["test_cases"]:
        cited = [by_id[i] for i in tc["source_node_ids"]]
        asserted = _anchors(_facts(" ".join([tc["title"], *tc["steps"], tc["expected_result"]])))
        touched = {f for c in cited for f in c.get("invalidated_facts", [])}
        broken = sorted(f for f in touched if _anchors([f]) & asserted)
        if broken or any(c["status"] == "removed" for c in cited):
            status = "stale"
        else:
            status = "review" if any(c["status"] == "changed" for c in cited) else "current"
        cases.append({**tc, "staleness": status, "invalidated_facts": broken,
                      "changed_sources": [c["number"] for c in cited if c["status"] != "current"]})
    worst = max((c["staleness"] for c in cases), key=["current", "review", "stale"].index)
    return {**gen, "test_cases": cases, "staleness": {
        "status": worst,
        "checked_against_version": latest.number,
        "generated_from_version": gen["version"],
        "sources": list(by_id.values()),
    }}


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+|\n+", text) if s.strip()]


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.split(), b.split()).ratio()


def _invalidated(old: list[str], new: list[str]) -> list[str]:
    """Facts an old sentence stated that its rewrite no longer states (all of them if it was deleted).
    3.2: '...in 40 mmHg increments up to 299 mmHg...' -> '...in 30 mmHg increments up to 299 mmHg...'
    invalidates only '40 mmHg', even though v2 still mentions 40 mmHg in an added sentence."""
    out = Counter()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if op in ("replace", "delete"):
            for s in old[i1:i2]:
                best = max(new[j1:j2], key=lambda t: _sim(s, t), default=None)
                # LIMIT: 0.5 word-overlap pairs a rewrite with its original; a rewrite that changes
                # more than half its words counts as deleted (all its facts invalidated), the safe side.
                out += _facts(s) - (_facts(best) if best and _sim(s, best) >= 0.5 else Counter())
    return sorted(out)


def _facts(text: str) -> Counter:
    return Counter(" ".join(m.group().lower().split()) for m in FACT.finditer(text))


def _anchors(facts) -> set[str]:
    # "2 seconds" and "2 second" are the same claim. LIMIT: "2 s" vs "2 seconds" won't match,
    # which downgrades stale -> review (never -> current); add a unit synonym table if that bites.
    return {f.rstrip("s") for f in facts}
