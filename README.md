# CT-200 manual → versioned tree → traceable QA test cases

A FastAPI backend that parses the CardioTrack CT-200 manual PDF into a section tree, stores every
re-ingested version without overwriting old ones, generates QA test cases from user-selected sections
with an LLM, and tells you at retrieval time whether those test cases still match the latest manual.

Design, trade-offs and the decision log are in [APPROACH.md](APPROACH.md).

## Setup

Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DB_URL` | `sqlite:///ct200.db` | SQLAlchemy URL for the tree, versions and selections |
| `GENERATIONS_DIR` | `generations` | JSON document store for LLM output |
| `LLM_API_KEY` | *(unset)* | Key for an OpenAI-compatible chat API. **Unset → offline stub** (deterministic, labelled `model: offline-stub`) |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint (Groq, OpenRouter, Gemini's OpenAI endpoint, ...) |
| `LLM_MODEL` | `llama-3.3-70b-versatile` | Model name for that endpoint |

Example with Groq's free tier:

```bash
export LLM_API_KEY=gsk_...   # PowerShell: $env:LLM_API_KEY="gsk_..."
```

## Run

```bash
uvicorn main:app --reload
```

Interactive docs: http://127.0.0.1:8000/docs

## Test

```bash
pytest -q
```

- `tests/test_parser.py`: one test per structural irregularity in the manual (duplicate heading,
  skipped level, out-of-order numbering, body-sized heading versus numbered list, tables, page breaks
  and Unicode).
- `tests/test_api.py`: the full v1 → selection → generation → v2 → staleness flow through the API,
  plus the LLM failure policy (malformed reply, unknown citation or invented value → retry; ×3 → 502, nothing stored;
  duplicate request → reused) and claim-level staleness (the E3 test goes stale, the E1 test needs only review).

## The v1 → v2 re-ingestion flow

The quickest way is the scripted walkthrough. With the server running:

```bash
python demo.py
```

It ingests v1, browses the tree, creates a selection, generates test cases, re-ingests v2, and then
shows the node diff, the still-pinned old selection and the staleness of each test case. Each run
uses a fresh document name, so you can re-run it.

The same flow by hand with curl:

```bash
# 1. ingest v1 (raw PDF body)
curl -X POST --data-binary @data/ct200_manual.pdf http://127.0.0.1:8000/documents/ct200/versions

# 2. browse
curl http://127.0.0.1:8000/documents/ct200/sections
curl "http://127.0.0.1:8000/documents/ct200/search?q=error%20codes"
curl http://127.0.0.1:8000/nodes/13

# 3. select section 3.1 (id 9) and section 4 with its subsections (id 13)
curl -X POST http://127.0.0.1:8000/selections -H "Content-Type: application/json" \
     -d '{"name": "alarms", "node_ids": [9, 13]}'

# 4. generate test cases (a second identical call returns the stored one; add ?force=true for a new one)
curl -X POST http://127.0.0.1:8000/selections/1/generations

# 5. ingest v2: the response lists added / modified (with diff summary) / removed sections
curl -X POST --data-binary @data/ct200_manual_v2.pdf http://127.0.0.1:8000/documents/ct200/versions

# 6. what changed, and are the test cases still valid?
curl http://127.0.0.1:8000/nodes/15/history            # 4.2 Error Codes: changed, with diff
curl http://127.0.0.1:8000/selections/1                # still the exact v1 text
curl http://127.0.0.1:8000/selections/1/generations    # staleness per source and per test case
curl http://127.0.0.1:8000/nodes/15/generations        # retrieval by node
```

Node ids above assume an empty database; take real ids from the responses.

## API

| Method | Path | |
|---|---|---|
| POST | `/documents/{doc}/versions` | Ingest a PDF (raw body) as the next version; identical file → no-op |
| GET | `/documents/{doc}/versions` | List versions |
| GET | `/documents/{doc}/sections?version=` | Top-level sections (default: latest version) |
| GET | `/documents/{doc}/search?q=&version=` | Substring search over headings and body text |
| GET | `/nodes/{id}` | Node with children, full text, blocks (paragraph/list/table), content hash, parser warnings |
| GET | `/nodes/{id}/history` | The same logical section in every version, with a diff summary wherever it changed |
| POST | `/selections` | `{name, node_ids, include_descendants=true}` → version-pinned selection |
| GET | `/selections/{id}` | The selection with the exact text it pins |
| POST | `/selections/{id}/generations?force=false` | Generate 3–5 test cases (see the reuse policy in APPROACH.md) |
| GET | `/selections/{id}/generations` | Generations for a selection, each with staleness |
| GET | `/nodes/{id}/generations` | Generations that used this section in any version, with staleness |
| GET | `/generations/{id}` | One generation with staleness |

## Layout

```
pdf_tree.py     PDF → section nodes (text layer, hierarchy, irregularity warnings)
versioning.py   ingest, cross-version matching, change classification, staleness
store.py        SQLAlchemy models (SQLite) + JSON-file generation store
llm.py          prompt, OpenAI-compatible call, schema validation and retry, offline stub
main.py         FastAPI routes
demo.py         end-to-end walkthrough against a running server
data/           ct200_manual.pdf (v1), ct200_manual_v2.pdf (v2)
```
