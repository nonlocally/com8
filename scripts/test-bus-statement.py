#!/usr/bin/env python3
"""Offline tests for lib/bus_statement.py, the gateway's signed viewer statement.

Every statement these tests make is signed by a node child process, so the
verifier meets an independent Ed25519 implementation and, for most cases,
JavaScript's own JSON serializer, as it will behind a real gateway. The
signing keys are made in that process and never leave it. Without a node that
supports Ed25519 the file reports itself unqualified instead of passing, and a
run of only some of its tests ends in PARTIAL, not PASS.

Every refusal carries the one code `unauthorized`, so each case also names the
reason it expects: a statement refused for the wrong reason is a failure.

Two kinds of statement were signed elsewhere and are verified as they stand:
those of scripts/fixtures/gateway-statement-vectors.json, which the gateway
team supplied, and the worked example of docs/GATEWAY-STATEMENTS.md.
"""
import base64
import concurrent.futures
import dataclasses
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import typing
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import bus_statement
from bus_statement import (MemoryReplay, Statement, StatementError, kid_for, load_keys, verify_request,
                           verify_statement)

NOW = 1_800_000_000
UUID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
# SHA-256 of "com8/statement-reader/v1" NUL ISSUER NUL "owui:" UUID, computed
# outside Python with printf and shasum.
READER_HASH = "60a5f230ce868920e94fee47234b9754a12a69467a06c697bac3389148cb1181"
GROUPS = ["0b3c1f8e-52a7-4d19-8c64-9a1e5f7d2b30", "5e8d2a41-9c3b-4f76-a1d0-6b7c8e9f0a12",
          "c4a7e9b2-1d36-4f58-9e0a-3b2c1d4e5f67"]
OTHER_UUID = "2f1e0d9c-8b7a-4f65-9e4d-3c2b1a098f76"
ISSUER = "https://bus.example.org/_gateway"
EMPTY_BODY_DIGEST = "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"
HEADER = {"alg": "EdDSA", "typ": "nl-viewer+jwt", "kid": None}     # the kid of key "a", set in setUpModule
UNPINNED_KID = "gw-AAAAAAAAAAAAAAAA"                                # well formed, and the kid of no key here
FIXTURE = ROOT / "scripts" / "fixtures" / "gateway-statement-vectors.json"
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
ABSENT = object()
BACKSLASH = chr(92)

SIGNER = None
PUBLIC = {}             # signer key name -> base64url public key
KID = {}                # signer key name -> the kid the gateway's rule gives that key
KEYS = {}               # kid -> raw public key, as load_keys returns it
UNQUALIFIED = []

NODE_PROBE = """
const crypto = require('crypto');
const pair = crypto.generateKeyPairSync('ed25519');
const signature = crypto.sign(null, Buffer.from('probe'), pair.privateKey);
if (signature.length !== 64 || !crypto.verify(null, Buffer.from('probe'), pair.publicKey, signature)) process.exit(3);
process.stdout.write('ed25519-ok');
"""

# One request per line. `jws` is what a gateway does: JSON.stringify, base64url,
# sign. `sign` signs exact bytes, for text that no serializer would produce.
# `public` derives a public key from a seed that a specification prints.
NODE_SIGNER = r"""
const crypto = require('crypto');
const keys = {};
const b64url = (buffer) => buffer.toString('base64').replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_');
require('readline').createInterface({input: process.stdin}).on('line', (line) => {
  const request = JSON.parse(line);
  let reply;
  if (request.op === 'key') {
    const pair = crypto.generateKeyPairSync('ed25519');
    keys[request.name] = pair.privateKey;
    reply = {public: b64url(pair.publicKey.export({type: 'spki', format: 'der'}).subarray(-32))};
  } else if (request.op === 'jws') {
    const input = b64url(Buffer.from(JSON.stringify(request.header), 'utf8')) + '.' +
      b64url(Buffer.from(JSON.stringify(request.claims), 'utf8'));
    reply = {token: input + '.' + b64url(crypto.sign(null, Buffer.from(input, 'ascii'), keys[request.name]))};
  } else if (request.op === 'sign') {
    reply = {signature: crypto.sign(null, Buffer.from(request.data, 'hex'), keys[request.name]).toString('hex')};
  } else if (request.op === 'public') {
    const key = crypto.createPrivateKey({key: Buffer.concat([Buffer.from('302e020100300506032b657004220420', 'hex'),
      Buffer.from(request.seed, 'hex')]), format: 'der', type: 'pkcs8'});
    reply = {public: b64url(crypto.createPublicKey(key).export({type: 'spki', format: 'der'}).subarray(-32))};
  } else if (request.op === 'sha256') {
    reply = {hex: crypto.createHash('sha256').update(Buffer.from(request.data, 'hex')).digest('hex')};
  }
  process.stdout.write(JSON.stringify(reply) + '\n');
});
"""


class Signer:
    def __init__(self, node):
        self.process = subprocess.Popen([node, "-e", NODE_SIGNER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, encoding="utf-8", bufsize=1)

    def call(self, **request):
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("the node signer exited")
        return json.loads(line)

    def close(self):
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.wait(timeout=30)


def setUpModule():
    global SIGNER
    node = shutil.which("node")
    reason = None
    if node is None:
        reason = "node is not installed"
    else:
        try:
            probe = subprocess.run([node, "-e", NODE_PROBE], capture_output=True, text=True, timeout=60)
            if probe.returncode != 0 or probe.stdout != "ed25519-ok":
                reason = "node has no Ed25519 support"
        except (OSError, subprocess.TimeoutExpired) as error:
            reason = "node could not be run (%s)" % type(error).__name__
    if reason is not None:
        UNQUALIFIED.append(reason)
        raise unittest.SkipTest("UNQUALIFIED: " + reason)
    SIGNER = Signer(node)
    for name in ("a", "b", "attacker"):
        PUBLIC[name] = SIGNER.call(op="key", name=name)["public"]
        KID[name] = kid_of(PUBLIC[name])
    HEADER["kid"] = KID["a"]
    KEYS.update(load_keys(pinned("a", "b")))


def tearDownModule():
    if SIGNER is not None:
        SIGNER.close()


def b64url(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64url(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def digest(body):
    return b64url(hashlib.sha256(body).digest())


def kid_of(public):
    """The gateway's kid rule, restated here without the module's kid_for."""
    return "gw-" + b64url(hashlib.sha256(unb64url(public)).digest())[:16]


def pinned(*names):
    """A BUS_GATEWAY_SIGNERS setting that pins these signer keys."""
    return json.dumps({KID[name]: PUBLIC[name] for name in names})


def running_replay(now=NOW):
    """A record of accepted statements that began an hour before the test's clock."""
    return MemoryReplay(clock=lambda: now - 3600)


def signed(claims, header=None, key="a"):
    """A compact JWS serialized and signed by node."""
    return SIGNER.call(op="jws", name=key, header=HEADER if header is None else header, claims=claims)["token"]


def signed_segments(header_segment, payload_segment, key="a"):
    """Sign two segments exactly as given, whatever they contain."""
    signing_input = header_segment + "." + payload_segment
    signature = SIGNER.call(op="sign", name=key, data=signing_input.encode("ascii").hex())["signature"]
    return signing_input + "." + b64url(bytes.fromhex(signature))


def signed_raw(header, payload, key="a"):
    """Sign exact header and payload text."""
    if isinstance(header, dict):
        header = json.dumps(header, separators=(",", ":"))
    return signed_segments(b64url(header), b64url(payload), key)


def loosened(segment):
    """The same bytes to a lenient decoder, spelled with non-zero spare bits."""
    assert len(segment) % 4 in (2, 3), "this segment has no spare bits"
    return segment[:-1] + ALPHABET[ALPHABET.index(segment[-1]) | 1]


def generate(cls, prefix, rows, run):
    """One named test per row of a table."""
    for row in rows:
        name = "test_%s_%s" % (prefix, row[0])
        assert not hasattr(cls, name), name

        def test(self, row=row):
            run(self, *row[1:])
        test.__name__ = name
        setattr(cls, name, test)


class Case(unittest.TestCase):
    method = "GET"
    target = "/_bus/example?since=41&limit=10"
    body = b""

    def setUp(self):
        # The control: the unmodified statement is accepted in every test, so a
        # refusal below is caused by the one thing that test changes.
        self.good = signed(self.claims())
        self.statement = self.accept(self.good)

    def claims(self, **changes):
        claims = {"iss": ISSUER, "aud": "bus", "sub": "owui:" + UUID, "groups": list(GROUPS),
                  "groups_at": NOW - 20, "name": "Ada Example", "iat": NOW, "exp": NOW + 60,
                  "jti": "q83vEjRWeJCrze8SNFZ4kA", "htm": self.method, "htu": self.target,
                  "bdy": digest(self.body)}
        for name, value in changes.items():
            if value is ABSENT:
                del claims[name]
            else:
                claims[name] = value
        return claims

    def raw_claims(self, **changes):
        return json.dumps(self.claims(**changes), separators=(",", ":"))

    def accept(self, token, **overrides):
        # A new record for each call, so that a test may verify one statement
        # several ways. The tests of replay pass a record of their own.
        arguments = dict(keys=KEYS, method=self.method, target=self.target, body=self.body, now=NOW,
                         issuer=ISSUER, audience="bus", max_groups_age=300, replay=running_replay())
        arguments.update(overrides)
        # Through the entry point a broker uses, as the one value of the header.
        return verify_request(header_values=[token], **arguments)

    def refuse(self, token, reason, **overrides):
        recorder = Recorder()
        overrides.setdefault("replay", recorder)
        with self.assertRaises(StatementError) as caught:
            self.accept(token, **overrides)
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertIn(reason, caught.exception.reason)
        # Whatever prints the error shows the one fixed word and not the reason.
        self.assertEqual(str(caught.exception), "unauthorized")
        # Refused for any reason of the verifier's own, a statement is never claimed.
        self.assertEqual(recorder.claims, [])
        return caught.exception


class Refusals(Case):
    """What a refusal shows to code that prints it, and what it keeps for the journal."""

    def test_printing_a_refusal_never_shows_the_reason(self):
        error = self.refuse(signed(self.claims(aud="other")), "claims: aud is not the configured audience")
        self.assertEqual(error.reason, "claims: aud is not the configured audience")
        self.assertEqual(error.code, "unauthorized")
        shown = [str(error), repr(error), "%s" % error, "{}".format(error), "{!r}".format(error),
                 "".join(traceback.format_exception_only(type(error), error)), repr(error.args)]
        self.assertEqual(shown[0], "unauthorized")
        self.assertEqual(shown[1], "StatementError('unauthorized')")
        for text in shown:
            self.assertIn("unauthorized", text)
            self.assertNotIn("aud", text)
            self.assertNotIn("claims", text)
        self.assertFalse(hasattr(error, "message"), "nothing invites a caller to print the reason")

    def test_every_refusal_reads_the_same(self):
        refusals = [self.refuse("", "structure:"), self.refuse("A" * 9000, "size:"),
                    self.refuse(signed(self.claims(), key="attacker"), "signature:"),
                    self.refuse(self.good, "claims: htm", method="POST"),
                    self.refuse(self.good, "verifier:", now=None)]
        self.assertEqual({str(error) for error in refusals}, {"unauthorized"})
        self.assertEqual(len({error.reason for error in refusals}), len(refusals))

    def test_a_refusal_is_made_from_its_reason_alone(self):
        error = StatementError("replay: an example reason")
        self.assertEqual((error.code, error.reason, str(error), error.args),
                         ("unauthorized", "replay: an example reason", "unauthorized", ("unauthorized",)))
        self.assertRaises(TypeError, StatementError)
        self.assertRaises(TypeError, StatementError, "unauthorized", "a reason")


class ValidStatements(Case):
    def test_get_with_an_empty_body(self):
        statement = self.statement
        self.assertIsInstance(statement, Statement)
        self.assertEqual(digest(b""), EMPTY_BODY_DIGEST)
        self.assertEqual(json.loads(unb64url(self.good.split(".")[1]))["bdy"], EMPTY_BODY_DIGEST)
        self.assertEqual(statement.sub, "owui:" + UUID)
        self.assertEqual(statement.uuid, UUID)
        self.assertEqual(statement.groups, tuple(GROUPS))
        self.assertEqual(statement.groups_at, NOW - 20)
        self.assertIs(statement.groups_stale, False)
        self.assertEqual(statement.name, "Ada Example")
        self.assertIs(statement.name_dropped, False)
        self.assertEqual(statement.jti, "q83vEjRWeJCrze8SNFZ4kA")
        self.assertEqual(statement.exp, NOW + 60)
        self.assertEqual(statement.kid, KID["a"])

    def test_no_body_may_be_passed_as_none(self):
        self.assertEqual(self.accept(self.good, body=None), self.statement)

    def test_reader_and_reader_hash_for_a_fixed_uuid(self):
        self.assertEqual(self.statement.reader, "owui." + UUID)
        material = b"com8/statement-reader/v1\x00https://bus.example.org/_gateway\x00owui:" + UUID.encode("ascii")
        self.assertEqual(SIGNER.call(op="sha256", data=material.hex())["hex"], READER_HASH,
                         "node's SHA-256 agrees with the value shasum gave")
        self.assertEqual(self.statement.reader_hash, READER_HASH)
        other = self.accept(signed(self.claims(sub="owui:" + OTHER_UUID)))
        self.assertEqual(other.reader, "owui." + OTHER_UUID)
        self.assertNotEqual(other.reader_hash, READER_HASH)
        self.assertRegex(other.reader_hash, r"\A[0-9a-f]{64}\Z")

    def test_reader_hash_covers_the_issuer(self):
        # The same viewer vouched for by another gateway is another reader.
        issuer = "https://hub.example.net/_gateway"
        statement = self.accept(signed(self.claims(iss=issuer)), issuer=issuer)
        self.assertEqual((statement.sub, statement.reader), (self.statement.sub, self.statement.reader))
        material = b"com8/statement-reader/v1\x00" + issuer.encode("ascii") + b"\x00owui:" + UUID.encode("ascii")
        self.assertEqual(statement.reader_hash, SIGNER.call(op="sha256", data=material.hex())["hex"])
        self.assertNotEqual(statement.reader_hash, READER_HASH)

    def test_nothing_a_caller_must_decide_has_a_default(self):
        # Whose statements, for which audience, how old a group list may be:
        # a caller that leaves one out gets a TypeError, never a silent default.
        complete = dict(keys=KEYS, method=self.method, target=self.target, body=self.body, now=NOW,
                        issuer=ISSUER, audience="bus", max_groups_age=300, replay=Recorder())
        parameters = inspect.signature(verify_statement).parameters
        self.assertEqual(list(parameters), ["token"] + list(complete))
        for name in complete:
            with self.subTest(name):
                self.assertIs(parameters[name].default, inspect.Parameter.empty)
                self.assertEqual(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
                without = {key: value for key, value in complete.items() if key != name}
                self.assertRaises(TypeError, verify_statement, self.good, **without)
        self.assertEqual(verify_statement(self.good, **complete), self.statement)
        self.assertEqual(len(complete["replay"].claims), 1, "only the complete call got as far as a claim")
        self.assertFalse(hasattr(bus_statement, "DEFAULT_AUDIENCE"))
        self.assertEqual(bus_statement.BUS_AUDIENCE, "bus")

    def test_statement_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.statement.groups = ()
        self.assertIs(type(self.statement.groups), tuple)
        self.assertEqual([field.name for field in dataclasses.fields(Statement)],
                         ["sub", "uuid", "reader", "reader_hash", "groups", "groups_at", "groups_stale",
                          "name", "name_dropped", "jti", "exp", "kid"])

    def test_python_serialization_is_accepted_too(self):
        # Escaped non-ASCII, spaces after separators, the names in another order.
        claims = self.claims(name="Zo\u00eb \u4f8b")
        text = json.dumps(dict(reversed(list(claims.items()))), indent=1)
        header = json.dumps(dict(reversed(list(HEADER.items()))), indent=1)
        self.assertEqual(self.accept(signed_raw(header, text)).name, "Zo\u00eb \u4f8b")
        self.assertEqual(self.accept(signed(claims)).name, "Zo\u00eb \u4f8b")

    def test_group_list_bounds(self):
        self.assertEqual(self.accept(signed(self.claims(groups=[]))).groups, ())
        many = sorted("%08x-0000-4000-8000-000000000000" % number for number in range(64))
        self.assertEqual(self.accept(signed(self.claims(groups=many))).groups, tuple(many))

    def test_clock_boundaries(self):
        # iat may lead the broker's clock by five seconds; exp must still be ahead.
        self.accept(signed(self.claims(iat=NOW + 5, exp=NOW + 65)))
        self.accept(signed(self.claims(iat=NOW - 59, exp=NOW + 1)))
        self.accept(signed(self.claims(iat=NOW - 59, exp=NOW + 1)), now=NOW + 0.999)
        self.accept(signed(self.claims(groups_at=NOW + 5)))
        self.accept(self.good, now=float(NOW))

    def test_largest_statement(self):
        token = self.sized(8192)
        self.assertEqual(len(token.encode("ascii")), 8192)
        self.accept(token)
        self.refuse(self.sized(8193), "size:")

    def sized(self, size):
        """A correctly signed statement of exactly `size` bytes, padded with JSON whitespace."""
        payload = self.raw_claims().encode("ascii")
        for spaces in range(4):
            header = json.dumps(HEADER).encode("ascii") + b" " * spaces
            room = size - len(b64url(header)) - 2 - 86
            if room % 4 != 1:
                token = signed_raw(header.decode("ascii"), payload + b" " * (room * 3 // 4 - len(payload)))
                self.assertEqual(len(token), size)
                return token
        self.fail("no padding reaches %d bytes" % size)

    def test_configured_issuer_and_audience(self):
        token = signed(self.claims(iss="https://hub.example/_gateway", aud="bus-staging"))
        self.refuse(token, "claims: iss")
        self.refuse(token, "claims: aud", issuer="https://hub.example/_gateway")
        self.refuse(token, "claims: iss", audience="bus-staging")
        self.accept(token, issuer="https://hub.example/_gateway", audience="bus-staging")
        self.refuse(self.good, "claims: iss", issuer="https://hub.example/_gateway")
        self.refuse(self.good, "claims: aud", audience="bus-staging")


class PostStatements(Case):
    method = "POST"
    target = "/_bus/example"
    body = b'{"op":"send","target":"reviewer","message":"hello \xe2\x9c\x93"}'

    def test_post_with_a_body(self):
        self.assertNotEqual(digest(self.body), EMPTY_BODY_DIGEST)
        self.assertEqual(self.statement.uuid, UUID)
        self.assertEqual(self.accept(self.good, body=bytearray(self.body)), self.statement)
        self.assertEqual(self.accept(self.good, body=memoryview(self.body)), self.statement)

    def test_body_must_be_the_exact_bytes(self):
        for label, body in (("one byte changed", self.body.replace(b"hello", b"hellp")),
                            ("truncated", self.body[:-1]), ("extended", self.body + b"\n"),
                            ("empty", b""), ("absent", None), ("re-serialized", self.body.replace(b":", b": "))):
            with self.subTest(label):
                self.refuse(self.good, "claims: bdy", body=body)

    def test_statement_does_not_carry_over_to_another_method(self):
        self.refuse(self.good, "claims: htm", method="GET")
        self.refuse(self.good, "claims: htm", method="PUT")
        self.refuse(self.good, "claims: htm", method="post")


# --- protected header -------------------------------------------------------

def run_header(self, change, reason):
    header = dict(HEADER)
    for name, value in change.items():
        if value is ABSENT:
            del header[name]
        else:
            header[name] = value() if callable(value) else value    # a kid is known only once the keys exist
    self.refuse(signed(self.claims(), header=header), reason)


class HeaderViolations(Case):
    def test_alg_none_without_a_signature(self):
        header, payload, _ = signed(self.claims(), header=dict(HEADER, alg="none")).split(".")
        self.refuse(header + "." + payload + ".", "structure:")
        self.refuse(header + "." + payload, "structure:")

    def test_hmac_with_the_public_key_as_the_secret(self):
        # The key-confusion forgery: anyone can compute this, the key is public.
        raw = unb64url(PUBLIC["a"])
        for alg, function in (("HS256", hashlib.sha256), ("HS512", hashlib.sha512)):
            for label, secret in (("raw", raw), ("base64url text", PUBLIC["a"].encode("ascii"))):
                signing_input = b64url(json.dumps(dict(HEADER, alg=alg))) + "." + b64url(self.raw_claims())
                mac = hmac.new(secret, signing_input.encode("ascii"), function).digest()
                with self.subTest(alg=alg, secret=label):
                    self.refuse(signing_input + "." + b64url(mac), "header: alg")
        self.assertEqual(len(hmac.new(raw, b"", hashlib.sha512).digest()), 64, "HS512 has the length of a signature")

    def test_embedded_key_is_not_trusted(self):
        jwk = {"kty": "OKP", "crv": "Ed25519", "x": PUBLIC["attacker"]}
        self.refuse(signed(self.claims(), header=dict(HEADER, jwk=jwk), key="attacker"), "header: parameters")

    def test_a_name_twice_in_the_header(self):
        kid, other = json.dumps(KID["a"]), json.dumps(KID["b"])
        for label, text in (
                ("alg none first", '{"alg":"none","alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s}' % kid),
                ("alg none last", '{"alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s,"alg":"none"}' % kid),
                ("kid", '{"alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s,"kid":%s}' % (other, kid)),
                ("typ", '{"alg":"EdDSA","typ":"JWT","typ":"nl-viewer+jwt","kid":%s}' % kid),
                ("identical", '{"alg":"EdDSA","alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s}' % kid),
                # One name spelled two ways: a parser that compares names
                # before unescaping them would see two different names.
                ("escaped spelling", '{"alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s,"\\u0061lg":"none"}' % kid),
                ("escaped spelling first", '{"\\u0061lg":"none","alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s}' % kid)):
            with self.subTest(label):
                self.assertEqual(text.count(BACKSLASH), 1 if "escaped" in label else 0)
                self.refuse(signed_raw(text, self.raw_claims()), "header: not valid JSON")
        # The control: the same text with each name once is accepted.
        self.accept(signed_raw('{"alg":"EdDSA","typ":"nl-viewer+jwt","kid":%s}' % kid, self.raw_claims()))

    def test_not_a_json_object(self):
        for text in (json.dumps(["EdDSA", "nl-viewer+jwt", KID["a"]]), "null", '"EdDSA"', "1", "true"):
            with self.subTest(text):
                self.refuse(signed_raw(text, self.raw_claims()), "header: not a JSON object")

    def test_not_json(self):
        good = json.dumps(HEADER, separators=(",", ":"))
        invalid_utf8 = good.encode("ascii").replace(b"EdDSA", b"EdDSA\xff")
        self.assertEqual(len(invalid_utf8), len(good) + 1)
        for label, raw in (("trailing object", good + "{}"), ("truncated", good[:-1]), ("empty", " "),
                           ("NaN", good.replace(json.dumps(KID["a"]), "NaN")), ("single quotes", good.replace('"', "'")),
                           ("comment", good + "//"), ("byte order mark", "\ufeff" + good),
                           ("invalid UTF-8", invalid_utf8)):
            with self.subTest(label):
                self.assertNotEqual(raw, good)
                self.refuse(signed_raw(raw, self.raw_claims()), "header: not valid JSON")

    def test_a_long_header_is_refused_before_it_is_decoded(self):
        # The header is read before any signature is checked, so anyone can
        # send one. Nested thousands deep it has overrun the stack of a JSON
        # parser; it is now refused by its length alone, undecoded.
        self.assertEqual(bus_statement.MAX_HEADER_CHARS, 256)
        self.assertLess(len(self.good.split(".")[0]), 100, "a genuine header is a third of the limit")
        nested = signed_raw("[" * 2500 + "]" * 2500, self.raw_claims())
        deep_object = signed_raw('{"a":' * 600 + "1" + "}" * 600, self.raw_claims())
        long_kid = signed(self.claims(), header=dict(HEADER, kid="k" * 200))
        with mock.patch.object(bus_statement.json, "loads", wraps=json.loads) as loads, \
                mock.patch.object(bus_statement.base64, "urlsafe_b64decode", wraps=base64.urlsafe_b64decode) as decode:
            for token in (nested, deep_object, long_kid):
                self.assertGreater(len(token.split(".")[0]), 256)
                self.refuse(token, "size: header segment is longer than 256 characters")
            self.assertEqual((loads.call_count, decode.call_count), (0, 0))

    def test_header_of_exactly_the_longest_length(self):
        # 192 bytes of JSON are 256 characters; one byte more is 258.
        text = json.dumps(HEADER, separators=(",", ":"))
        longest = signed_raw(text + " " * (192 - len(text)), self.raw_claims())
        self.assertEqual(len(longest.split(".")[0]), 256)
        self.assertEqual(self.accept(longest), self.statement)
        longer = signed_raw(text + " " * (193 - len(text)), self.raw_claims())
        self.assertEqual(len(longer.split(".")[0]), 258)
        self.refuse(longer, "size: header segment is longer than 256 characters")
        # 257 characters are no encoding of anything; the length is judged first.
        header, payload, signature = longest.split(".")
        self.refuse(".".join((header + "A", payload, signature)), "size: header segment is longer than 256 characters")
        # The limit is on the header alone.
        self.assertGreater(len(signed(self.claims(name="n" * 160)).split(".")[1]), 256)


generate(HeaderViolations, "header", [
    ("alg_none", {"alg": "none"}, "header: alg"),
    ("alg_none_capitalized", {"alg": "None"}, "header: alg"),
    ("alg_lowercase", {"alg": "eddsa"}, "header: alg"),
    ("alg_ed25519", {"alg": "Ed25519"}, "header: alg"),
    ("alg_es256", {"alg": "ES256"}, "header: alg"),
    ("alg_list", {"alg": ["EdDSA"]}, "header: alg"),
    ("alg_null", {"alg": None}, "header: alg"),
    ("alg_missing", {"alg": ABSENT}, "header: parameters"),
    ("typ_jwt", {"typ": "JWT"}, "header: typ"),
    ("typ_other_case", {"typ": "NL-VIEWER+JWT"}, "header: typ"),
    ("typ_media_type_prefix", {"typ": "application/nl-viewer+jwt"}, "header: typ"),
    ("typ_null", {"typ": None}, "header: typ"),
    ("typ_missing", {"typ": ABSENT}, "header: parameters"),
    ("kid_unknown", {"kid": UNPINNED_KID}, "header: kid"),
    ("kid_of_an_unpinned_key", {"kid": lambda: KID["attacker"]}, "header: kid"),
    ("kid_other_case", {"kid": lambda: KID["a"].swapcase()}, "header: kid"),
    ("kid_with_a_trailing_character", {"kid": lambda: KID["a"] + "A"}, "header: kid"),
    ("kid_with_a_leading_space", {"kid": lambda: " " + KID["a"]}, "header: kid"),
    ("kid_with_a_trailing_space", {"kid": lambda: KID["a"] + " "}, "header: kid"),
    ("kid_with_a_trailing_newline", {"kid": lambda: KID["a"] + "\n"}, "header: kid"),
    ("kid_with_a_trailing_tab", {"kid": lambda: KID["a"] + "\t"}, "header: kid"),
    ("kid_with_a_no_break_space", {"kid": lambda: KID["a"] + "\u00a0"}, "header: kid"),
    ("kid_truncated", {"kid": lambda: KID["a"][:-1]}, "header: kid"),
    ("kid_a_short_name_after_the_prefix", {"kid": "gw-a"}, "header: kid"),
    ("kid_empty", {"kid": ""}, "header: kid"),
    ("kid_number", {"kid": 1}, "header: kid"),
    ("kid_list", {"kid": lambda: [KID["a"]]}, "header: kid"),
    ("kid_object", {"kid": lambda: {KID["a"]: True}}, "header: kid"),
    ("kid_null", {"kid": None}, "header: kid"),
    ("kid_missing", {"kid": ABSENT}, "header: parameters"),
    ("extra_crit", {"crit": ["exp"]}, "header: parameters"),
    ("extra_empty_crit", {"crit": []}, "header: parameters"),
    ("extra_b64_false", {"b64": False, "crit": ["b64"]}, "header: parameters"),
    ("extra_jku", {"jku": "https://attacker.example/keys.json"}, "header: parameters"),
    ("extra_x5c", {"x5c": ["AAAA"]}, "header: parameters"),
    ("extra_cty", {"cty": "JWT"}, "header: parameters"),
    ("extra_unknown", {"note": "harmless"}, "header: parameters"),
], run_header)


# --- structure and size -----------------------------------------------------

class StructureViolations(Case):
    def test_not_a_string(self):
        arguments = dict(keys=KEYS, method=self.method, target=self.target, body=self.body, now=NOW, issuer=ISSUER,
                         audience="bus", max_groups_age=300)
        for value in (self.good.encode("ascii"), None, 0, ["a", "b", "c"], self.good.split(".")):
            with self.subTest(type(value).__name__):
                self.refuse(value, "request: the statement header is not present exactly once")
                with self.assertRaises(StatementError) as caught:
                    verify_statement(value, replay=Recorder(), **arguments)
                self.assertEqual(caught.exception.reason, "structure: statement is not a string")

    def test_segment_count(self):
        header, payload, signature = self.good.split(".")
        for label, token in (("extra segment", self.good + ".AAAA"), ("trailing dot", self.good + "."),
                             ("leading dot", "." + self.good), ("five segments", self.good + ".AAAA.AAAA"),
                             ("two segments", header + "." + payload), ("one segment", header),
                             ("empty", ""), ("only dots", "..")):
            with self.subTest(label):
                self.refuse(token, "structure:")
        for label, token in (("empty header", "." + payload + "." + signature),
                             ("empty payload", header + ".." + signature),
                             ("empty signature", header + "." + payload + ".")):
            with self.subTest(label):
                self.refuse(token, "structure:")

    def test_whitespace(self):
        header, payload, signature = self.good.split(".")
        for label, token in (("trailing space", self.good + " "), ("trailing newline", self.good + "\n"),
                             ("trailing CRLF", self.good + "\r\n"), ("trailing tab", self.good + "\t"),
                             ("leading space", " " + self.good), ("space after dot", header + ". " + payload + "." + signature),
                             ("folded line", header + "." + payload[:20] + "\n " + payload[20:] + "." + signature),
                             ("trailing NUL", self.good + "\x00"), ("scheme prefix", "Bearer " + self.good)):
            with self.subTest(label):
                self.refuse(token, "structure:")

    def test_padded_base64url(self):
        header, payload, signature = self.good.split(".")
        self.assertEqual(len(signature), 86)
        self.refuse(".".join((header, payload, signature + "==")), "structure:")
        # Padding that the signature itself covers: only the padding is wrong.
        for spaces in range(3):
            text = json.dumps(HEADER) + " " * spaces
            if len(text) % 3:
                self.refuse(signed_segments(b64url(text) + "=" * (-len(b64url(text)) % 4), payload), "structure:")
            text = self.raw_claims() + " " * spaces
            if len(text) % 3:
                self.refuse(signed_segments(header, b64url(text) + "=" * (-len(b64url(text)) % 4)), "structure:")

    def test_spare_bits_must_be_zero(self):
        # A lenient decoder reads each of these as the very same bytes, which
        # would give one signed statement many spellings.
        header, payload, signature = self.good.split(".")
        loose = loosened(signature)
        self.assertEqual(unb64url(loose), unb64url(signature))
        self.refuse(".".join((header, payload, loose)), "structure:")
        text = self.raw_claims()
        text += " " * (1 if len(text) % 3 == 0 else 0)
        loose = loosened(b64url(text))
        self.assertEqual(unb64url(loose), text.encode("ascii"))
        self.refuse(signed_segments(header, loose), "structure:")
        self.accept(signed_segments(header, b64url(text)))

    def test_impossible_segment_length(self):
        header, payload, signature = self.good.split(".")
        self.refuse(".".join((header, payload, signature[:85])), "structure:")
        self.refuse(".".join((header, payload, signature + "A")), "signature:")

    def test_standard_base64_alphabet(self):
        for attempt in range(64):
            token = signed(self.claims(jti="standard-alphabet-%04d" % attempt))
            header, payload, signature = token.split(".")
            if "-" in signature or "_" in signature:
                break
        self.accept(token)
        self.refuse(".".join((header, payload, signature.replace("-", "+").replace("_", "/"))), "structure:")

    def test_non_ascii(self):
        header, payload, signature = self.good.split(".")
        self.refuse(self.good + "\u00e9", "structure:")
        self.refuse(header + "." + payload + "\uff21" + "." + signature, "structure:")

    def test_oversize_is_refused_before_anything_is_parsed(self):
        with mock.patch.object(bus_statement.json, "loads", wraps=json.loads) as loads:
            self.refuse("A" * 8193, "size:")
            self.refuse(self.good + "." * 9000, "size:")
            self.refuse("\u00e9" * 8193, "size:")
        self.assertEqual(loads.call_count, 0)


# --- signature and keys -----------------------------------------------------

class SignatureViolations(Case):
    def test_payload_changed_after_signing(self):
        header, _, signature = self.good.split(".")
        for label, changes in (("name", {"name": "Mallory"}), ("sub", {"sub": "owui:" + OTHER_UUID}),
                               ("groups", {"groups": sorted(GROUPS + [OTHER_UUID])}), ("exp", {"iat": NOW + 5, "exp": NOW + 65}),
                               ("htu", {"htu": "/_bus/admin"})):
            payload = signed(self.claims(**changes)).split(".")[1]
            with self.subTest(label):
                self.refuse(".".join((header, payload, signature)), "signature:")

    def test_header_changed_after_signing(self):
        _, payload, signature = self.good.split(".")
        respelled = b64url(json.dumps(HEADER))                  # the same header, other bytes
        self.assertNotEqual(respelled, self.good.split(".")[0])
        self.refuse(".".join((respelled, payload, signature)), "signature:")

    def test_signature_bit_flips(self):
        header, payload, signature = self.good.split(".")
        raw = bytearray(unb64url(signature))
        for bit in (0, 7, 255, 256, 300, 511):
            changed = bytearray(raw)
            changed[bit >> 3] ^= 1 << (bit & 7)
            with self.subTest(bit=bit):
                self.refuse(".".join((header, payload, b64url(bytes(changed)))), "signature:")

    def test_wrong_signature_sizes(self):
        header, payload, signature = self.good.split(".")
        raw = unb64url(signature)
        for label, value in (("zeros", bytes(64)), ("R only", raw[:32]), ("63 bytes", raw[:63]),
                             ("65 bytes", raw + b"\0"), ("doubled", raw + raw)):
            with self.subTest(label):
                self.refuse(".".join((header, payload, b64url(value))), "signature:")

    def test_another_key_under_the_same_kid(self):
        self.refuse(signed(self.claims(), key="attacker"), "signature:")

    def test_kid_naming_the_other_pinned_key(self):
        self.refuse(signed(self.claims(), header=dict(HEADER, kid=KID["b"]), key="a"), "signature:")
        self.refuse(signed(self.claims(), header=dict(HEADER, kid=KID["a"]), key="b"), "signature:")

    def test_signature_from_another_statement(self):
        header, payload, _ = self.good.split(".")
        other = signed(self.claims(jti="AAAAAAAAAAAAAAAAAAAAAA")).split(".")[2]
        self.refuse(".".join((header, payload, other)), "signature:")


class KeyRotation(Case):
    def test_second_key_under_another_kid_is_accepted(self):
        token = signed(self.claims(), header=dict(HEADER, kid=KID["b"]), key="b")
        self.assertNotEqual(KID["a"], KID["b"])
        self.assertEqual(self.accept(token).kid, KID["b"])
        self.assertEqual(self.accept(self.good).kid, KID["a"])

    def test_removed_kid_is_refused(self):
        token = signed(self.claims(), header=dict(HEADER, kid=KID["b"]), key="b")
        only_a, only_b = load_keys(pinned("a")), load_keys(pinned("b"))
        self.refuse(token, "header: kid", keys=only_a)
        self.accept(self.good, keys=only_a)
        self.refuse(self.good, "header: kid", keys=only_b)
        self.accept(token, keys=only_b)

    def test_unpinned_key_under_its_own_kid_is_refused(self):
        token = signed(self.claims(), header=dict(HEADER, kid=KID["attacker"]), key="attacker")
        self.refuse(token, "header: kid")
        # The statement itself is sound: pinning that key would accept it.
        self.assertEqual(self.accept(token, keys=load_keys(pinned("attacker"))).kid, KID["attacker"])

    def test_a_key_dictionary_built_by_hand_skips_what_load_keys_checks(self):
        # What the docstrings warn of, shown. The verifier looks only at the
        # shape of the keys it is given: a key under a label that is not its
        # kid verifies a statement whose header carries that label, and so
        # does a key whose secret is published. load_keys refuses both.
        label = "any-label"
        self.assertRaises(ValueError, load_keys, json.dumps({label: PUBLIC["a"]}))
        mislabelled = signed(self.claims(), header=dict(HEADER, kid=label))
        self.assertEqual(self.accept(mislabelled, keys={label: unb64url(PUBLIC["a"])}).kid, label)

        vector = [vector for vector in json.loads(FIXTURE.read_text(encoding="utf-8"))
                  if vector["options"]["audience"] == bus_statement.BUS_AUDIENCE][-1]
        request = vector["options"]["request"]
        self.assertRaises(ValueError, load_keys, json.dumps({vector["kid"]: vector["public_key"]}))
        published = verify_request(
            header_values=[vector["token"]], keys={vector["kid"]: unb64url(vector["public_key"])},
            issuer=vector["claims"]["iss"], audience=bus_statement.BUS_AUDIENCE, method=request["method"].upper(),
            target=request["target"], body=request["body_utf8"].encode("utf-8"), now=vector["now"],
            max_groups_age=300, replay=running_replay(vector["now"]))
        self.assertEqual(published.kid, vector["kid"])

    def test_hand_built_key_set_with_a_moved_kid_still_refuses(self):
        # load_keys never returns this. The signature check stands without it.
        self.refuse(self.good, "signature:", keys={KID["a"]: KEYS[KID["b"]]})


# --- claims -----------------------------------------------------------------

def run_claims(self, changes, reason):
    self.refuse(signed(self.claims(**changes)), reason)


class ClaimViolations(Case):
    pass


generate(ClaimViolations, "claim", [
    ("iss_other_origin", {"iss": "https://attacker.example/_gateway"}, "claims: iss"),
    ("iss_trailing_slash", {"iss": ISSUER + "/"}, "claims: iss"),
    ("iss_http", {"iss": ISSUER.replace("https", "http")}, "claims: iss"),
    ("iss_list", {"iss": [ISSUER]}, "claims: iss"),
    ("iss_null", {"iss": None}, "claims: iss"),
    ("iss_missing", {"iss": ABSENT}, "claims: iss"),
    ("aud_other", {"aud": "other"}, "claims: aud"),
    ("aud_list", {"aud": ["bus"]}, "claims: aud"),
    ("aud_list_of_two", {"aud": ["bus", "other"]}, "claims: aud"),
    ("aud_other_case", {"aud": "Bus"}, "claims: aud"),
    ("aud_null", {"aud": None}, "claims: aud"),
    ("aud_missing", {"aud": ABSENT}, "claims: aud"),
    ("sub_uppercase_uuid", {"sub": "owui:" + UUID.upper()}, "claims: sub"),
    ("sub_not_a_uuid", {"sub": "owui:alice"}, "claims: sub"),
    ("sub_bare_uuid", {"sub": UUID}, "claims: sub"),
    ("sub_bus_spelling", {"sub": "owui." + UUID}, "claims: sub"),
    ("sub_other_prefix", {"sub": "github:" + UUID}, "claims: sub"),
    ("sub_prefix_other_case", {"sub": "OWUI:" + UUID}, "claims: sub"),
    ("sub_uuid_without_hyphens", {"sub": "owui:" + UUID.replace("-", "")}, "claims: sub"),
    ("sub_uuid_in_braces", {"sub": "owui:{" + UUID + "}"}, "claims: sub"),
    ("sub_urn", {"sub": "owui:urn:uuid:" + UUID}, "claims: sub"),
    ("sub_trailing_newline", {"sub": "owui:" + UUID + "\n"}, "claims: sub"),
    ("sub_leading_space", {"sub": " owui:" + UUID}, "claims: sub"),
    ("sub_trailing_text", {"sub": "owui:" + UUID + "0"}, "claims: sub"),
    ("sub_fullwidth_digit", {"sub": "owui:" + UUID[:-1] + "\uff17"}, "claims: sub"),
    ("sub_number", {"sub": 7}, "claims: sub"),
    ("sub_list", {"sub": ["owui:" + UUID]}, "claims: sub"),
    ("sub_missing", {"sub": ABSENT}, "claims: missing"),
    ("groups_unsorted", {"groups": list(reversed(GROUPS))}, "claims: groups is"),
    ("groups_duplicate", {"groups": [GROUPS[0], GROUPS[0], GROUPS[1]]}, "claims: groups is"),
    ("groups_65", {"groups": sorted("%08x-0000-4000-8000-000000000000" % number for number in range(65))},
     "claims: groups is"),
    ("groups_uppercase_uuid", {"groups": [GROUPS[0].upper()]}, "claims: groups is"),
    ("groups_not_a_uuid", {"groups": ["admins"]}, "claims: groups is"),
    ("groups_uuid_with_a_trailing_character", {"groups": [GROUPS[0] + "0"]}, "claims: groups is"),
    ("groups_uuid_with_a_trailing_newline", {"groups": [GROUPS[0] + "\n"]}, "claims: groups is"),
    ("groups_uuid_with_a_trailing_space", {"groups": [GROUPS[0] + " "]}, "claims: groups is"),
    ("groups_uuid_with_a_leading_space", {"groups": [" " + GROUPS[0]]}, "claims: groups is"),
    ("groups_second_uuid_with_a_suffix", {"groups": [GROUPS[0], GROUPS[1] + "0"]}, "claims: groups is"),
    ("groups_number", {"groups": [1]}, "claims: groups is"),
    ("groups_nested_list", {"groups": [[GROUPS[0]]]}, "claims: groups is"),
    ("groups_string", {"groups": GROUPS[0]}, "claims: groups is"),
    ("groups_object", {"groups": {GROUPS[0]: True}}, "claims: groups is"),
    ("groups_null", {"groups": None}, "claims: groups is"),
    ("groups_missing", {"groups": ABSENT}, "claims: missing"),
    ("groups_at_string", {"groups_at": str(NOW - 20)}, "claims: groups_at is not"),
    ("groups_at_true", {"groups_at": True}, "claims: groups_at is not"),
    ("groups_at_null", {"groups_at": None}, "claims: groups_at is not"),
    ("groups_at_fraction", {"groups_at": NOW - 20.5}, "claims: groups_at is not"),
    ("groups_at_ahead_of_the_clock", {"groups_at": NOW + 6}, "claims: groups_at is in the future"),
    ("groups_at_missing", {"groups_at": ABSENT}, "claims: missing"),
    ("groups_at_negative", {"groups_at": -1}, "claims: groups_at is not"),
    ("groups_at_2_to_the_53", {"groups_at": 2 ** 53}, "claims: groups_at is not"),
    ("groups_at_2_to_the_64", {"groups_at": 2 ** 64}, "claims: groups_at is not"),
    ("name_lone_surrogate", {"name": "Ada\ud800"}, "claims: name is not valid text"),
    ("name_number", {"name": 7}, "claims: name is not a string"),
    ("name_null", {"name": None}, "claims: name is not a string"),
    ("name_list", {"name": ["Ada"]}, "claims: name is not a string"),
    ("lifetime_59", {"exp": NOW + 59}, "claims: exp is not iat + 60"),
    ("lifetime_61", {"exp": NOW + 61}, "claims: exp is not iat + 60"),
    ("lifetime_two_minutes", {"exp": NOW + 120}, "claims: exp is not iat + 60"),
    ("lifetime_an_hour", {"exp": NOW + 3600}, "claims: exp is not iat + 60"),
    ("lifetime_zero", {"exp": NOW}, "claims: exp is not iat + 60"),
    ("lifetime_negative", {"iat": NOW + 60, "exp": NOW}, "claims: exp is not iat + 60"),
    ("iat_beyond_the_skew", {"iat": NOW + 6, "exp": NOW + 66}, "claims: iat is in the future"),
    ("iat_far_ahead", {"iat": NOW + 86400, "exp": NOW + 86460}, "claims: iat is in the future"),
    ("expired_this_second", {"iat": NOW - 60, "exp": NOW}, "claims: expired"),
    ("expired_long_ago", {"iat": NOW - 86460, "exp": NOW - 86400}, "claims: expired"),
    ("iat_string", {"iat": str(NOW)}, "claims: iat and exp"),
    ("exp_string", {"exp": str(NOW + 60)}, "claims: iat and exp"),
    ("iat_fraction", {"iat": NOW + 0.5}, "claims: iat and exp"),
    ("exp_true", {"exp": True}, "claims: iat and exp"),
    ("iat_null", {"iat": None}, "claims: iat and exp"),
    ("iat_missing", {"iat": ABSENT}, "claims: missing"),
    ("iat_negative", {"iat": -60, "exp": 0}, "claims: iat and exp"),
    ("exp_2_to_the_53", {"iat": 2 ** 53 - 60, "exp": 2 ** 53}, "claims: iat and exp"),
    ("iat_and_exp_beyond_a_float", {"iat": 10 ** 30, "exp": 10 ** 30 + 60}, "claims: iat and exp"),
    ("exp_missing", {"exp": ABSENT}, "claims: missing"),
    ("jti_21_characters", {"jti": "q83vEjRWeJCrze8SNFZ4k"}, "claims: jti"),
    ("jti_23_characters", {"jti": "q83vEjRWeJCrze8SNFZ4kAA"}, "claims: jti"),
    ("jti_standard_alphabet", {"jti": "q83vEjRWeJCrze8SNFZ4k+"}, "claims: jti"),
    ("jti_padded", {"jti": "q83vEjRWeJCrze8SNFZ4=="}, "claims: jti"),
    ("jti_uuid", {"jti": OTHER_UUID}, "claims: jti"),
    ("jti_trailing_newline", {"jti": "q83vEjRWeJCrze8SNFZ4k\n"}, "claims: jti"),
    ("jti_empty", {"jti": ""}, "claims: jti"),
    ("jti_number", {"jti": 1234567890123456789012}, "claims: jti"),
    ("jti_missing", {"jti": ABSENT}, "claims: missing"),
    ("htm_other_method", {"htm": "POST"}, "claims: htm"),
    ("htm_lowercase", {"htm": "get"}, "claims: htm"),
    ("htm_mixed_case", {"htm": "Get"}, "claims: htm"),
    ("htm_trailing_space", {"htm": "GET "}, "claims: htm"),
    ("htm_list", {"htm": ["GET"]}, "claims: htm"),
    ("htm_missing", {"htm": ABSENT}, "claims: missing"),
    ("htu_other_path", {"htu": "/_bus/other?since=41&limit=10"}, "claims: htu"),
    ("htu_query_value", {"htu": "/_bus/example?since=42&limit=10"}, "claims: htu"),
    ("htu_query_order", {"htu": "/_bus/example?limit=10&since=41"}, "claims: htu"),
    ("htu_query_missing", {"htu": "/_bus/example"}, "claims: htu"),
    ("htu_query_extra", {"htu": "/_bus/example?since=41&limit=10&all=1"}, "claims: htu"),
    ("htu_trailing_slash", {"htu": "/_bus/example/?since=41&limit=10"}, "claims: htu"),
    ("htu_path_case", {"htu": "/_bus/Example?since=41&limit=10"}, "claims: htu"),
    ("htu_percent_encoded", {"htu": "/_bus/example?since=4%31&limit=10"}, "claims: htu"),
    ("htu_absolute", {"htu": "https://bus.example.org/_bus/example?since=41&limit=10"}, "claims: htu"),
    ("htu_fragment", {"htu": "/_bus/example?since=41&limit=10#top"}, "claims: htu"),
    ("htu_empty", {"htu": ""}, "claims: htu"),
    ("htu_null", {"htu": None}, "claims: htu"),
    ("htu_missing", {"htu": ABSENT}, "claims: missing"),
    ("bdy_of_another_body", {"bdy": digest(b"{}")}, "claims: bdy"),
    ("bdy_hex", {"bdy": hashlib.sha256(b"").hexdigest()}, "claims: bdy"),
    ("bdy_padded", {"bdy": EMPTY_BODY_DIGEST + "="}, "claims: bdy"),
    ("bdy_standard_alphabet", {"bdy": EMPTY_BODY_DIGEST.replace("-", "+").replace("_", "/")}, "claims: bdy"),
    ("bdy_empty", {"bdy": ""}, "claims: bdy"),
    ("bdy_null", {"bdy": None}, "claims: bdy"),
    ("bdy_missing", {"bdy": ABSENT}, "claims: missing"),
    ("unknown_nbf", {"nbf": NOW}, "claims: unknown"),
    ("unknown_admin", {"admin": True}, "claims: unknown"),
    ("unknown_misspelled_groups", {"group": GROUPS}, "claims: unknown"),
    ("unknown_display_name", {"display_name": "Ada Example"}, "claims: unknown"),
    ("unknown_other_case", {"Sub": "owui:" + UUID}, "claims: unknown"),
    ("misspelled_instead_of_groups", {"groups": ABSENT, "group": GROUPS}, "claims: unknown"),
], run_claims)


# --- the shape of a target --------------------------------------------------

class TargetShape(Case):
    """`htu` is a path: `/`, then printable ASCII without `#`, 2048 bytes at most.

    Each statement here is signed for the very target it is checked against,
    so that only the shape of the target can refuse it.
    """
    SHAPE = "claims: htu is not / and at most 2047 further characters of printable ASCII other than #"

    def bound(self, target):
        return signed(self.claims(htu=target))

    def assert_refused(self, target):
        self.refuse(self.bound(target), self.SHAPE, target=target)

    def assert_accepted(self, target):
        self.assertEqual(self.accept(self.bound(target), target=target), self.statement)

    def test_every_character_a_target_may_hold(self):
        allowed = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) != "#")
        self.assertEqual(len(allowed), 93)
        self.assert_accepted("/" + allowed)
        self.assert_accepted("/")
        self.assert_accepted("/?")
        self.assert_accepted("//")
        self.assert_accepted("/_bus/a%20b?q=%23&r=%C3%A9")

    def test_each_character_it_may_not_hold(self):
        for code in list(range(0x00, 0x21)) + [0x23, 0x7F, 0x80, 0x85, 0xA0, 0xE9, 0x2028, 0x4F8B, 0xFF0F, 0x1F600]:
            for target in ("/a" + chr(code) + "b", "/a" + chr(code), "/" + chr(code)):
                with self.subTest("U+%04X" % code, target=ascii(target)):
                    self.assert_refused(target)

    def test_it_begins_with_a_slash(self):
        for target in ("_bus/example", "*", "?x=1", "a", "https://bus.example.org/_bus/example", "x/",
                       "\\_bus", "%2F_bus", " /_bus", "http:/x"):
            with self.subTest(target=target):
                self.assert_refused(target)

    def test_it_is_at_most_2048_bytes(self):
        self.assertEqual(bus_statement.MAX_TARGET_BYTES, 2048)
        self.assert_accepted("/" + "a" * 2047)
        self.assert_accepted("/a?" + "x=1&" * 511 + "y")
        for target in ("/" + "a" * 2048, "/" + "a" * 4000):
            with self.subTest(length=len(target)):
                self.assert_refused(target)

    def test_shape_is_judged_before_the_match_with_the_request(self):
        self.refuse(self.bound("/a#b"), self.SHAPE)
        self.refuse(self.bound("/a b"), self.SHAPE, target="/a%20b")
        self.refuse(self.bound("/a"), "claims: htu is not the request target as received", target="/a#b")
        self.refuse(self.bound("/a"), "claims: htu is not the request target as received", target="/a b")

    def test_a_statement_at_every_limit_at_once_fits(self):
        # 64 groups, a 160-byte name, a 2048-byte target, an issuer of 100
        # characters and the longest common method: under 7000 of the 8192
        # bytes, as the note says, when serialized without padding.
        issuer = "https://" + "h" * 77 + ".example.org/_g"
        self.assertEqual(len(issuer), 100)
        groups = sorted("%08x-0000-4000-8000-000000000000" % number for number in range(64))
        target = "/" + "a" * 2047
        token = signed(self.claims(iss=issuer, groups=groups, name="n" * 160, htu=target, htm="OPTIONS"))
        self.assertLess(len(token), 7000)
        self.assertGreater(len(token), 6900)
        statement = self.accept(token, issuer=issuer, target=target, method="OPTIONS")
        self.assertEqual((len(statement.groups), statement.name, statement.name_dropped), (64, "n" * 160, False))
        # What the cap leaves for the payload beside a header and signature of the usual size.
        header, payload, signature = self.good.split(".")
        self.assertEqual((len(header), len(signature)), (87, 86))
        self.assertEqual((bus_statement.MAX_TOKEN_BYTES - 87 - 86 - 2) // 4 * 3, 6012)

        # The note's other figures: what the groups, the name and the rest take.
        def payload_bytes(**changes):
            return len(unb64url(signed(self.claims(iss=issuer, **changes)).split(".")[1]))

        bare = payload_bytes(groups=[], name=ABSENT, htu="/")
        self.assertEqual(payload_bytes(groups=groups, name=ABSENT, htu="/") - bare, 2495)
        self.assertEqual(payload_bytes(groups=[], name="n" * 160, htu="/") - bare, 170)
        self.assertLess(abs(bare - 350), 10)
        self.assertGreater(6012 - bare - 2495 - 170 + len("/"), 2900)

    def test_it_is_the_json_that_counts_toward_the_size(self):
        # JSON must escape the quotation mark and the backslash. Both may
        # stand in a name and in a target, and each then costs two bytes.
        issuer = "https://" + "h" * 77 + ".example.org/_g"
        groups = sorted("%08x-0000-4000-8000-000000000000" % number for number in range(64))
        quote, backslash = chr(34), BACKSLASH

        def statement(name, target):
            """The token for 64 groups, this name and this target; its payload's size; and the verdict on it."""
            token = signed(self.claims(iss=issuer, groups=groups, name=name, htu=target, htm="OPTIONS"))
            try:
                accepted = self.accept(token, issuer=issuer, target=target, method="OPTIONS")
            except StatementError as error:
                return token, len(unb64url(token.split(".")[1])), error.reason
            self.assertEqual((accepted.name, accepted.name_dropped), (name, False))
            return token, len(unb64url(token.split(".")[1])), None

        plain = statement("n" * 160, "/" + "a" * 2047)
        self.assertEqual((len(plain[0]) < 7000, plain[2]), (True, None))
        for escaped in (quote, backslash):
            with self.subTest(character=escaped):
                # In the name: 160 bytes more of JSON, and it still fits.
                named = statement(escaped * 160, "/" + "a" * 2047)
                self.assertEqual((named[1] - plain[1], named[2]), (160, None))
                # In the target: 2047 bytes more, and the statement is too large.
                large = statement("n" * 160, "/" + escaped * 2047)
                self.assertEqual(large[1] - plain[1], 2047)
                self.assertGreater(len(large[0]), bus_statement.MAX_TOKEN_BYTES)
                self.assertEqual(large[2], "size: statement is larger than 8192 bytes")
        # The longest target of a slash and backslashes that still fits beside all that.
        fits = [length for length in range(1480, 1520)
                if statement("n" * 160, "/" + backslash * (length - 1))[2] is None]
        self.assertEqual(fits, list(range(1480, fits[-1] + 1)), "every shorter one fits too")
        self.assertLess(abs(fits[-1] - 1500), 10)
        # A serializer that pads its JSON and escapes what it need not uses the room up sooner.
        claims = self.claims(name="\u20ac" * 53)
        self.assertGreater(len(signed_raw(HEADER, json.dumps(claims))), len(signed(claims)) + 200)


# --- the display name -------------------------------------------------------

# The code points a name may not hold, as docs/GATEWAY-STATEMENTS.md lists them.
# Any U+xFFFE or U+xFFFF is excluded besides.
EXCLUDED_FROM_NAMES = ("U+0000-001F U+007F-009F U+00AD U+061C U+180E U+200B-200F U+2028-202E U+2060-206F "
                       "U+FDD0-FDEF U+FEFF U+FFF9-FFFB U+1D173-1D17A U+E0000-E007F")


def excluded_ranges():
    ranges = []
    for item in EXCLUDED_FROM_NAMES.split():
        first, _, last = item[2:].partition("-")
        low = int(first, 16)
        ranges.append((low, int(first[:len(first) - len(last)] + last, 16) if last else low))
    return ranges


EXCLUDED_RANGES = excluded_ranges()
# Characters that show as nothing and that the fixed list does not hold: a
# combining joiner, four Hangul fillers, a Mongolian variation selector and
# the blank Braille pattern.
BLANK_AND_KEPT = (0x034F, 0x115F, 0x1160, 0x180B, 0x2800, 0x3164, 0xFFA0)


def excluded(point):
    """The rule restated from the list above, for comparison with the module's."""
    return point & 0xFFFE == 0xFFFE or any(low <= point <= high for low, high in EXCLUDED_RANGES)


class DisplayName(Case):
    """`name` is for display only: a name the rule excludes is dropped and the statement stands."""

    def named(self, name):
        return self.accept(signed(self.claims(name=name)))

    def assert_dropped(self, name, context=None):
        statement = self.named(name)
        self.assertIsNone(statement.name, context)
        self.assertIs(statement.name_dropped, True, context)
        # Everything else is as in the statement whose name was kept.
        self.assertEqual(dataclasses.replace(statement, name="Ada Example", name_dropped=False), self.statement)

    def assert_kept(self, name, context=None):
        statement = self.named(name)
        self.assertEqual(statement.name, name, context)
        self.assertIs(statement.name_dropped, False, context)

    def test_a_name_is_kept_and_none_is_none(self):
        self.assertEqual((self.statement.name, self.statement.name_dropped), ("Ada Example", False))
        absent = self.accept(signed(self.claims(name=ABSENT)))
        self.assertEqual((absent.name, absent.name_dropped), (None, False))
        self.assert_kept("")
        self.assert_kept("Zo\u00eb \u4f8b \U0001f600 d'Arc-O\u2019Neil, Jr. <b>&amp;</b>")

    def test_the_list_parses_as_written(self):
        self.assertEqual(EXCLUDED_RANGES, [
            (0x0000, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x061C, 0x061C), (0x180E, 0x180E),
            (0x200B, 0x200F), (0x2028, 0x202E), (0x2060, 0x206F), (0xFDD0, 0xFDEF), (0xFEFF, 0xFEFF),
            (0xFFF9, 0xFFFB), (0x1D173, 0x1D17A), (0xE0000, 0xE007F)])

    def test_the_first_and_last_of_each_excluded_range_drop_the_name(self):
        for low, high in EXCLUDED_RANGES:
            for point in sorted({low, high}):
                with self.subTest("U+%04X" % point):
                    self.assert_dropped("Ada" + chr(point) + "Example")
        for point in (0xFFFE, 0xFFFF, 0x1FFFE, 0x1FFFF, 0xEFFFE, 0x10FFFE, 0x10FFFF):
            with self.subTest("U+%04X" % point):
                self.assert_dropped(chr(point))

    def test_the_code_points_beside_each_excluded_range_are_kept(self):
        beside = set()
        for low, high in EXCLUDED_RANGES:
            beside.update((low - 1, high + 1))
        beside.update((0xFFFD, 0x10000, 0x1FFFD, 0x20000, 0x10FFFD))
        beside = sorted(point for point in beside if point >= 0 and not excluded(point) and not 0xD800 <= point <= 0xDFFF)
        self.assertEqual(len(beside), 29)
        for point in beside:
            with self.subTest("U+%04X" % point):
                self.assert_kept("Ada" + chr(point) + "Example")

    def test_the_verdict_does_not_follow_the_interpreters_unicode_tables(self):
        # Format characters by one Unicode version and unassigned by another,
        # or format characters that the fixed list leaves out: kept everywhere.
        self.assertNotIn("unicodedata", vars(bus_statement))
        for point in (0x0600, 0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2, 0x110BD, 0x110CD, 0x13430, 0x1343F, 0x1BCA0):
            with self.subTest("U+%04X" % point):
                self.assert_kept("Ada" + chr(point))

    def test_the_list_is_not_every_character_that_shows_as_nothing(self):
        # A fixed list, and it says so: these seven render as blanks, are on
        # no line of it, and are kept.
        for point in BLANK_AND_KEPT:
            with self.subTest("U+%04X" % point):
                self.assertFalse(excluded(point))
                self.assert_kept("Ada" + chr(point) + "Example")
        note = " ".join((ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8").split())
        self.assertIn(", ".join("U+%04X" % point for point in BLANK_AND_KEPT[:-1]) + " and U+%04X" % BLANK_AND_KEPT[-1],
                      note)
        self.assertIn("not every character that shows as nothing", note)
        self.assertNotIn("the characters that are invisible", note)

    def test_the_rule_is_the_list_code_point_by_code_point(self):
        # Every code point of the first two planes, the start of the
        # fifteenth and the final four of each: the module's verdict against
        # the list's. No other plane holds an excluded code point.
        points = list(range(0x20000)) + list(range(0xE0000, 0xE0400)) + [
            plane << 16 | low for plane in range(17) for low in (0xFFFC, 0xFFFD, 0xFFFE, 0xFFFF)]
        points = [point for point in points if not 0xD800 <= point <= 0xDFFF]
        wrong = [point for point in points if bus_statement._displayable(chr(point)) == excluded(point)]
        self.assertEqual(["U+%04X" % point for point in wrong[:10]], [])
        swept = sum(1 for point in set(points) if excluded(point))
        self.assertEqual(swept, sum(high - low + 1 for low, high in EXCLUDED_RANGES) + 2 * 17)
        self.assertEqual(swept, 302)

    def test_one_excluded_code_point_anywhere_drops_the_name(self):
        for name in ("\u0007Ada", "Ada\u0007", "Ada\nAdmin", "Ada\u0000", "A\u200bda", "\u202eadA", "Ada\ufeff",
                     "Ada\u00ad", "A" * 159 + "\u0085"):
            with self.subTest(name=ascii(name)):
                self.assert_dropped(name)

    def test_a_name_over_160_bytes_is_dropped(self):
        for name in ("n" * 160, "\u20ac" * 53 + "n", "\U0001f600" * 40):
            self.assertEqual(len(name.encode("utf-8")), 160)
            self.assert_kept(name)
        for name in ("n" * 161, "\u20ac" * 53 + "nn", "\U0001f600" * 40 + "n", "n" * 4000):
            self.assertGreater(len(name.encode("utf-8")), 160)
            self.assert_dropped(name)

    def test_a_name_that_is_not_a_string_is_a_refusal(self):
        for name in (7, 1.5, None, True, ["Ada"], {"given": "Ada"}, []):
            with self.subTest(name=name):
                self.refuse(signed(self.claims(name=name)), "claims: name is not a string")

    def test_a_lone_surrogate_in_the_name_is_a_refusal(self):
        for name in ("Ada\ud800", "\udc00Ada", "Ada\udfff Example", "\ud800", "\ud83d \ude00", "\ude00\ud83d"):
            with self.subTest(name=ascii(name)):
                self.refuse(signed(self.claims(name=name)), "claims: name is not valid text")
        # The two halves of one character, in order, are that character.
        token = signed_raw(HEADER, self.raw_claims().replace('"Ada Example"', '"Ada ' + BACKSLASH + "ud83d"
                                                             + BACKSLASH + 'ude00"'))
        self.assertEqual(self.accept(token).name, "Ada \U0001f600")

    def test_a_lone_surrogate_anywhere_in_the_payload_is_a_refusal(self):
        lone = "\ud800"
        texts = {name: value for name, value in self.claims().items() if isinstance(value, str)}
        self.assertEqual(sorted(texts), ["aud", "bdy", "htm", "htu", "iss", "jti", "name", "sub"])
        for name, value in texts.items():
            for position, changed in (("after", value + lone), ("before", lone + value), ("alone", lone)):
                with self.subTest(name, position=position):
                    self.refuse(signed(self.claims(**{name: changed})), "claims: ")
        for label, claims in (("in a group", self.claims(groups=[GROUPS[0] + lone])),
                              ("as a group", self.claims(groups=[lone])),
                              ("in the name of a claim", dict(self.claims(), **{"na" + lone + "me": "Ada"})),
                              ("in a claim that is not known", dict(self.claims(), note=lone)),
                              ("deep in a claim that is not known", dict(self.claims(), note={"a": [lone]}))):
            with self.subTest(label):
                self.refuse(signed(claims), "claims: ")
        # And where the caller's own values could match one.
        self.refuse(signed(self.claims(aud="bus" + lone)), "verifier:", audience="bus" + lone)
        self.refuse(signed(self.claims(iss=ISSUER + lone)), "verifier:", issuer=ISSUER + lone)


class TimesInRange(Case):
    """groups_at, iat and exp are whole seconds from 0 to 2**53 - 1, at clocks where nothing else refuses."""

    def times(self, iat, groups_at):
        return signed(self.claims(iat=iat, exp=iat + 60, groups_at=groups_at))

    def test_the_first_minute_of_1970(self):
        # A record that accepts all: no MemoryReplay can have begun before 1970.
        statement = self.accept(self.times(0, 0), now=0, replay=Recorder())
        self.assertEqual((statement.exp, statement.groups_at, statement.groups_stale), (60, 0, False))

    def test_a_negative_time_is_refused_where_the_clock_would_allow_it(self):
        self.refuse(self.times(-1, 0), "claims: iat and exp are not both whole seconds from 0 to 2**53 - 1", now=30)
        self.refuse(self.times(0, -1), "claims: groups_at is not whole seconds from 0 to 2**53 - 1", now=30)

    def test_the_last_second_a_float_can_count(self):
        last = 2 ** 53 - 1
        statement = self.accept(self.times(last - 60, last), now=last - 1, replay=Recorder())
        self.assertEqual((statement.exp, statement.groups_at, statement.groups_stale), (last, last, False))

    def test_2_to_the_53_is_refused_where_the_clock_would_allow_it(self):
        last = 2 ** 53 - 1
        self.refuse(self.times(last - 59, last), "claims: iat and exp are not both whole seconds from 0 to 2**53 - 1",
                    now=last - 1)
        self.refuse(self.times(last - 60, last + 1), "claims: groups_at is not whole seconds from 0 to 2**53 - 1",
                    now=last - 1)


class RawClaimViolations(Case):
    """Payload text that JSON.stringify cannot produce, signed all the same."""

    def pair(self, name):
        return json.dumps(name) + ":" + json.dumps(self.claims()[name], separators=(",", ":"))

    def test_a_name_twice_in_the_claims(self):
        good = self.raw_claims()
        for label, extra in (("sub", '"sub":"owui:%s"' % OTHER_UUID), ("groups", '"groups":[]'),
                             ("exp", '"exp":%d' % (NOW + 3600)), ("aud", '"aud":"other"'),
                             ("identical", self.pair("jti")),
                             ("escaped spelling", '"\\u0073ub":"owui:%s"' % OTHER_UUID)):
            for position, text in (("first", "{" + extra + "," + good[1:]), ("last", good[:-1] + "," + extra + "}")):
                with self.subTest(label, position=position):
                    self.assertEqual(text.count(BACKSLASH), 1 if label == "escaped spelling" else 0)
                    self.refuse(signed_raw(HEADER, text), "claims: not valid JSON")

    def test_not_a_json_object(self):
        for text in ("[" + self.raw_claims() + "]", "null", '"owui:%s"' % UUID, "1", "false"):
            with self.subTest(text[:12]):
                self.refuse(signed_raw(HEADER, text), "claims: not a JSON object")

    def test_not_json(self):
        good = self.raw_claims()
        for label, raw in (("trailing object", good + "{}"), ("truncated", good[:-1]), ("blank", " "),
                           ("NaN", good.replace(self.pair("iat"), '"iat":NaN')),
                           ("Infinity", good.replace(self.pair("exp"), '"exp":Infinity')),
                           ("byte order mark", "\ufeff" + good),
                           ("invalid UTF-8", good.encode("ascii").replace(b"Ada", b"Ada\xff")),
                           ("encoded surrogate", good.encode("ascii").replace(b"Ada", b"Ada\xed\xa0\x80"))):
            with self.subTest(label):
                self.assertNotEqual(raw, good)
                self.refuse(signed_raw(HEADER, raw), "claims: not valid JSON")

    def test_a_parser_that_gives_up_is_a_refusal_on_every_interpreter(self):
        # Whether deep nesting ends in RecursionError depends on the Python and
        # its stack. Here the decoder is made to raise it, for the header and
        # then for the claims, so the test does not.
        gives_up = RecursionError("maximum recursion depth exceeded while decoding a JSON document")
        with mock.patch.object(bus_statement.json, "loads", side_effect=gives_up) as loads:
            self.refuse(self.good, "header: not valid JSON")
            self.assertEqual(loads.call_count, 1)
        real = json.loads
        calls = []

        def on_the_claims(*arguments, **named):
            calls.append(arguments[0])
            if len(calls) == 2:
                raise gives_up
            return real(*arguments, **named)

        with mock.patch.object(bus_statement.json, "loads", side_effect=on_the_claims):
            self.refuse(self.good, "claims: not valid JSON")
        self.assertEqual(len(calls), 2)
        self.assertIn('"sub"', calls[1])
        with mock.patch.object(bus_statement.json, "loads", side_effect=gives_up):
            with self.assertRaises(ValueError) as caught:
                load_keys(pinned("a"))
            self.assertIn("BUS_GATEWAY_SIGNERS must be one JSON object", str(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_raw_control_characters_inside_a_string_are_not_json(self):
        good = self.raw_claims()
        header = json.dumps(HEADER, separators=(",", ":"))
        for code in (0x00, 0x01, 0x08, 0x09, 0x0A, 0x0D, 0x1B, 0x1F):
            with self.subTest("U+%04X" % code):
                raw = good.replace("Ada Example", "Ada" + chr(code) + "Example")
                self.assertEqual(len(raw), len(good))
                self.refuse(signed_raw(HEADER, raw), "claims: not valid JSON")
                self.refuse(signed_raw(HEADER, good.replace('"bus"', '"bus' + chr(code) + '"')), "claims: not valid JSON")
                self.refuse(signed_raw(header.replace("EdDSA", "EdDSA" + chr(code)), good), "header: not valid JSON")
                self.refuse(signed_raw(header.replace(KID["a"], KID["a"] + chr(code)), good), "header: not valid JSON")
                # Written as an escape the same character is JSON, and in the
                # name it costs the name and nothing else.
                escaped = good.replace("Ada Example", "Ada" + BACKSLASH + "u%04x" % code + "Example")
                statement = self.accept(signed_raw(HEADER, escaped))
                self.assertEqual((statement.name, statement.name_dropped), (None, True))
        # U+007F need not be escaped in JSON; in a name it is on the list all the same.
        statement = self.accept(signed_raw(HEADER, good.replace("Ada Example", "Ada" + chr(0x7F) + "Example")))
        self.assertEqual((statement.name, statement.name_dropped), (None, True))

    def test_a_payload_nested_deep_is_refused_before_it_is_parsed(self):
        # The claims are one object that holds one list. A payload with far
        # more structure is refused unparsed, signed or not: nested thousands
        # deep it has overrun the stack of a JSON parser in a small thread.
        good = self.raw_claims()
        deepest = '{"iss":"%s","aud":"bus","x":' % ISSUER + "[" * 5800
        closed = good.replace(self.pair("groups"), '"groups":' + "[" * 2000 + "]" * 2000)
        objects = good[:-1] + ',"x":' + '{"a":' * 900 + "1" + "}" * 900 + "}"
        tokens = [(label, signed_raw(HEADER, raw))
                  for label, raw in (("unterminated", deepest), ("closed", closed), ("objects", objects))]
        with mock.patch.object(bus_statement.json, "loads", wraps=json.loads) as loads:
            for label, token in tokens:
                with self.subTest(label):
                    loads.reset_mock()
                    self.refuse(token, "claims: more structure than a statement holds")
                    self.assertEqual(loads.call_count, 1, "the header was parsed and the payload was not")

    def test_how_much_structure_is_too_much(self):
        self.assertEqual(bus_statement.MAX_PAYLOAD_BRACKETS, 16)
        good = self.raw_claims()
        self.assertEqual(good.count("{") + good.count("["), 2)
        # Sixteen opening brackets are parsed and judged claim by claim; seventeen are not parsed.
        within = good[:-1] + ',"x":' + "[" * 14 + "]" * 14 + "}"
        beyond = good[:-1] + ',"x":' + "[" * 15 + "]" * 15 + "}"
        self.refuse(signed_raw(HEADER, within), "claims: unknown claim name")
        self.refuse(signed_raw(HEADER, beyond), "claims: more structure than a statement holds")
        mixed = good[:-1] + ',"x":' + '{"a":[' * 7 + "1" + "]}" * 7 + ',"y":[]}'
        self.assertEqual(mixed.count("{") + mixed.count("["), 17)
        self.refuse(signed_raw(HEADER, mixed), "claims: more structure than a statement holds")

    def test_brackets_inside_strings_are_not_structure(self):
        # A name and a target may hold any number of them, beside quotation
        # marks and backslashes that a naive count of quotes would trip over.
        quote, backslash = chr(34), BACKSLASH
        for name in ("[" * 160, "{[" * 80, quote + "[" * 100 + quote, (backslash + quote + "[{") * 40,
                     backslash * 3 + quote + "[" * 50 + backslash, "]" * 80 + "[" * 80):
            with self.subTest(name=name[:12]):
                statement = self.accept(signed(self.claims(name=name)))
                self.assertEqual((statement.name, statement.name_dropped), (name, False))
        for target in ("/a?x=" + "[" * 2000, "/" + "{[" * 1000, "/" + (quote + "[" + backslash) * 600):
            with self.subTest(target=target[:12]):
                self.assertEqual(self.accept(signed(self.claims(htu=target)), target=target).uuid, UUID)
        # Spelled by a serializer that escapes more than it must.
        escaped = self.raw_claims().replace('"Ada Example"', '"' + (BACKSLASH + "u005b") * 30 + BACKSLASH + '"[[' + BACKSLASH
                                            + BACKSLASH + '"')
        self.assertEqual(self.accept(signed_raw(HEADER, escaped)).name, "[" * 30 + quote + "[[" + backslash)

    def test_no_name_or_target_made_of_json_punctuation_is_taken_for_structure(self):
        # A hundred names and targets drawn from the characters that end
        # strings, escape and nest, each serialized by node and by Python.
        rng = random.Random(20261126)
        alphabet = chr(34) + BACKSLASH + "[]{}:,/ u00"
        for _ in range(100):
            name = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 60)))
            target = "/" + "".join(rng.choice(alphabet.replace(" ", "")) for _ in range(rng.randrange(0, 200)))
            claims = self.claims(name=name, htu=target)
            for token in (signed(claims), signed_raw(HEADER, json.dumps(claims))):
                statement = self.accept(token, target=target)
                self.assertEqual((statement.name, statement.name_dropped), (name, False), ascii(name))

    def test_a_string_that_never_ends_is_one_string_to_the_end(self):
        # As a parser reads it: after an opening quotation mark everything is
        # the string until one closes it. Taken so, the scan for structure
        # passes over each byte once, however the payload is made up.
        quote, backslash = b'"', BACKSLASH.encode("ascii")
        strip = bus_statement._JSON_STRING.sub
        for label, text, left in (
                ("never closed", quote + b"abc[[[", b""),
                ("escaped quotes, never closed", quote + (backslash + quote) * 3000, b""),
                ("ends in a backslash", quote + b"abc[" + backslash, b""),
                ("closed, then structure", quote + b"a[" + quote + b":[1]", b":[1]"),
                ("two strings and a third never closed", b'{"a":"[","b' + backslash + quote + b":[{", b"{:,"),
                ("three strings, the third with an escaped quote", b'{"a":"[","b' + backslash + quote + b'":[{', b"{:,:[{"),
                ("an escaped backslash before the close", quote + backslash * 2 + quote + b"[", b"["),
                ("a backslash before a line break", quote + b"a" + backslash + b"\n[" + quote + b"{", b"{"),
                ("no strings", b"[[1,2],{}]", b"[[1,2],{}]")):
            with self.subTest(label):
                self.assertEqual(strip(b"", text), left)
        # A payload of the worst such kind is refused as the bad JSON it is.
        raw = self.raw_claims()[:-1] + ',"x":"' + (BACKSLASH + '"') * 2500
        self.refuse(signed_raw(HEADER, raw), "claims: not valid JSON")

    def test_extreme_values_are_refused_without_a_crash(self):
        # Which check refuses a numeral of 5000 digits depends on the Python
        # and its limit on integer digits; that one does is what matters.
        good = self.raw_claims()
        raw = good.replace(self.pair("exp"), '"exp":' + "9" * 5000)
        self.assertNotEqual(raw, good)
        self.refuse(signed_raw(HEADER, raw), "claims: ")

    def test_integers_spelled_as_floats(self):
        good = self.raw_claims()
        for name, spelling, reason in (("iat", "%d.0" % NOW, "claims: iat and exp"),
                                       ("exp", "%d.0" % (NOW + 60), "claims: iat and exp"),
                                       ("iat", "1.8e9", "claims: iat and exp"),
                                       ("exp", "180000006e1", "claims: iat and exp"),
                                       ("groups_at", "%d.0" % (NOW - 20), "claims: groups_at is not")):
            raw = good.replace(self.pair(name), json.dumps(name) + ":" + spelling)
            with self.subTest(name, spelling=spelling):
                self.assertNotEqual(raw, good)
                self.refuse(signed_raw(HEADER, raw), reason)


# --- binding to the request -------------------------------------------------

def run_request(self, overrides, reason):
    self.refuse(self.good, reason, **overrides)


class RequestBinding(Case):
    def test_an_htm_not_in_upper_case_is_refused_even_when_the_method_equals_it(self):
        # htm is upper case by rule, not only by having to equal the method.
        for method in ("get", "Get", "gET", "G3T", "GE-T", "GET ", " GET", "GET\n", "G\u00c9T", ""):
            with self.subTest(method=method):
                self.refuse(signed(self.claims(htm=method)), "claims: htm is not the request method in upper case",
                            method=method)
        for method in ("GET", "G", "PROPFIND"):
            with self.subTest(method=method):
                self.assertEqual(self.accept(signed(self.claims(htm=method)), method=method).uuid, UUID)

    def test_statement_is_bound_to_each_part_together(self):
        self.accept(self.good, method="GET", target=self.target, body=b"")


generate(RequestBinding, "request", [
    ("other_method", {"method": "POST"}, "claims: htm"),
    ("head_instead_of_get", {"method": "HEAD"}, "claims: htm"),
    ("method_in_lower_case", {"method": "get"}, "claims: htm"),
    ("other_path", {"target": "/_bus/other?since=41&limit=10"}, "claims: htu"),
    ("query_value", {"target": "/_bus/example?since=42&limit=10"}, "claims: htu"),
    ("query_order", {"target": "/_bus/example?limit=10&since=41"}, "claims: htu"),
    ("query_dropped", {"target": "/_bus/example"}, "claims: htu"),
    ("query_emptied", {"target": "/_bus/example?"}, "claims: htu"),
    ("query_extended", {"target": "/_bus/example?since=41&limit=10&all=1"}, "claims: htu"),
    ("trailing_slash", {"target": "/_bus/example/?since=41&limit=10"}, "claims: htu"),
    ("trailing_slash_after_query", {"target": "/_bus/example?since=41&limit=10/"}, "claims: htu"),
    ("doubled_slash", {"target": "//_bus/example?since=41&limit=10"}, "claims: htu"),
    ("dot_segment", {"target": "/_bus/./example?since=41&limit=10"}, "claims: htu"),
    ("percent_encoding", {"target": "/_bus/ex%61mple?since=41&limit=10"}, "claims: htu"),
    ("path_case", {"target": "/_BUS/example?since=41&limit=10"}, "claims: htu"),
    ("absolute_form", {"target": "https://bus.example.org/_bus/example?since=41&limit=10"}, "claims: htu"),
    ("body_added", {"body": b"{}"}, "claims: bdy"),
    ("body_of_one_newline", {"body": b"\n"}, "claims: bdy"),
    ("a_minute_later", {"now": NOW + 60}, "claims: expired"),
    ("six_seconds_earlier", {"now": NOW - 6}, "claims: iat is in the future"),
], run_request)


class TrailingSlashTarget(Case):
    target = "/_bus/example/"

    def test_slash_may_not_be_removed_either(self):
        self.refuse(self.good, "claims: htu", target="/_bus/example")
        self.refuse(signed(self.claims(htu="/_bus/example")), "claims: htu")


class VerifierArguments(Case):
    def test_unusable_arguments_refuse_instead_of_raising(self):
        for label, overrides in (("now NaN", {"now": float("nan")}), ("now infinite", {"now": float("inf")}),
                                 ("now None", {"now": None}), ("now text", {"now": str(NOW)}), ("now bool", {"now": True}),
                                 ("now negative", {"now": -1}), ("now beyond a float", {"now": 10 ** 400}),
                                 ("now 2**53", {"now": 2 ** 53}), ("now 2.0**53", {"now": 2.0 ** 53}),
                                 ("method None", {"method": None}), ("method bytes", {"method": b"GET"}),
                                 ("target None", {"target": None}), ("target bytes", {"target": self.target.encode()}),
                                 ("target empty", {"target": ""}),
                                 ("body text", {"body": ""}), ("body number", {"body": 0}),
                                 ("keys None", {"keys": None}), ("keys list", {"keys": list(KEYS.items())}),
                                 ("keys as the setting text", {"keys": pinned("a", "b")}),
                                 ("issuer None", {"issuer": None}), ("issuer empty", {"issuer": ""}),
                                 ("issuer bytes", {"issuer": ISSUER.encode("ascii")}),
                                 ("issuer with a lone surrogate", {"issuer": ISSUER + "\ud800"}),
                                 ("audience list", {"audience": ["bus"]}), ("audience empty", {"audience": ""}),
                                 ("audience None", {"audience": None}), ("audience bytes", {"audience": b"bus"}),
                                 ("audience with a lone surrogate", {"audience": "bus\ud800"}),
                                 ("age text", {"max_groups_age": "300"}), ("age negative", {"max_groups_age": -1}),
                                 ("age NaN", {"max_groups_age": float("nan")}), ("age bool", {"max_groups_age": True}),
                                 ("age infinite", {"max_groups_age": float("inf")}),
                                 ("age beyond a float", {"max_groups_age": 10 ** 400}),
                                 # No limit is not a choice: the limit is a whole number of seconds, at least 1.
                                 ("age None", {"max_groups_age": None}), ("age zero", {"max_groups_age": 0}),
                                 ("age fraction", {"max_groups_age": 0.5}), ("age float", {"max_groups_age": 300.0}),
                                 ("age 2**53", {"max_groups_age": 2 ** 53})):
            with self.subTest(label):
                self.refuse(self.good, "verifier:", **overrides)

    def test_the_docstrings_say_what_else_can_be_raised_and_what_the_module_reads(self):
        # An argument left out is a TypeError (the tests of required arguments
        # show it), and what a record raises that is not an Exception passes
        # through (OnlyOnce shows it). The docstring claims no more than that.
        lower = " ".join(verify_statement.__doc__.split())
        self.assertNotIn("no argument can make this raise anything else", lower)
        for words in ("An argument left out is a TypeError", "that is not an Exception",
                      "a value of the wrong kind is refused and raises nothing else"):
            self.assertIn(words, lower)
        self.assertRaises(TypeError, verify_statement, self.good)
        # The module reads no setting: it names the two it expects a broker to have.
        module = " ".join(bus_statement.__doc__.split())
        self.assertIn("This module reads no setting", module)
        for name in (bus_statement.SIGNERS_SETTING, "BUS_GATEWAY_ISSUER"):
            self.assertIn(name, module)
        self.assertNotIn("is required whenever signers are configured", module)
        self.assertNotIn("environ", (ROOT / "lib" / "bus_statement.py").read_text(encoding="utf-8"))

    def test_keys_must_have_the_shape_load_keys_gives(self):
        kid, key = KID["a"], KEYS[KID["a"]]
        nine = dict(KEYS, **{"gw-%016d" % number: key for number in range(7)})
        self.assertEqual(len(nine), 9)
        for label, keys in (("empty", {}), ("nine entries", nine), ("key as text", {kid: PUBLIC["a"]}),
                            ("31 bytes", {kid: key[:31]}), ("33 bytes", {kid: key + b"\0"}), ("None", {kid: None}),
                            ("bytearray", {kid: bytearray(key)}), ("kid as bytes", {kid.encode("ascii"): key}),
                            ("kid as a number", {7: key}), ("one good and one bad", {kid: key, KID["b"]: None})):
            with self.subTest(label):
                self.refuse(self.good, "verifier:", keys=keys)
        eight = dict(KEYS, **{"gw-%016d" % number: key for number in range(6)})
        self.assertEqual(self.accept(self.good, keys=eight), self.statement)


# --- hostile arguments ------------------------------------------------------

class Trap(BaseException):
    """Raised by any use of a hostile object.

    Not an Exception, so no `except Exception` in the verifier can hide that
    an argument was compared, measured, converted or called before its type
    was known.
    """


def trap(*arguments, **named):
    raise Trap()


TRAPPED = ("__eq__ __ne__ __lt__ __le__ __gt__ __ge__ __hash__ __bool__ __len__ __iter__ __contains__ __getitem__ "
           "__str__ __repr__ __bytes__ __int__ __float__ __index__ __add__ __radd__ __mod__ __call__ __format__ "
           "__getattribute__").split()


def hostile():
    """An object of no builtin type whose every operation, and whose class's, raises Trap."""
    meta = type("HostileMeta", (type,), {name: trap for name in ("__eq__", "__ne__", "__hash__")})
    return meta("Hostile", (), {name: trap for name in TRAPPED})()


def lookalike(value):
    """A subclass instance of value's own type, equal in content, whose every operation raises Trap."""
    names = [name for name in TRAPPED if name != "__getattribute__"]
    return type("Hostile" + type(value).__name__.capitalize(), (type(value),), {name: trap for name in names})(value)


class HostileArguments(Case):
    """No argument is trusted to be of its type: nothing is done with it before that is known."""

    def genuine(self):
        return dict(header_values=[self.good], keys=KEYS, issuer=ISSUER, audience="bus", method=self.method,
                    target=self.target, body=self.body, now=NOW, max_groups_age=300)

    def candidates(self, name, value):
        """Hostile stand-ins for one argument: (label, value)."""
        yield "an object of no type", hostile()
        yield "a subclass of its own type", lookalike(value)
        if name == "now":
            yield "a float subclass", lookalike(float(value))
        if name == "body":
            yield "a bytearray subclass", lookalike(bytearray(value))
        if name == "header_values":
            yield "a tuple subclass", lookalike(tuple(value))
            yield "a list holding a str subclass", [lookalike(value[0])]
            yield "a list holding an object of no type", [hostile()]
            yield "a tuple holding an object of no type", (hostile(),)
        if name == "keys":
            kid, key = KID["a"], value[KID["a"]]
            colliding = type("Colliding", (), {"__hash__": lambda self: hash(kid), "__eq__": trap})()
            yield "a key that is an object of no type", {kid: hostile()}
            yield "a key that is a bytes subclass", {kid: lookalike(key)}
            yield "a kid that hashes as the real one and cannot be compared", {colliding: key}
            yield "a second entry of no type", {kid: key, KID["b"]: hostile()}

    def test_fixtures_are_hostile(self):
        thing = hostile()
        for use in (lambda: thing == 1, lambda: thing != 1, lambda: hash(thing), lambda: bool(thing), lambda: len(thing),
                    lambda: thing.claim, lambda: isinstance(thing, bytes), lambda: type(thing) == str,
                    lambda: type(thing) in (int, float), lambda: bytes(thing), lambda: str(thing)):
            self.assertRaises(Trap, use)
        self.assertIs(type(thing) is str, False, "asking for the type itself runs nothing")
        text = lookalike("GET")
        self.assertIs(isinstance(text, str), True)
        for use in (lambda: text == "GET", lambda: len(text), lambda: text + "", lambda: hash(text), lambda: {"GET": 1}[text]):
            self.assertRaises(Trap, use)

    def test_verify_request_refuses_each_hostile_argument(self):
        for name, value in self.genuine().items():
            for label, candidate in self.candidates(name, value):
                with self.subTest(name, hostile=label):
                    arguments = dict(self.genuine(), replay=Recorder())
                    arguments[name] = candidate
                    with self.assertRaises(StatementError) as caught:
                        verify_request(**arguments)
                    self.assertRegex(caught.exception.reason, r"\A(request|verifier): ")
                    self.assertEqual(arguments["replay"].claims, [])

    def test_verify_statement_refuses_each_hostile_argument(self):
        genuine = dict(self.genuine(), token=self.good)
        del genuine["header_values"]
        for name, value in genuine.items():
            for label, candidate in self.candidates(name, value):
                with self.subTest(name, hostile=label):
                    arguments = dict(genuine, replay=Recorder())
                    arguments[name] = candidate
                    token = arguments.pop("token")
                    with self.assertRaises(StatementError) as caught:
                        verify_statement(token, **arguments)
                    self.assertRegex(caught.exception.reason, r"\A(structure|verifier): ")
                    self.assertEqual(arguments["replay"].claims, [])

    def test_a_hostile_answer_from_the_record_is_a_refusal(self):
        self.refuse(self.good, "replay: the record of accepted statements answered neither", replay=Recorder(hostile()))
        self.refuse(self.good, "replay: the record of accepted statements answered neither",
                    replay=Recorder(lookalike(1)))

    def test_a_body_that_can_no_longer_be_read_is_a_refusal(self):
        view = memoryview(bytearray(self.body))
        self.assertEqual(self.accept(self.good, body=view), self.statement)
        view.release()
        self.assertRaises(ValueError, bytes, view)
        self.refuse(self.good, "verifier: the request body could not be read", body=view)

    def test_the_other_functions_refuse_hostile_arguments_in_their_own_way(self):
        key = KEYS[KID["a"]]
        for label, candidate in (("an object of no type", hostile()), ("a bytes subclass", lookalike(key))):
            with self.subTest(label, function="kid_for"):
                self.assertRaises(ValueError, kid_for, candidate)
        for label, candidate in (("an object of no type", hostile()), ("a str subclass", lookalike(pinned("a")))):
            with self.subTest(label, function="load_keys"):
                self.assertRaises(ValueError, load_keys, candidate)
        record = running_replay()
        for label, jti, exp in (("jti of no type", hostile(), NOW + 60), ("exp of no type", "a", hostile()),
                                ("jti a str subclass", lookalike("a"), NOW + 60),
                                ("exp an int subclass", "a", lookalike(NOW + 60))):
            with self.subTest(label, function="MemoryReplay.claim"):
                self.assertRaises(StatementError, record.claim, jti, exp)
        self.assertEqual(record.held(), 0)
        for label, arguments in (("capacity of no type", {"capacity": hostile()}),
                                 ("capacity an int subclass", {"capacity": lookalike(16)}),
                                 ("clock answering an object of no type", {"clock": hostile}),
                                 ("clock answering a float subclass", {"clock": lambda: lookalike(1000.0)})):
            with self.subTest(label, function="MemoryReplay"):
                self.assertRaises(ValueError, MemoryReplay, **arguments)


# --- one header -------------------------------------------------------------

class OneHeader(Case):
    """verify_request, the entry point: the header's values as the HTTP layer saw them."""

    def request(self, header_values, **overrides):
        arguments = dict(keys=KEYS, issuer=ISSUER, audience="bus", method=self.method, target=self.target,
                         body=self.body, now=NOW, max_groups_age=300, replay=running_replay())
        arguments.update(overrides)
        return verify_request(header_values=header_values, **arguments)

    def refused(self, header_values, reason="request: the statement header is not present exactly once", **overrides):
        recorder = Recorder()
        with self.assertRaises(StatementError) as caught:
            self.request(header_values, replay=recorder, **overrides)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(str(caught.exception), "unauthorized")
        self.assertEqual(recorder.claims, [])

    def test_exactly_one_value_is_verified(self):
        self.assertEqual(self.request([self.good]), self.statement)
        self.assertEqual(self.request((self.good,)), self.statement)

    def test_it_is_the_same_check_as_verify_statement(self):
        arguments = dict(keys=KEYS, issuer=ISSUER, audience="bus", method=self.method, target=self.target,
                         body=self.body, now=NOW, max_groups_age=300)
        self.assertEqual(verify_request(header_values=[self.good], replay=running_replay(), **arguments),
                         verify_statement(self.good, replay=running_replay(), **arguments))
        for label, token, changed in (("signature", signed(self.claims(), key="attacker"), {}),
                                      ("audience", self.good, {"audience": "other"}),
                                      ("issuer", self.good, {"issuer": "https://hub.example.net/_gateway"}),
                                      ("method", self.good, {"method": "POST"}), ("target", self.good, {"target": "/"}),
                                      ("body", self.good, {"body": b"x"}), ("clock", self.good, {"now": NOW + 60}),
                                      ("keys", self.good, {"keys": load_keys(pinned("b"))}),
                                      ("age", self.good, {"max_groups_age": 0}), ("structure", self.good + "=", {})):
            reasons = []
            for call in (lambda **kw: verify_request(header_values=[token], **kw),
                         lambda **kw: verify_statement(token, **kw)):
                with self.assertRaises(StatementError) as caught:
                    call(replay=running_replay(), **dict(arguments, **changed))
                reasons.append(caught.exception.reason)
            with self.subTest(label):
                self.assertEqual(reasons[0], reasons[1])
        # The record a caller passes is the record that is used.
        replay = running_replay()
        verify_request(header_values=[self.good], replay=replay, **arguments)
        with self.assertRaises(StatementError) as caught:
            verify_statement(self.good, replay=replay, **arguments)
        self.assertEqual(caught.exception.reason, "replay: this statement was already accepted")

    def test_no_header_is_refused(self):
        for values in ([], (), None):
            with self.subTest(values=values):
                self.refused(values)

    def test_the_header_twice_is_refused(self):
        other = signed(self.claims(jti="AAAAAAAAAAAAAAAAAAAAAA"))
        for label, values in (("the same statement twice", [self.good, self.good]),
                              ("two statements", [self.good, other]), ("a good one and nonsense", [self.good, "x"]),
                              ("nonsense and a good one", ["x", self.good]), ("a good one and an empty value", [self.good, ""]),
                              ("three", [self.good, other, self.good]), ("a pair", (self.good, other))):
            with self.subTest(label):
                self.refused(values)

    def test_values_joined_by_a_proxy_are_refused(self):
        # What an intermediary makes of a repeated header: one value, comma separated.
        other = signed(self.claims(jti="AAAAAAAAAAAAAAAAAAAAAA"))
        for joined in (self.good + ", " + other, self.good + "," + other, self.good + ", " + self.good):
            with self.subTest(joined=joined[-30:]):
                self.refused([joined], reason="structure: statement is not three segments")
        self.refused([self.good + ","], reason="structure: a segment is not canonical unpadded base64url")
        self.refused([self.good + ", "], reason="structure: a segment is not canonical unpadded base64url")

    def test_only_a_list_or_tuple_of_one_string_will_do(self):
        class ListLike(list):
            pass

        for label, values in (("the string itself", self.good), ("one character", "A"), ("bytes", self.good.encode("ascii")),
                              ("a list of bytes", [self.good.encode("ascii")]), ("a list of None", [None]),
                              ("a nested list", [[self.good]]), ("an iterator", iter([self.good])),
                              ("a set", {self.good}), ("a dict", {self.good: self.good}),
                              ("a list subclass", ListLike([self.good])), ("a number", 1), ("True", True),
                              ("a generator", (value for value in [self.good]))):
            with self.subTest(label):
                self.refused(values)

    def test_every_argument_is_required_and_named(self):
        expected = ["header_values", "keys", "issuer", "audience", "method", "target", "body", "now",
                    "max_groups_age", "replay"]
        parameters = inspect.signature(verify_request).parameters
        self.assertEqual(list(parameters), expected)
        complete = dict(header_values=[self.good], keys=KEYS, issuer=ISSUER, audience="bus", method=self.method,
                        target=self.target, body=self.body, now=NOW, max_groups_age=300, replay=Recorder())
        self.assertEqual(list(complete), expected)
        for name in expected:
            with self.subTest(name):
                self.assertIs(parameters[name].default, inspect.Parameter.empty)
                self.assertEqual(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
                without = {key: value for key, value in complete.items() if key != name}
                self.assertRaises(TypeError, verify_request, **without)
        self.assertRaises(TypeError, verify_request, [self.good], **{k: v for k, v in complete.items() if k != "header_values"})
        self.assertEqual(verify_request(**complete), self.statement)
        self.assertEqual(len(complete["replay"].claims), 1)

    def test_the_lower_function_points_to_this_one(self):
        self.assertIn("verify_request", verify_statement.__doc__)
        self.assertIn("more than once", " ".join(verify_request.__doc__.split()))
        self.assertEqual(bus_statement.__all__, ["MemoryReplay", "Replay", "Statement", "StatementError", "kid_for",
                                                 "load_keys", "verify_request", "verify_statement"])


# --- each statement once ----------------------------------------------------

class Recorder:
    """A replay record for tests: it notes each claim and answers as told."""

    def __init__(self, answer=True):
        self.claims = []
        self.answer = answer

    def claim(self, jti, exp):
        self.claims.append((jti, exp))
        if issubclass(type(self.answer), BaseException):
            raise self.answer
        return self.answer


class OnlyOnce(Case):
    def test_the_same_statement_is_accepted_once(self):
        replay = running_replay()
        self.assertEqual(self.accept(self.good, replay=replay), self.statement)
        for _ in range(3):
            self.refuse(self.good, "replay: this statement was already accepted", replay=replay)
        # Another statement for the same request, as a retry sends, is new.
        retry = signed(self.claims(jti="AAAAAAAAAAAAAAAAAAAAAA"))
        self.assertEqual(self.accept(retry, replay=replay).jti, "AAAAAAAAAAAAAAAAAAAAAA")
        self.refuse(retry, "replay: this statement was already accepted", replay=replay)
        self.assertEqual(replay.held(), 2)

    def test_claim_is_the_last_step_and_gets_jti_and_exp(self):
        recorder = Recorder()
        statement = self.accept(self.good, replay=recorder)
        self.assertEqual(recorder.claims, [(statement.jti, statement.exp)])
        self.assertEqual(recorder.claims, [("q83vEjRWeJCrze8SNFZ4kA", NOW + 60)])
        self.assertEqual([type(value) for value in recorder.claims[0]], [str, int])

    def test_a_refused_statement_is_never_claimed(self):
        # refuse() checks this for every refusal in this file; here is one of each kind.
        for label, token, overrides in (
                ("size", "A" * 9000, {}), ("structure", self.good + ".", {}),
                ("header", signed(self.claims(), header=dict(HEADER, alg="none")), {}),
                ("signature", signed(self.claims(), key="attacker"), {}),
                ("issuer", self.good, {"issuer": "https://hub.example.net/_gateway"}),
                ("audience", self.good, {"audience": "other"}),
                ("unknown claim", signed(self.claims(nbf=NOW)), {}),
                ("expired", self.good, {"now": NOW + 60}), ("not yet", self.good, {"now": NOW - 6}),
                ("method", self.good, {"method": "POST"}), ("target", self.good, {"target": "/_bus/other"}),
                ("body", self.good, {"body": b"{}"}), ("argument", self.good, {"now": None})):
            recorder = Recorder()
            with self.subTest(label):
                with self.assertRaises(StatementError) as caught:
                    self.accept(token, replay=recorder, **overrides)
                self.assertFalse(caught.exception.reason.startswith("replay:"))
                self.assertEqual(recorder.claims, [])

    def test_a_statement_stays_on_record_for_as_long_as_it_can_verify(self):
        # Issued as far ahead as the skew allows, so that it verifies for the
        # longest time there is: 65 seconds. Both clocks move together.
        elapsed = [0.0]
        replay = MemoryReplay(clock=lambda: NOW - 3600, monotonic=lambda: 5000.0 + elapsed[0])
        token = signed(self.claims(iat=NOW + 5, exp=NOW + 65))
        self.accept(token, replay=replay)
        for elapsed[0] in (0.0, 1.0, 30.0, 64.0, 64.999):
            with self.subTest(elapsed=elapsed[0]):
                self.refuse(token, "replay: this statement was already accepted", replay=replay, now=NOW + elapsed[0])
        elapsed[0] = 65.0
        self.refuse(token, "claims: expired", replay=replay, now=NOW + 65)
        self.assertEqual(replay.held(), 1, "and the record outlives it by a margin")

    def test_a_statement_with_a_stale_group_list_is_claimed_too(self):
        replay = running_replay()
        token = signed(self.claims(groups_at=NOW - 301))
        self.assertIs(self.accept(token, replay=replay).groups_stale, True)
        self.refuse(token, "replay: this statement was already accepted", replay=replay)

    def test_only_the_answer_true_accepts(self):
        recorder = Recorder(False)
        self.refuse(self.good, "replay: this statement was already accepted", replay=recorder)
        self.assertEqual(len(recorder.claims), 1)
        for answer in (None, 0, 1, "yes", [True], mock.Mock()):
            recorder = Recorder(answer)
            with self.subTest(answer=repr(answer)[:20]):
                self.refuse(self.good, "replay: the record of accepted statements answered neither True nor False",
                            replay=recorder)
                self.assertEqual(len(recorder.claims), 1)
        # A test double with no answer of its own accepts nothing.
        self.refuse(self.good, "replay: the record of accepted statements answered neither", replay=mock.Mock())
        self.refuse(self.good, "replay: the record of accepted statements answered neither", replay=mock.MagicMock())

    def test_a_record_may_refuse_for_its_own_reason(self):
        error = self.refuse(self.good, "replay: the shared table is full",
                            replay=Recorder(StatementError("replay: the shared table is full")))
        self.assertEqual(error.reason, "replay: the shared table is full")

    def test_a_failing_record_is_a_refusal_that_keeps_its_cause(self):
        for failure in (RuntimeError("database is locked"), OSError(5, "input/output error"), KeyError("jti"),
                        ValueError("bad row"), TypeError("claim() takes 2 positional arguments")):
            with self.subTest(type(failure).__name__):
                error = self.refuse(self.good, "replay: the record of accepted statements failed",
                                    replay=Recorder(failure))
                self.assertIs(error.__cause__, failure, "the journal can still see why")
                self.assertEqual(str(error), "unauthorized")
        # The class in place of an instance: its claim cannot be called with two arguments.
        error = self.refuse(self.good, "replay: the record of accepted statements failed", replay=MemoryReplay)
        self.assertIsInstance(error.__cause__, TypeError)

    def test_what_a_record_raises_that_is_not_an_exception_passes_through(self):
        # The record is the caller's own code. Its Exceptions become refusals;
        # an interrupt or an exit is not the verifier's to swallow.
        for raised in (KeyboardInterrupt(), SystemExit(3), GeneratorExit()):
            with self.subTest(type(raised).__name__):
                recorder = Recorder(raised)
                with self.assertRaises(type(raised)) as caught:
                    self.accept(self.good, replay=recorder)
                self.assertIs(caught.exception, raised)
                self.assertEqual(len(recorder.claims), 1)

        class Interrupted:
            @property
            def claim(self):
                raise KeyboardInterrupt()

        self.assertRaises(KeyboardInterrupt, self.accept, self.good, replay=Interrupted())

    def test_the_verifier_waits_for_the_record_in_the_callers_thread(self):
        seen = []

        class Slow:
            def claim(self, jti, exp):
                time.sleep(0.05)
                seen.append(threading.get_ident())
                return True

        began = time.monotonic()
        self.assertEqual(self.accept(self.good, replay=Slow()), self.statement)
        self.assertGreater(time.monotonic() - began, 0.04)
        self.assertEqual(seen, [threading.get_ident()])

    def test_something_that_cannot_claim_is_refused_before_any_check(self):
        class NotCallable:
            claim = True

        class Raising:
            @property
            def claim(self):
                raise RuntimeError("no such table")

        for label, replay in (("None", None), ("object", object()), ("set", set()), ("dict", {}), ("True", True),
                              ("claim is not callable", NotCallable()), ("claim cannot be read", Raising()),
                              ("a function", lambda jti, exp: True)):
            with self.subTest(label):
                self.refuse(self.good, "verifier: replay", replay=replay)
                # Before any check: the same answer for a statement that is nonsense.
                self.refuse("nonsense", "verifier: replay", replay=replay)

    def test_a_record_of_the_callers_own_serves(self):
        class Shared:
            """As small as a record can be: one set, one lock."""
            def __init__(self):
                self.seen, self.lock = set(), threading.Lock()

            def claim(self, jti, exp):
                with self.lock:
                    if jti in self.seen:
                        return False
                    self.seen.add(jti)
                    return True

        record = Shared()
        self.accept(self.good, replay=record)
        self.refuse(self.good, "replay: this statement was already accepted", replay=record)

    def test_what_one_process_accepted_another_refuses_after_a_restart(self):
        # The first process accepts a statement from a gateway whose clock leads by 2 s.
        token = signed(self.claims(iat=NOW + 2, exp=NOW + 62, groups_at=NOW))
        before = MemoryReplay(clock=lambda: NOW - 3600)
        self.accept(token, replay=before)
        # It restarts a second later. The new process knows nothing of that, so it
        # refuses whatever its predecessor could have accepted.
        after = MemoryReplay(clock=lambda: NOW + 1)
        for later in (1, 2, 30, 61):
            self.refuse(token, "replay: issued before this record began", replay=after, now=NOW + later)
        # The last statement the old process could have accepted, and the first it could not.
        for iat, accepted in ((NOW + 6, False), (NOW + 7, True)):
            fresh = signed(self.claims(iat=iat, exp=iat + 60, groups_at=NOW, jti="restart-%014d" % iat))
            with self.subTest(iat=iat - NOW):
                if accepted:
                    self.accept(fresh, replay=after, now=iat)
                else:
                    self.refuse(fresh, "replay: issued before this record began", replay=after, now=iat)

    def test_a_record_made_for_each_request_accepts_nothing(self):
        # The mistake of writing replay=MemoryReplay() in the request handler.
        with mock.patch.object(bus_statement.time, "time", return_value=float(NOW)):
            self.refuse(self.good, "replay: issued before this record began", replay=MemoryReplay())


# --- the wall clock moved ---------------------------------------------------

class Timeline:
    """A gateway and a broker on clocks that a test moves.

    Both read one wall clock, which a test may set back or forward. The
    record's own monotonic clock only runs.
    """

    def __init__(self, case):
        self.case = case
        self.wall = NOW
        self.elapsed = 0
        self.accepted = {}          # token -> (jti, exp), for each statement the record has taken
        self.issued = 0
        self.record = MemoryReplay(clock=lambda: NOW - 3600, monotonic=lambda: 5000.0 + self.elapsed)

    def run(self, seconds):
        self.wall += seconds
        self.elapsed += seconds

    def issue(self, lead=0):
        """A new statement issued now: `lead` seconds ahead of the wall clock, or behind it when negative.

        Its group list was read half a minute before that.
        """
        self.issued += 1
        iat = self.wall + lead
        return signed(self.case.claims(iat=iat, exp=iat + 60, groups_at=iat - 30, jti="timeline-%013d" % self.issued))

    def verdict(self, token, record=None):
        """None when the statement is accepted now; otherwise the reason it is refused."""
        try:
            statement = self.case.accept(token, now=self.wall, replay=self.record if record is None else record)
        except StatementError as error:
            return error.reason
        if record is None:
            self.accepted[token] = (statement.jti, statement.exp)
        return None

    def traffic(self, seconds, lead=0):
        """A statement issued and accepted every ten seconds; the last of them."""
        for _ in range(seconds // 10):
            self.run(10)
            token = self.issue(lead)
            self.case.assertIsNone(self.verdict(token))
        return token

    def assert_nothing_is_taken_twice(self):
        """Asked again for anything it has taken, the record says no: also when the verifier would not have asked."""
        for jti, exp in self.accepted.values():
            try:
                answer = self.record.claim(jti, exp)
            except StatementError:
                continue
            self.case.assertIs(answer, False)


class ClockSteps(Case):
    """What happens to accepted statements when the wall clock is moved: MemoryReplay's docstring, run."""
    FORGOTTEN = "replay: not newer than a statement this record has forgotten"
    # Every way the verifier turns away a statement it has accepted before. None is an acceptance.
    NO_REPLAY = ("replay: this statement was already accepted", FORGOTTEN, "claims: expired",
                 "claims: iat is in the future", "claims: groups_at is in the future")

    def after_a_step(self, step, watch, lead_before=0, late=False):
        """Eighty seconds of traffic, the wall clock set back by `step`, then a look every ten seconds.

        Gives the verdict on a fresh statement at each look (with `late`, on
        one verified 59 seconds after it was issued). At each look the newest
        statement from before the step is sent again and is not accepted, and
        the record takes nothing twice. A negative step sets the clock forward.
        """
        timeline = Timeline(self)
        newest = timeline.traffic(80, lead_before)
        timeline.wall -= step
        verdicts = {}
        for elapsed in range(0, watch + 10, 10):
            if elapsed:
                timeline.run(10)
            verdicts[elapsed] = timeline.verdict(timeline.issue(-59 if late else 0))
            self.assertIn(timeline.verdict(newest), self.NO_REPLAY)
            timeline.assert_nothing_is_taken_twice()
        return verdicts

    def refused(self, verdicts):
        """The looks at which the fresh statement was refused, each for the one reason there is."""
        looks = [elapsed for elapsed in sorted(verdicts) if verdicts[elapsed] is not None]
        self.assertEqual({verdicts[elapsed] for elapsed in looks} - {self.FORGOTTEN}, set())
        return looks

    def test_set_back_by_the_skew_or_less_nothing_more_is_refused(self):
        # Not even with the earlier statements issued as far ahead as allowed
        # and the fresh ones verified in their last second.
        self.assertEqual(bus_statement.CLOCK_SKEW, 5)
        self.assertEqual(self.refused(self.after_a_step(5, 80, lead_before=5, late=True)), [])

    def test_set_back_by_a_little_more_only_statements_verified_late_are_refused(self):
        self.assertEqual(self.refused(self.after_a_step(6, 80, lead_before=5, late=True)), list(range(0, 80, 10)))
        # Half a minute back, and every statement that is verified as it is issued is still accepted.
        self.assertEqual(self.refused(self.after_a_step(30, 80, lead_before=5)), [])

    def test_set_back_by_over_a_minute_fresh_statements_wait_until_the_clock_has_caught_up(self):
        # Statements issued and verified on the one clock: 69 seconds back
        # costs nothing; 70 seconds back, or 120, costs as long again.
        self.assertEqual(bus_statement.REPLAY_RETENTION, 70)
        self.assertEqual(self.refused(self.after_a_step(69, 80)), [])
        for step in (70, 120):
            with self.subTest(step=step):
                self.assertEqual(self.refused(self.after_a_step(step, step + 20)), list(range(0, step + 10, 10)))
        # With the earlier statements issued five seconds ahead it begins five seconds sooner.
        self.assertEqual(self.refused(self.after_a_step(64, 80, lead_before=5)), [])
        self.assertEqual(self.refused(self.after_a_step(65, 80, lead_before=5)), list(range(0, 80, 10)))

    def test_set_forward_nothing_is_refused(self):
        for step in (30, 3600):
            with self.subTest(step=step):
                self.assertEqual(self.refused(self.after_a_step(-step, 20)), [])

    def test_a_restart_after_the_clock_was_set_back_can_accept_a_statement_again(self):
        # The limit the record's docstring states: after a step back, wait.
        # A record made after the step compares with the clock as it then
        # reads, and takes for new what an earlier process accepted.
        timeline = Timeline(self)
        token = timeline.traffic(80)
        timeline.wall -= 30
        made_at = timeline.wall
        restarted = MemoryReplay(clock=lambda: made_at, monotonic=lambda: 5000.0 + timeline.elapsed)
        verdicts = {}
        for elapsed in range(0, 95, 5):
            if elapsed:
                timeline.run(5)
            # The record that was left running never takes it again.
            self.assertIn(timeline.verdict(token), self.NO_REPLAY)
            verdicts[elapsed] = timeline.verdict(token, restarted)
        # To the clock as set back the statement is first too new, then good
        # for 65 seconds, and the restarted record has no trace of it.
        self.assertEqual({verdicts[elapsed] for elapsed in range(0, 25, 5)}, {"claims: iat is in the future"})
        self.assertIsNone(verdicts[25])
        self.assertEqual({verdicts[elapsed] for elapsed in range(30, 90, 5)},
                         {"replay: this statement was already accepted"})
        self.assertEqual(verdicts[90], "claims: expired")

    def test_a_record_made_while_the_clock_read_ahead_refuses_everything_until_the_clock_has_passed_that(self):
        record = MemoryReplay(clock=lambda: NOW + 3600)
        for later, accepted in ((0, False), (1800, False), (3600, False), (3605, False), (3606, True)):
            now = NOW + later
            fresh = signed(self.claims(iat=now, exp=now + 60, groups_at=now, jti="clock-ahead-%010d" % later))
            with self.subTest(later=later):
                if accepted:
                    self.assertEqual(self.accept(fresh, now=now, replay=record).exp, now + 60)
                else:
                    self.refuse(fresh, "replay: issued before this record began", now=now, replay=record)

    def test_a_record_that_only_drops_by_time_takes_a_statement_twice_once_the_clock_is_set_back(self):
        # Why the Replay protocol asks a record to go on refusing what it has
        # dropped. Three records of a caller's own, each dropping a jti in due
        # time, and MemoryReplay. A statement is accepted; 71 seconds later it
        # has expired and other traffic passes; then the wall clock is set
        # back 12 seconds, to a second before the statement expires.
        class ByTheWallClock:
            """Keeps a jti until `exp + CLOCK_SKEW` has passed on the wall clock."""

            def __init__(self, timeline):
                self.timeline, self.kept, self.latest_dropped = timeline, {}, -1

            def due(self, exp, since):
                return exp + bus_statement.CLOCK_SKEW < self.timeline.wall

            def still_refused(self, exp):
                return False

            def claim(self, jti, exp):
                for old, (old_exp, since) in list(self.kept.items()):
                    if self.due(old_exp, since):
                        del self.kept[old]
                        self.latest_dropped = max(self.latest_dropped, old_exp)
                if jti in self.kept or self.still_refused(exp):
                    return False
                self.kept[jti] = (exp, self.timeline.elapsed)
                return True

        class ByAClockThatCannotBeSetBack(ByTheWallClock):
            """Keeps a jti for REPLAY_RETENTION seconds on the monotonic clock."""

            def due(self, exp, since):
                return self.timeline.elapsed - since >= bus_statement.REPLAY_RETENTION

        class AndTheLatestExpDropped(ByTheWallClock):
            """Drops by the wall clock too, but never accepts an `exp` that is not later than the latest it dropped."""

            def still_refused(self, exp):
                return exp <= self.latest_dropped

        verdicts = {}
        for kind in (ByTheWallClock, ByAClockThatCannotBeSetBack, AndTheLatestExpDropped, None):
            timeline = Timeline(self)
            if kind is not None:
                timeline.record = kind(timeline)
            token = timeline.traffic(20)
            timeline.run(71)
            self.assertEqual(timeline.verdict(token), "claims: expired")
            self.assertIsNone(timeline.verdict(timeline.issue()))
            timeline.wall -= 12
            verdicts[kind.__name__ if kind else "MemoryReplay"] = timeline.verdict(token)
        self.assertEqual(verdicts, {
            "ByTheWallClock": None,                     # accepted a second time
            "ByAClockThatCannotBeSetBack": None,        # and here too: it is the verifier's clock that went back
            "AndTheLatestExpDropped": "replay: this statement was already accepted",
            "MemoryReplay": self.FORGOTTEN})


class MemoryReplayTests(unittest.TestCase):
    """The record itself, on clocks the test moves by hand."""

    def setUp(self):
        self.monotonic = 5000.0
        self.replay = MemoryReplay(clock=lambda: 1000, monotonic=lambda: self.monotonic)
        self.exp = 2000

    def refused(self, reason, jti, exp, replay=None):
        with self.assertRaises(StatementError) as caught:
            (self.replay if replay is None else replay).claim(jti, exp)
        self.assertIn(reason, caught.exception.reason)
        self.assertEqual(str(caught.exception), "unauthorized")

    def test_it_has_the_shape_the_protocol_names(self):
        self.assertEqual(inspect.signature(MemoryReplay.claim), inspect.signature(bus_statement.Replay.claim))
        self.assertEqual(list(inspect.signature(bus_statement.Replay.claim).parameters), ["self", "jti", "exp"])
        self.assertEqual(typing.get_type_hints(bus_statement.Replay.claim), {"jti": str, "exp": int, "return": bool})
        protocol = " ".join(bus_statement.Replay.__doc__.split())
        for duty in ("atomic", "exp + CLOCK_SKEW", "restarts must either persist", "every process", "does the second",
                     # What ClockSteps shows a record needs beyond keeping a jti for a time:
                     "a record that drops a jti must still refuse it",
                     "never accepts an `exp` that is not later",
                     "does not replace this",
                     # and what OnlyOnce shows of the call itself:
                     "that is not an Exception", "sets no limit on the time"):
            self.assertIn(duty, protocol)
        record = " ".join(MemoryReplay.__doc__.split())
        for duty in ("restarts must either persist", "does the second", "one lock", "never drops a live claim",
                     "monotonic clock",
                     # The wall clock moved, as ClockSteps runs it:
                     "Set forward, it changes nothing here",
                     "never makes this record accept a statement twice",
                     "Set back by CLOCK_SKEW seconds or less, nothing more is refused",
                     "for about as long as the step",
                     "no more of its life is left than the step less 10 seconds",
                     "can be accepted again",
                     "the safe course is to wait, not to restart",
                     "A record made while the clock read ahead refuses everything"):
            self.assertIn(duty, record)

    def test_first_claim_true_and_every_later_one_false(self):
        self.assertIs(self.replay.claim("a", self.exp), True)
        self.assertIs(self.replay.claim("a", self.exp), False)
        self.assertIs(self.replay.claim("a", self.exp + 1), False, "the jti alone is the identity")
        self.assertIs(self.replay.claim("b", self.exp), True)
        self.assertEqual(self.replay.held(), 2)

    def test_retention_is_the_longest_life_of_a_statement_and_a_margin(self):
        self.assertEqual((bus_statement.LIFETIME, bus_statement.CLOCK_SKEW), (60, 5))
        self.assertEqual(bus_statement.REPLAY_RETENTION, 70)

    def test_a_claim_is_kept_for_the_retention_on_the_monotonic_clock(self):
        self.assertIs(self.replay.claim("a", self.exp), True)
        for elapsed in (0.0, 1.0, 69.0, 69.999):
            self.monotonic = 5000.0 + elapsed
            self.assertIs(self.replay.claim("a", self.exp), False, elapsed)
            self.assertEqual(self.replay.held(), 1)
        self.monotonic = 5070.0
        self.assertEqual(self.replay.held(), 0)

    def test_what_is_forgotten_is_still_refused(self):
        # Seventy seconds on, the statement is long expired by any honest wall
        # clock. Should that clock have been set back, the record still refuses
        # it, and anything issued no later.
        self.assertIs(self.replay.claim("a", self.exp), True)
        self.monotonic += 70
        self.refused("replay: not newer than a statement this record has forgotten", "a", self.exp)
        self.refused("replay: not newer than a statement this record has forgotten", "b", self.exp)
        self.refused("replay: not newer than a statement this record has forgotten", "c", self.exp - 30)
        self.assertIs(self.replay.claim("d", self.exp + 1), True)
        self.assertEqual(self.replay.held(), 1)
        # It is the latest `exp` forgotten that counts, not the last one: a
        # statement that expires sooner may well be accepted, and dropped, later.
        replay = MemoryReplay(clock=lambda: 1000, monotonic=lambda: self.monotonic)
        self.assertIs(replay.claim("a", 2000), True)
        self.monotonic += 1
        self.assertIs(replay.claim("b", 1990), True)
        self.monotonic += 70
        self.assertEqual(replay.held(), 0)
        for jti, exp in (("a", 2000), ("b", 1990), ("c", 1995), ("d", 2000)):
            self.refused("replay: not newer than a statement this record has forgotten", jti, exp, replay)
        self.assertIs(replay.claim("e", 2001), True)

    def test_the_wall_clock_is_read_once_and_never_again(self):
        readings = []

        def clock():
            readings.append(len(readings))
            return 1000

        replay = MemoryReplay(clock=clock, monotonic=lambda: self.monotonic)
        for number in range(5):
            self.assertIs(replay.claim("jti-%d" % number, self.exp + number), True)
            self.monotonic += 100
        self.assertEqual(readings, [0])

    def test_statements_a_predecessor_could_have_accepted_are_refused(self):
        # Begun at 1000: a predecessor reading the same second accepted iat up to 1005.
        for exp, accepted in ((1, False), (1064, False), (1065, False), (1066, True), (2000, True)):
            with self.subTest(exp=exp):
                if accepted:
                    self.assertIs(self.replay.claim("jti-%d" % exp, exp), True)
                else:
                    self.refused("replay: issued before this record began", "jti-%d" % exp, exp)
        late = MemoryReplay(clock=lambda: 1000.5, monotonic=lambda: self.monotonic)
        self.refused("replay: issued before this record began", "a", 1065, late)
        self.assertIs(late.claim("a", 1066), True)

    def test_it_is_bounded_and_never_drops_a_live_claim_to_make_room(self):
        replay = MemoryReplay(capacity=3, clock=lambda: 1000, monotonic=lambda: self.monotonic)
        for number in range(3):
            self.assertIs(replay.claim("jti-%d" % number, self.exp + number), True)
            self.monotonic += 1
        for _ in range(2):
            self.refused("replay: the record of accepted statements is full", "one-more", self.exp + 9, replay)
        self.assertEqual(replay.held(), 3)
        for number in range(3):
            self.assertIs(replay.claim("jti-%d" % number, self.exp + number), False, "still on record")
        # Room returns only as claims reach the end of their retention, oldest first.
        self.monotonic = 5000.0 + 70
        self.assertEqual(replay.held(), 2)
        self.assertIs(replay.claim("one-more", self.exp + 9), True)
        self.refused("replay: the record of accepted statements is full", "and-another", self.exp + 9, replay)
        self.monotonic = 5000.0 + 72
        self.assertEqual(replay.held(), 1)

    def test_default_capacity_and_clocks(self):
        self.assertEqual(bus_statement.REPLAY_CAPACITY, 65536)
        with mock.patch.object(bus_statement.time, "time", return_value=1234.5) as wall, \
                mock.patch.object(bus_statement.time, "monotonic", return_value=77.0) as monotonic:
            replay = MemoryReplay()
            self.assertEqual((wall.call_count, monotonic.call_count), (1, 0))
            self.refused("replay: issued before this record began", "a", 1299, replay)
            self.assertIs(replay.claim("a", 1300), True)
            self.assertEqual(wall.call_count, 1)
            self.assertGreaterEqual(monotonic.call_count, 1)
        parameters = inspect.signature(MemoryReplay).parameters
        self.assertEqual(list(parameters), ["capacity", "clock", "monotonic"])
        self.assertTrue(all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()))

    def test_unusable_settings_are_refused_when_it_is_made(self):
        for label, arguments in (("capacity zero", {"capacity": 0}), ("capacity negative", {"capacity": -1}),
                                 ("capacity fraction", {"capacity": 1.5}), ("capacity bool", {"capacity": True}),
                                 ("capacity None", {"capacity": None}),
                                 ("clock NaN", {"clock": lambda: float("nan")}), ("clock negative", {"clock": lambda: -1}),
                                 ("clock text", {"clock": lambda: "1000"}), ("clock None", {"clock": lambda: None}),
                                 ("clock not callable", {"clock": 1000}), ("monotonic not callable", {"monotonic": 5.0})):
            with self.subTest(label):
                self.assertRaises(ValueError, MemoryReplay, **arguments)
        self.assertRaises(TypeError, MemoryReplay, 16)

    def test_claims_of_the_wrong_shape_are_refused(self):
        for jti, exp in ((None, self.exp), (b"a", self.exp), (7, self.exp), ("a", None), ("a", str(self.exp)),
                         ("a", float(self.exp)), ("a", True)):
            with self.subTest(jti=jti, exp=exp):
                self.refused("replay: a claim is a jti string and an integer exp", jti, exp)
        self.assertEqual(self.replay.held(), 0)

    def test_the_whole_claim_runs_under_one_lock(self):
        held = []

        def monotonic():
            held.append(replay._lock.locked())
            return 5000.0

        replay = MemoryReplay(clock=lambda: 1000, monotonic=monotonic)
        replay.claim("a", self.exp)
        replay.claim("a", self.exp)
        replay.held()
        self.assertEqual(held, [True, True, True])
        self.assertIs(replay._lock.locked(), False)

    def test_an_empty_record_is_not_false(self):
        # A record that counted as false when empty would be replaced by
        # `replay or default` in a caller, silently.
        self.assertEqual(self.replay.held(), 0)
        self.assertIs(bool(self.replay), True)
        self.assertFalse(hasattr(self.replay, "__len__"))

    def test_many_threads_claiming_the_same_statements_get_each_once(self):
        replay = MemoryReplay(clock=lambda: 1000, monotonic=time.monotonic)
        jtis = ["jti-%03d" % number for number in range(300)]
        start = threading.Barrier(8)

        def worker(_):
            start.wait(timeout=30)
            return [jti for jti in jtis if replay.claim(jti, self.exp)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            won = [jti for claimed in pool.map(worker, range(8)) for jti in claimed]
        self.assertEqual(sorted(won), jtis)
        self.assertEqual(replay.held(), 300)


# --- stale groups -----------------------------------------------------------

class StaleGroups(Case):
    def test_groups_within_the_age_limit_are_kept(self):
        for age in (0, 20, 299, 300):
            statement = self.accept(signed(self.claims(groups_at=NOW - age)), max_groups_age=300)
            with self.subTest(age=age):
                self.assertEqual(statement.groups, tuple(GROUPS))
                self.assertIs(statement.groups_stale, False)

    def test_stale_groups_come_back_empty_with_the_flag(self):
        for age in (301, 3600, NOW):
            statement = self.accept(signed(self.claims(groups_at=NOW - age)), max_groups_age=300)
            with self.subTest(age=age):
                self.assertEqual(statement.groups, ())
                self.assertIs(statement.groups_stale, True)
                self.assertEqual(statement.groups_at, NOW - age)
                self.assertEqual((statement.uuid, statement.name, statement.jti), (UUID, "Ada Example", "q83vEjRWeJCrze8SNFZ4kA"))

    def test_smallest_and_largest_limit_and_a_fractional_clock(self):
        token = signed(self.claims(groups_at=NOW - 1))
        self.assertIs(self.accept(token, max_groups_age=1).groups_stale, False)
        self.assertIs(self.accept(token, max_groups_age=1, now=NOW + 0.5).groups_stale, True)
        self.assertIs(self.accept(self.good, max_groups_age=21, now=NOW + 0.5).groups_stale, False)
        self.assertIs(self.accept(self.good, max_groups_age=20, now=NOW + 0.5).groups_stale, True)
        self.assertIs(self.accept(self.good, max_groups_age=2 ** 53 - 1).groups_stale, False)

    def test_absurd_groups_at_is_refused_without_a_crash(self):
        # More digits than a float holds, against a fractional clock.
        pair = '"groups_at":%d' % (NOW - 20)
        self.assertIn(pair, self.raw_claims())
        for numeral in ("-1" + "0" * 400, "1" + "0" * 400):
            token = signed_raw(HEADER, self.raw_claims().replace(pair, '"groups_at":' + numeral))
            self.refuse(token, "claims: groups_at is not", now=NOW + 0.5)

    def test_a_list_dated_1970_grants_nothing(self):
        # There is no way to ask for "any age": the limit is a required argument.
        statement = self.accept(signed(self.claims(groups_at=0)))
        self.assertEqual((statement.groups, statement.groups_stale, statement.groups_at), ((), True, 0))
        self.assertEqual(statement.uuid, UUID)

    def test_stale_empty_list_is_still_flagged(self):
        statement = self.accept(signed(self.claims(groups=[], groups_at=NOW - 301)), max_groups_age=300)
        self.assertEqual((statement.groups, statement.groups_stale), ((), True))


# --- order of checks --------------------------------------------------------

class OrderOfChecks(Case):
    def test_payload_is_neither_decoded_nor_parsed_before_the_signature_verifies(self):
        header, _, signature = self.good.split(".")
        payload = b64url("this is not JSON, and it was never signed")
        with mock.patch.object(bus_statement.json, "loads", wraps=json.loads) as loads, \
                mock.patch.object(bus_statement.base64, "urlsafe_b64decode", wraps=base64.urlsafe_b64decode) as decode:
            self.refuse(".".join((header, payload, signature)), "signature:")
            decoded = [str(call.args[0]) for call in decode.call_args_list]
            self.assertEqual(loads.call_count, 1, "only the protected header is parsed")
            self.assertEqual(len(decoded), 2, "only the header and the signature are decoded")
            self.assertFalse(any(text.startswith(payload) for text in decoded))
            loads.reset_mock()
            decode.reset_mock()
            self.accept(self.good)
            self.assertEqual((loads.call_count, decode.call_count), (2, 3))

        # Nor is the request body hashed before the signature has verified.
        # Every use the verifier makes of hashlib is noted here, in order,
        # with the verdict on the signature.
        events = []

        class Hashes:
            def __getattr__(self, name):
                def hashing(data=b"", *more, **named):
                    events.append((name, bytes(data)))
                    return getattr(hashlib, name)(data, *more, **named)
                return hashing

        def signature_checked(*arguments):
            verdict = signature_check(*arguments)
            events.append(("signature", verdict))
            return verdict

        signature_check = bus_statement.ed25519_verify.verify
        body = b'{"op":"example","text":"a body that is hashed last"}'
        request = dict(method="POST", body=body)
        good = signed(self.claims(htm="POST", bdy=digest(body)))
        header, payload, signature = good.split(".")
        forged = ".".join((header, payload, signed(self.claims(htm="POST", bdy=digest(body)), key="attacker").split(".")[2]))
        with mock.patch.object(bus_statement, "hashlib", Hashes()), \
                mock.patch.object(bus_statement.ed25519_verify, "verify", side_effect=signature_checked):
            self.refuse(forged, "signature:", **request)
            self.assertEqual(events, [("signature", False)], "refused for its signature with nothing hashed")
            del events[:]
            self.refuse(".".join((header, b64url("never signed"), signature)), "signature:", **request)
            self.assertEqual(events, [("signature", False)])
            del events[:]
            statement = self.accept(good, **request)
        self.assertEqual(events[:2], [("signature", True), ("sha256", body)], "the body is hashed once the signature holds")
        self.assertEqual([name for name, _ in events], ["signature", "sha256", "sha256"])
        self.assertEqual(hashlib.sha256(events[2][1]).hexdigest(), statement.reader_hash)

    def test_unsigned_payload_never_reaches_a_claim_check(self):
        header, _, signature = self.good.split(".")
        for label, text in (("expired", self.raw_claims(iat=NOW - 600, exp=NOW - 540)),
                            ("unknown claim", self.raw_claims(admin=True)), ("array", "[]"),
                            ("duplicate", '{"sub":1,"sub":2}'), ("invalid UTF-8", b"\xff\xfe")):
            with self.subTest(label):
                self.refuse(".".join((header, b64url(text), signature)), "signature:")

    def test_header_is_judged_before_the_signature(self):
        _, payload, signature = self.good.split(".")
        zeros = b64url(bytes(64))
        self.refuse(".".join((b64url(json.dumps(dict(HEADER, kid=UNPINNED_KID))), payload, zeros)), "header: kid")
        self.refuse(".".join((b64url(json.dumps(dict(HEADER, alg="none"))), payload, signature)), "header: alg")

    def test_structure_is_judged_before_the_header(self):
        _, payload, signature = self.good.split(".")
        self.refuse(".".join((b64url(json.dumps(dict(HEADER, alg="none"))), payload, signature + "=")), "structure:")

    def test_size_is_judged_before_structure(self):
        self.refuse("not a statement " * 600, "size:")


# --- nonsense, signed and unsigned ------------------------------------------

class Robustness(Case):
    """Whatever a statement holds, the answer is a refusal and never a crash."""
    ODD = (None, True, False, 0, -1, 1.5, NOW, NOW + 60, 2 ** 64, -2 ** 64, "", "bus", "GET", "x" * 22, "x" * 200,
           "\u0000", "\u200b", "\ud800", [], {}, [None], [[]], {"a": 1}, ["x"], list(reversed(GROUPS)))
    # Numerals a serializer would not write, placed into the text as they are.
    NUMERALS = ("1" + "0" * 400, "-1" + "0" * 400, "1e400", "-1e400", "1800000000.0", "1e3", "[" * 300 + "]" * 300)

    def verdict(self, token, context, **overrides):
        """The Statement, or None for a refusal; anything else fails the test."""
        try:
            return self.accept(token, **overrides)
        except StatementError as error:
            self.assertEqual(error.code, "unauthorized", context)
            return None
        except Exception as error:      # the point of the test: nothing else may escape
            self.fail("%s: %s, for %s" % (type(error).__name__, error, context))

    def texts(self, pairs, names):
        """Not an object at all; then every name in turn removed and set to each odd value and numeral."""
        for text in ("null", "true", "0", '"x"', "[]", "[{}]", "{}"):
            yield text
        for name in names:
            if name in pairs:
                yield json.dumps({key: value for key, value in pairs.items() if key != name})
            for value in self.ODD:
                yield json.dumps(dict(pairs, **{name: value}))
            for numeral in self.NUMERALS:
                yield json.dumps(dict(pairs, **{name: "numeral"})).replace('"numeral"', numeral)

    def test_signed_nonsense_in_the_claims(self):
        expected = self.claims()
        accepted = []
        for text in self.texts(expected, list(expected) + ["nbf", "extra", ""]):
            # A fractional clock and an age limit: the arithmetic most likely to trip.
            statement = self.verdict(signed_raw(HEADER, text), text, now=NOW + 0.5, max_groups_age=300)
            if statement is not None:
                accepted.append(text)
                parsed = json.loads(text)
                self.assertLessEqual(set(parsed), set(expected), text)
                for name in ("iss", "aud", "sub", "htm", "htu", "bdy", "iat", "exp"):
                    # Compared as JSON text, so that 1.0 or true cannot pass for 1.
                    self.assertEqual(json.dumps(parsed[name]), json.dumps(expected[name]), text)
                self.assertEqual((statement.sub, statement.jti), (parsed["sub"], parsed["jti"]), text)
                self.assertEqual((type(statement.groups_at), type(statement.exp)), (int, int), text)
                # A name is kept as signed, or dropped and flagged; never altered.
                self.assertIn((statement.name, statement.name_dropped),
                              ((parsed.get("name"), False), (None, "name" in parsed)), text)
        # What may vary and stay valid: no name or another one, another jti,
        # no groups, and a group list read at another time in the past.
        changed = sorted({name for text in accepted for name in expected
                          if json.loads(text).get(name, ABSENT) != expected[name]})
        self.assertEqual(changed, ["groups", "groups_at", "jti", "name"])
        self.assertLess(len(accepted), 20)

    def test_signed_nonsense_in_the_header(self):
        for text in self.texts(HEADER, list(HEADER) + ["crit", "jwk", "b64", ""]):
            self.assertIsNone(self.verdict(signed_raw(text, self.raw_claims()), text), text)

    def test_no_other_spelling_of_a_valid_statement_is_accepted(self):
        segments = self.good.split(".")
        tokens = []
        # The last character of a segment is where its spare bits are.
        for index, segment in enumerate(segments):
            for character in ALPHABET + "=":
                respelled = list(segments)
                respelled[index] = segment[:-1] + character
                tokens.append(".".join(respelled))
        self.assertEqual(tokens.count(self.good), 3, "the sweep passes over the true spelling once per segment")
        rng = random.Random(20261125)
        for _ in range(200):
            characters = list(self.good)
            position, kind = rng.randrange(len(characters)), rng.randrange(4)
            if kind == 0:
                characters[position] = rng.choice(ALPHABET + ".=+/ \n\x00\u00e9")
            elif kind == 1:
                del characters[position]
            elif kind == 2:
                characters.insert(position, rng.choice(ALPHABET + ".= "))
            else:
                characters = characters[:position] if rng.randrange(2) else characters + [rng.choice(".=A ")]
            tokens.append("".join(characters))
        for token in tokens:
            if token != self.good:
                self.assertIsNone(self.verdict(token, repr(token)), repr(token))


# --- pinned keys ------------------------------------------------------------

class LoadKeys(unittest.TestCase):
    def refuse(self, config, *fragments):
        with self.assertRaises(ValueError) as caught:
            load_keys(config)
        message = str(caught.exception)
        for fragment in fragments:
            self.assertIn(fragment, message)
        for public in PUBLIC.values():
            self.assertNotIn(public, message)
            self.assertNotIn(public[:12], message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__, "no chained error may carry the setting")
        return message

    def test_returns_raw_keys_by_kid(self):
        keys = load_keys(pinned("a", "b"))
        self.assertEqual(keys, {KID["a"]: unb64url(PUBLIC["a"]), KID["b"]: unb64url(PUBLIC["b"])})
        self.assertTrue(all(type(key) is bytes and len(key) == 32 for key in keys.values()))

    def test_kid_for_is_the_gateways_rule(self):
        for name, public in PUBLIC.items():
            with self.subTest(name):
                self.assertEqual(kid_for(unb64url(public)), KID[name])
                self.assertRegex(KID[name], r"\Agw-[A-Za-z0-9_-]{16}\Z")
        # The RFC 8032 TEST 1 public key; its kid was computed with xxd, openssl
        # and base64, and is also the kid the gateway's own vectors carry.
        rfc = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        self.assertEqual(kid_for(rfc), "gw-If4x36FUomFia_hU")
        for label, bad in (("31 bytes", rfc[:31]), ("33 bytes", rfc + b"\0"), ("empty", b""),
                           ("bytearray", bytearray(rfc)), ("base64url text", b64url(rfc)), ("hex text", rfc.hex()),
                           ("None", None)):
            with self.subTest(label):
                self.assertRaises(ValueError, kid_for, bad)

    def test_one_to_eight_keys(self):
        public = [SIGNER.call(op="key", name="k%d" % number)["public"] for number in range(9)]

        def setting(count):
            return json.dumps({kid_of(key): key for key in public[:count]})
        self.assertEqual(len(load_keys(setting(1))), 1)
        self.assertEqual(len(load_keys(setting(8))), 8)
        self.refuse(setting(9), "BUS_GATEWAY_SIGNERS", "1 to 8")
        self.refuse("{}", "BUS_GATEWAY_SIGNERS", "1 to 8")

    def test_setting_must_be_one_json_object(self):
        good = pinned("a")
        for label, config in (("not JSON", PUBLIC["a"]), ("array", json.dumps([PUBLIC["a"]])), ("null", "null"),
                              ("string", json.dumps(PUBLIC["a"])), ("empty", ""), ("None", None),
                              ("bytes", good.encode("ascii")), ("dict", json.loads(good)),
                              ("truncated", good[:-1]), ("two objects", good + good)):
            with self.subTest(label):
                self.refuse(config, "BUS_GATEWAY_SIGNERS")

    def test_duplicate_kid(self):
        entry = "%s:%s" % (json.dumps(KID["a"]), json.dumps(PUBLIC["a"]))
        self.assertEqual(load_keys("{" + entry + "}"), {KID["a"]: unb64url(PUBLIC["a"])})
        self.refuse("{" + entry + "," + entry + "}", "BUS_GATEWAY_SIGNERS")

    def test_key_under_another_kid_is_refused(self):
        # A mislabelled key is a configuration error, not a second name for the
        # key. The message names the label it found and not the right one:
        # the remedy is to check which key was meant, not to copy a kid over.
        hexadecimal = "gw-" + hashlib.sha256(unb64url(PUBLIC["a"])).hexdigest()[:16]
        for label, config, found, right in (
                ("kid of another key", {KID["b"]: PUBLIC["a"]}, KID["b"], KID["a"]),
                ("two kids exchanged", {KID["a"]: PUBLIC["b"], KID["b"]: PUBLIC["a"]}, KID["a"], KID["b"]),
                ("second entry mislabelled", {KID["b"]: PUBLIC["b"], UNPINNED_KID: PUBLIC["a"]}, UNPINNED_KID, KID["a"]),
                ("hash in hexadecimal", {hexadecimal: PUBLIC["a"]}, hexadecimal, KID["a"]),
                ("last character changed", {KID["a"][:-1] + ("A" if KID["a"][-1] != "A" else "B"): PUBLIC["a"]},
                 KID["a"][:-1], KID["a"])):
            with self.subTest(label):
                message = self.refuse(json.dumps(config), "BUS_GATEWAY_SIGNERS", "kid " + found,
                                      "is not the kid of the key it labels")
                self.assertNotIn(right, message)

    def test_kid_must_have_the_derived_shape(self):
        kid, key = KID["a"], PUBLIC["a"]
        untruncated = "gw-" + b64url(hashlib.sha256(unb64url(key)).digest())
        for label, bad in (("a short name after the prefix", "gw-a"), ("free name", "prod-2026"), ("empty", ""),
                           ("prefix missing", kid[3:]), ("other prefix", "kw-" + kid[3:]),
                           ("prefix in upper case", "GW-" + kid[3:]), ("15 characters", kid[:-1]),
                           ("17 characters", kid + "A"), ("untruncated hash", untruncated),
                           ("leading space", " " + kid), ("trailing newline", kid + "\n"),
                           ("standard base64 character", kid[:-1] + "+"), ("the key itself", key)):
            with self.subTest(label):
                message = self.refuse(json.dumps({bad: key}), "BUS_GATEWAY_SIGNERS", "a kid is")
                if bad:
                    self.assertNotIn(bad, message, "what stands in a kid's place is not echoed")
        # Key and kid written the wrong way round.
        self.refuse(json.dumps({key: kid}), "BUS_GATEWAY_SIGNERS", "a kid is")

    def test_invalid_key_names_the_kid_and_nothing_of_the_key(self):
        good = PUBLIC["a"]
        small_order = b64url(bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"))
        identity = b64url(b"\x01" + bytes(31))
        for label, kid, value in (("padded", KID["a"], good + "="), ("truncated", KID["a"], good[:-1]),
                                  ("hex", KID["a"], unb64url(good).hex()),
                                  # Correctly labelled, and still not a key that may be pinned.
                                  ("small order", kid_of(small_order), small_order),
                                  ("identity", kid_of(identity), identity),
                                  ("number", KID["a"], 7), ("null", KID["a"], None), ("list", KID["a"], [good]),
                                  ("object", KID["a"], {"kty": "OKP", "crv": "Ed25519", "x": good})):
            with self.subTest(label):
                message = self.refuse(json.dumps({KID["b"]: PUBLIC["b"], kid: value}),
                                      "BUS_GATEWAY_SIGNERS", "kid " + kid + ": Ed25519 public key")
                if isinstance(value, str):
                    self.assertNotIn(value[:12], message)

    def test_a_key_outside_the_subgroup_of_prime_order_is_refused(self):
        # An honest key with the point of order 2 added to it: one of the seven
        # keys of mixed order in scripts/test-ed25519-verify.py. It is a valid
        # point and not of small order, so only this check stands in its way.
        mixed = b64url(bytes.fromhex("16a567fe7d4ef5482ab4012c369bf8c5f11e8d0c2559dcda50fde59708f8aee5"))
        message = self.refuse(json.dumps({KID["b"]: PUBLIC["b"], kid_of(mixed): mixed}), "BUS_GATEWAY_SIGNERS: kid "
                              + kid_of(mixed) + ": Ed25519 public key is not in the subgroup of prime order")
        self.assertNotIn(mixed[:12], message)
        # load_keys is the only place this is tested: the verifier takes the
        # keys it is given, and says so.
        self.assertIn("built by hand", " ".join(load_keys.__doc__.split()))
        for function in (verify_statement, verify_request):
            with self.subTest(function.__name__):
                self.assertIn("built by hand", " ".join(function.__doc__.split()))
                self.assertIn("load_keys", " ".join(function.__doc__.split()))


# --- published keys ---------------------------------------------------------

# The seed of each private key that a specification prints, and the seed of all
# zeros. The public keys are derived from them here by node, not written out.
PUBLISHED_SEEDS = {
    "RFC 8032 section 7.1, TEST 1": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
    "RFC 8032 section 7.1, TEST 2": "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
    "RFC 8032 section 7.1, TEST 3": "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
    "RFC 8032 section 7.1, TEST 1024": "f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5",
    "RFC 8032 section 7.1, TEST SHA(abc)": "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
    "RFC 8032 section 7.2, foo, bar and foo2": "0305334e381af78f141cb666f6199f57bc3495335a256a95bd2a55bf546663f6",
    "RFC 8032 section 7.2, foo3": "ab9c2853ce297ddab85c993b3ae14bcad39b2c682beabc27d6d4eb20711d6560",
    "RFC 8410 section 10.3": "d4ee72dbf913584ad5b6d8f1f769f8ad3afe7c28cbf1d4fbe097a88f44755842",
    "the seed of all zeros": "00" * 32,
}
BASE_POINT = "58" + "66" * 31       # as a public key its secret scalar is 1


def note_example_key():
    text = (ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8")
    (kid, key), = json.loads(re.findall(r"```json\n(.*?)```", text, re.S)[0]).items()
    return key


class PublishedKeys(unittest.TestCase):
    """load_keys refuses eleven published keys unless a test asks for them.

    Anyone can sign with ten of them: a specification prints the seed, or the
    seed is all zeros, or the secret scalar is 1. The eleventh is the example
    key of the contract note, which is on the list for being printed there.
    """
    REFUSED = "this is a published key, which no broker may pin"

    @classmethod
    def setUpClass(cls):
        cls.published = {name: SIGNER.call(op="public", seed=seed)["public"] for name, seed in PUBLISHED_SEEDS.items()}
        cls.published["the base point"] = b64url(bytes.fromhex(BASE_POINT))
        cls.published["the example key of the contract note"] = note_example_key()

    def refuse(self, config, **named):
        with self.assertRaises(ValueError) as caught:
            load_keys(config, **named)
        self.assertIsNone(caught.exception.__context__)
        return str(caught.exception)

    def test_the_list_is_these_eleven(self):
        self.assertEqual(len(self.published), 11)
        self.assertEqual(len(set(self.published.values())), 11)
        self.assertEqual(bus_statement._PUBLISHED_KEYS, frozenset(unb64url(key) for key in self.published.values()))
        # What is said of the list holds for each key on it. Nothing prints
        # the secret of the note's key, so no text says anyone can sign with it.
        said = " ".join(load_keys.__doc__.split())
        self.assertIn("Eleven published keys are refused. Anyone can sign with ten of them:", said)
        self.assertIn("The eleventh is the example key of docs/GATEWAY-STATEMENTS.md.", said)
        note = " ".join((ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8").split())
        self.assertIn("It also refuses the example key of this note.", note)

    def test_each_is_refused_by_default_and_loads_when_asked_for(self):
        for name, key in self.published.items():
            kid = kid_of(key)
            setting = json.dumps({kid: key})
            with self.subTest(name):
                message = self.refuse(setting)
                self.assertEqual(message, "BUS_GATEWAY_SIGNERS: kid %s: %s" % (kid, self.REFUSED))
                self.assertNotIn(key[:12], message)
                self.assertEqual(self.refuse(setting, allow_published=False), message)
                self.assertEqual(load_keys(setting, allow_published=True), {kid: unb64url(key)})

    def test_one_published_key_refuses_the_whole_setting(self):
        key = self.published["RFC 8032 section 7.1, TEST 1"]
        for setting in ({KID["a"]: PUBLIC["a"], kid_of(key): key}, {kid_of(key): key, KID["a"]: PUBLIC["a"]}):
            with self.subTest(first=list(setting)[0]):
                self.assertIn(self.REFUSED, self.refuse(json.dumps(setting)))
                self.assertEqual(len(load_keys(json.dumps(setting), allow_published=True)), 2)

    def test_only_the_value_true_allows_them(self):
        key = self.published["the seed of all zeros"]
        setting = json.dumps({kid_of(key): key})
        for value in (None, 0, 1, "yes", "true", "false", [True], b"1"):
            with self.subTest(value=value):
                self.assertIn(self.REFUSED, self.refuse(setting, allow_published=value))
        self.assertRaises(TypeError, load_keys, setting, True)
        parameter = inspect.signature(load_keys).parameters["allow_published"]
        self.assertEqual((parameter.kind, parameter.default), (inspect.Parameter.KEYWORD_ONLY, False))

    def test_a_key_of_ones_own_is_not_on_the_list(self):
        self.assertEqual(load_keys(pinned("a", "b")), load_keys(pinned("a", "b"), allow_published=True))
        self.assertEqual(load_keys(pinned("a", "b")), KEYS)

    def test_the_fixtures_and_the_note_are_signed_with_keys_on_the_list(self):
        vectors = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual({vector["public_key"] for vector in vectors}, {self.published["RFC 8032 section 7.1, TEST 1"]})
        note = " ".join((ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8").split())
        for words in ("`allow_published=True`", "refuses", "RFC 8032", "RFC 8410", "not a complete"):
            self.assertIn(words, note)

    def test_nothing_in_lib_asks_for_them(self):
        # Tests and the note's example may; the broker and its tools may not.
        asking = re.compile(r"allow_published\s*=\s*(?!False\b)")
        found = [path.name for path in sorted((ROOT / "lib").glob("*.py"))
                 if any(asking.search(line) for line in path.read_text(encoding="utf-8").splitlines())]
        self.assertEqual(found, [])


# --- the gateway's own vectors ----------------------------------------------

class GatewayVectors(unittest.TestCase):
    """Statements made by the gateway team's signer, verified as shipped.

    The file is data from another team and is kept byte for byte. Every
    statement in it is signed with the RFC 8032 section 7.1 TEST 1 key, whose
    private half is printed in the RFC: a key for fixtures and nothing else.

    Each statement's name, audience and request are read from the file; no
    test spells them. Two statements are for the bus. The others are for
    another audience under the same key and issuer, and the bus refuses them.
    """
    RFC8032_TEST_1_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    KEY_ID = "gw-If4x36FUomFia_hU"
    VIEWER = "55555555-5555-4555-8555-555555555555"
    GROUPS = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")

    @classmethod
    def setUpClass(cls):
        cls.vectors = json.loads(FIXTURE.read_text(encoding="utf-8"))
        bus = bus_statement.BUS_AUDIENCE
        cls.for_the_bus = [vector for vector in cls.vectors if vector["options"]["audience"] == bus]
        cls.for_another = [vector for vector in cls.vectors if vector["options"]["audience"] != bus]
        # Of those for another audience: the ones that bind a request, and the ones that bind none.
        cls.bound = [vector for vector in cls.for_another if "request" in vector["options"]]
        cls.unbound = [vector for vector in cls.for_another if "request" not in vector["options"]]

    @staticmethod
    def request_of(vector):
        """The vector's own request as a request line carries it: the method in upper case."""
        request = vector["options"]["request"]
        return dict(method=request["method"].upper(), target=request["target"],
                    body=request["body_utf8"].encode("utf-8"))

    def arguments(self, vector, **overrides):
        """The verifier's arguments as the vector itself gives them."""
        arguments = dict(
            # Pinned as an operator would pin it, the public key under its kid,
            # but for one thing: the vectors are signed with the key of RFC 8032
            # TEST 1, which no broker may pin and which only a test may load.
            keys=load_keys(json.dumps({vector["kid"]: vector["public_key"]}), allow_published=True),
            now=vector["now"],
            # The issuer is whatever the vector carries; the verifier has no default.
            issuer=vector["claims"]["iss"],
            # The bus's own audience, and a limit their 30-second-old group list is within.
            audience=bus_statement.BUS_AUDIENCE, max_groups_age=300, replay=running_replay(vector["now"]))
        if "request" in vector["options"]:
            request = vector["options"]["request"]
            arguments.update(method=request["method"], target=request["target"],
                             body=request["body_utf8"].encode("utf-8"))
        arguments.update(overrides)
        return arguments

    def verify(self, vector, **overrides):
        return verify_request(header_values=[vector["token"]], **self.arguments(vector, **overrides))

    def refuse(self, vector, reason, **overrides):
        recorder = Recorder()
        with self.assertRaises(StatementError) as caught:
            self.verify(vector, replay=recorder, **overrides)
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertIn(reason, caught.exception.reason)
        self.assertEqual(str(caught.exception), "unauthorized")
        self.assertEqual(recorder.claims, [])

    def check(self, statement, vector):
        self.assertEqual(statement.sub, "owui:" + self.VIEWER)
        self.assertEqual(statement.uuid, self.VIEWER)
        self.assertEqual(statement.reader, "owui." + self.VIEWER)
        self.assertEqual(statement.groups, self.GROUPS)
        self.assertEqual(statement.groups_at, 1_799_999_970)
        self.assertIs(statement.groups_stale, False)
        self.assertEqual((statement.name, statement.name_dropped), ("Fixture Viewer", False))
        self.assertEqual(statement.jti, "AAECAwQFBgcICQoLDA0ODw")
        self.assertEqual(statement.exp, 1_800_000_060)
        self.assertEqual(statement.kid, self.KEY_ID)
        # The reader hash from the vector's own issuer, by node's SHA-256.
        material = b"\x00".join((b"com8/statement-reader/v1", vector["claims"]["iss"].encode("utf-8"),
                                 ("owui:" + self.VIEWER).encode("ascii")))
        self.assertEqual(statement.reader_hash, SIGNER.call(op="sha256", data=material.hex())["hex"])

    def test_the_file_holds_two_statements_for_the_bus_and_two_for_another_audience(self):
        self.assertEqual(len(self.vectors), 4)
        self.assertEqual(len({vector["name"] for vector in self.vectors}), 4)
        self.assertEqual((len(self.for_the_bus), len(self.for_another)), (2, 2))
        self.assertEqual(len({vector["options"]["audience"] for vector in self.for_another}), 1)
        self.assertEqual((len(self.bound), len(self.unbound)), (1, 1))
        self.assertEqual(len({vector["claims"]["iss"] for vector in self.vectors}), 1, "one issuer signs them all")
        for vector in self.vectors:
            claims = vector["claims"]
            with self.subTest(vector["name"]):
                self.assertEqual(vector["now"], 1_800_000_000)
                self.assertEqual(unb64url(vector["public_key"]).hex(), self.RFC8032_TEST_1_KEY)
                self.assertEqual(unb64url(vector["seed"]).hex(), PUBLISHED_SEEDS["RFC 8032 section 7.1, TEST 1"])
                header, payload, _ = vector["token"].split(".")
                self.assertEqual(json.loads(unb64url(header)), vector["header"])
                self.assertEqual(json.loads(unb64url(payload)), claims)
                self.assertEqual(claims["sub"], "owui:" + vector["subject"]["sub"])
                self.assertEqual(claims["aud"], vector["options"]["audience"])
                self.assertNotEqual(claims["iss"], ISSUER, "the vectors carry their own issuer")
                if "request" in vector["options"]:
                    request = self.request_of(vector)
                    self.assertEqual((claims["htm"], claims["htu"], claims["bdy"]),
                                     (request["method"], request["target"], digest(request["body"])))
                    self.assertEqual(claims["exp"] - claims["iat"], 60)
                else:
                    self.assertEqual({"htm", "htu", "bdy"} & set(claims), set())
                    self.assertNotEqual(claims["exp"] - claims["iat"], 60)
        # Between them the two for the bus have a body and none, and a method
        # given in lower case and one in upper case, so both are met below.
        self.assertEqual(sorted(bool(vector["options"]["request"]["body_utf8"]) for vector in self.for_the_bus),
                         [False, True])
        self.assertEqual(sorted(vector["options"]["request"]["method"].isupper() for vector in self.for_the_bus),
                         [False, True])

    def test_their_kid_is_kid_for_of_their_key(self):
        for vector in self.vectors:
            with self.subTest(vector["name"]):
                self.assertEqual(vector["kid"], self.KEY_ID)
                self.assertEqual(vector["header"]["kid"], self.KEY_ID)
                self.assertEqual(kid_for(unb64url(vector["public_key"])), self.KEY_ID)
                self.assertEqual(self.arguments(vector)["keys"], {self.KEY_ID: bytes.fromhex(self.RFC8032_TEST_1_KEY)})

    def test_statements_for_the_bus_verify_for_their_own_requests(self):
        for vector in self.for_the_bus:
            with self.subTest(vector["name"]):
                # A request line carries the method in upper case, and that is
                # what a broker hands the verifier.
                self.check(self.verify(vector, **self.request_of(vector)), vector)
                if vector["options"]["request"]["method"].isupper():
                    self.check(self.verify(vector), vector)     # and exactly as the file gives it

    def test_a_method_given_in_lower_case_is_refused(self):
        # One vector's options give the method in lower case while its `htm`
        # claim is in upper case: the signer writes the claim in upper case.
        # Taken literally from the options, the method does not equal the
        # claim. The verifier compares the two as given and never folds case,
        # so this is a refusal; it was not changed to make the vector pass.
        given_in_lower_case = [vector for vector in self.for_the_bus
                               if not vector["options"]["request"]["method"].isupper()]
        self.assertEqual(len(given_in_lower_case), 1)
        for vector in given_in_lower_case:
            given = vector["options"]["request"]["method"]
            self.assertEqual((given, vector["claims"]["htm"]), (given.lower(), given.upper()))
            self.assertEqual(self.arguments(vector)["method"], given)
            self.refuse(vector, "claims: htm is not the request method in upper case")
            self.check(self.verify(vector, method=given.upper()), vector)

    def test_statements_for_the_bus_are_bound_to_their_own_requests(self):
        for vector in self.for_the_bus:
            own = self.request_of(vector)
            with self.subTest(vector["name"]):
                for other in self.for_the_bus:
                    if other is not vector:
                        theirs = self.request_of(other)
                        self.assertNotEqual(theirs["method"], own["method"])
                        self.refuse(vector, "claims: htm", **theirs)
                        self.refuse(vector, "claims: htu", **dict(own, target=theirs["target"]))
                        self.refuse(vector, "claims: bdy", **dict(own, body=theirs["body"]))
                self.refuse(vector, "claims: htu", **dict(own, target=own["target"] + "/"))
                self.refuse(vector, "claims: htu", **dict(own, target=own["target"] + "?"))
                if "?" in own["target"]:
                    self.refuse(vector, "claims: htu", **dict(own, target=own["target"].split("?")[0]))
                    self.refuse(vector, "claims: htu", **dict(own, target=own["target"][:-1] + "2"))
                self.refuse(vector, "claims: bdy", **dict(own, body=own["body"] + b"\n"))
                self.refuse(vector, "claims: bdy", **dict(own, body=b"{}"))
                if own["body"]:
                    self.refuse(vector, "claims: bdy", **dict(own, body=b""))
                    # The same JSON written with a space after each colon.
                    self.refuse(vector, "claims: bdy", **dict(own, body=own["body"].replace(b":", b": ")))

    def test_statements_for_the_bus_are_refused_outside_their_minute_and_for_another_issuer_or_audience(self):
        another, = {vector["options"]["audience"] for vector in self.for_another}
        for vector in self.for_the_bus:
            own = self.request_of(vector)
            with self.subTest(vector["name"]):
                self.check(self.verify(vector, now=vector["now"] + 59, **own), vector)
                self.refuse(vector, "claims: expired", now=vector["now"] + 60, **own)
                self.refuse(vector, "claims: iat is in the future", now=vector["now"] - 6, **own)
                self.refuse(vector, "claims: iss is not the configured issuer", issuer=ISSUER, **own)
                self.refuse(vector, "claims: aud is not the configured audience", audience=another, **own)
                self.refuse(vector, "header: kid", keys=KEYS, **own)

    def test_statements_for_another_audience_are_refused_by_the_bus(self):
        # Under the same key, issuer and typ: their signatures are good, so it
        # is the audience that refuses them, whatever request they come with.
        requests = [self.request_of(vector) for vector in self.for_the_bus + self.bound]
        self.assertEqual(len(requests), 3)
        for vector in self.for_another:
            self.assertNotEqual(vector["options"]["audience"], bus_statement.BUS_AUDIENCE)
            for request in requests:
                with self.subTest(vector["name"], method=request["method"], target=request["target"]):
                    self.refuse(vector, "claims: aud is not the configured audience", **request)

    def test_the_one_that_binds_no_request_is_refused_under_its_own_audience_too(self):
        for vector in self.unbound:
            own_audience = vector["options"]["audience"]
            for request in [self.request_of(other) for other in self.for_the_bus + self.bound]:
                with self.subTest(vector["name"], method=request["method"]):
                    self.refuse(vector, "claims: missing required claim", audience=own_audience, **request)

    def test_the_lifetime_of_the_one_that_binds_no_request_is_refused_by_the_bus_too(self):
        # The statement that binds no request also has another lifetime. The
        # file cannot show the bus refusing it for that, because its audience
        # and its missing claims are judged first. So its claims are signed
        # again here, by node and a key of this file, with the bus's audience
        # and a request added: the lifetime alone then refuses them, and with
        # a lifetime of a minute they verify.
        request = dict(method="GET", target="/_bus/example", body=b"")
        for vector in self.unbound:
            claims = dict(vector["claims"], iss=ISSUER, aud=bus_statement.BUS_AUDIENCE, htm=request["method"],
                          htu=request["target"], bdy=digest(request["body"]))
            lifetime = claims["exp"] - claims["iat"]
            arguments = dict(keys=KEYS, issuer=ISSUER, audience=bus_statement.BUS_AUDIENCE, now=vector["now"],
                             max_groups_age=300, **request)
            with self.subTest(vector["name"], lifetime=lifetime):
                self.assertNotEqual(lifetime, bus_statement.LIFETIME)
                recorder = Recorder()
                with self.assertRaises(StatementError) as caught:
                    verify_request(header_values=[signed(claims)], replay=recorder, **arguments)
                self.assertEqual(caught.exception.reason, "claims: exp is not iat + 60")
                self.assertEqual(recorder.claims, [])
                minute = dict(claims, exp=claims["iat"] + bus_statement.LIFETIME)
                accepted = verify_request(header_values=[signed(minute)], replay=running_replay(vector["now"]),
                                          **arguments)
                self.assertEqual((accepted.exp, accepted.jti), (minute["exp"], claims["jti"]))

    def test_the_one_that_binds_a_request_is_kept_from_the_bus_by_its_audience_alone(self):
        # It has every claim a statement for the bus has and binds a request of
        # its own. Asked as its own audience it verifies, so the audience is all
        # that stands between it and the bus. Both directions are pinned here.
        for vector in self.bound:
            own = self.request_of(vector)
            own_audience = vector["options"]["audience"]
            with self.subTest(vector["name"]):
                self.assertEqual(vector["options"]["request"]["method"], own["method"])
                self.assertTrue(own["body"])
                self.refuse(vector, "claims: aud is not the configured audience")
                self.check(self.verify(vector, audience=own_audience), vector)
                # Under its own audience it is bound to its request like any other.
                self.refuse(vector, "claims: bdy", audience=own_audience, body=own["body"] + b" ")
                self.refuse(vector, "claims: bdy", audience=own_audience, body=b"")
                self.refuse(vector, "claims: htu", audience=own_audience, target=own["target"] + "/")
                self.refuse(vector, "claims: htm", audience=own_audience, method="GET")
                for other in self.for_the_bus:
                    theirs = self.request_of(other)
                    self.refuse(vector, "claims: bdy", audience=own_audience, body=theirs["body"])
                    # And a statement for the bus is no more acceptable to that audience.
                    self.refuse(other, "claims: aud is not the configured audience", audience=own_audience, **theirs)

    def test_one_record_takes_the_jti_the_vectors_share_once(self):
        # The vectors were all made with one jti. A statement refused for its
        # audience leaves the record as it was; the first accepted one is put
        # on it; after that the jti is taken, whichever statement carries it.
        self.assertEqual(len({vector["claims"]["jti"] for vector in self.vectors}), 1)
        record = running_replay(self.vectors[0]["now"])
        first, second = self.for_the_bus
        for vector in self.for_another:
            with self.assertRaises(StatementError) as caught:
                self.verify(vector, replay=record, **self.request_of(first))
            self.assertEqual(caught.exception.reason, "claims: aud is not the configured audience")
        self.assertEqual(record.held(), 0)
        self.check(self.verify(first, replay=record, **self.request_of(first)), first)
        self.assertEqual(record.held(), 1)
        for vector, request in ((first, self.request_of(first)), (second, self.request_of(second))):
            with self.assertRaises(StatementError) as caught:
                self.verify(vector, replay=record, **request)
            self.assertEqual(caught.exception.reason, "replay: this statement was already accepted")
        self.assertEqual(record.held(), 1)


def statements_named(text):
    """The statements a passage names: every hyphenated lower-case word it has in backticks."""
    return sorted(set(re.findall(r"`([a-z]+(?:-[a-z]+)+)`", text)))


def assert_the_two_pairs_are_told_apart(case, section):
    """Of a passage's two bullets, one names the statements for the bus and one those for another audience."""
    vectors = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bus = sorted(vector["name"] for vector in vectors if vector["options"]["audience"] == bus_statement.BUS_AUDIENCE)
    another = sorted(vector["name"] for vector in vectors if vector["options"]["audience"] != bus_statement.BUS_AUDIENCE)
    bullets = [" ".join(bullet.split()) for bullet in re.split(r"^- ", section, flags=re.M)[1:]]
    case.assertEqual(len(bullets), 2)
    case.assertEqual([statements_named(bullet) for bullet in bullets], [bus, another])
    case.assertIn("made for the bus", bullets[0])
    case.assertIn("made for another audience", bullets[1])
    # In the second bullet, the sentence about each of the two names that one alone.
    unbound, = [vector["name"] for vector in vectors if vector["options"]["audience"] != bus_statement.BUS_AUDIENCE
                and "request" not in vector["options"]]
    bound, = [name for name in another if name != unbound]
    sentences = re.split(r"(?<=\.) ", bullets[1])
    case.assertEqual([statements_named(sentence) for sentence in sentences if "binds no request" in sentence], [[unbound]])
    case.assertEqual([statements_named(sentence) for sentence in sentences if "binds a request of its own" in sentence],
                     [[bound]])


class FixtureReadme(unittest.TestCase):
    """What scripts/fixtures/README.md says of the vectors."""

    def section(self):
        text = (ROOT / "scripts" / "fixtures" / "README.md").read_text(encoding="utf-8")
        return text.split("# Gateway statement vectors", 1)[1]

    def test_it_gives_the_files_hash_and_no_other_identifier(self):
        supplied = FIXTURE.read_bytes()
        digest = hashlib.sha256(supplied).hexdigest()
        self.assertEqual((digest, len(supplied)),
                         ("f2b73340b019a3c98d135fd5dc2164ecca8beba28efff789fcf009a9963ded44", 7959),
                         "the vectors are kept byte for byte as they were supplied")
        section = self.section()
        # The hash of the file, and no commit, build or deployment identifier beside it.
        self.assertEqual(re.findall(r"[0-9a-f]{7,}", section), [digest])
        flat = " ".join(section.split())
        self.assertIn("supplied by the gateway team that signs for the hosted hub", flat)
        self.assertIn("kept byte for byte", flat)
        self.assertIn("`allow_published=True`", flat)
        self.assertNotIn("http", flat)

    def test_it_names_the_statements_of_the_file_and_no_other(self):
        names = [vector["name"] for vector in json.loads(FIXTURE.read_text(encoding="utf-8"))]
        section = self.section()
        self.assertIn("holds four signed viewer statements", " ".join(section.split()))
        self.assertEqual(len(names), 4)
        self.assertEqual(statements_named(section), sorted(names))
        assert_the_two_pairs_are_told_apart(self, section)


# --- the contract note ------------------------------------------------------

class ContractNote(unittest.TestCase):
    """The worked example in docs/GATEWAY-STATEMENTS.md is a real statement."""

    def test_worked_example_verifies_and_is_long_expired(self):
        text = (ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8")
        setting, header, claims = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.S)[:3]]
        request = re.search(r"^GET (\S+) HTTP/1\.1\nX-Nonlocally-Viewer: (\S+)$", text, re.M)
        self.assertIsNotNone(request, "the example request and its header")
        target, token = request.groups()
        self.assertEqual(json.loads(unb64url(token.split(".")[0])), header)
        self.assertEqual(json.loads(unb64url(token.split(".")[1])), claims)
        self.assertEqual((claims["htm"], claims["htu"], claims["bdy"]), ("GET", target, EMPTY_BODY_DIGEST))
        self.assertEqual(claims["iss"], ISSUER, "the note uses the example issuer")
        self.assertEqual(list(setting), [header["kid"]])
        self.assertEqual(kid_of(setting[header["kid"]]), header["kid"], "the example follows the kid rule")
        self.assertRaises(ValueError, load_keys, json.dumps(setting))
        keys = load_keys(json.dumps(setting), allow_published=True)
        request = dict(keys=keys, method="GET", target=target, body=b"", issuer=ISSUER, audience="bus",
                       max_groups_age=300)
        statement = verify_statement(token, now=claims["iat"], replay=running_replay(claims["iat"]), **request)
        self.assertEqual(statement.sub, claims["sub"])
        self.assertEqual(statement.groups, tuple(claims["groups"]))
        self.assertEqual(statement.kid, header["kid"])
        self.assertIn(statement.reader_hash, text)
        with self.assertRaises(StatementError) as caught:
            verify_statement(token, now=NOW, replay=running_replay(claims["iat"]), **request)
        self.assertIn("claims: expired", caught.exception.reason)

    def test_the_notes_own_call_runs_as_printed(self):
        # The shell block is what a gateway developer will paste. It is run
        # here as it stands, against the note's own example.
        text = (ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```sh\npython3 - \"\$STATEMENT\" <<'EOF'\n(.*?)\nEOF\n```", text, re.S)
        self.assertEqual(len(blocks), 1, "one runnable call in the note")
        self.assertIn("bus_statement.verify_request(", blocks[0], "the note shows the entry point a broker uses")
        self.assertNotIn("verify_statement", blocks[0])
        setting = re.findall(r"```json\n(.*?)```", text, re.S)[0]
        token = re.search(r"^X-Nonlocally-Viewer: (\S+)$", text, re.M).group(1)
        with tempfile.TemporaryDirectory(prefix="com8-statement-note-") as tmp:
            Path(tmp, "gateway-signers.json").write_text(setting, encoding="utf-8")
            run = subprocess.run([sys.executable, "-B", "-c", blocks[0], token], cwd=tmp, capture_output=True,
                                 text=True, timeout=60, env=dict(os.environ, PYTHONPATH=str(ROOT / "lib")))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("reader='owui.00000000-0000-4000-8000-000000000001'", run.stdout)
        self.assertIn("groups=('00000000-0000-4000-8000-00000000000a', '00000000-0000-4000-8000-00000000000b')",
                      run.stdout)

    def test_note_and_module_name_the_same_things(self):
        text = (ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8")
        self.assertEqual(bus_statement.HEADER_NAME, "X-Nonlocally-Viewer")
        self.assertIn("    %s: <statement>" % bus_statement.HEADER_NAME, text)
        self.assertEqual(bus_statement.SIGNERS_SETTING, "BUS_GATEWAY_SIGNERS")
        self.assertEqual((bus_statement.ALGORITHM, bus_statement.TYPE, bus_statement.BUS_AUDIENCE),
                         ("EdDSA", "nl-viewer+jwt", "bus"))
        self.assertIn('    {"alg":"%s","typ":"%s","kid":"<kid>"}' % (bus_statement.ALGORITHM, bus_statement.TYPE), text)
        for setting in (bus_statement.SIGNERS_SETTING, "BUS_GATEWAY_ISSUER"):
            self.assertIn("`%s`" % setting, text)
        self.assertNotIn("BUS_GATEWAY_KEYS", text)
        # The limits the note states in words, wherever its lines happen to break.
        flat = " ".join(text.split())
        self.assertEqual(bus_statement._MAX_SECONDS - 1, 9007199254740991)
        self.assertEqual((bus_statement.LIFETIME, bus_statement.CLOCK_SKEW, bus_statement.MAX_TOKEN_BYTES,
                          bus_statement.MAX_GROUPS, bus_statement.MAX_NAME_BYTES, bus_statement.MAX_KEYS),
                         (60, 5, 8192, 64, 160, 8))
        for words in ("`iat + 60`, exactly", "more than 5 seconds ahead", "at most 8192 bytes", "at most 64, no repeats",
                      "at most 160 bytes of UTF-8", "- 1 to 8 keys.", "in its first 5 seconds",
                      "from 0 to 9007199254740991", "at most 256 characters", "at most 2048 bytes",
                      "up to 6012 bytes of JSON",
                      "A statement carries at most 64 groups", "never truncates the list",
                      "upper-case letters A to Z only", "prints as that one word",
                      "refused without being parsed",
                      # What ClockSteps shows of a clock set back, said for whoever runs the two:
                      "does not let a statement be used twice", "until the clock has caught up",
                      "The safe course is to wait", "restarted after its clock was set back",
                      # What TargetShape measures of the size:
                      "a statement over that is refused for its size", "is under 7000 bytes",
                      "costs two bytes", "up to about 1500 bytes",
                      "more than 16 `{` and `[`",
                      # No broker reads the header yet, so this is what one is to do:
                      "A broker is to answer that as HTTP 401",
                      # The example key: what can be shown of it.
                      "Being printed here, the key is one that `load_keys` refuses",
                      "with a `Content-Length`"):
            self.assertIn(words, flat)
        for words in ("which the broker answers", "was discarded", "reached only by a serializer"):
            self.assertNotIn(words, flat)
        self.assertEqual(bus_statement.MAX_PAYLOAD_BRACKETS, 16)
        self.assertEqual((bus_statement.MAX_HEADER_CHARS, bus_statement.MAX_TARGET_BYTES), (256, 2048))
        # The code points a name may not hold, in the note as in the tests of the rule.
        self.assertIn(", ".join(EXCLUDED_FROM_NAMES.split()) + ", and every U+xFFFE and U+xFFFF", flat)
        for words in ("for display only", "is dropped", "A gateway omits `name`"):
            self.assertIn(words, flat)

    def test_the_note_names_the_fixed_vectors_of_the_file_and_no_other(self):
        text = (ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8")
        section = text.split("\n## Fixed vectors\n", 1)[1].split("\n## ", 1)[0]
        names = [vector["name"] for vector in json.loads(FIXTURE.read_text(encoding="utf-8"))]
        self.assertIn("holds four statements", " ".join(section.split()))
        self.assertEqual(statements_named(section), sorted(names))
        assert_the_two_pairs_are_told_apart(self, section)

    def test_note_names_no_deployment(self):
        text = (ROOT / "docs" / "GATEWAY-STATEMENTS.md").read_text(encoding="utf-8")
        hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", text))
        self.assertTrue(hosts)
        self.assertEqual({host for host in hosts if not host.endswith(("example.org", "example.net", "example.com"))}, set())


# --- what the last line says ------------------------------------------------

class FinalLine(unittest.TestCase):
    """PASS is for the whole file, signed by node; a selection of it is PARTIAL."""

    def run_file(self, *selection, path=None):
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if path is not None:
            environment["PATH"] = path
        child = subprocess.run([sys.executable, str(Path(__file__).resolve())] + list(selection),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300,
                               env=environment)
        return child.returncode, child.stdout.strip().splitlines()[-1], child.stdout

    def test_the_three_endings(self):
        self.assertEqual(final_line(340, 340, []), (0, "PASS: gateway statements (340 tests, signed by node)"))
        self.assertEqual(final_line(3, 340, []),
                         (0, "PARTIAL: gateway statements, 3 of 340 tests selected (signed by node)"))
        self.assertEqual(final_line(339, 340, [])[1][:8], "PARTIAL:")
        self.assertEqual(final_line(0, 340, ["node is not installed"]),
                         (1, "UNQUALIFIED: gateway statements (node is not installed; every test skipped)"))

    def test_this_file_run_in_part_says_partial(self):
        status, line, output = self.run_file("Refusals")
        self.assertEqual(status, 0, output)
        counted = re.fullmatch(r"PARTIAL: gateway statements, (\d+) of (\d+) tests selected \(signed by node\)", line)
        self.assertIsNotNone(counted, line)
        self.assertLess(int(counted.group(1)), int(counted.group(2)))
        self.assertEqual(int(counted.group(2)), whole_file(), "of every test the file holds")
        self.assertNotIn("PASS", output)

    def test_this_file_run_with_a_pattern_says_partial(self):
        # -k narrows the very loader unittest.main uses, so the whole file is
        # counted with a loader of its own.
        status, line, output = self.run_file("-k", "Refusals")
        self.assertEqual(status, 0, output)
        counted = re.fullmatch(r"PARTIAL: gateway statements, (\d+) of (\d+) tests selected \(signed by node\)", line)
        self.assertIsNotNone(counted, line)
        self.assertLess(int(counted.group(1)), int(counted.group(2)))
        self.assertEqual(int(counted.group(2)), whole_file(), "of every test the file holds")
        self.assertNotIn("PASS", output)

    def test_this_file_run_where_node_is_missing_says_unqualified(self):
        with tempfile.TemporaryDirectory(prefix="com8-statement-no-tools-") as empty:
            status, line, output = self.run_file(path=empty)
        self.assertEqual(status, 1, output)
        self.assertEqual(line, "UNQUALIFIED: gateway statements (node is not installed; every test skipped)")
        self.assertNotIn("PASS", output)


def final_line(ran, total, unqualified):
    """The exit status and last line of a run in which no test failed."""
    if unqualified:
        # Nothing here can run without the independent signer.
        return 1, "UNQUALIFIED: gateway statements (%s; every test skipped)" % unqualified[0]
    if ran < total:
        return 0, "PARTIAL: gateway statements, %d of %d tests selected (signed by node)" % (ran, total)
    return 0, "PASS: gateway statements (%d tests, signed by node)" % ran


def whole_file():
    """How many tests this file holds, whatever was selected.

    Counted with a loader of its own: a selection made with -k narrows the
    default loader, the one unittest.main loads with.
    """
    return unittest.TestLoader().loadTestsFromModule(sys.modules["__main__"]).countTestCases()


def main():
    program = unittest.main(verbosity=2, exit=False)
    if not program.result.wasSuccessful():
        sys.exit(1)
    status, line = final_line(program.result.testsRun, whole_file(), UNQUALIFIED)
    if status:
        sys.exit(line)
    print(line)


if __name__ == "__main__":
    main()
