#!/usr/bin/env bash
# Checks that a PR's title and every commit it brings are conventional commits,
# with the hook's own checker at the hook's own version. Run by CI; needs TITLE,
# BASE and HEAD in the environment and the history of both.
set -euo pipefail

# Keep in step with .pre-commit-config.yaml: the rev there, the args there.
checker=(uvx --quiet conventional-pre-commit@4.4.0)
types=(build chore ci deps docs feat fix perf refactor revert style test)

message=$(mktemp)
failed=0

check() {
  printf '%s\n' "$2" >"$message"
  if "${checker[@]}" --strict --no-color "${types[@]}" "$message" >/dev/null; then
    echo "ok    $1"
  else
    echo "FAIL  $1: $(head -n1 "$message")"
    failed=1
  fi
}

check "PR title" "$TITLE"
for sha in $(git rev-list --no-merges "$BASE..$HEAD"); do
  check "commit ${sha:0:8}" "$(git log -1 --format=%B "$sha")"
done

if [ "$failed" -ne 0 ]; then
  echo
  echo "Use <type>[(scope)][!]: <description>, with type one of: ${types[*]}."
  echo "See https://www.conventionalcommits.org/en/v1.0.0/"
  exit 1
fi
