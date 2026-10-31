"""Hand labels (ground truth) and per-model accuracy against them.

    python3 labels.py          # label every unlabelled failure; shows only the log, never the model answers
    python3 labels.py score    # accuracy of each model on the labelled failures

Labels go to data/labels.csv (job,category,note); edit it by hand to fix one.
"""
import csv
import json
import sys
from pathlib import Path

from triage import DATA, SCHEMA

LABELS = DATA / "labels.csv"
CATS = SCHEMA["properties"]["category"]["enum"]


def load():
    if not LABELS.exists():
        return {}
    with open(LABELS) as f:
        return {r["job"]: r["category"] for r in csv.DictReader(f)}


def accuracy(labels, preds):
    """labels, preds: {job: category}. Scores only jobs in both; returns (right, total)."""
    common = labels.keys() & preds.keys()
    return sum(labels[j] == preds[j] for j in common), len(common)


def label():
    done = load()
    todo = [p for p in sorted(DATA.glob("failures/*/filtered.txt")) if p.parent.name not in done]
    new = not LABELS.exists()
    with open(LABELS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["job", "category", "note"])
        for i, p in enumerate(todo, 1):
            print("\n" + "=" * 100 + f"\n{p.parent.name}  ({i}/{len(todo)})\n" + "=" * 100)
            print(p.read_text())
            print("  ".join(f"{n}={c}" for n, c in enumerate(CATS, 1)), " s=skip  q=quit")
            while (k := input("category> ").strip()) not in {"1", "2", "3", "4", "5", "s", "q"}:
                pass
            if k == "q":
                break
            if k == "s":
                continue
            w.writerow([p.parent.name, CATS[int(k) - 1], input("note (optional)> ").strip()])
            f.flush()  # quitting halfway keeps what you labelled


def score():
    labels = load()
    models = {}
    for t in DATA.glob("failures/*/triage-*.json"):
        r = json.loads(t.read_text())
        models.setdefault(r["model"], {})[t.parent.name] = r["category"]
    print(f"{len(labels)} labelled failures")
    for m, preds in sorted(models.items()):
        right, n = accuracy(labels, preds)
        if n:
            print(f"  {m:45} {right:3}/{n:<3} {right / n:.0%}")


if __name__ == "__main__":
    score() if sys.argv[1:2] == ["score"] else label()
