"""Verifier: check every line a model cited against the log excerpt it was shown.

    python3 verify.py    # adds "citations" + "citation_validity" to every triage-*.json

A cited line is valid if its number was in filtered.txt: the model saw only those lines, so any
other number is made up even when the full log happens to be that long.
"""
import json
import re
from pathlib import Path

DATA = Path(__file__).parent / "data"
LINE = re.compile(r"^(\d+): (.*)$")


def shown_lines(filtered_text):
    return {int(m[1]): m[2] for m in map(LINE.match, filtered_text.splitlines()) if m}


def verify(cited, shown):
    """Return (citations, validity); validity is None when nothing was cited."""
    citations = [{"line": n, "found": n in shown, "text": shown.get(n)} for n in cited]
    validity = sum(c["found"] for c in citations) / len(citations) if citations else None
    return citations, validity


def main():
    for path in sorted(DATA.glob("failures/*/triage-*.json")):
        result = json.loads(path.read_text())
        shown = shown_lines((path.parent / "filtered.txt").read_text())
        result["citations"], result["citation_validity"] = verify(result["cited_lines"], shown)
        path.write_text(json.dumps(result, indent=2))
        bad = [c["line"] for c in result["citations"] if not c["found"]]
        v = result["citation_validity"]
        print(f"{path.parent.name:45} {result['model']:20} validity={'-' if v is None else f'{v:.0%}':>5}"
              + (f"  not found: {bad}" if bad else ""))


if __name__ == "__main__":
    main()
