#!/usr/bin/env bash
# Runs the whole test suite. Each file is stdlib unittest, run as its own
# process (no pytest dependency needed).
set -e
cd "$(dirname "$0")"
rm -f arena.db
for f in tests/test_*.py; do
  echo "=== $f ==="
  python3 "$f"
done
echo
echo "All tests passed."
