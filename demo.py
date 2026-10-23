"""End-to-end walk through a running server: v1 -> selection -> generation -> v2 -> staleness.

    uvicorn main:app            # in one terminal
    python demo.py              # in another (BASE_URL overrides http://127.0.0.1:8000)

Uses a fresh document name per run, so it can be re-run against the same database.
"""
import os
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8")
api = httpx.Client(base_url=os.environ.get("BASE_URL", "http://127.0.0.1:8000"), timeout=120)
DOC = f"ct200-demo-{int(time.time())}"


def call(method, url, **kw):
    r = api.request(method, url, **kw)
    if r.is_error:
        sys.exit(f"{method} {url} -> {r.status_code}: {r.text}")
    return r.json()


def step(msg):
    print(f"\n=== {msg}")


step(f"1. Ingest v1 as document {DOC!r}")
v1 = call("POST", f"/documents/{DOC}/versions", content=open("data/ct200_manual.pdf", "rb").read())
print(f"version {v1['version']}: {v1['nodes']} nodes, title: {v1['title']}")

step("2. Browse: top-level sections, then section 4 with children")
top = call("GET", f"/documents/{DOC}/sections")["sections"]
for s in top:
    print(f"  [{s['id']}] {s['number']} {s['heading']}")
sec4 = call("GET", f"/nodes/{top[3]['id']}")
print("  children of 4:", [f"{c['number']} {c['heading']}" for c in sec4["children"]])
flagged = [n for n in call("GET", f"/documents/{DOC}/search", params={"q": ""})["results"] if n["warnings"]]
print("  parser warnings:", {n["number"]: n["warnings"] for n in flagged})
by_num = {n["number"]: n["id"] for n in call("GET", f"/documents/{DOC}/search", params={"q": ""})["results"]}

step("3. Search 'error codes' (duplicate heading -> two distinct nodes)")
for n in call("GET", f"/documents/{DOC}/search", params={"q": "error codes"})["results"]:
    print(f"  [{n['id']}] {n['number']} {n['heading']}")

step("4. Create a selection: 3.1 + section 4 (descendants included)")
sel = call("POST", "/selections", json={"name": "alarms-and-power-on", "node_ids": [by_num["3.1"], by_num["4"]]})
print(f"selection {sel['id']} pinned to v{sel['version']}:", [i["number"] for i in sel["items"]])

step("5. Generate test cases (then submit the same request again)")
gen = call("POST", f"/selections/{sel['id']}/generations")
print(f"generation {gen['id']} model={gen['model']} attempts={gen['attempts']}")
for tc in gen["test_cases"]:
    print(f"  {tc['id']} [{tc['staleness']}] {tc['title']}")
again = call("POST", f"/selections/{sel['id']}/generations")
print(f"second request: reused={again['reused']} same id={again['id'] == gen['id']}")

step("6. Re-ingest the modified manual as v2")
v2 = call("POST", f"/documents/{DOC}/versions", content=open("data/ct200_manual_v2.pdf", "rb").read())
print(f"version {v2['version']}: unchanged={v2['unchanged']} added={[a['number'] for a in v2['added']]} "
      f"removed={v2['removed']}")
for m in v2["modified"]:
    print(f"  modified {m['number']} {m['heading']}: {m['severity']}  -{m['facts_removed']} +{m['facts_added']}")

step("7. Node history for 4.2 (v1 node id) and 3.1")
h = call("GET", f"/nodes/{by_num['4.2']}/history")
print(f"  4.2 changed={h['changed']}; diff:")
for line in h["versions"][-1]["diff_vs_previous"]["diff"]:
    print("     ", line)
h31 = call("GET", f"/nodes/{by_num['3.1']}/history")
print(f"  3.1 changed={h31['changed']} (v1 splits it across a page break; v2 does not)")

step("8. Old selection still resolves to the exact v1 text")
old = call("GET", f"/selections/{sel['id']}")
print(f"  pinned to v{old['version']}; 4.2 row E3:",
      next(l for l in next(i for i in old["items"] if i["number"] == "4.2")["text"].splitlines() if l.startswith("E3")))

step("9. Retrieve the generation by selection: staleness against latest version")
g = call("GET", f"/selections/{sel['id']}/generations")[0]
print(f"  overall: {g['staleness']['status']} (generated from v{g['staleness']['generated_from_version']}, "
      f"checked against v{g['staleness']['checked_against_version']})")
for s in g["staleness"]["sources"]:
    print(f"    source {s['number']:<4} {s['status']:<8} {s.get('severity', '')}")
for tc in g["test_cases"]:
    print(f"  {tc['id']} -> {tc['staleness']:<8} changed sources: {tc['changed_sources']}")

step("10. Retrieve by node: generations that used section 4.2 (queried with its v2 node id)")
v2_42 = h["versions"][-1]["node_id"]
print("  ", [(x["id"], x["staleness"]["status"]) for x in call("GET", f"/nodes/{v2_42}/generations")])
