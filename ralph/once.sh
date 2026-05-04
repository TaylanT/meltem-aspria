#!/bin/bash
set -eo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

issues=$(cat .scratch/kursbuchungs-bot/issues/*.md 2>/dev/null || echo "No issues found")
commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
prompt=$(cat ralph/prompt.md)

codex exec \
  -C . \
  --skip-git-repo-check \
  --sandbox workspace-write \
  "Previous commits: $commits Issues: $issues $prompt"
