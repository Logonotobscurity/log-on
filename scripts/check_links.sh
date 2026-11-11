#!/usr/bin/env bash
# Usage: scripts/check_links.sh learning_plan.txt   -> prints HTTP status for every URL in the file
set -euo pipefail
grep -oE 'https?://[^ )>"\]]+' "$1" | sed 's/[.,;]$//' | sort -u | while read -r u; do
  code=$(curl -s -L -o /dev/null --max-time 20 -w '%{http_code}' "$u" || echo ERR)
  printf '%s\t%s\n' "$code" "$u"
done
