"""
Run every self-test in this repo and exit non-zero if any failed.

Usage:
    python run_all_tests.py
    python run_all_tests.py --skip-run     # don't re-run converters
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


TESTS = [
    "test_gemini_conversion.py",
    "test_grok_conversion.py",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-run", action="store_true",
                    help="Pass --skip-run to each child test")
    args = ap.parse_args()

    repo = Path(__file__).parent
    failures: list[str] = []

    for name in TESTS:
        path = repo / name
        if not path.exists():
            print(f"SKIP   {name} (not found)")
            continue
        print(f"\n========= {name} =========")
        cmd = [sys.executable, str(path)]
        if args.skip_run:
            cmd.append("--skip-run")
        rc = subprocess.call(cmd, cwd=str(repo))
        if rc != 0:
            failures.append(f"{name} (exit {rc})")

    print("\n========= summary =========")
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        return 1
    print(f"All {len(TESTS)} test scripts passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
