"""Run the full verification programme in order, propagating the first failure.

This is the project's single verification entry point: it keeps the original
regression suite and adds the merged-handoff examples from ``merge_examples``.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(commands):
    for command in commands:
        print('+ ' + ' '.join(command), flush=True)
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode:
            return completed.returncode
    return 0


def main():
    return run([
        [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-t', '.', '-v'],
        [sys.executable, '-B', 'merge_examples.py', 'verify'],
    ])


if __name__ == '__main__':
    sys.exit(main())
