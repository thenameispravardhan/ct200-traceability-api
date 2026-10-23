"""CT-200 manual PDF -> flat list of section nodes with parent links.

The manual is born-digital (every page has a text layer that matches the
rendered page), so we read the text layer with PyMuPDF instead of OCR-ing
pixels: OCR could only add errors here. A page with no text layer raises
instead of silently yielding an empty section.
"""
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

import fitz

HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2})*)\.?\s+(\S.*)$")
LIST_ITEM = re.compile(r"^(?:\d{1,2}[.)]|[•●▪\-])\s+")
TITLE_MIN_SIZE = 18
PARA_GAP = 4  # pt of vertical whitespace that starts a new paragraph


def norm(s: str) -> str:
    # NFKC folds the ﬁ/ﬀ ligatures; U+2011 (non-breaking hyphen, as in "CT‑200") folds to U+2010.
    return unicodedata.normalize("NFKC", s).replace("‐", "-")


def squash(s: str) -> str:
    return " ".join(s.split())


@dataclass
class Node:
    number: str
    heading: str
    page: int
    parent: int | None = None  # index into the returned node list
    blocks: list = field(default_factory=list)  # paragraph / list / table, in reading order
    warnings: list = field(default_factory=list)

    @property
    def level(self) -> int:
        return self.number.count(".") + 1

    @property
    def body(self) -> str:
        out = []
        for b in self.blocks:
            if b["type"] == "paragraph":
                out.append(b["text"])
            elif b["type"] == "list":
                out.append("\n".join(b["items"]))
            else:
                out.append("\n".join(" | ".join(r) for r in b["rows"]))
        return "\n\n".join(out)

    @property
    def content_hash(self) -> str:
        # Whitespace-insensitive: a paragraph split by a page break in v1 but not in v2 is not a change.
        return hashlib.sha256(squash(self.heading + "\n" + self.body).encode()).hexdigest()


def _tables(page):
    # find_tables also reports every cell as its own tiny "table"; keep only outermost ones.
    found = page.find_tables().tables
    keep = [t for t in found if not any(o is not t and fitz.Rect(o.bbox).contains(fitz.Rect(t.bbox)) for o in found)]
    return [(fitz.Rect(t.bbox), [[squash(norm(c or "")) for c in row] for row in t.extract()]) for t in keep]


def _lines(page):
    for b in page.get_text("dict", sort=True)["blocks"]:
        for l in b.get("lines", []):
            spans = [s for s in l["spans"] if s["text"].strip()]
            if spans:
                yield {
                    "text": squash(norm("".join(s["text"] for s in l["spans"]))),
                    "size": max(s["size"] for s in spans),
                    "bold": all(s["flags"] & 16 or "Bold" in s["font"] for s in spans),
                    "x": l["bbox"][0], "y0": l["bbox"][1], "y1": l["bbox"][3],
                }


def parse(src: str | bytes):
    """Path or PDF bytes -> (title, nodes, doc_warnings). Nodes are in document order."""
    doc = fitz.open(src) if isinstance(src, str) else fitz.open(stream=src, filetype="pdf")
    title, nodes, doc_warnings = [], [], []
    margin = float("inf")  # x of the leftmost heading seen so far
    by_number: dict[str, int] = {}
    seen_heading: dict[str, str] = {}
    prev = None  # previous body line, for paragraph joining: (page, y1, text)

    for pno, page in enumerate(doc, 1):
        if not page.get_text().strip():
            raise ValueError(f"page {pno} has no text layer; scanned pages are not supported")
        if page.get_images():
            doc_warnings.append(f"page {pno}: embedded image(s) not extracted")
        tables = _tables(page)
        # ponytail: single-column layout assumed, so sorting by y is reading order.
        events = [(r.y0, "table", rows) for r, rows in tables]
        events += [(l["y0"], "line", l) for l in _lines(page)
                   if not any(r.contains(fitz.Point(l["x"] + 1, (l["y0"] + l["y1"]) / 2)) for r, _ in tables)]
        events.sort(key=lambda e: e[0])

        for _, kind, item in events:
            cur = nodes[-1] if nodes else None
            if kind == "table":
                if cur is None:
                    doc_warnings.append(f"page {pno}: table before first heading dropped")
                else:
                    cur.blocks.append({"type": "table", "rows": item})
                prev = None
                continue

            line = item
            m = HEADING.match(line["text"]) if line["bold"] else None
            if m:
                nodes.append(_new_node(m.group(1), m.group(2), pno, nodes, by_number, seen_heading))
                margin = min(margin, line["x"])
                prev = None
                continue
            if cur is None:
                if line["size"] >= TITLE_MIN_SIZE:
                    title.append(line["text"])
                else:
                    doc_warnings.append(f"page {pno}: text before first heading: {line['text'][:60]!r}")
                continue

            last = cur.blocks[-1] if cur.blocks else None
            if HEADING.match(line["text"]) and line["x"] <= margin + 2:
                # Not bold, so kept as body; but numbered list items are indented and this is not.
                cur.warnings.append(f"possible unstyled heading kept as body text: {line['text'][:60]!r}")
            if LIST_ITEM.match(line["text"]):
                if last is None or last["type"] != "list":
                    last = {"type": "list", "items": [], "x": line["x"]}
                    cur.blocks.append(last)
                last["items"].append(line["text"])
            elif last and last["type"] == "list" and line["x"] > last["x"] + 2:
                last["items"][-1] += " " + line["text"]  # wrapped list item
            elif last and last["type"] == "paragraph" and _continues(prev, pno, line):
                last["text"] += " " + line["text"]
            else:
                cur.blocks.append({"type": "paragraph", "text": line["text"]})
            prev = (pno, line["y1"], line["text"])

    for n in nodes:
        for b in n.blocks:
            b.pop("x", None)
    return " ".join(title), nodes, doc_warnings


def _continues(prev, pno, line) -> bool:
    if prev is None:
        return False
    ppage, py1, ptext = prev
    if ppage != pno:  # page break: same paragraph unless the previous line closed a sentence
        return not ptext.rstrip().endswith((".", ":", ";", "!", "?"))
    return line["y0"] - py1 < PARA_GAP


def _new_node(number, heading, pno, nodes, by_number, seen_heading) -> Node:
    node = Node(number, heading, pno)
    parts = number.split(".")
    idx = len(nodes)

    # Parent = closest existing numeric prefix, so "2.1.1.1" with no "2.1.1" lands under "2.1".
    for k in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:k])
        if prefix in by_number:
            node.parent = by_number[prefix]
            if k < len(parts) - 1:
                node.warnings.append(f"skipped level: no section {'.'.join(parts[:-1])}; attached to {prefix}")
            break
    else:
        if len(parts) > 1:
            node.warnings.append(f"orphan: no ancestor section for {number}; kept at top level")

    siblings = [n for n in nodes if n.parent == node.parent]
    if siblings and _key(siblings[-1].number) > _key(number):
        node.warnings.append(f"out of order: {number} appears after {siblings[-1].number}")
    if number in by_number:
        node.warnings.append(f"duplicate section number {number}")
    h = heading.casefold()
    if h in seen_heading:
        node.warnings.append(f"heading text duplicates section {seen_heading[h]}")
    seen_heading.setdefault(h, number)
    by_number[number] = idx
    return node


def _key(number: str):
    return tuple(int(p) for p in number.split("."))


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    t, ns, w = parse(sys.argv[1])
    print("TITLE:", t, "| doc warnings:", w)
    for n in ns:
        depth = 0
        p = n.parent
        while p is not None:
            depth, p = depth + 1, ns[p].parent
        print("  " * depth + f"[{n.number}] {n.heading}  p{n.page} {n.content_hash[:8]} {n.warnings or ''}")
        for line in n.body.splitlines():
            print("  " * depth + "    | " + line)
