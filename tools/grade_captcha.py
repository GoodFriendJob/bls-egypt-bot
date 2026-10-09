"""Score the vision model against human-labelled CAPTCHA samples.

Joins two files in logs/captcha_samples/:

  ground_truth.json  - what a human read, added by hand
  readings.jsonl     - what the model read, appended automatically each attempt

and reports per-tile accuracy, whether the resulting selection would have been
accepted, and which digits get confused for which.

Usage:
    python tools/grade_captcha.py
    python tools/grade_captcha.py --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "logs" / "captcha_samples"
TRUTH_FILE = SAMPLE_DIR / "ground_truth.json"
READINGS_FILE = SAMPLE_DIR / "readings.jsonl"


def load_truth() -> dict[str, dict]:
    if not TRUTH_FILE.exists():
        print(f"No ground truth at {TRUTH_FILE}")
        return {}
    data = json.loads(TRUTH_FILE.read_text(encoding="utf-8"))
    return data.get("samples", {})


def load_readings() -> dict[str, dict]:
    """Latest reading per sample."""
    if not READINGS_FILE.exists():
        print(f"No readings at {READINGS_FILE} — run the bot first.")
        return {}
    rows: dict[str, dict] = {}
    for line in READINGS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("sample"):
            rows[row["sample"]] = row
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="show every tile")
    args = parser.parse_args()

    truth = load_truth()
    readings = load_readings()
    if not truth:
        return 1

    graded = 0
    tiles_total = tiles_right = 0
    selection_right = 0
    confusions: Counter[str] = Counter()
    unlabelled: list[str] = []

    for sample, info in truth.items():
        reading = readings.get(sample)
        if reading is None:
            unlabelled.append(sample)
            continue

        expected = [str(t) for t in info.get("tiles", [])]
        got = [str(t) for t in (reading.get("tiles") or [])]
        target = str(info.get("target", reading.get("target", "")))
        if not expected:
            print(f"  ! {sample}: no human labels")
            continue

        # Older readings recorded only the chosen positions, not per-tile
        # digits. Those can still be scored on the selection itself, which is
        # what actually decides pass or fail.
        if not got:
            chosen = sorted(reading.get("positions") or [])
            want = sorted(i for i, t in enumerate(expected, 1) if t == target)
            ok = chosen == want
            graded += 1
            selection_right += int(ok)
            print(f"\n{'PASS' if ok else 'FAIL'}  {sample}   target={target}  "
                  f"method={reading.get('method')}  (selection only)")
            print(f"   would select  : {chosen}")
            print(f"   should select : {want}")
            if not ok:
                missed = [i for i in want if i not in chosen]
                extra = [i for i in chosen if i not in want]
                if missed:
                    print(f"   MISSED        : {missed} "
                          f"(tiles {[expected[i - 1] for i in missed]})")
                if extra:
                    print(f"   FALSE POSITIVE: {extra} "
                          f"(actually {[expected[i - 1] for i in extra]})")
                    for i in extra:
                        confusions[f"{expected[i - 1]} -> {target}"] += 1
            continue

        if len(expected) != len(got):
            print(f"  ! {sample}: tile count mismatch ({len(expected)} vs {len(got)})")
            continue

        graded += 1
        per_tile = [e == g for e, g in zip(expected, got)]
        tiles_total += len(expected)
        tiles_right += sum(per_tile)

        want = sorted(i for i, t in enumerate(expected, 1) if t == target)
        mine = sorted(i for i, t in enumerate(got, 1) if t == target)
        ok = want == mine
        selection_right += int(ok)

        for exp, act in zip(expected, got):
            if exp != act:
                confusions[f"{exp} -> {act}"] += 1

        status = "PASS" if ok else "FAIL"
        print(f"\n{status}  {sample}   target={target}  method={reading.get('method')}")
        print(f"   tiles correct : {sum(per_tile)}/{len(expected)}")
        print(f"   would select  : {mine}")
        print(f"   should select : {want}")
        if not ok:
            missed = [i for i in want if i not in mine]
            extra = [i for i in mine if i not in want]
            if missed:
                print(f"   MISSED        : {missed}")
            if extra:
                print(f"   FALSE POSITIVE: {extra} "
                      f"(actually {[expected[i - 1] for i in extra]})")
        if args.verbose or not ok:
            for idx, (exp, act) in enumerate(zip(expected, got), 1):
                mark = " " if exp == act else "X"
                print(f"     {mark} {idx}: human={exp:<5} model={act}")

    print("\n" + "=" * 58)
    if graded:
        print(f"samples graded      : {graded}")
        if tiles_total:
            print(f"tile accuracy       : {tiles_right}/{tiles_total} "
                  f"({100 * tiles_right / tiles_total:.1f}%)")
        else:
            print("tile accuracy       : n/a (readings hold positions only)")
        print(f"correct selections  : {selection_right}/{graded} "
              f"({100 * selection_right / graded:.1f}%)")
    else:
        print("Nothing graded yet.")
    if confusions:
        print("\nmost common misreads (human -> model):")
        for pair, n in confusions.most_common(12):
            print(f"   {pair}   x{n}")
    if unlabelled:
        print(f"\n{len(unlabelled)} labelled sample(s) have no model reading yet:")
        for name in unlabelled[:10]:
            print(f"   {name}")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
