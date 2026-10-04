#!/usr/bin/env bash
# Seat approval menus, against a REAL screen (scripts/fixtures/seat-claude-write-prompt.txt: Claude Code 2.1.278,
# Write tool, "Do you want to create seatlab.txt?", captured 2026-09-22). Pure lib-level: no tmux, no network.
#
# What went wrong before this test existed (found by driving the seat through herdr):
#   * the menu block walked UP through the diff preview (its "╌" border is not in the border set), so file
#     content joined the menu -- a file containing "1. Yes" could become the approval target;
#   * option 2 wraps onto three lines with a different indent, which stopped the DOWNWARD walk, so "3. No" was
#     never parsed and respond(deny) failed closed;
#   * the skip list (always / don't ask / all future) did not exclude "Yes, and switch to accept edits
#     (auto-approve ... for this session)": respond(allow) picked plain "Yes" only because it came first.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX="$HERE/scripts/fixtures/seat-claude-write-prompt.txt"
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
check(){ # $1 label  $2 python expression that must print True
  out="$(python3 - "$HERE" "$FIX" "$2" <<'PY'
import sys, re
sys.path.insert(0, sys.argv[1] + "/lib")
import com8_seat as hs
lines = [l.rstrip() for l in open(sys.argv[2]).read().splitlines()]
bottom = [l for l in lines if l.strip()][-16:]
menu = hs.SeatDriver._menu_block(bottom)
texts = [r["text"] for r in menu]
digits = [r.get("digit") for r in menu]
# a permuted menu: the sticky option listed FIRST, highlighted
sticky_first = [
    {"hl": True,  "digit": "1", "text": "Yes, and switch to accept edits (auto-approve file edits and common file commands) for this session (shift+tab)"},
    {"hl": False, "digit": "2", "text": "Yes"},
    {"hl": False, "digit": "3", "text": "No"},
]
only_sticky = [sticky_first[0], sticky_first[2]]
# sticky phrasings WITHOUT the words "switch to" (each must be caught on its own)
sticky_words = [
    [{"hl": True, "digit": "1", "text": "Yes, and auto-approve file edits"}, {"hl": False, "digit": "2", "text": "Yes"}],
    [{"hl": True, "digit": "1", "text": "Yes, don't ask again"}, {"hl": False, "digit": "2", "text": "Yes"}],
    [{"hl": True, "digit": "1", "text": "Yes, for this session"}, {"hl": False, "digit": "2", "text": "Yes"}],
    [{"hl": True, "digit": "1", "text": "Yes, allow all future edits"}, {"hl": False, "digit": "2", "text": "Yes"}],
]
# a deny must never land on a Yes row that happens to contain a negative word
mixed = [{"hl": True, "digit": "1", "text": "Yes, drop the old one"}, {"hl": False, "digit": "2", "text": "No"}]
# a dashed rule bounds the menu even when no question line sits between preview and options
dashed = ["  yes we ship it", "╌╌╌╌╌╌╌╌╌╌", "❯ Yes", "  No"]   # unnumbered, so only the border can stop the walk
# an UNNUMBERED menu: the ask line right above the bare highlighted row is not an option
unnumbered = ["Deliver this message?", "❯ Yes", "  No"]
print(bool(eval(sys.argv[3])))
PY
)"
  if [ "$out" = "True" ]; then ok "$1"; else bad "$1 (got: $out)"; fi
}

echo "== menu block is exactly the three options, nothing from the diff preview or the question"
check "three rows parsed"                      'len(menu) == 3'
check "digits 1,2,3 in order"                  'digits == ["1","2","3"]'
check "row 3 is No (multi-line row 2 did not stop the walk)"   'texts[2] == "No"'
check "row 2 keeps its wrapped continuation"  '"for this session" in texts[1]'
check "no diff content in the menu"           'not any("marigold" in t or "Create file" in t or "seatlab.txt" == t for t in texts)'
check "question line is not an option"        'not any(t.startswith("Do you want") for t in texts)'

echo "== choosing: allow never picks a sticky/session-wide option; deny finds No"
check "allow picks plain Yes here"            'hs.choose_option(menu, "allow")["text"] == "Yes"'
check "allow skips sticky even when first"    'hs.choose_option(sticky_first, "allow")["text"] == "Yes"'
check "allow fails closed if only sticky"     'hs.choose_option(only_sticky, "allow") is None'
check "deny picks No"                         'hs.choose_option(menu, "deny")["text"] == "No"'
check "deny never picks a Yes"                'hs.choose_option(sticky_first, "deny")["text"] == "No"'
check "each sticky phrasing excluded alone"   'all(hs.choose_option(m, "allow")["text"] == "Yes" for m in sticky_words)'
check "deny skips a Yes row with a negative word" 'hs.choose_option(mixed, "deny")["text"] == "No"'
check "dashed rule bounds an unquestioned menu" '[r["text"] for r in hs.SeatDriver._menu_block(dashed)] == ["Yes", "No"]'
check "ask line excluded from an unnumbered menu" '[r["text"] for r in hs.SeatDriver._menu_block(unnumbered)] == ["Yes", "No"]'

echo "== the screen is still classified as an approval (unchanged behaviour)"
check "approval classifier holds"             'bool(hs._PERM_ASK.search("\n".join(bottom))) and bool(hs._AFFORD.search("\n".join(bottom)))'

printf '\n%d ok, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
