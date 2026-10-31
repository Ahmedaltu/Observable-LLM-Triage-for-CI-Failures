"""LLM gateway: classify each failure from its filtered log, with a local and a cloud model.

    python3 triage.py            # every model that is available
    python3 triage.py ollama     # just one: ollama | openrouter | claude
    python3 triage.py serve [log.jsonl]   # production path: server model, edge model as fallback

Writes data/failures/<job>/triage-<model>.json; already-triaged (job, model) pairs are skipped.
"""
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATA = Path(__file__).parent / "data"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:11435")  # llm-server container (docker-compose.yml)
SERVER_MODEL = "llama3"
SERVER_TIMEOUT = float(os.environ.get("SERVER_TIMEOUT", "120"))  # 8B takes ~42 s median, ~100 s worst on CPU
SERVER_COOLDOWN = 30  # s to skip the server after it failed, then probe it again
EDGE_MODEL = "llama3.2:3b"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")  # cloud model, free tier
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


def ask_ollama(log, url=OLLAMA_URL, model=OLLAMA_MODEL, timeout=600):
    body = {"model": model, "stream": False, "format": SCHEMA, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": log}]}
    req = urllib.request.Request(f"{url}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


def ask_openrouter(log):
    body = {"model": OPENROUTER_MODEL, "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {"name": "triage", "strict": True, "schema": SCHEMA}},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": log}]}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"})
    for attempt in range(4):  # free tier answers 429 when busy
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))
    u = resp["usage"]
    return json.loads(resp["choices"][0]["message"]["content"]), u["prompt_tokens"], u["completion_tokens"], 0.0  # free model


def available():
    models = {}
    if os.environ.get("OPENROUTER_API_KEY"):
        models["openrouter-" + OPENROUTER_MODEL.split("/")[-1].removesuffix(":free")] = ask_openrouter
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


def serve(log_path):
    """Triage every failure once: server model first, edge model if the server errors or is too slow.

    Appends one JSON line per job to log_path, flushed right away so a dashboard can follow it live.
    """
    from verify import shown_lines, verify

    server_down_until = 0  # circuit breaker: after a failure, skip the server for SERVER_COOLDOWN s
    with open(log_path, "a") as out:
        for filtered in sorted(DATA.glob("failures/*/filtered.txt")):
            log = filtered.read_text()
            rec = {"job": filtered.parent.name, "t_start": time.time(), "fallback": False, "error": None}
            for url, model in [(SERVER_URL, SERVER_MODEL), (OLLAMA_URL, EDGE_MODEL)]:
                if model == SERVER_MODEL and time.time() < server_down_until:
                    rec.update(fallback=True, error=f"{model}: circuit open")
                    continue
                try:
                    if model == SERVER_MODEL:
                        # WSL drops (not refuses) connections to a dead localhost port; don't wait SERVER_TIMEOUT to notice
                        u = urllib.parse.urlsplit(url)
                        socket.create_connection((u.hostname, u.port), timeout=2).close()
                    answer = ask_ollama(log, url, model, SERVER_TIMEOUT if model == SERVER_MODEL else 600)[0]
                    rec["served_by"] = model
                    break
                except Exception as e:
                    rec.update(fallback=True, error=f"{model}: {type(e).__name__}: {e}"[:300])
                    if model == SERVER_MODEL:
                        server_down_until = time.time() + SERVER_COOLDOWN
            else:
                rec["served_by"] = None  # both down
            rec["t_end"] = time.time()
            rec["latency_s"] = round(rec["t_end"] - rec["t_start"], 2)
            if rec["served_by"]:
                rec["category"] = answer["category"]
                rec["citation_validity"] = verify(answer["cited_lines"], shown_lines(log))[1]
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"{rec['job']:45} {str(rec['served_by']):12} {'FALLBACK' if rec['fallback'] else '':8} {rec['latency_s']:>6}s")


def main():
    if sys.argv[1:2] == ["serve"]:
        serve(sys.argv[2] if len(sys.argv) > 2 else DATA / "serve.jsonl")
        return
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
