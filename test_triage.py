"""Trace shape of one triaged failure; spans go to memory, no LLM or Jaeger needed.  .venv/bin/python test_triage.py"""
import json
import tempfile
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import triage

mem = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(mem))
trace.set_tracer_provider(provider)

job = Path(tempfile.mkdtemp()) / "failures" / "unit-1"
job.mkdir(parents=True)
(job / "meta.json").write_text(json.dumps({"job": "unit", "build_id": "1", "type": "periodic", "refs": {}, "url": "u"}))
(job / "build-log.txt").write_text("ok\n" * 300 + "make: *** Error 1\n")


def down(log, **kw):
    raise ConnectionRefusedError("server down")


def edge(log, **kw):
    return {"category": "code_bug", "summary": "s", "cited_lines": [301, 7]}, 100, 20, 0.0, {"ttft_s": 1.5}


with triage.job_span(job) as (root, log):
    try:
        triage.llm_call(down, log, "llama3", "server")
    except ConnectionRefusedError:
        pass
    answer = triage.llm_call(edge, log, "llama3.2:3b", "edge")[0]
    assert triage.verify_span(answer["cited_lines"], log)[1] == 0.5  # 301 shown, 7 cut by the filter

spans = {s.name: s for s in mem.get_finished_spans()}
assert set(spans) == {"triage unit-1", "filter", "llm server llama3", "llm edge llama3.2:3b", "verify"}
assert len({s.context.trace_id for s in spans.values()}) == 1                   # one trace per failure
assert all(s.parent.span_id == spans["triage unit-1"].context.span_id for n, s in spans.items() if n != "triage unit-1")
assert spans["llm server llama3"].status.status_code == trace.StatusCode.ERROR   # failed attempt visible
assert spans["llm edge llama3.2:3b"].attributes["gen_ai.usage.input_tokens"] == 100
assert spans["llm edge llama3.2:3b"].attributes["llm.ttft_s"] == 1.5
assert spans["filter"].attributes["filter.lines_in"] == 301
assert "ci.pr" not in spans["triage unit-1"].attributes                          # periodic: no PR, no None attribute
# serve(): server down -> edge answers, then the circuit breaker skips the server for the next job
(job.parent / "unit-2").mkdir()
for f in ("meta.json", "build-log.txt"):
    (job.parent / "unit-2" / f).write_text((job / f).read_text())
for d in (job, job.parent / "unit-2"):
    (d / "filtered.txt").write_text("")
triage.DATA = job.parent.parent
calls = []
triage.ask_server = lambda log, **kw: calls.append(kw["model"]) or down(log)
triage.ask_ollama = lambda log, **kw: calls.append(kw["model"]) or edge(log)
out = job.parent.parent / "serve.jsonl"
triage.serve(out)
recs = [json.loads(line) for line in out.read_text().splitlines()]
assert [r["served_by"] for r in recs] == ["llama3.2:3b", "llama3.2:3b"] and all(r["fallback"] for r in recs)
assert calls == ["llama3", "llama3.2:3b", "llama3.2:3b"]                       # 2nd job: server skipped
assert "circuit open" in recs[1]["error"] and recs[0]["trace_id"] != recs[1]["trace_id"]
print("ok")
