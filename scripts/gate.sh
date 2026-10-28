#!/usr/bin/env bash
# Run the same local quality gates expected from a pull request.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root/backend"
uv run --frozen pytest tests -q --tb=short -p no:cacheprovider
uvx ruff check app --select F821 --no-cache --isolated

cd "$root/frontend"
pnpm exec tsc -b
pnpm test
pnpm build

cd "$root"
python3 scripts/check_docs.py
git diff --check

echo "All ZenTrade quality gates passed."
