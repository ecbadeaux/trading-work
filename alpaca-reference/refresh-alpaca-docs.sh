#!/bin/sh
# Re-download the Alpaca US docs as markdown into <workspace>/thridparty-repos/alpaca-docs/,
# using the site's llms.txt index.
#
# Usage: trading-work/alpaca-reference/refresh-alpaca-docs.sh   (run from anywhere)
#
# The user runs this, not Claude: thridparty-repos/ is off-limits to Claude (see CLAUDE.md).
set -e
DEST="$(cd "$(dirname "$0")/../.." && pwd)/thridparty-repos/alpaca-docs"
mkdir -p "$DEST"
cd "$DEST"
curl -sf https://docs.alpaca.markets/us/llms.txt -o llms-us.txt
curl -sf https://docs.alpaca.markets/llms.txt -o llms-all.txt
grep -oE 'https://docs\.alpaca\.markets/us/[^ )]*\.md' llms-us.txt | sort -u \
  | sed 's#https://docs.alpaca.markets/##' \
  | xargs -P 6 -n 1 sh -c 'mkdir -p "$(dirname "$1")"; curl -sf --retry 3 -o "$1" "https://docs.alpaca.markets/$1" || echo "FAIL $1"' _
echo "pages: $(find us -name '*.md' | wc -l) in $DEST"
date -u +%Y-%m-%dT%H:%M:%SZ > .last-refresh
