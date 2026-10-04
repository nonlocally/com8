#!/usr/bin/env bash
# Cross-USER connect: signed card v2 + the connect choreography, driven by
# `com8 init` handles (NOT the COM8_FLEET env) — proving user.json drives the
# wire. Also the hardening floor the user layer stands on: auto-grant TTL,
# per-user proxy cap, qualified names in wait, grant fingerprint pin, and the
# sun_path guard. Two isolated daemons over direct sockets (fleet-suite
# topology); authorized_keys writes land in per-side fake HOMEs.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMM="$HERE/bin/communicate"
T="$(mktemp -d /tmp/com8-conn.XXXXXX)"
mkdir -p "$T/a-sess" "$T/b-sess" "$T/ahome" "$T/bhome"
export COM8_CONNECT_DIRECT=1   # --direct (the same-host transport) is env-gated
acomm(){ COMM_STATE="$T/a" COM8_SOCK_DIR="$T/as" COM8_SESSIONS_DIR="$T/a-sess" \
         COM8_SELF=alice-dev COM8_TICK=1 HOME="$T/ahome" "$COMM" "$@"; }
bcomm(){ COMM_STATE="$T/b" COM8_SOCK_DIR="$T/bs" COM8_SESSIONS_DIR="$T/b-sess" \
         COM8_SELF=bob-dev COM8_TICK=1 HOME="$T/bhome" "$COMM" "$@"; }
pass=0; fail=0
ok(){ pass=$((pass+1)); printf 'ok   %s\n' "$*"; }
bad(){ fail=$((fail+1)); printf 'FAIL %s\n' "$*"; }
cleanup(){ acomm com8 stop >/dev/null 2>&1||true; bcomm com8 stop >/dev/null 2>&1||true
           ccomm com8 stop >/dev/null 2>&1||true; rm -rf "$T"; }
ccomm(){ COMM_STATE="$T/c" COM8_SOCK_DIR="$T/cs-long" COM8_SESSIONS_DIR="$T/c-sess" \
         COM8_SELF=carol-dev COM8_TICK=1 "$COMM" "$@"; }
trap cleanup EXIT

jget() { python3 -c '
import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1:]:
    d=d[k]
print(d)' "$@" 2>/dev/null; }

wait_for() { local dl=$((SECONDS + $1)); shift; local desc="$1"; shift
  while [ $SECONDS -lt $dl ]; do
    if "$@" >/dev/null 2>&1; then ok "$desc"; return 0; fi; sleep 0.5
  done; bad "$desc"; return 1; }

acomm com8 start >/dev/null 2>&1; bcomm com8 start >/dev/null 2>&1
acomm com8 init --handle alice >/dev/null 2>&1
bcomm com8 init --handle bob   >/dev/null 2>&1

echo "== the invite: a signed v2 card"
INV="$(acomm com8 connect --invite 2>/dev/null)"
CODE="$(printf '%s' "$INV" | grep -o 'com8v1\.[A-Za-z0-9+/=]*' | head -1)"
if [ -n "$CODE" ]; then ok "invite prints a com8v1. code"; else bad "invite prints a com8v1. code (got: $INV)"; fi
CARDJSON="$(printf '%s' "${CODE#com8v1.}" | python3 -c 'import base64,sys; print(base64.b64decode(sys.stdin.read().strip()).decode())' 2>/dev/null)"
if [ "$(printf '%s' "$CARDJSON" | jget v)" = "2" ] && [ "$(printf '%s' "$CARDJSON" | jget user)" = "alice" ] \
   && [ -n "$(printf '%s' "$CARDJSON" | jget sig)" ]; then
  ok "code decodes to v2 with user + signature"
else bad "code decodes to v2 with user + signature"; fi
FP="$(printf '%s' "$CARDJSON" | python3 -c '
import json,sys,subprocess,tempfile,os
c=json.load(sys.stdin)
with tempfile.TemporaryDirectory() as d:
    p=os.path.join(d,"k.pub"); open(p,"w").write(c["pubkey"]+"\n")
    out=subprocess.run(["ssh-keygen","-lf",p],capture_output=True,text=True)
    print(out.stdout.split()[1])' 2>/dev/null)"
if [ -n "$FP" ] && [ "$(printf '%s' "$CARDJSON" | jget fingerprint)" = "$FP" ]; then
  ok "card fingerprint matches its own key material"
else bad "card fingerprint matches its own key material"; fi

echo "== accept on bob (one verb, one confirm)"
out="$(bcomm com8 connect @alice --code "$CODE" --yes --direct 2>&1)"
rc=$?
if [ $rc -eq 0 ]; then ok "connect @alice accepts"; else bad "connect @alice accepts (rc=$rc: $out)"; fi
if printf '%s' "$out" | grep -qi "verified"; then ok "signature reported VERIFIED"; else bad "signature reported VERIFIED"; fi
if printf '%s' "$out" | grep -q "$FP"; then ok "recomputed fingerprint displayed"; else bad "recomputed fingerprint displayed"; fi
if grep -q "com8-fleet:alice" "$T/bhome/.ssh/authorized_keys" 2>/dev/null \
   && grep -q "restrict,port-forwarding" "$T/bhome/.ssh/authorized_keys"; then
  ok "forward-only key line installed under the fake HOME"
else bad "forward-only key line installed"; fi
lk="$(cat "$T/b/com8/links.json" 2>/dev/null)"
if [ "$(printf '%s' "$lk" | jget alice kind)" = "fleet" ] && [ "$(printf '%s' "$lk" | jget alice handle)" = "alice" ] \
   && [ "$(printf '%s' "$lk" | jget alice card_v)" = "2" ] && [ "$(printf '%s' "$lk" | jget alice key_fp)" = "$FP" ]; then
  ok "link carries handle + card_v + the RECOMPUTED key_fp"
else bad "link carries handle/card_v/key_fp (got: $(printf '%s' "$lk" | jget alice))"; fi
BOBCODE="$(printf '%s' "$out" | grep -o 'com8v1\.[A-Za-z0-9+/=]*' | head -1)"
if [ -n "$BOBCODE" ]; then ok "counter-code printed (the loop closes itself)"; else bad "counter-code printed"; fi

echo "== tampered code is refused"
TAMPERED="$(printf '%s' "$CARDJSON" | python3 -c '
import base64,json,sys
c=json.load(sys.stdin); c["device"]="evil-dev"
print("com8v1."+base64.b64encode(json.dumps(c).encode()).decode())')"
out="$(bcomm com8 connect @alice --code "$TAMPERED" --yes --direct 2>&1)"
if [ $? -ne 0 ] && printf '%s' "$out" | grep -qi "signature"; then
  ok "tampered card refused on signature"
else bad "tampered card refused (got: $out)"; fi

echo "== typed-name cross-check"
out="$(bcomm com8 connect @wrongname --code "$CODE" --yes --direct 2>&1)"
if [ $? -ne 0 ] && printf '%s' "$out" | grep -q "alice"; then
  ok "code for @alice refused when you typed @wrongname"
else bad "typed-name cross-check (got: $out)"; fi

echo "== v1 (unsigned) card needs the explicit escape hatch"
V1CARD="$(acomm com8 card 2>/dev/null)"
out="$(bcomm com8 connect @alice --code "$V1CARD" --yes --direct 2>&1)"
if [ $? -ne 0 ] && printf '%s' "$out" | grep -q "allow-unsigned"; then
  ok "unsigned card refused, escape hatch named"
else bad "unsigned card refusal (got: $out)"; fi

echo "== device-link collision refused, then the loop closes on alice"
acomm com8 link bob --sock "$T/nowhere.sock" >/dev/null 2>&1
out="$(acomm com8 connect @bob --code "$BOBCODE" --yes --direct 2>&1)"
if [ $? -ne 0 ] && printf '%s' "$out" | grep -qi "device link\|collision\|already"; then
  ok "petname colliding with a device link refused"
else bad "device-link collision (got: $out)"; fi
acomm com8 unlink bob >/dev/null 2>&1
out="$(acomm com8 connect @bob --code "$BOBCODE" --yes --direct 2>&1)"
if [ $? -eq 0 ]; then ok "counter-code accepted on alice (loop closed)"; else bad "loop close (got: $out)"; fi
if printf '%s' "$out" | grep -q "round trip"; then
  ok "loop-closing side measures the transport"
else bad "loop-closing side measures (got: $out)"; fi

echo "== re-keying a bound petname is refused"
EVIL="$T/evilkey"
ssh-keygen -t ed25519 -N "" -q -f "$EVIL"
EVILCODE="$(python3 - "$EVIL" "$T" <<'PY'
import base64, json, subprocess, sys, os
key, t = sys.argv[1], sys.argv[2]
pub = open(key + ".pub").read().strip()
card = {"v": 2, "kind": "com8-card", "fleet": "alice", "user": "alice",
        "device": "evil-dev", "addr": "evil@evil-dev", "tailscale_ip": None,
        "pubkey": pub, "fingerprint": "", "identity_file": key,
        "inbound_dir": t + "/evil-in"}
payload = json.dumps({k: v for k, v in card.items()},
                     sort_keys=True, separators=(",", ":")).encode()
pf = t + "/evil-payload"
open(pf, "wb").write(payload)
subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key, "-n", "com8-card", pf],
               check=True, capture_output=True)
card["sig"] = open(pf + ".sig").read()
print("com8v1." + base64.b64encode(json.dumps(card).encode()).decode())
PY
)"
out="$(bcomm com8 connect @alice --code "$EVILCODE" --yes --direct 2>&1)"
if [ $? -ne 0 ] && printf '%s' "$out" | grep -qi "different key\|re-key\|already bound"; then
  ok "same handle, different key: refused (fingerprint pinned)"
else bad "re-key refusal (got: $out)"; fi
lk="$(cat "$T/b/com8/links.json" 2>/dev/null)"
if [ "$(printf '%s' "$lk" | jget alice key_fp)" = "$FP" ]; then
  ok "pinned fingerprint untouched by the attempt"
else bad "pinned fingerprint untouched"; fi

echo "== granted mail flows BOTH ways with zero manual petname matching"
bcomm com8 claim librarian >/dev/null 2>&1
bcomm com8 grant alice librarian >/dev/null 2>&1
( acomm com8 ask librarian@bob "what is 2+2?" --from orchestrator --timeout 25 --json > "$T/ask.json" 2>/dev/null ) &
ASKPID=$!
deadline=$((SECONDS+15)); TOKEN=""
while [ $SECONDS -lt $deadline ]; do
  TOKEN="$(bcomm com8 inbox librarian 2>/dev/null | grep -o 'com8 reply [^ ]*' | tail -1 | awk '{print $3}')"
  [ -n "$TOKEN" ] && break
  sleep 1
done
if printf '%s' "$TOKEN" | grep -q '^orchestrator@alice~'; then
  ok "ask token crosses as agent@user ($TOKEN)"
else bad "ask token crosses as agent@user (got: $TOKEN)"; fi
bcomm com8 reply "$TOKEN" "it is 4" --from librarian >/dev/null 2>&1
wait $ASKPID
if python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("ok") and d.get("reply")=="it is 4" else 1)' "$T/ask.json" 2>/dev/null; then
  ok "cross-user ask/reply round-trips on handle-derived petnames"
else bad "cross-user ask/reply (got: $(cat "$T/ask.json" 2>/dev/null))"; fi

echo "== the stored reply is fleet-qualified (no bare-thread spoof on the r plane)"
check_rrec(){ python3 -c '
import json,sys
lines=open(sys.argv[1]).read().strip().splitlines()
d=json.loads(lines[-1])
assert d.get("from_name")=="librarian@bob", d
assert d.get("via")=="bob", d
' "$T/a/com8/mail/orchestrator/inbox.jsonl"; }
wait_for 5 "link reply stored as librarian@bob via bob (both planes carry provenance)" check_rrec

echo "== the return-path auto-grant is scoped (TTL'd, separate from human grants)"
g="$(cat "$T/a/com8/grants/bob.json" 2>/dev/null)"
if python3 -c '
import json,sys
d=json.loads(sys.argv[1])
assert "orchestrator" in (d.get("auto") or {}), d
assert "orchestrator" not in (d.get("granted") or []), d
assert (d["auto"]["orchestrator"]) > 0
' "$g" 2>/dev/null; then
  ok "auto-grant recorded under auto{} with an expiry, not as a human grant"
else bad "auto-grant shape (got: $g)"; fi

echo "== qualified names are first-class in wait (P19)"
r="$(bcomm com8 wait 'orchestrator@alice' --timeout 1 --json 2>/dev/null)"
if [ "$(printf '%s' "$r" | jget err)" = "timeout" ]; then
  ok "wait on a fleet-qualified proxy is legal (honest timeout, not a rejection)"
else bad "wait on a fleet-qualified proxy (got: $r)"; fi

echo "== per-user proxy cap (M-3)"
bcomm com8 stop >/dev/null 2>&1; sleep 0.5
COM8_PROXY_CAP=2 bcomm com8 start >/dev/null 2>&1; sleep 1
for s in s1 s2 s3; do
  acomm com8 send librarian@bob "from $s" --from "$s" >/dev/null 2>&1
done
sleep 3
nprox="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for k in d if k.endswith("@alice")))' "$T/b/com8/identities.json" 2>/dev/null)"
if [ "$nprox" = "2" ]; then
  ok "proxy minting capped at COM8_PROXY_CAP=2 (got $nprox @alice proxies)"
else bad "proxy cap (got $nprox @alice proxies)"; fi

echo "== auto-grant expiry actually closes the path"
acomm com8 stop >/dev/null 2>&1; sleep 0.5
COM8_AUTOGRANT_TTL=1 acomm com8 start >/dev/null 2>&1; sleep 1
acomm com8 send librarian@bob "opening" --from shortlived >/dev/null 2>&1
sleep 2.5   # TTL=1s: the return path has expired by now
bdead0="$(ls "$T/b/com8/out/alice/dead" 2>/dev/null | wc -l | tr -d ' ')"
bcomm com8 send shortlived@alice "too late" --from librarian >/dev/null 2>&1
sleep 3
bdead1="$(ls "$T/b/com8/out/alice/dead" 2>/dev/null | wc -l | tr -d ' ')"
if [ "$bdead1" -gt "$bdead0" ]; then
  ok "expired return path refused (reply dead-lettered on sender)"
else bad "expired return path refused (dead $bdead0 -> $bdead1)"; fi
slbox="$(acomm com8 inbox shortlived 2>/dev/null)"
if ! printf '%s' "$slbox" | grep -q "too late"; then
  ok "nothing landed after expiry"
else bad "mail leaked through an expired auto-grant"; fi

echo "== link handle/card_v survive a daemon restart (the links allowlist)"
bcomm com8 stop >/dev/null 2>&1; sleep 0.5
bcomm com8 start >/dev/null 2>&1; sleep 1
lk="$(cat "$T/b/com8/links.json" 2>/dev/null)"
if [ "$(printf '%s' "$lk" | jget alice handle)" = "alice" ] && [ "$(printf '%s' "$lk" | jget alice card_v)" = "2" ]; then
  ok "handle + card_v survived the restart"
else bad "handle + card_v survived the restart"; fi

echo "== grant fp pin: a re-keyed link does not inherit grants"
python3 - "$T/b/com8/links.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["alice"]["key_fp"] = "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
open(p, "w").write(json.dumps(d, indent=1))
PY
bcomm com8 stop >/dev/null 2>&1; sleep 0.5
bcomm com8 start >/dev/null 2>&1; sleep 1
adead0="$(ls "$T/a/com8/out/bob/dead" 2>/dev/null | wc -l | tr -d ' ')"
acomm com8 send librarian@bob "post-rekey" --from orchestrator >/dev/null 2>&1
sleep 3
adead1="$(ls "$T/a/com8/out/bob/dead" 2>/dev/null | wc -l | tr -d ' ')"
libbox="$(bcomm com8 inbox librarian 2>/dev/null)"
if [ "$adead1" -gt "$adead0" ] && ! printf '%s' "$libbox" | grep -q "post-rekey"; then
  ok "grants pinned to the ORIGINAL key: swapped-key link refused"
else bad "grant fp pin (dead $adead0 -> $adead1)"; fi

echo "== the re-key ceremony: revoke clears the pin, re-connect works"
bcomm com8 federate revoke alice >/dev/null 2>&1
if ! grep -q '"fp"' "$T/b/com8/grants/alice.json" 2>/dev/null; then
  ok "revoke cleared the persisted fingerprint pin"
else bad "revoke cleared the pin (got: $(cat "$T/b/com8/grants/alice.json" 2>/dev/null))"; fi
out="$(bcomm com8 connect @alice --code "$CODE" --yes --direct 2>&1)"
if [ $? -eq 0 ]; then
  ok "re-connect after revoke accepted (the documented re-key path is livable)"
else bad "re-connect after revoke (got: $out)"; fi
bcomm com8 grant alice librarian >/dev/null 2>&1
acomm com8 send librarian@bob "after re-key ceremony" --from orchestrator >/dev/null 2>&1
wait_for 12 "mail flows again after the ceremony" \
  grep -q "after re-key ceremony" "$T/b/com8/mail/librarian/inbox.jsonl"

echo "== links restore-tuple guard: the persisted link record's key set is pinned"
lkeys="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print(",".join(sorted(d["alice"].keys())))' "$T/b/com8/links.json" 2>/dev/null)"
lgolden="addr,allow_seats,card_v,created_at,handle,identity_file,key_fp,kind,remote_home,remote_in,sock"
if [ "$lkeys" = "$lgolden" ]; then
  ok "link record keys unchanged ($lkeys)"
else bad "link record keys unchanged (got: $lkeys, want: $lgolden)"; fi

echo "== sun_path guard: a too-long socket path fails loud, daemon stays up"
LONG="$T/cs-long-$(python3 -c 'print("x"*60)')"
mkdir -p "$LONG" "$T/c-sess"
r="$(COMM_STATE="$T/c" COM8_SOCK_DIR="$LONG" COM8_SESSIONS_DIR="$T/c-sess" COM8_SELF=carol-dev COM8_TICK=1 "$COMM" com8 start 2>&1; \
     COMM_STATE="$T/c" COM8_SOCK_DIR="$LONG" COM8_SESSIONS_DIR="$T/c-sess" COM8_SELF=carol-dev COM8_TICK=1 "$COMM" com8 claim "$(python3 -c 'print("n"*40)')" 2>&1)"
if printf '%s' "$r" | grep -qi "too long\|sun_path\|104"; then
  ok "over-long socket path refused with a clear reason"
else bad "sun_path guard (got: $r)"; fi
st="$(COMM_STATE="$T/c" COM8_SOCK_DIR="$LONG" COM8_SESSIONS_DIR="$T/c-sess" COM8_SELF=carol-dev "$COMM" com8 status --json 2>/dev/null)"
if [ "$(printf '%s' "$st" | jget ok)" = "True" ]; then ok "daemon healthy after the refusal"; else bad "daemon healthy after refusal"; fi

echo
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
