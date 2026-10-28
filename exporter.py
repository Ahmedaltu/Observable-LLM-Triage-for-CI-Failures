"""Prometheus exporter for CI health, built from the collector's snapshots.

    python3 exporter.py    # serves http://localhost:8000/metrics
"""
import json
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = metrics(load_jobs()).encode()  # ponytail: re-reads every snapshot per scrape; cache by mtime if it gets slow
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("", 8000), Handler).serve_forever()
