"""Snapshot failed Metal3 Prow jobs and their build logs to ./data.

Run daily: Prow only keeps a recent window, so unsaved failures are lost.
    python3 collector.py
"""
import html
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROW = "https://prow.apps.test.metal3.io"
DATA = Path(__file__).parent / "data"
SNAPSHOT_EVERY = 3600  # s; triage.py watch polls every 5 min, but a ~250 KB snapshot per poll is 70 MB/day


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def build_log(job):
    # url looks like .../view/s3/prow-logs/<path>; the lens page holds a signed S3 link (10 min expiry)
    src = job["url"].split("/view/", 1)[1]
    req = urllib.parse.quote(json.dumps({"artifacts": ["build-log.txt"], "index": 1, "src": src}))
    page = get(f"{PROW}/spyglass/lens/buildlog/iframe?req={req}&topURL=x&lensIndex=1").decode()
    m = re.search(r'href="(https://[^"]*/build-log\.txt\?[^"]*)"', page)
    if not m:
        raise RuntimeError("no raw build-log link in lens page")
    return get(html.unescape(m.group(1)), {"x-amz-checksum-mode": "ENABLED"})


def main():
    """Returns the directories of newly saved failures."""
    jobs = json.loads(get(f"{PROW}/data.js"))
    # keep snapshots: the CI-health dashboard and rerun-based flaky labels need successes too
    snaps = DATA / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    last = max((p.stat().st_mtime for p in snaps.glob("*.json")), default=0)
    if time.time() - last >= SNAPSHOT_EVERY:
        (snaps / f"{time.strftime('%Y%m%dT%H%M%S')}.json").write_text(json.dumps(jobs))

    new, skipped, failed = [], 0, 0
    for job in jobs:
        if job["state"] != "failure":
            continue
        if not job["url"].startswith(f"{PROW}/view/"):  # ponytail: Jenkins-run jobs have no Prow log, skipped
            skipped += 1
            continue
        out = DATA / "failures" / f"{job['job']}-{job['build_id']}"
        if (out / "build-log.txt").exists():
            continue
        t0 = time.perf_counter()
        try:
            log = build_log(job)
        except Exception as e:
            print(f"FAIL {out.name}: {e}")
            failed += 1
            continue
        job["fetch_s"] = round(time.perf_counter() - t0, 2)  # shown on the triage trace (triage.py job_span)
        out.mkdir(parents=True, exist_ok=True)
        (out / "meta.json").write_text(json.dumps(job, indent=2))
        (out / "build-log.txt").write_bytes(log)
        new.append(out)
        print(f"saved {out.name} ({len(log)} bytes)")

    print(f"{len(jobs)} jobs, {len(new)} new failures saved, {skipped} non-Prow skipped, {failed} errors")
    return new


if __name__ == "__main__":
    main()
