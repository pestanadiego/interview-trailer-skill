"""Look up word-level timestamps.

usage:
  $PY words.py --project P 9:05 9:40            # every word with start-end between two times
  $PY words.py --project P --find "pricing"   # every occurrence of a word/phrase, with times
"""
import argparse
import re

from common import fmt_t, parse_t, work_dir, words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("range", nargs="*")
    ap.add_argument("--find")
    a = ap.parse_args()
    ws = words(work_dir(a.project))
    if a.find:
        target = [t.lower() for t in re.findall(r"\w+", a.find)]
        low = [re.sub(r"\W", "", w["word"].lower()) for w in ws]
        for i in range(len(ws) - len(target) + 1):
            if low[i:i + len(target)] == target:
                ctx = " ".join(w["word"] for w in ws[max(0, i - 6):i + len(target) + 6])
                print(f"{ws[i]['start']:.2f} ({fmt_t(ws[i]['start'])})  ...{ctx}...")
        return
    lo, hi = parse_t(a.range[0]), parse_t(a.range[1])
    print(" ".join(f"{w['word']}@{w['start']:.2f}-{w['end']:.2f}" for w in ws if lo <= w["start"] <= hi))


if __name__ == "__main__":
    main()
