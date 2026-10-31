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

job = Path(tempfile.mkdtemp()) / "unit-1"
job.mkdir()
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
print("ok")
