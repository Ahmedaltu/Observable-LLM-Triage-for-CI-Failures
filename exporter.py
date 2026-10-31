"""Prometheus exporter for CI health, built from the collector's snapshots.

    python3 exporter.py    # serves http://localhost:8000/metrics
"""
import json
import statistics
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DATA = Path(__file__).parent / "data"


def load_jobs():
    """All jobs seen across snapshots, one per build_id; the newest snapshot wins (pending -> final state)."""
    jobs = {}
    for snap in sorted((DATA / "snapshots").glob("*.json")):
        for job in json.loads(snap.read_text()):
            jobs[job["build_id"]] = job
    return list(jobs.values())


def repo(job):
    return job["refs"].get("repo") or job["type"]  # periodics have no repo


def metrics(jobs):
    counts = Counter((repo(j), j["job"], j["type"], j["state"]) for j in jobs)
    # flaky = same job on the same commit(s) both failed and passed; periodics have no refs_key, so never flaky
    states = defaultdict(set)
    for j in jobs:
        if j["refs_key"]:
            states[repo(j), j["job"], j["refs_key"]].add(j["state"])
    flaky = Counter((r, name) for (r, name, _), s in states.items() if {"failure", "success"} <= s)

    out = ["# HELP metal3_prow_jobs Prow jobs seen across all snapshots.", "# TYPE metal3_prow_jobs gauge"]
    out += [f'metal3_prow_jobs{{repo="{r}",job="{n}",type="{t}",state="{s}"}} {c}' for (r, n, t, s), c in sorted(counts.items())]
    out += ["# HELP metal3_prow_flaky Commits where the same job both failed and passed.", "# TYPE metal3_prow_flaky gauge"]
    out += [f'metal3_prow_flaky{{repo="{r}",job="{n}"}} {c}' for (r, n), c in sorted(flaky.items())]
    out += ["# HELP metal3_failures_saved Failed jobs with a downloaded build log.", "# TYPE metal3_failures_saved gauge",
            f"metal3_failures_saved {len(list(DATA.glob('failures/*/build-log.txt')))}"]
    return "\n".join(out) + "\n"


def llm_metrics(results, labels, serve_runs):
    """results: triage-*.json dicts with "job" added; labels: {job: category}; serve_runs: {run: [jsonl records]}."""
    from labels import accuracy

    by_model = defaultdict(list)
    for r in results:
        by_model[r["model"]].append(r)
    out = ["# HELP metal3_triage_results Triaged failures per model and category.", "# TYPE metal3_triage_results gauge"]
    out += [f'metal3_triage_results{{model="{m}",category="{c}"}} {n}'
            for m, rs in sorted(by_model.items()) for c, n in sorted(Counter(r["category"] for r in rs).items())]
    out += ["# HELP metal3_triage_latency_seconds Median and max triage latency per model.", "# TYPE metal3_triage_latency_seconds gauge"]
    for m, rs in sorted(by_model.items()):
        lat = [r["latency_s"] for r in rs]
        out += [f'metal3_triage_latency_seconds{{model="{m}",stat="median"}} {statistics.median(lat)}',
                f'metal3_triage_latency_seconds{{model="{m}",stat="max"}} {max(lat)}']
        ttft = [r["ttft_s"] for r in rs if "ttft_s" in r]  # local models only, from Ollama timings
        if ttft:
            out.append(f'metal3_triage_latency_seconds{{model="{m}",stat="ttft_median"}} {statistics.median(ttft)}')
        tps = [r["tokens_per_s"] for r in rs if "tokens_per_s" in r]
        if tps:
            out.append(f'metal3_triage_tokens_per_second{{model="{m}"}} {statistics.median(tps)}')
    out += ["# HELP metal3_triage_citation_validity Mean share of cited lines that exist in the filtered log.",
            "# TYPE metal3_triage_citation_validity gauge"]
    for m, rs in sorted(by_model.items()):
        v = [r["citation_validity"] for r in rs if r.get("citation_validity") is not None]
        if v:
            out.append(f'metal3_triage_citation_validity{{model="{m}"}} {statistics.mean(v):.4f}')
    out += ["# HELP metal3_triage_tokens Tokens used per model.", "# TYPE metal3_triage_tokens gauge"]
    for m, rs in sorted(by_model.items()):
        out += [f'metal3_triage_tokens{{model="{m}",dir="input"}} {sum(r["input_tokens"] for r in rs)}',
                f'metal3_triage_tokens{{model="{m}",dir="output"}} {sum(r["output_tokens"] for r in rs)}']
    out += ["# HELP metal3_triage_cost_usd Total API cost per model.", "# TYPE metal3_triage_cost_usd gauge"]
    out += [f'metal3_triage_cost_usd{{model="{m}"}} {sum(r["cost_usd"] for r in rs)}' for m, rs in sorted(by_model.items())]
    out += ["# HELP metal3_triage_accuracy Share of hand-labelled failures the model got right.", "# TYPE metal3_triage_accuracy gauge"]
    for m, rs in sorted(by_model.items()):
        right, n = accuracy(labels, {r["job"]: r["category"] for r in rs})
        if n:
            out.append(f'metal3_triage_accuracy{{model="{m}"}} {right / n:.4f}')
    out += ["# HELP metal3_serve_answers Answers in a serve/failover run, by the model that served them (none = both down).",
            "# TYPE metal3_serve_answers gauge"]
    for run, recs in sorted(serve_runs.items()):
        for who, n in sorted(Counter(r["served_by"] or "none" for r in recs).items()):
            out.append(f'metal3_serve_answers{{run="{run}",served_by="{who}"}} {n}')
    out += ["# HELP metal3_serve_fallbacks Jobs in a serve/failover run that fell back from the server model.",
            "# TYPE metal3_serve_fallbacks gauge"]
    out += [f'metal3_serve_fallbacks{{run="{run}"}} {sum(r["fallback"] for r in recs)}' for run, recs in sorted(serve_runs.items())]
    return "\n".join(out) + "\n"


def watch_metrics(status):
    """status: data/watch.json written by `triage.py watch` after every poll, or None if it never ran."""
    if not status:
        return ""
    out = ["# HELP metal3_watch_last_poll_timestamp_seconds When triage.py watch last polled Prow.",
           "# TYPE metal3_watch_last_poll_timestamp_seconds gauge",
           f"metal3_watch_last_poll_timestamp_seconds {status['last_poll']}",
           "# HELP metal3_watch_last_success_timestamp_seconds Last poll that collected and triaged without error.",
           "# TYPE metal3_watch_last_success_timestamp_seconds gauge",
           f"metal3_watch_last_success_timestamp_seconds {status['last_success']}",
           "# HELP metal3_watch_poll_ok 1 if the last poll succeeded.", "# TYPE metal3_watch_poll_ok gauge",
           f"metal3_watch_poll_ok {0 if status.get('error') else 1}"]
    return "\n".join(out) + "\n"


def load_watch():
    p = DATA / "watch.json"
    return json.loads(p.read_text()) if p.exists() else None


def load_llm():
    results = [{**json.loads(p.read_text()), "job": p.parent.name} for p in DATA.glob("failures/*/triage-*.json")]
    from labels import load
    runs = {p.stem: [json.loads(line) for line in p.read_text().splitlines() if line.strip()] for p in DATA.glob("*.jsonl")}
    return results, load(), runs


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = (metrics(load_jobs()) + llm_metrics(*load_llm()) + watch_metrics(load_watch())).encode()  # ponytail: re-reads every snapshot per scrape; cache by mtime if it gets slow
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("", 8000), Handler).serve_forever()
