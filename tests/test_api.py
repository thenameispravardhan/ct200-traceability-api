"""v1 -> select -> generate -> v2 -> staleness, plus the LLM failure policy. One module-scoped DB."""
import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["GENERATIONS_DIR"] = f"{_tmp}/generations"
os.environ.pop("LLM_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

import llm  # noqa: E402
from main import app  # noqa: E402

c = TestClient(app)
pdf = lambda name: open(f"data/{name}", "rb").read()  # noqa: E731
S = {}  # state shared by the ordered tests below


def nid(number, version=None):
    q = c.get("/documents/ct200/search", params={"q": ""} | ({"version": version} if version else {})).json()["results"]
    return next(n["id"] for n in q if n["number"] == number)


def test_ingest_v1_and_browse():
    r = c.post("/documents/ct200/versions", content=pdf("ct200_manual.pdf"))
    assert r.status_code == 201 and r.json()["nodes"] == 27
    top = c.get("/documents/ct200/sections").json()["sections"]
    assert [s["number"] for s in top] == [str(i) for i in range(1, 9)]
    sec4 = c.get(f"/nodes/{top[3]['id']}").json()
    assert [ch["number"] for ch in sec4["children"]] == ["4.1", "4.2", "4.3"]
    assert c.post("/documents/ct200/versions", content=b"not a pdf").status_code == 415


def test_selection_is_version_pinned_and_idempotent():
    body = {"name": "alarms", "node_ids": [nid("3.1"), nid("4")]}  # 4 expands to 4.1-4.3
    s = c.post("/selections", json=body).json()
    assert [i["number"] for i in s["items"]] == ["3.1", "4", "4.1", "4.2", "4.3"] and s["version"] == 1
    assert c.post("/selections", json=body).json() == {**s, "reused": True}
    S["sel"] = s


def test_generation_validates_retries_and_dedupes(monkeypatch):
    sel = S["sel"]
    good = json.loads(llm._offline_stub(llm.build_prompt(sel["items"])))
    replies = iter(["Sure! Here are your tests:", json.dumps({"test_cases": good["test_cases"][:1]}), json.dumps(good)])
    monkeypatch.setattr(llm, "complete", lambda messages: next(replies))
    g = c.post(f"/selections/{sel['id']}/generations").json()
    assert g["attempts"] == 3 and 3 <= len(g["test_cases"]) <= 5 and g["staleness"]["status"] == "current"
    assert c.post(f"/selections/{sel['id']}/generations").json()["id"] == g["id"]  # reused, LLM not called
    S["gen"] = g


def tc(expected, node, title="Inflation behaviour"):
    return {"title": title, "steps": ["Start a measurement."], "expected_result": expected, "source_node_ids": [node]}


def test_invalid_output_is_rejected_and_not_stored(monkeypatch):
    n41 = nid("4.1")
    replies = iter([
        json.dumps({"test_cases": [tc("Valve vents the cuff", 999)] * 3}),                       # unknown node
        json.dumps({"test_cases": [tc("Cuff vented within 5 seconds", n41)] * 3}),               # invented value
        "```json\n{\"test_cases\": []}\n```",                                                    # too few
    ])
    monkeypatch.setattr(llm, "complete", lambda messages: next(replies))
    r = c.post(f"/selections/{S['sel']['id']}/generations", params={"force": True})
    errors = r.json()["detail"]["errors"]
    assert r.status_code == 502 and "999" in errors[0] and "['5']" in errors[1] and "at least 3" in errors[2]
    assert len(c.get(f"/selections/{S['sel']['id']}/generations").json()) == 1


def test_generation_for_inflation_section(monkeypatch):
    n32 = nid("3.2")
    sel = c.post("/selections", json={"name": "inflation", "node_ids": [n32]}).json()
    cases = [tc("Inflates in 40 mmHg increments up to 299 mmHg", n32),
             tc("Deflates in controlled steps of approximately 3 mmHg", n32),
             tc("Deflation steps are approximately 3 mmHg", n32)]
    monkeypatch.setattr(llm, "complete", lambda messages: json.dumps({"test_cases": cases}))
    S["gen32"] = c.post(f"/selections/{sel['id']}/generations").json()


def test_v2_reingest_flags_changes_and_staleness():
    r = c.post("/documents/ct200/versions", content=pdf("ct200_manual_v2.pdf")).json()
    assert r["version"] == 2 and r["removed"] == [] and [a["number"] for a in r["added"]] == ["5.3"]
    assert {m["number"] for m in r["modified"]} == {"2.1.1.1", "3.2", "4.2", "4.3"}
    assert c.post("/documents/ct200/versions", content=pdf("ct200_manual_v2.pdf")).json()["reused"] is True

    h = c.get(f"/nodes/{nid('4.2', 1)}/history").json()
    assert h["changed"] and "1.5 seconds" in h["versions"][1]["diff_vs_previous"]["facts_added"]
    assert c.get(f"/nodes/{nid('3.1', 1)}/history").json()["changed"] is False
    assert nid("3.1", 1) != nid("3.1", 2)  # new rows per version, same logical id

    sel = c.get(f"/selections/{S['sel']['id']}").json()
    assert sel["items"] == S["sel"]["items"]  # still resolves to the v1 text

    g = c.get(f"/generations/{S['gen']['id']}").json()
    st = {s["number"]: s for s in g["staleness"]["sources"]}
    assert g["staleness"]["status"] == "stale" and g["staleness"]["checked_against_version"] == 2
    assert st["3.1"]["status"] == "current" and st["4.2"]["severity"] == "material"
    # Claim-level: in table 4.2 only the E3 row changed, so only the test asserting "2 seconds" is stale;
    # tests of the untouched E1/E2 rows need review, tests of unchanged sections stay current.
    for case in g["test_cases"]:
        cited = {s["number"] for s in g["staleness"]["sources"] if s["node_id"] in case["source_node_ids"]}
        want = "current" if not cited & {"4.2", "4.3"} else "stale" if "2 seconds" in case["expected_result"] else "review"
        assert case["staleness"] == want, case
    assert [x["id"] for x in c.get(f"/nodes/{nid('4.2', 2)}/generations").json()] == [S["gen"]["id"]]

    # 3.2 still contains "40 mmHg" in v2, but the increment sentence was rewritten: that test is stale.
    g32 = c.get(f"/generations/{S['gen32']['id']}").json()
    assert [t["staleness"] for t in g32["test_cases"]] == ["stale", "review", "review"]
    assert "40 mmhg" in g32["test_cases"][0]["invalidated_facts"]
