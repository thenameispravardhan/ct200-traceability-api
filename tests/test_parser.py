"""One test per structural irregularity found in the CT-200 manual."""
from functools import cache

from pdf_tree import parse

V1, V2 = "data/ct200_manual.pdf", "data/ct200_manual_v2.pdf"


@cache
def tree(path):
    title, nodes, warnings = parse(path)
    return title, nodes, {n.number: n for n in nodes}, warnings


def num(nodes, n):
    return None if n.parent is None else nodes[n.parent].number


def test_duplicate_heading_gets_two_nodes_with_correct_parents():
    _, nodes, _, _ = tree(V1)
    dups = [n for n in nodes if n.heading == "Error Codes"]
    assert [n.number for n in dups] == ["4.2", "7.1"]
    assert [num(nodes, n) for n in dups] == ["4", "7"]
    assert dups[0].content_hash != dups[1].content_hash
    assert "duplicates section 4.2" in dups[1].warnings[0]


def test_skipped_level_attaches_to_nearest_ancestor_and_is_flagged():
    _, nodes, by, _ = tree(V1)
    n = by["2.1.1.1"]
    assert n.level == 4 and num(nodes, n) == "2.1"
    assert "no section 2.1.1" in n.warnings[0]


def test_out_of_order_numbering_keeps_document_order_and_parent():
    _, nodes, by, _ = tree(V1)
    sec3 = [n.number for n in nodes if num(nodes, n) == "3"]
    assert sec3 == ["3.1", "3.2", "3.4", "3.3"]
    assert "out of order" in by["3.3"].warnings[0]


def test_body_sized_bold_heading_detected_but_numbered_list_is_not():
    _, nodes, by, _ = tree(V1)
    assert by["3.2"].body.startswith("On starting a measurement")  # 3.2 is set in 11pt, same as body
    assert not any(n.heading.startswith(("Normal", "Elevated")) for n in nodes)
    lst = [b for b in by["3.3"].blocks if b["type"] == "list"][0]["items"]
    assert len(lst) == 5 and lst[4].endswith("seeking immediate medical attention")  # wrapped item joined


def test_tables_keep_multiline_cells_and_drop_cell_fragments():
    _, _, by, _ = tree(V2)
    tables = [b for b in by["4.2"].blocks if b["type"] == "table"]
    assert len(tables) == 1
    rows = tables[0]["rows"]
    assert rows[0] == ["Code", "Meaning", "Device Behavior"]
    assert rows[2] == ["E2", "Motion artifact detected during measurement", "Aborts measurement, displays E2, prompts retry"]
    assert rows[-1][0] == "E6"


def test_page_breaks_and_unicode_do_not_create_false_changes():
    _, _, b1, _ = tree(V1)
    title, _, b2, warnings = tree(V2)
    assert title.startswith("CardioTrack CT-200") and warnings == []
    # 4.3 wraps across a page mid-sentence in v2: still one paragraph.
    assert [b["type"] for b in b2["4.3"].blocks] == ["paragraph"]
    # 3.1 is split by a page break in v1 only; the content hash must not see that.
    assert b1["3.1"].content_hash == b2["3.1"].content_hash
    assert "CT-200" in b1["8.1"].body and "classified" in b1["8.1"].body  # U+2011 hyphen, ﬁ ligature
