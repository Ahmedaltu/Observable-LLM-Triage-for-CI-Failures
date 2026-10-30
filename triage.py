"""LLM gateway: classify each failure from its filtered log, with a local and a cloud model.

    python3 triage.py            # every model that is available
    python3 triage.py ollama     # just one: ollama | claude

Writes data/failures/<job>/triage-<model>.json; already-triaged (job, model) pairs are skipped.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).parent / "data"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
CLAUDE_MODEL = "claude-haiku-5-5"
CLAUDE_PRICE = (0.10, 0.50)  # $ per 1M input / output tokens (prompts under 100K tokens)

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["flaky_test", "infra_timeout", "code_bug", "config_error", "unknown"]},
        "summary": {"type": "string"},
        "cited_lines": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["category", "summary", "cited_lines"],
    "additionalProperties": False,
}
SYSTEM = """You triage failed CI jobs of the Metal3 project (Prow, Kubernetes).
You get an excerpt of the build log. Each line starts with its line number in the full log ("123: ...");
"..." marks lines that were cut out.

Answer with:
- category: one of
  flaky_test     - a test that fails intermittently, unrelated to the change
  infra_timeout  - CI infrastructure problem: timeouts, network, quota, missing resources, node/pod failures
  code_bug       - the change breaks build, lint, unit or e2e tests
  config_error   - wrong job, tooling, dependency or version configuration
  unknown        - the excerpt does not show why the job failed
- summary: one sentence naming the root cause.
- cited_lines: the line numbers (from the prefixes) that show the root cause. Cite only numbers that appear in the excerpt."""


def ask_ollama(log):
    body = {"model": OLLAMA_MODEL, "stream": False, "format": SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": log}]}
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        resp = json.load(r)
    return json.loads(resp["message"]["content"]), resp.get("prompt_eval_count", 0), resp.get("eval_count", 0), 0.0


def ask_claude(log):
    import anthropic  # imported here so the Ollama path works without the SDK

    resp = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=SYSTEM,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": log}],
    )
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"stop_reason={resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    i, o = resp.usage.input_tokens, resp.usage.output_tokens
    return json.loads(text), i, o, (i * CLAUDE_PRICE[0] + o * CLAUDE_PRICE[1]) / 1e6


def available():
    models = {}
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3)
        models[f"ollama-{OLLAMA_MODEL}"] = ask_ollama
    except OSError:
        print(f"ollama not reachable at {OLLAMA_URL}, skipped")
    try:
        import anthropic  # noqa: F401
        models[CLAUDE_MODEL] = ask_claude
    except ImportError:
        print("anthropic SDK not installed (pip install anthropic), claude skipped")
    return models


def main():
    models = available()
    if len(sys.argv) > 1:
        models = {k: v for k, v in models.items() if k.startswith(sys.argv[1]) or sys.argv[1] in k}
    for filtered in sorted(DATA.glob("failures/*/filtered.txt")):
        for name, ask in models.items():
            out = filtered.parent / f"triage-{name.replace(':', '-')}.json"
            if out.exists():
                continue
            t0 = time.perf_counter()
            try:
                answer, tok_in, tok_out, cost = ask(filtered.read_text())
            except Exception as e:  # one bad call must not stop the batch
                print(f"FAIL {filtered.parent.name} {name}: {e}")
                continue
            # ponytail: total latency only; time-to-first-token needs streaming, add with the tracing step
            result = {"model": name, **answer, "latency_s": round(time.perf_counter() - t0, 2),
                      "input_tokens": tok_in, "output_tokens": tok_out, "cost_usd": cost}
            out.write_text(json.dumps(result, indent=2))
            print(f"{filtered.parent.name:45} {name:20} {answer['category']:14} {result['latency_s']:>6}s  {answer['summary'][:70]}")


if __name__ == "__main__":
    main()
