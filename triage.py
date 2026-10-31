"""LLM gateway: classify each failure from its filtered log, with a local and a cloud model.

    python3 triage.py            # every model that is available
    python3 triage.py ollama     # just one: ollama | openrouter | claude
    python3 triage.py serve [log.jsonl]   # production path: self-hosted server model, API model as fallback

Run with .venv/bin/python: it has the OpenTelemetry SDK. Each failure becomes one trace
(filter -> llm attempt(s) -> verify), sent over OTLP to Jaeger (http://localhost:16686).
Writes data/failures/<job>/triage-<model>.json; already-triaged (job, model) pairs are skipped.
"""
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from opentelemetry import trace

from logfilter import filter_log, render
from verify import shown_lines, verify

DATA = Path(__file__).parent / "data"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:11435")  # llm-server link (docker-compose.yml) -> GCP VM
SERVER_MODEL = "llama3"
SERVER_TIMEOUT = float(os.environ.get("SERVER_TIMEOUT", "120"))  # 8B takes ~42 s median, ~100 s worst on CPU
SERVER_COOLDOWN = 30  # s to skip the server after it failed, then probe it again
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")  # cloud model, free tier
API_NAME = "openrouter-" + OPENROUTER_MODEL.split("/")[-1].removesuffix(":free")
CLAUDE_MODEL = "claude-haiku-5-5"
CLAUDE_PRICE = (0.10, 0.50)  # $ per 1M input / output tokens (prompts under 100K tokens)
tracer = trace.get_tracer("triage")

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
    # ponytail: TTFT from Ollama's own timings (load + prompt processing), not streaming; excludes network time
    ns = 1e9
    extra = {"ttft_s": round((resp.get("load_duration", 0) + resp.get("prompt_eval_duration", 0)) / ns, 2)}
    if resp.get("eval_duration"):
        extra["tokens_per_s"] = round(resp["eval_count"] / (resp["eval_duration"] / ns), 1)
    return json.loads(resp["message"]["content"]), resp.get("prompt_eval_count", 0), resp.get("eval_count", 0), 0.0, extra


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
    return json.loads(text), i, o, (i * CLAUDE_PRICE[0] + o * CLAUDE_PRICE[1]) / 1e6, {}


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
    # free model; no TTFT without streaming
    return json.loads(resp["choices"][0]["message"]["content"]), u["prompt_tokens"], u["completion_tokens"], 0.0, {}


def available():
    models = {}
    if os.environ.get("OPENROUTER_API_KEY"):
        models[API_NAME] = ask_openrouter
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


@contextmanager
def job_span(job_dir):
    """Root span of one failure's trace, with its Prow metadata; re-runs the filter (~0.1 s) as a child span.

    Yields (root span, filtered log). Fetching happened earlier in collector.py, so it is an attribute, not a span.
    """
    meta = json.loads((job_dir / "meta.json").read_text())
    pull = (meta["refs"].get("pulls") or [{}])[0]
    attrs = {"ci.job": meta["job"], "ci.build_id": meta["build_id"], "ci.repo": meta["refs"].get("repo") or meta["type"],
             "ci.url": meta["url"], "ci.pr": pull.get("number"), "ci.pr_title": pull.get("title"),
             "ci.log_bytes": (job_dir / "build-log.txt").stat().st_size, "ci.fetch_s": meta.get("fetch_s")}
    with tracer.start_as_current_span(f"triage {job_dir.name}", attributes={k: v for k, v in attrs.items() if v is not None}) as root:
        with tracer.start_as_current_span("filter") as s:
            text = (job_dir / "build-log.txt").read_text(errors="replace")
            kept = filter_log(text)
            n = len(text.splitlines())
            s.set_attributes({"filter.lines_in": n, "filter.lines_out": len(kept),
                              "filter.reduction": round(1 - len(kept) / n, 4) if n else 0.0})
        yield root, render(kept)


def llm_call(ask, log, name, tier, **kw):
    """One LLM attempt as a span with the GenAI attributes; a raised error marks the span failed and propagates."""
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.request.model": name, "llm.tier": tier}
    with tracer.start_as_current_span(f"llm {tier} {name}", attributes=attrs) as s:
        t0 = time.perf_counter()
        answer, tok_in, tok_out, cost, extra = ask(log, **kw)
        s.set_attributes({"gen_ai.usage.input_tokens": tok_in, "gen_ai.usage.output_tokens": tok_out, "llm.cost_usd": cost,
                          "llm.latency_s": round(time.perf_counter() - t0, 2), "triage.category": answer["category"],
                          **{f"llm.{k}": v for k, v in extra.items()}})
        return answer, tok_in, tok_out, cost, extra


def verify_span(cited, log):
    with tracer.start_as_current_span("verify") as s:
        citations, validity = verify(cited, shown_lines(log))
        s.set_attributes({"verify.cited": len(citations), "verify.found": sum(c["found"] for c in citations)}
                         | ({"verify.validity": validity} if validity is not None else {}))
        return citations, validity


def trace_id(span):
    return format(span.get_span_context().trace_id, "032x")  # paste into Jaeger's search box


def ask_server(log, url, model, timeout):
    # WSL drops (not refuses) connections to a dead localhost port; don't wait SERVER_TIMEOUT to notice
    u = urllib.parse.urlsplit(url)
    socket.create_connection((u.hostname, u.port), timeout=2).close()
    return ask_ollama(log, url, model, timeout)


def serve(log_path):
    """Triage every failure once: self-hosted server model first, the API model if the server errors or is too slow.

    Appends one JSON line per job to log_path, flushed right away so a dashboard can follow it live.
    """
    server_down_until = 0  # circuit breaker: after a failure, skip the server for SERVER_COOLDOWN s
    attempts = [("server", SERVER_MODEL, ask_server, {"url": SERVER_URL, "model": SERVER_MODEL, "timeout": SERVER_TIMEOUT}),
                ("api", API_NAME, ask_openrouter, {})]
    with open(log_path, "a") as out:
        for job_dir in sorted(p.parent for p in DATA.glob("failures/*/filtered.txt")):
            rec = {"job": job_dir.name, "t_start": time.time(), "fallback": False, "error": None, "served_by": None}
            with job_span(job_dir) as (root, log):
                rec["trace_id"] = trace_id(root)
                for tier, model, ask, kw in attempts:
                    if tier == "server" and time.time() < server_down_until:
                        rec.update(fallback=True, error=f"{model}: circuit open")
                        root.add_event("server skipped: circuit open")
                        continue
                    try:
                        answer = llm_call(ask, log, model, tier, **kw)[0]
                        rec["served_by"] = model
                        break
                    except Exception as e:
                        rec.update(fallback=True, error=f"{model}: {type(e).__name__}: {e}"[:300])
                        if tier == "server":
                            server_down_until = time.time() + SERVER_COOLDOWN
                if rec["served_by"]:
                    rec["category"] = answer["category"]
                    rec["citation_validity"] = verify_span(answer["cited_lines"], log)[1]
                root.set_attributes({"triage.fallback": rec["fallback"], "triage.served_by": rec["served_by"] or "none"})
            rec["t_end"] = time.time()
            rec["latency_s"] = round(rec["t_end"] - rec["t_start"], 2)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            print(f"{rec['job']:45} {str(rec['served_by']):12} {'FALLBACK' if rec['fallback'] else '':8} {rec['latency_s']:>6}s")


def init_tracing():
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "metal3-triage"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # OTEL_EXPORTER_OTLP_ENDPOINT, default localhost:4318
    trace.set_tracer_provider(provider)
    return provider


def main():
    provider = init_tracing()
    try:
        if sys.argv[1:2] == ["serve"]:
            serve(sys.argv[2] if len(sys.argv) > 2 else DATA / "serve.jsonl")
        else:
            batch(sys.argv[1] if len(sys.argv) > 1 else None)
    finally:
        provider.shutdown()  # flush spans still queued


def batch(only):
    models = available()
    if only:
        models = {k: v for k, v in models.items() if k.startswith(only) or only in k}
    for job_dir in sorted(p.parent for p in DATA.glob("failures/*/filtered.txt")):
        todo = {n: a for n, a in models.items() if not (job_dir / f"triage-{n.replace(':', '-')}.json").exists()}
        if not todo:
            continue
        with job_span(job_dir) as (root, log):
            for name, ask in todo.items():
                t0 = time.perf_counter()
                try:
                    answer, tok_in, tok_out, cost, extra = llm_call(ask, log, name, "local" if ask is ask_ollama else "cloud")
                except Exception as e:  # one bad call must not stop the batch
                    print(f"FAIL {job_dir.name} {name}: {e}")
                    continue
                citations, validity = verify_span(answer["cited_lines"], log)
                result = {"model": name, **answer, "latency_s": round(time.perf_counter() - t0, 2), "input_tokens": tok_in,
                          "output_tokens": tok_out, "cost_usd": cost, **extra, "citations": citations,
                          "citation_validity": validity, "trace_id": trace_id(root)}
                (job_dir / f"triage-{name.replace(':', '-')}.json").write_text(json.dumps(result, indent=2))
                print(f"{job_dir.name:45} {name:20} {answer['category']:14} {result['latency_s']:>6}s  {answer['summary'][:70]}")


if __name__ == "__main__":
    main()
