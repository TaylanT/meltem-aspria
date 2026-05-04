#!/bin/bash
set -eo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
cd "$repo_root"

if [ -z "$1" ]; then
  echo "Usage: $0 <iterations>"
  exit 1
fi

for ((i=1; i<=$1; i++)); do
  tmpfile=$(mktemp)
  trap "rm -f $tmpfile" EXIT

  commits=$(git log -n 5 --format="%H%n%ad%n%B---" --date=short 2>/dev/null || echo "No commits found")
  issues=$(cat .scratch/kursbuchungs-bot/issues/*.md 2>/dev/null || echo "No issues found")
  prompt=$(cat ralph/prompt.md)

  echo "Ralph iteration $i of $1"
  codex exec \
    -C . \
    --skip-git-repo-check \
    --sandbox workspace-write \
    -o "$tmpfile" \
    "Previous commits: $commits Issues: $issues $prompt"

  if grep -q "<promise>NO MORE TASKS</promise>" "$tmpfile"; then
    echo "Ralph complete after $i iterations."
    exit 0
  fi
done
