# Released broker compatibility fixture

`bus_broker_v04.py` is the unmodified `lib/bus_broker.py` from COM8 v0.4.0,
commit `f043986d2c4509870d11325911d96395cd13bdc3` (MIT license).
SHA-256: `bb5aeb1cf17dad3c36ab84c346c0bbdf8f8285c5b6ee8a21e51c329f9ef7ea63`.

The ownership regression imports it only to exercise the real released database
startup, bus creation, enrollment and delivery during an upgrade/rollback/upgrade
cycle in a temporary directory. It never starts a server. Do not update this
fixture when changing the current broker: its frozen behavior is the check.

The fixture belongs to the test tree and is not included in the runtime package.

# Gateway statement vectors

`gateway-statement-vectors.json` holds four signed viewer statements. They
were supplied by the gateway team that signs for the hosted hub. The file is
kept byte for byte as it was supplied.
SHA-256: `f2b73340b019a3c98d135fd5dc2164ecca8beba28efff789fcf009a9963ded44`.

`scripts/test-bus-statement.py` checks the verifier against all four. It reads
each statement's name, audience and request from the file.

- `bus-post` and `bus-get-session` are made for the bus, and each verifies for
  its own request. The `bus-post` options give the method as `post` while the
  signed `htm` claim says `POST`; the verifier is given `POST`, as a request
  line carries it, and a test also pins that the lower-case spelling is
  refused.
- `other-plain` and `other-post` are made for another audience, under the same
  key and issuer, and the bus refuses both because of the audience.
  `other-plain` binds no request and is refused under its own audience as
  well. `other-post` binds a request of its own and has every claim a
  statement for the bus has: asked as its own audience it verifies, so the
  audience is all that keeps it from the bus.

The issuer inside the vectors is signed data: it is the one that signer used.
The tests read it from the file and pass it to the verifier, which has no
default issuer.

The statements are signed with the RFC 8032 section 7.1 TEST 1 key. Its seed
is in the file and printed in the RFC, so anyone can sign with it. It is a
fixture key: `load_keys` refuses to pin it, and only these tests load it, with
`allow_published=True`. Do not edit the file to make a test pass: a vector
that stops verifying is a finding about the verifier or the signer.
