"""Prompt, call, and validate. A reply is accepted only if it parses into TestCaseSet and every
cited node id belongs to the selection; otherwise the model is told why and asked again."""
import json
import os
import re
import time

import httpx
from pydantic import BaseModel, Field, ValidationError

PROMPT_VERSION = "tc-v1"
MAX_ATTEMPTS = 3
API_KEY = os.environ.get("LLM_API_KEY")
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")  # any OpenAI-compatible API
MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile") if API_KEY else "offline-stub"

SYSTEM = """You are a QA engineer writing verification test cases for a regulated medical device.
Use ONLY the specification text you are given. Never invent thresholds, timings, codes or behaviour
that the text does not state. Each test case must be executable by another engineer: concrete
steps and an observable, measurable expected result quoting the exact values from the text.

Reply with ONE JSON object and nothing else, in exactly this shape:
{"test_cases": [{"title": str, "steps": [str, ...], "expected_result": str, "source_node_ids": [int, ...]}]}
Rules: 3 to 5 test cases. source_node_ids lists the [node N] ids whose text the test verifies;
use only ids that appear in the input. Prefer boundary and failure-mode tests over happy paths."""


class TestCase(BaseModel):
    title: str = Field(min_length=5, max_length=200)
    steps: list[str] = Field(min_length=1, max_length=12)
    expected_result: str = Field(min_length=5)
    source_node_ids: list[int] = Field(min_length=1)


class TestCaseSet(BaseModel):
    test_cases: list[TestCase] = Field(min_length=3, max_length=5)


class GenerationFailed(Exception):
    def __init__(self, errors: list[str], last_raw: str | None):
        super().__init__("; ".join(errors))
        self.errors, self.last_raw = errors, last_raw


def build_prompt(sources: list[dict]) -> str:
    parts = [f"[node {s['node_id']}] Section {s['number']} {s['text']}" for s in sources]
    return "Specification sections:\n\n" + "\n\n".join(parts) + "\n\nWrite the test cases now."


def generate(sources: list[dict]) -> tuple[TestCaseSet, int, str]:
    """Returns (validated set, attempts used, raw reply). Raises GenerationFailed after MAX_ATTEMPTS."""
    allowed = {s["node_id"] for s in sources}
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": build_prompt(sources)}]
    errors, raw = [], None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = complete(messages)
        except httpx.HTTPError as e:  # rate limit / timeout / 5xx: back off and retry the same request
            errors.append(f"attempt {attempt}: transport error: {e!r}"[:300])
            time.sleep(2 ** attempt)
            continue
        try:
            result = TestCaseSet.model_validate(json.loads(_strip_fences(raw)))
            unknown = {i for tc in result.test_cases for i in tc.source_node_ids} - allowed
            if unknown:
                raise ValueError(f"source_node_ids {sorted(unknown)} are not in the input; allowed: {sorted(allowed)}")
            return result, attempt, raw
        except (ValueError, ValidationError) as e:  # JSONDecodeError is a ValueError
            errors.append(f"attempt {attempt}: {e}"[:500])
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Your reply was rejected: {e}\nReply again with ONLY the corrected JSON object."}]
    raise GenerationFailed(errors, raw)


def _strip_fences(raw: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    return m.group(1) if m else raw


def complete(messages: list[dict]) -> str:
    if not API_KEY:
        return _offline_stub(messages[1]["content"])
    r = httpx.post(f"{BASE_URL}/chat/completions", timeout=60,
                   headers={"Authorization": f"Bearer {API_KEY}"},
                   json={"model": MODEL, "messages": messages, "temperature": 0.2,
                         "response_format": {"type": "json_object"}})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _offline_stub(prompt: str) -> str:
    """No API key: deterministic stand-in that turns sentences with numbers into test cases, so the
    pipeline (validation, storage, staleness) runs end to end offline. Not an LLM; labelled as such."""
    cases = []
    for node_id, body in re.findall(r"\[node (\d+)\] Section \S+ (.*?)(?=\n\n\[node |\n\nWrite the test)", prompt, re.S):
        heading, _, text = body.partition("\n")
        for s in re.split(r"(?<=[.;])\s+|\n", text):
            if re.search(r"\d", s) and len(cases) < 5:
                cases.append({"title": f"{heading}: verify '{s[:60]}'",
                              "steps": [f"Set up the device per section '{heading}'.", f"Exercise the condition: {s}"],
                              "expected_result": s, "source_node_ids": [int(node_id)]})
    while len(cases) < 3:  # pad so a numberless selection still yields a valid set
        cases.append({"title": "Review section manually", "steps": ["Read the section text."],
                      "expected_result": "Behaviour matches the text.", "source_node_ids": [int(re.search(r"\[node (\d+)\]", prompt)[1])]})
    return json.dumps({"test_cases": cases})
