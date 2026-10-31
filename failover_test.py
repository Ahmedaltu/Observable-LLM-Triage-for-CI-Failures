"""Failure injection: kill the server model mid-run and measure how long until the edge model answers.

    .venv/bin/python failover_test.py    # needs Ollama (llama3 + llama3.2:3b) and Docker for the llm-server link

Recovery time = from the kill until the first fallback answer is written.
"""
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
WARM = 2  # server-model answers before the outage


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=HERE, check=True, capture_output=True)


def summary(recs, label):
    if not recs:
        return
    v = [r["citation_validity"] for r in recs if r.get("citation_validity") is not None]
    print(f"  {label:7} {len(recs):2} jobs  median latency {statistics.median(r['latency_s'] for r in recs):5.1f} s"
          f"  citation validity {statistics.mean(v):.0%}" if v else "")


def main():
    log = HERE / "data" / f"failover-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    compose("start", "llm-server")
    run = subprocess.Popen([sys.executable, "triage.py", "serve", str(log)], cwd=HERE)
    while not log.exists() or log.read_text().count('"served_by": "llama3"') < WARM:
        if run.poll() is not None or (log.exists() and '"fallback": true' in log.read_text()):
            run.kill()
            sys.exit(f"server model failed during warm-up, see {log}")
        time.sleep(0.5)

    killed = time.time()
    compose("kill", "llm-server")  # SIGKILL: an outage, not a graceful shutdown
    print(f"--- server killed at {time.strftime('%H:%M:%S')} ---")
    run.wait()

    recs = [json.loads(line) for line in log.read_text().splitlines()]
    first = next((r for r in recs if r["fallback"] and r["served_by"]), None)
    if not first:
        sys.exit("no fallback answer recorded")
    print(f"\nrecovery: {first['t_end'] - killed:.1f} s (kill -> first edge answer, job {first['job']})")
    print(f"  error seen: {first['error']}")
    summary([r for r in recs if not r["fallback"]], "server")
    summary([r for r in recs if r["fallback"] and r["served_by"]], "edge")
    print(f"log: {log.relative_to(HERE)}   restart the server with: docker compose start llm-server")


if __name__ == "__main__":
    main()
