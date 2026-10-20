#!/usr/bin/env python3
"""Export safe append-only document resolutions before replaying a candidate."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herdr.replay_append_only import export_resolutions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--base', required=True)
    parser.add_argument('--onto', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        result = export_resolutions(args.repo, args.base, args.onto, args.candidate, args.output_dir)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
