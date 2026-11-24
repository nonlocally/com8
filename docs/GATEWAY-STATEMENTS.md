# Gateway statements

This note is the contract between an HTTPS gateway and the bus broker for
saying who the signed-in viewer is. It is written for whoever builds or runs
the gateway. The receiving side is `lib/bus_statement.py`, and
`scripts/test-bus-statement.py` tests each rule that module enforces.

Status: the verifier exists, but the broker does not read this header yet.
Until it does, the `X-Communicate-Bus-*` headers and `BUS_GATEWAY_SHARED_SECRET`
described in [Buses](BUSES.md#operating-a-self-hosted-origin) remain what the
broker trusts.

## What the gateway sends

One header on each request it forwards for a signed-in viewer:

    X-Nonlocally-Viewer: <statement>

The statement is a JWS in compact serialization (RFC 7515) signed with Ed25519
(`EdDSA`, RFC 8037): three base64url segments joined by two dots.

- Send the header exactly once, and remove any `X-Nonlocally-Viewer` that
  arrives from a client. The broker refuses a request that carries it more
  than once, and one whose value was joined from several.
- The whole value is at most 8192 bytes; see [Size](#size).
- Each segment is base64url without `=` padding, with no whitespace or line
  breaks, as any standard encoder produces it. A segment whose unused trailing
  bits are not zero is refused.
- The header and payload are UTF-8 JSON objects. No name may appear twice in
  an object. The order of the names does not matter. Integers are written as
  integers: `1700000000`, never `1700000000.0` or `1.7e9`.

## Protected header

Exactly three parameters:

    {"alg":"EdDSA","typ":"nl-viewer+jwt","kid":"<kid>"}

`kid` is the kid of the signing key, as [Keys](#keys) derives it, and that key
must be pinned at the broker. Any other `alg` (including `none` and the HMAC
family), any other `typ`, a `kid` that is not pinned, and any further
parameter (`crit`, `jwk`, `jku`, `x5c`, `b64` and so on) are refused. The
header's segment is at most 256 characters; the one above has 87.

## Claims

All are required except `name`. A claim name that is not in this table is
refused, so that a misspelled name cannot silently drop a check.

| Claim | Type | Rule |
| --- | --- | --- |
| `iss` | string | The gateway's own identifier, for example `https://bus.example.org/_gateway`. The broker accepts the one issuer in its `BUS_GATEWAY_ISSUER` setting and compares exactly; there is no default. |
| `aud` | string | The service the statement is for: `bus` for a bus broker. A string, never a list. The broker compares it exactly with its own audience. |
| `sub` | string | `owui:` and the viewer's OpenWebUI account UUID in lowercase canonical form (8-4-4-4-12). |
| `groups` | list of strings | The viewer's OpenWebUI group UUIDs, lowercase canonical: at most 64, no repeats, sorted ascending as plain strings. May be empty. See [Groups](#groups). |
| `groups_at` | integer | Unix time in seconds at which the gateway read that list from OpenWebUI. Not more than 5 s ahead of the broker's clock. |
| `name` | string, optional | Display name, for display only: see [Display names](#display-names). Leave `name` out when there is none; anything but a string, `null` included, is refused. |
| `iat` | integer | Unix time in seconds at which the statement was signed. |
| `exp` | integer | `iat + 60`, exactly. |
| `jti` | string | 22 base64url characters, new for every statement: 16 random bytes, encoded without padding. |
| `htm` | string | The request method, upper-case letters A to Z only, for example `GET`. |
| `htu` | string | The request target exactly as sent to the broker: the path and, when there is one, `?` and the query. It begins with `/`, is at most 2048 bytes, and holds only printable ASCII (0x21 to 0x7E) without `#`: no scheme, host, fragment or space, and anything else percent-encoded. |
| `bdy` | string | Unpadded base64url of the SHA-256 of the exact request body bytes. Without a body, the hash of the empty string, `47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU`. |

The payload is one object, and `groups` is the only list in it. A payload with
far more structure than that, more than 16 `{` and `[` outside its strings, is
refused without being parsed.

A statement the gateway makes for another service is not accepted by the bus,
although the same key signs it: another `aud`, a claim this table does not
list, a missing `htm`, `htu` or `bdy`, or another lifetime each refuse it.

## Groups

A statement carries at most 64 groups. For a viewer who belongs to more, a
gateway refuses to sign. It never truncates the list.

## Display names

`name` is shown to people and decides nothing, so a name the broker will not
show does not cost the viewer the request. The broker keeps a name of at most
160 bytes of UTF-8 that holds none of the code points below. Any other name is
dropped: the statement is accepted as if it had been sent without one.

    U+0000-001F, U+007F-009F, U+00AD, U+061C, U+180E, U+200B-200F,
    U+2028-202E, U+2060-206F, U+FDD0-FDEF, U+FEFF, U+FFF9-FFFB,
    U+1D173-1D17A, U+E0000-E007F, and every U+xFFFE and U+xFFFF

This is a fixed list: the control characters, a set of format characters and
separators that show as nothing or change the direction of text, and the
noncharacters. It does not follow the Unicode version of whatever runs the
broker, and it is not every character that shows as nothing: U+034F, U+115F,
U+1160, U+180B, U+2800, U+3164 and U+FFA0, for instance, are kept. A gateway
omits `name` when it is too long or holds one of the listed code points, since
the broker would drop it anyway.

A lone surrogate is not text. A payload that holds one anywhere, as half of a
`\uD83D\uDE00` pair without the other, is refused, in `name` as in any other
claim.

## Binding to the request

A statement authorizes one request: this method, this target and this body.

- Sign last. Anything that rewrites the path or query, or serializes the body
  again after signing, makes the statement wrong for the request.
- The comparison is byte for byte and nothing is normalized. `/a` and `/a/`,
  `?x=1&y=2` and `?y=2&x=1`, `%2F` and `%2f` are different targets.
- The broker does not fold the case of the method either. `htm` is upper case,
  so the method on the request line must be upper case too: a request sent as
  `post` does not match a statement that says `POST`.
- A target that does not have the shape the `htu` row describes cannot be
  signed for: the broker refuses the statement whatever the request was.
- Send a body with a `Content-Length` and without a `Transfer-Encoding`. The
  bus broker reads a posted body by that length and refuses a request that
  has a `Transfer-Encoding`, so `bdy` is to be the hash of exactly the bytes
  that the length covers.
- Make a new statement, with a new `jti`, for every request, a retry included.
  The broker accepts each statement once: it keeps the `jti` on record until
  the statement has expired, and a second request with the same statement is
  refused.
- A broker that has just started may refuse statements issued before it came
  up or in its first 5 seconds, because it cannot always know which of them an
  earlier process accepted. A request refused then is to be sent again with a
  new statement.

## Size

The header value is at most 8192 bytes, and a statement over that is refused
for its size. Beside a protected header of 87 characters and the signature's
86, that leaves the payload up to 6012 bytes of JSON. Sixty-four groups take
about 2500 of them, a 160-byte name 170, and the remaining claims about 350
with a target of `/` and an issuer of 100 characters. More than 2900 bytes of
JSON are left for `htu`.

With 64 groups, a 160-byte name and a 2048-byte target, all of plain
characters, a statement with that issuer is under 7000 bytes when its JSON is
written without padding. It is the JSON that counts, though, and JSON must
escape `"` and `\`: each one in the name or the target costs two bytes. Both
may stand in a target, so a target within its 2048 bytes can make a statement
too large. Beside 64 groups and such a name, a target of `/` and backslashes
fits up to about 1500 bytes. A serializer that pads its JSON, or escapes what
it need not, uses the room up sooner.

## Lifetimes and clocks

- `groups_at`, `iat` and `exp` are each a whole number of seconds since 1970,
  from 0 to 9007199254740991 (2^53 - 1).
- `exp - iat` is exactly 60 seconds.
- A statement is refused when `iat` is more than 5 seconds ahead of the
  broker's clock, and from the second `exp` is reached. A gateway clock that
  runs ahead by more than 5 seconds therefore fails every request; one that
  runs behind shortens the 60 seconds.
- `groups_at` must be the time the group list was really read. The broker
  always applies a maximum age to it, a whole number of seconds that its
  operator sets. An older list is not an error: the statement still verifies
  and identifies the viewer, and its groups count as empty.
- Setting the broker's clock back does not let a statement be used twice, but
  it makes the broker refuse. A statement whose `iat` is then more than 5
  seconds ahead of the clock is refused for that. And once the clock has been
  set back by about a minute or more, a broker that keeps its record of
  accepted statements in memory refuses every statement until the clock has
  caught up with where it was. The safe course is to wait. Such a broker,
  restarted after its clock was set back, has lost that record and can accept
  again a statement it accepted before the step.

## Errors

Every failure has the same result: the verifier refuses the statement with the
one code `unauthorized`. A broker is to answer that as HTTP 401, and its
response is not to say which check failed. The verifier's error prints as that
one word. It keeps the reason apart, in fixed words such as `claims: htu is not
the request target as received`, for the broker's journal only. Nothing is
repaired on the way: no trimming, no case folding, no padding tolerated, no
clock allowance beyond the 5 seconds above.

## Keys

The gateway holds an Ed25519 private key. The broker holds public keys only, in
its private setting `BUS_GATEWAY_SIGNERS`: a JSON object from `kid` to the raw
32-byte public key in unpadded base64url (the `x` value of the key's JWK).
Whenever signers are configured the broker also needs `BUS_GATEWAY_ISSUER`, the
one `iss` it accepts; there is no built-in default. A bus broker's audience is
`bus`.

- A key has one kid: `gw-` followed by the first 16 characters of the unpadded
  base64url of the SHA-256 of the 32 raw public key bytes.
  `bus_statement.kid_for` computes it. The gateway sends that kid and the
  broker pins the key under it.
- 1 to 8 keys.
- The setting is rejected as a whole if it does not parse, repeats a `kid`,
  holds a key that is not a valid Ed25519 public key (a wrong length, a
  non-canonical encoding, a point off the curve, one of small order or one
  outside the subgroup of prime order), or labels a key with any kid but its
  own. A mislabelled key is a configuration error: check which key was meant
  before changing the label.
- The public key is not a secret, but it is the whole of what the broker
  trusts. Change it only through the broker's own settings.

To rotate: add the new public key under its kid and restart the broker; switch
the gateway to the new key; after at least 65 seconds remove the old entry. A
key must be pinned before the gateway signs with it. Removing an entry refuses
that key's statements at once, which is also how a compromised key is
withdrawn.

Never pin a key taken from documentation or from a test: anyone can sign with
a key whose private half is in print. `load_keys` refuses the ones it knows:
the test keys of RFC 8032 and RFC 8410, the key of the all-zero seed and the
base point. It also refuses the example key of this note. That list guards
against pinning this repository's own fixtures and is not a complete one. The
fixed vectors below are signed with the first test key of RFC 8032, so the
tests that load it say `allow_published=True`. Nothing else may.

## What the broker derives

From a verified statement the broker takes the reader `owui.<uuid>` and a
reader hash: the SHA-256 (hex) of `com8/statement-reader/v1`, a NUL byte, the
issuer, a NUL byte and `sub`. The gateway sends neither.

## Fixed vectors

`scripts/fixtures/gateway-statement-vectors.json` holds four statements
produced by a gateway's signer, kept as that signer wrote them, and
`scripts/test-bus-statement.py` checks the broker's verifier against them.

- `bus-post` and `bus-get-session` are made for the bus and verify for their
  requests. In `bus-post` the signer was given the method as `post` and wrote
  `htm` as `POST`; it verifies for a request whose method is `POST`.
- `other-plain` and `other-post` are made for another audience with the same
  key and issuer, and the bus refuses both. `other-plain` binds no request.
  `other-post` binds a request of its own and lacks nothing: a verifier of
  that audience accepts it, so `aud` alone keeps it from the bus.

## Worked example

Every value here is invented. The key pair was made for this page, and the
statement expired in November 2023. Being printed here, the key is one that
`load_keys` refuses by default, like the other keys named under [Keys](#keys).

`BUS_GATEWAY_ISSUER` at the broker is `https://bus.example.org/_gateway`, and
`BUS_GATEWAY_SIGNERS` is:

```json
{"gw-L90YFoCw3Q0_f3H4": "a5R2yjtE8wY0FL8EkFbfhZZdYDhOfkMa4xmw1AprxX4"}
```

Protected header:

```json
{"alg": "EdDSA", "typ": "nl-viewer+jwt", "kid": "gw-L90YFoCw3Q0_f3H4"}
```

Claims:

```json
{
  "iss": "https://bus.example.org/_gateway",
  "aud": "bus",
  "sub": "owui:00000000-0000-4000-8000-000000000001",
  "groups": [
    "00000000-0000-4000-8000-00000000000a",
    "00000000-0000-4000-8000-00000000000b"
  ],
  "groups_at": 1699999990,
  "name": "Example Viewer",
  "iat": 1700000000,
  "exp": 1700000060,
  "jti": "EXAMPLEjtiEXAMPLEjti00",
  "htm": "GET",
  "htu": "/_bus/example?limit=10",
  "bdy": "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
}
```

The request as the broker receives it, without a body:

```http
GET /_bus/example?limit=10 HTTP/1.1
X-Nonlocally-Viewer: eyJhbGciOiJFZERTQSIsInR5cCI6Im5sLXZpZXdlcitqd3QiLCJraWQiOiJndy1MOTBZRm9DdzNRMF9mM0g0In0.eyJpc3MiOiJodHRwczovL2J1cy5leGFtcGxlLm9yZy9fZ2F0ZXdheSIsImF1ZCI6ImJ1cyIsInN1YiI6Im93dWk6MDAwMDAwMDAtMDAwMC00MDAwLTgwMDAtMDAwMDAwMDAwMDAxIiwiZ3JvdXBzIjpbIjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwYSIsIjAwMDAwMDAwLTAwMDAtNDAwMC04MDAwLTAwMDAwMDAwMDAwYiJdLCJncm91cHNfYXQiOjE2OTk5OTk5OTAsIm5hbWUiOiJFeGFtcGxlIFZpZXdlciIsImlhdCI6MTcwMDAwMDAwMCwiZXhwIjoxNzAwMDAwMDYwLCJqdGkiOiJFWEFNUExFanRpRVhBTVBMRWp0aTAwIiwiaHRtIjoiR0VUIiwiaHR1IjoiL19idXMvZXhhbXBsZT9saW1pdD0xMCIsImJkeSI6IjQ3REVRcGo4SEJTYS1fVEltVy01SkNldVFlUmttNU5NcEpXWkczaFN1RlUifQ.E0Sr221ArExc810dFZAHfA7M3ijOGmM5TWYUNzvtzXxYcAAtmRthrmgQ9KFNwIcSPow_PaaDhAqX8SZkNQJ1Dg
```

With the broker's clock at 1700000000 (2023-11-14T22:13:20Z) this verifies. The
reader is `owui.00000000-0000-4000-8000-000000000001` and its hash is
`aa8ec41a7acf8f0a5da1dbe2e25b228703bfe5083af0ad9b3abed00bf90cb4b6`. From
1700000060 on it is refused as expired, and changing one character of the
method, the target, the body or the statement refuses it at any time.

A gateway's own output can be checked the same way from a checkout, with every
value of the header as the request carried it, the method, target, body,
clock, issuer and audience of that request, the oldest group list to honour, a
record of accepted statements, and a file that holds the `BUS_GATEWAY_SIGNERS`
value. None of these arguments has a default. The record here is given a clock
an hour behind the statement, as if the broker had been up that long; a broker
makes one record when it starts and keeps it. `allow_published=True` is there
only because this note's key is one that `load_keys` refuses, and has no place
in a broker:

```sh
python3 - "$STATEMENT" <<'EOF'
import sys
sys.path.insert(0, "lib")
import bus_statement
now = 1700000000
keys = bus_statement.load_keys(open("gateway-signers.json").read(), allow_published=True)
print(bus_statement.verify_request(
    header_values=[sys.argv[1]], keys=keys, issuer="https://bus.example.org/_gateway",
    audience="bus", method="GET", target="/_bus/example?limit=10", body=b"", now=now,
    max_groups_age=300, replay=bus_statement.MemoryReplay(clock=lambda: now - 3600)))
EOF
```
