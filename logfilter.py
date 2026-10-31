"""Edge-side log filter: keep error lines + context, with original line numbers.

    python3 logfilter.py    # writes filtered.txt next to every saved build-log.txt
"""
import re
from pathlib import Path

ERROR = re.compile(
    r"(?<![-/=])\b(error|errors|fail|failed|failure|fatal|panic|timeout|timed out|deadline exceeded"
    r"|denied|forbidden|not found|no such|cannot|unable to|exit (status|code))\b"
    r"|\*\*\*"                    # make: *** [target] Error 1
    r"|^\S+\.\w+:\d+(:\d+)?:"     # file.go:185:4: compiler/lint finding
    r"|Traceback",
    re.IGNORECASE,
)
NOISE = re.compile(r"^level=(info|debug)\b")  # structured info logs that merely mention errors
ANSI = re.compile(r"\x1b\[[0-9;]*m")
BEFORE, AFTER, TAIL = 3, 3, 10
MAX_LINES = 400   # ponytail: keeps the last 400 kept lines; rank hits if huge e2e logs lose the root cause
MAX_CHARS = 500   # truncate minified/json blobs


def filter_log(text):
    """Return [(line_no, line)] (1-based) of the lines worth sending to the LLM."""
    lines = [ANSI.sub("", l) for l in text.splitlines()]
    keep = set(range(max(0, len(lines) - TAIL), len(lines)))
    for i, line in enumerate(lines):
        if ERROR.search(line) and not NOISE.search(line):
            keep.update(range(max(0, i - BEFORE), min(len(lines), i + AFTER + 1)))
    kept = sorted(keep)[-MAX_LINES:]
    return [(i + 1, lines[i][:MAX_CHARS]) for i in kept]


def render(kept):
    """Prompt-ready text: 'N: line', with '...' where lines were dropped."""
    out, prev = [], 0
    for n, line in kept:
        if n != prev + 1:
            out.append("...")
        out.append(f"{n}: {line}")
        prev = n
    return "\n".join(out)


def filter_dir(job_dir):
    log = job_dir / "build-log.txt"
    text = log.read_text(errors="replace")
    filtered = render(filter_log(text))
    (job_dir / "filtered.txt").write_text(filtered)
    return text, filtered


def main():
    for log in sorted(Path(__file__).parent.glob("data/failures/*/build-log.txt")):
        text, filtered = filter_dir(log.parent)
        print(f"{log.parent.name:55} {len(text):>7} -> {len(filtered):>6} bytes"
              f" ({100 - 100 * len(filtered) // max(len(text), 1)}% cut)")


if __name__ == "__main__":
    main()
