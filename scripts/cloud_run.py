"""
cloud_run.py — GitHub Actions entry point.
Responsibilities: fetch data (incl. inject queue) -> evaluate.
Does not create workspaces, touch jobs/, or write jobs_index.csv.
"""

import sys
import os
from pathlib import Path

os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, str(Path(__file__).parent))

from fetch_jobs import fetch_all
from evaluate_jobs import evaluate_all


def main():
    print("=== Cloud Run ===")

    print("\n[1/2] Fetch sources + process inject queue")
    fetch_all()

    print("\n[2/2] Evaluate")
    evaluate_all()

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
