"""Run the original handoff demo followed by the merged-handoff demo.

Both programmes print ``POINTWISE_SCENARIO`` records so the original business
demonstration is preserved alongside the new merge scenarios.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(commands):
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode:
            return completed.returncode
    return 0


def main():
    return run([
        [sys.executable, '-B', 'demo.py'],
        [sys.executable, '-B', 'merge_examples.py', 'demo'],
    ])


if __name__ == '__main__':
    sys.exit(main())
