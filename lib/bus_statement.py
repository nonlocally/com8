#!/usr/bin/env python3
"""The gateway's signed statement about the signed-in viewer (stdlib only).

The HTTPS gateway in front of the broker sends one statement with each
request, in the `X-Nonlocally-Viewer` header: a compact JWS, signed with
Ed25519, that says who the viewer is, which groups they belong to and which
single request the statement is for. The broker verifies it against a pinned
public key. No secret is shared with the gateway and nothing depends on the
network path between the two.

A broker calls `verify_request` with every value of that header as its HTTP
layer saw them. Unless there is exactly one, the request is refused. The one
statement goes to `verify_statement`, which checks size, structure, protected
header, signature and claims, in that order, and stops at the first failure.
The payload is not decoded, parsed or consulted in any way until the signature
has verified.

A broker that uses this pins the gateway's public keys and names the one
issuer whose statements it accepts. This module reads no setting. It expects
the keys as the text of a setting it calls BUS_GATEWAY_SIGNERS, which
`load_keys` turns into the `keys` argument: each key stands under the one kid
that `kid_for` derives from it, which is also the kid the gateway sends, and
any other label is refused. It expects the issuer as the `issuer` argument,
which the contract note calls BUS_GATEWAY_ISSUER. Neither has a default.

A statement is accepted once. The caller supplies the record of accepted
statements, a Replay, and `verify_statement` puts each statement on it as its
last step. MemoryReplay is that record for a single process.

Nothing a caller must decide has a default: whose statements are accepted, for
which audience, how old a group list may be and where accepted statements are
recorded are all required arguments.

docs/GATEWAY-STATEMENTS.md is the same contract written for the sending side.
"""
import base64
import collections
import dataclasses
import hashlib
import json
import re
import threading
import time
import typing

import ed25519_verify

__all__ = ["MemoryReplay", "Replay", "Statement", "StatementError", "kid_for", "load_keys", "verify_request",
           "verify_statement"]

HEADER_NAME = "X-Nonlocally-Viewer"
SIGNERS_SETTING = "BUS_GATEWAY_SIGNERS"
BUS_AUDIENCE = "bus"        # what a bus broker passes as `audience`; nothing defaults to it
ALGORITHM = "EdDSA"
TYPE = "nl-viewer+jwt"
MAX_TOKEN_BYTES = 8192
MAX_HEADER_CHARS = 256      # of the first segment; a genuine one has 87
MAX_TARGET_BYTES = 2048     # of htu
MAX_PAYLOAD_BRACKETS = 16   # "{" and "[" outside strings in the payload; a genuine one has one of each
MAX_KEYS = 8
MAX_GROUPS = 64
MAX_NAME_BYTES = 160
LIFETIME = 60               # exp - iat, exactly
CLOCK_SKEW = 5              # seconds the gateway's clock may lead the broker's
REPLAY_RETENTION = LIFETIME + 2 * CLOCK_SKEW    # seconds a MemoryReplay keeps a claim
REPLAY_CAPACITY = 65536     # claims a MemoryReplay holds unless told otherwise

# Domain separation for Statement.reader_hash.
READER_HASH_DOMAIN = b"com8/statement-reader/v1"

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_GROUP = re.compile(_UUID)
_SUB = re.compile("owui:(" + _UUID + ")")
_JTI = re.compile(r"[A-Za-z0-9_-]{22}")
_METHOD = re.compile(r"[A-Z]+")
_TARGET = re.compile(r"/[!\"$-~]*")      # "/", then ASCII 0x21 to 0x7E without "#"
_KID = re.compile(r"gw-[A-Za-z0-9_-]{16}")
# One string of a JSON text, escapes and all, as a parser reads it: one that
# is never closed runs to the end. Every match therefore succeeds at its first
# attempt and a scan passes over each byte once.
_JSON_STRING = re.compile(rb'"(?:[^"\\]|\\.)*\\?(?:"|\Z)', re.DOTALL)
_HEADER_PARAMETERS = frozenset(("alg", "typ", "kid"))
_REQUIRED = frozenset(("iss", "aud", "sub", "groups", "groups_at", "iat", "exp", "jti", "htm", "htu", "bdy"))
_OPTIONAL = frozenset(("name",))
_MAX_SECONDS = 2 ** 53      # whole seconds below this are exact in a float
# Code points a display name may not hold. A fixed list: the control
# characters, a set of format characters and separators that show as nothing
# or change the direction of text, and the noncharacters, to which every
# U+xFFFE and U+xFFFF is added. It is not every character that shows as
# nothing. Being fixed here, the verdict on a name does not depend on the
# Unicode tables of the interpreter that runs this.
_NOT_FOR_DISPLAY = (
    (0x0000, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x061C, 0x061C), (0x180E, 0x180E),
    (0x200B, 0x200F), (0x2028, 0x202E), (0x2060, 0x206F), (0xFDD0, 0xFDEF), (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB), (0x1D173, 0x1D17A), (0xE0000, 0xE007F))
# Published keys, which no broker may pin. Anyone can sign with all but the
# last: a specification prints the seed, or the seed is all zeros, or the
# secret scalar is 1. The last is the example key of the contract note.
# load_keys refuses them unless a test asks for them. The list guards against
# pinning this repository's own fixtures and examples. It is not a list of
# every published key.
_PUBLISHED_KEYS = frozenset(bytes.fromhex(key) for key in (
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",     # RFC 8032 section 7.1, TEST 1
    "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",     # RFC 8032 section 7.1, TEST 2
    "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",     # RFC 8032 section 7.1, TEST 3
    "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",     # RFC 8032 section 7.1, TEST 1024
    "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",     # RFC 8032 section 7.1, TEST SHA(abc)
    "dfc9425e4f968f7f0c29f0259cf5f9aed6851c2bb4ad8bfb860cfee0ab248292",     # RFC 8032 section 7.2, foo, bar, foo2
    "0f1d1274943b91415889152e893d80e93275a1fc0b65fd71b4b0dda10ad7d772",     # RFC 8032 section 7.2, foo3
    "19bf44096984cdfe8541bac167dc3b96c85086aa30b6b6cb0c5c38ad703166e1",     # RFC 8410 section 10.3
    "3b6a27bcceb6a42d62a3a8d02a6f0d73653215771de243a63ac048a18b59da29",     # the seed of all zeros
    "5866666666666666666666666666666666666666666666666666666666666666",     # the base point: its secret is 1
    "6b9476ca3b44f3063414bf049056df85965d60384e7e431ae319b0d40a6bc57e",     # docs/GATEWAY-STATEMENTS.md
))
_INVALID = object()


class StatementError(Exception):
    """A statement was refused.

    `code` and `str()` are always the one word "unauthorized": every refusal
    deserves the same answer, and code that prints the error shows nothing
    else. `reason` names the check that failed, in fixed text that never quotes
    the statement. It is for the broker's own journal and not for a client.
    """
    code = "unauthorized"

    def __init__(self, reason):
        super().__init__(self.code)
        self.reason = reason

    def __str__(self):
        return self.code


@dataclasses.dataclass(frozen=True)
class Statement:
    """What a verified statement says. Nothing here is usable before it verifies."""
    sub: str                        # "owui:<uuid>", as signed
    uuid: str                       # the viewer's OpenWebUI account
    reader: str                     # the bus spelling, "owui.<uuid>"
    reader_hash: str                # SHA-256 hex of READER_HASH_DOMAIN NUL issuer NUL sub
    groups: tuple                   # sorted group UUIDs; empty when groups_stale
    groups_at: int                  # when the gateway read the group list
    groups_stale: bool              # the list was older than max_groups_age and was dropped
    name: typing.Optional[str]      # display name; None when not sent or when dropped
    name_dropped: bool              # a name was sent that is too long or holds an excluded character
    jti: str                        # put on record in the caller's Replay, so it is accepted once
    exp: int
    kid: str                        # which pinned key signed it


def _refused(reason):
    return StatementError(reason)


class Replay(typing.Protocol):
    """The record of accepted statements, which the caller supplies.

    verify_statement calls `claim(jti, exp)` once, as its last step, for a
    statement that has passed every other check. In one atomic step `claim`
    puts `jti` on record and returns True if it was not there, False if it
    was. No jti may be answered True a second time while its statement can
    still verify.

    A statement verifies until `exp` by the clock that verify_statement is
    given. While that clock only runs forward, a record may drop a jti once
    `exp + CLOCK_SKEW` has passed. But that clock can be set back, and a
    statement whose jti was dropped then verifies again. So a record that
    drops a jti must still refuse it: it keeps the latest `exp` it has
    dropped, with its claims if those outlive the process, and never accepts
    an `exp` that is not later. Timing the drop by a clock that cannot be set
    back does not replace this: it is the verifier's clock that brings the
    statement back, not the record's.

    `claim` may raise StatementError to refuse for a reason of its own, such
    as a full record. Any answer other than True is a refusal, and so is any
    other Exception that reading or calling `claim` raises. What it raises
    that is not an Exception, such as KeyboardInterrupt, passes through.
    verify_statement waits for the answer and sets no limit on the time, so a
    record that can block must bound its own waiting.

    One record must serve every process that verifies statements of the same
    issuer for the same audience. A process that restarts must either persist
    its claims or refuse statements issued before it started. MemoryReplay
    serves one process and does the second. A broker of several processes, or
    one that must accept across a restart, supplies a record kept in its
    database.
    """

    def claim(self, jti: str, exp: int) -> bool:
        ...


class MemoryReplay:
    """A Replay kept in this process's memory: for one process, and for tests.

    - `claim` runs under one lock, so threads may share a record.
    - A claim is kept for REPLAY_RETENTION seconds on a monotonic clock:
      the longest a statement can still verify after it was accepted,
      LIFETIME + CLOCK_SKEW, and one more CLOCK_SKEW so that a wall clock set
      back by no more than that changes nothing. What is forgotten is still
      refused: no statement is accepted whose `exp` is not later than that of
      every forgotten one.
    - It holds at most `capacity` claims. When full it refuses new statements
      with StatementError and never drops a live claim to make room.
    - A process that restarts must either persist its claims or refuse
      statements issued before it started. This record does the second. It
      knows nothing of what an earlier process accepted, so it refuses what
      that process could have accepted: a statement issued, by `exp -
      LIFETIME`, no later than CLOCK_SKEW seconds after this record began.
      For the first seconds after a restart every statement is refused, and
      a record made anew for each request refuses them all.

    When the wall clock is moved:

    - Set forward, it changes nothing here: claims are kept by the monotonic
      clock.
    - Set back, it never makes this record accept a statement twice. But the
      record then refuses fresh statements too, those whose `exp` is not
      later than the latest it has forgotten. Set back by CLOCK_SKEW seconds
      or less, nothing more is refused. Set back by about a minute or more
      (70 seconds, or 65 when the statements before were issued 5 seconds
      ahead), every fresh statement is refused until the clock has caught up
      with where it was, so for about as long as the step. In between, a
      fresh statement is refused only if, as it is verified, no more of its
      life is left than the step less 10 seconds, plus as many seconds as the
      statements before were issued ahead (at most 5); under steady traffic
      every such statement is.
    - A restart after the clock was set back is different. The new record
      compares with the clock as it reads then, so a statement that the old
      process accepted before the step can be accepted again. After a step
      back the safe course is to wait, not to restart. A record made while
      the clock read ahead refuses everything until the clock has passed that
      reading by CLOCK_SKEW seconds.

    `clock` is read once, when the record is made, and `monotonic` at every
    claim. They default to time.time and time.monotonic. A test that moves a
    wall clock by hand passes both.
    """

    def __init__(self, *, capacity=REPLAY_CAPACITY, clock=None, monotonic=None):
        clock = time.time if clock is None else clock
        monotonic = time.monotonic if monotonic is None else monotonic
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be an integer of at least 1")
        if not callable(clock) or not callable(monotonic):
            raise ValueError("clock and monotonic must be callable")
        began = clock()
        if not _seconds(began):
            raise ValueError("clock must give the seconds since 1970 as a number")
        self._capacity = capacity
        self._monotonic = monotonic
        self._began = began
        self._forgotten = -1                        # the latest exp among forgotten claims
        self._claims = collections.OrderedDict()    # jti -> (monotonic deadline, exp), oldest first
        self._lock = threading.Lock()

    def _forget(self, now):
        """Drop the claims whose retention is over. The caller holds the lock."""
        while self._claims:
            jti, (deadline, exp) = next(iter(self._claims.items()))
            if deadline > now:
                break
            del self._claims[jti]
            self._forgotten = max(self._forgotten, exp)

    def claim(self, jti: str, exp: int) -> bool:
        """True the first time a jti is claimed and False every other time."""
        if type(jti) is not str or type(exp) is not int:
            raise _refused("replay: a claim is a jti string and an integer exp")
        with self._lock:
            now = self._monotonic()
            self._forget(now)
            if jti in self._claims:
                return False
            if exp - LIFETIME <= self._began + CLOCK_SKEW:
                raise _refused("replay: issued before this record began")
            if exp <= self._forgotten:
                raise _refused("replay: not newer than a statement this record has forgotten")
            if len(self._claims) >= self._capacity:
                raise _refused("replay: the record of accepted statements is full")
            self._claims[jti] = (now + REPLAY_RETENTION, exp)
            return True

    def held(self) -> int:
        """How many claims are on record now, out of `capacity`."""
        with self._lock:
            self._forget(self._monotonic())
            return len(self._claims)


def _can_claim(replay):
    try:
        return callable(replay.claim)
    except Exception:
        return False


def _unique_names(pairs):
    """The object these name and value pairs make, unless a name is there twice."""
    made = {}
    for name, value in pairs:
        if name in made:
            raise ValueError("a name appears twice in one JSON object")
        made[name] = value
    return made


def _no_constants(name):
    raise ValueError("NaN and Infinity are not JSON")


def _strict_json(text):
    """Parse JSON, refusing a name twice in one object at any depth, NaN and Infinity.

    Returns _INVALID instead of raising, so that a caller's own error is never
    chained to a parser error that carries the text it was given.
    """
    try:
        return json.loads(text, object_pairs_hook=_unique_names, parse_constant=_no_constants)
    except (ValueError, RecursionError):
        return _INVALID


def _json_object(raw, part):
    try:
        value = _strict_json(raw.decode("utf-8"))
    except UnicodeDecodeError:
        value = _INVALID
    if value is _INVALID:
        raise _refused(part + ": not valid JSON")
    if type(value) is not dict:
        raise _refused(part + ": not a JSON object")
    return value


def _canonical_b64url(segment):
    """A lexical test; nothing is decoded.

    True for a non-empty run of the URL-safe alphabet, without padding, of a
    length some byte string encodes to, whose bits that carry no data are zero.
    Each byte string then has exactly one accepted spelling.
    """
    spare = (0, None, 15, 3)[len(segment) % 4]
    return (spare is not None and _SEGMENT.fullmatch(segment) is not None
            and (_ALPHABET.index(segment[-1]) & spare) == 0)


def _b64url_decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _utf8(text):
    """The UTF-8 bytes of a str; None for anything else or a lone surrogate."""
    try:
        return text.encode("utf-8") if type(text) is str else None
    except UnicodeEncodeError:
        return None


def _seconds(value):
    """An int or float in [0, 2**53). NaN, the infinities and bools are not.

    Within this range a clock, an age and the times in a statement can be
    added, subtracted and compared without overflow or loss.
    """
    return (type(value) is int or type(value) is float) and 0 <= value < _MAX_SECONDS


def _displayable(text):
    """True unless `text` holds a code point of _NOT_FOR_DISPLAY, a U+xFFFE or a U+xFFFF."""
    for character in text:
        point = ord(character)
        if point & 0xFFFE == 0xFFFE or any(low <= point <= high for low, high in _NOT_FOR_DISPLAY):
            return False
    return True


def _epoch(value):
    """Whole seconds from 0 to 2**53 - 1: what groups_at, iat and exp must be."""
    return type(value) is int and 0 <= value < _MAX_SECONDS


def _usable_keys(keys):
    """A dict of the shape load_keys returns: 1 to MAX_KEYS kids, each to 32 bytes."""
    return (type(keys) is dict and 1 <= len(keys) <= MAX_KEYS
            and all(type(kid) is str and type(key) is bytes and len(key) == 32 for kid, key in keys.items()))


def kid_for(public_key: bytes) -> str:
    """The one kid a key is pinned and sent under.

    "gw-" and the first 16 characters of the unpadded base64url of SHA-256
    over the 32 raw key bytes: the gateway's own rule. A kid therefore cannot
    be moved to another key. Raises ValueError unless given 32 bytes.
    """
    if type(public_key) is not bytes or len(public_key) != 32:
        raise ValueError("an Ed25519 public key is 32 raw bytes")
    return "gw-" + base64.urlsafe_b64encode(hashlib.sha256(public_key).digest())[:16].decode("ascii")


def load_keys(config_json: str, *, allow_published=False) -> dict:
    """Read the BUS_GATEWAY_SIGNERS setting into {kid: raw 32-byte public key}.

    The setting is one JSON object, `{"<kid>": "<base64url raw Ed25519 public
    key>"}`, holding 1 to 8 keys. Every key is validated by
    ed25519_verify.load_public_key and must be labelled with `kid_for(key)`: a
    key under any other kid is a configuration error, not a second name for
    it. Raises ValueError otherwise. The message names at most a well-formed
    kid from the setting, never any part of a key, the kid a key should have
    had, or anything else in the setting.

    Eleven published keys are refused. Anyone can sign with ten of them: the
    five test keys of RFC 8032 section 7.1 and the two of section 7.2, the key
    of RFC 8410 section 10.3, the key of the all-zero seed and the base point.
    The eleventh is the example key of docs/GATEWAY-STATEMENTS.md. The list
    guards against pinning this repository's own fixtures and examples; it is
    not a list of every published key. Only a test of those fixtures sets
    `allow_published`, and only the value True allows them.

    This is the only place a key is examined in full: that it is a point of
    the subgroup of prime order is tested here and not at each verification.
    A dictionary of keys built by hand has not been through that test, so the
    keys given to verify_request must be the ones this function returned.
    """
    config = _strict_json(config_json) if type(config_json) is str else _INVALID
    if type(config) is not dict:
        raise ValueError(SIGNERS_SETTING + " must be one JSON object of kid to public key, each kid once")
    if not 1 <= len(config) <= MAX_KEYS:
        raise ValueError(SIGNERS_SETTING + " must hold 1 to %d keys" % MAX_KEYS)
    keys, problem = {}, None
    for kid, text in config.items():
        if not _KID.fullmatch(kid):
            # Not echoed: whatever stands in a kid's place may be a key.
            problem = 'a kid is "gw-" and 16 base64url characters, as kid_for derives it from the key'
            break
        try:
            key = ed25519_verify.load_public_key(text)
        except ValueError as error:
            problem = "kid %s: %s" % (kid, error)       # fixed text from the verifier
            break
        if kid != kid_for(key):
            problem = "kid %s is not the kid of the key it labels" % kid
            break
        if allow_published is not True and key in _PUBLISHED_KEYS:
            problem = "kid %s: this is a published key, which no broker may pin" % kid
            break
        keys[kid] = key
    if problem is not None:
        raise ValueError(SIGNERS_SETTING + ": " + problem)
    return keys


def verify_statement(token: str, *, keys, method, target, body, now, issuer, audience,
                     max_groups_age, replay) -> Statement:
    """Verify one `X-Nonlocally-Viewer` value against the request it came with.

    A broker calls verify_request, which first refuses a request that carries
    the header more or less than once. This is the function it then calls.

    token    the header value, exactly as received
    keys     pinned keys as load_keys returned them, that is from
             BUS_GATEWAY_SIGNERS: a dict of 1 to 8 kids, each to the 32 raw
             bytes of its key. Only their shape is checked here. A dict built
             by hand skips what load_keys tests of each key and of its kid
    method   the request method exactly as received, for example "GET"; it is
             compared with `htm` as it stands and its case is never folded
    target   the request target as received: the path and, if present,
             "?query". Only a target that begins with "/" and holds at most
             2048 bytes of printable ASCII without "#" can match a statement
    body     the exact request body as bytes, a bytearray or a memoryview;
             None or b"" when there is none
    now      the broker's clock, in seconds
    issuer   whose statements are accepted: the exact `iss` of the gateway in
             front of this broker, from BUS_GATEWAY_ISSUER
    audience whom the statement must be for: the exact `aud` of this service,
             BUS_AUDIENCE for a bus broker. A gateway signs for several
             services with one key, and this is what keeps them apart
    max_groups_age
             how old the group list may be, in whole seconds, at least 1. An
             older list is not an error: the statement still verifies, but
             `groups` comes back empty and `groups_stale` is set
    replay   the record of accepted statements, a Replay: one MemoryReplay
             kept for the life of a single process, or the broker's own. The
             statement is put on record as the last step, so each is accepted
             once

    None of these has a default. What a caller must decide, it must pass.

    Returns a Statement, or raises StatementError with code "unauthorized" for
    every refusal, an unusable argument included. Each argument must be of
    exactly the type named here, not a subclass: nothing is compared, measured
    or converted before its type is known, so a value of the wrong kind is
    refused and raises nothing else. Two things are not refusals. An argument
    left out is a TypeError, raised by the call itself. And the record is the
    caller's own code: what it raises that is not an Exception, such as
    KeyboardInterrupt, passes through, whether reading `replay.claim` raises
    it or calling it does.
    """
    if not _can_claim(replay):
        raise _refused("verifier: replay has no claim method")
    issuer_bytes = _utf8(issuer)
    if (not _usable_keys(keys) or type(method) is not str or type(target) is not str or not target
            or not issuer_bytes or not _utf8(audience) or not _seconds(now)
            or type(max_groups_age) is not int or not 1 <= max_groups_age < _MAX_SECONDS):
        raise _refused("verifier: unusable keys, method, target, clock, issuer, audience or group age")
    if body is None:
        body = b""
    elif type(body) is bytearray or type(body) is memoryview:
        try:
            body = bytes(body)
        except Exception:               # a released memoryview, for one
            raise _refused("verifier: the request body could not be read") from None
    elif type(body) is not bytes:
        raise _refused("verifier: the request body must be bytes")

    # 1. Size, before any other work is done on the value.
    if type(token) is not str:
        raise _refused("structure: statement is not a string")
    if len(token) > MAX_TOKEN_BYTES:
        raise _refused("size: statement is larger than %d bytes" % MAX_TOKEN_BYTES)

    # 2. Structure: three canonical base64url segments and nothing else. This
    #    also makes the value pure ASCII, so its length above was in bytes.
    segments = token.split(".")
    if len(segments) != 3:
        raise _refused("structure: statement is not three segments")
    # The header is parsed before any signature is checked, so its length is
    # bounded first: 192 bytes of JSON cannot be nested deep enough to hurt.
    if len(segments[0]) > MAX_HEADER_CHARS:
        raise _refused("size: header segment is longer than %d characters" % MAX_HEADER_CHARS)
    if not all(_canonical_b64url(segment) for segment in segments):
        raise _refused("structure: a segment is not canonical unpadded base64url")

    # 3. Protected header: exactly these three parameters with these values.
    header = _json_object(_b64url_decode(segments[0]), "header")
    if set(header) != _HEADER_PARAMETERS:
        raise _refused("header: parameters are not exactly alg, typ and kid")
    if header["alg"] != ALGORITHM:
        raise _refused("header: alg is not " + ALGORITHM)
    if header["typ"] != TYPE:
        raise _refused("header: typ is not " + TYPE)
    kid = header["kid"]
    if type(kid) is not str or kid not in keys:
        raise _refused("header: kid is not a pinned key")

    # 4. Signature over the first two segments as received (RFC 7515, 5.2).
    signing_input = (segments[0] + "." + segments[1]).encode("ascii")
    if not ed25519_verify.verify(keys[kid], signing_input, _b64url_decode(segments[2])):
        raise _refused("signature: does not verify")

    # 5. Claims. Only now is the payload decoded. Whom the statement is from
    #    and for comes first: the gateway signs for other audiences with the
    #    same key, and one of those is best reported as exactly that.
    payload = _b64url_decode(segments[1])
    # A statement is one object that holds one list. A payload with far more
    # structure is refused before it is parsed, whoever signed it: nested
    # thousands deep it has overrun the stack of a JSON parser in a thread
    # with a small one. Brackets inside strings are not structure.
    bare = _JSON_STRING.sub(b"", payload)
    if bare.count(b"{") + bare.count(b"[") > MAX_PAYLOAD_BRACKETS:
        raise _refused("claims: more structure than a statement holds")
    claims = _json_object(payload, "claims")
    if type(claims.get("iss")) is not str or claims["iss"] != issuer:
        raise _refused("claims: iss is not the configured issuer")
    if type(claims.get("aud")) is not str or claims["aud"] != audience:
        raise _refused("claims: aud is not the configured audience")
    if set(claims) - _REQUIRED - _OPTIONAL:
        raise _refused("claims: unknown claim name")
    if _REQUIRED - set(claims):
        raise _refused("claims: missing required claim")
    sub = claims["sub"]
    subject = _SUB.fullmatch(sub) if type(sub) is str else None
    if subject is None:
        raise _refused("claims: sub is not owui: and a lowercase canonical UUID")
    groups = claims["groups"]
    if (type(groups) is not list or len(groups) > MAX_GROUPS
            or any(type(group) is not str or not _GROUP.fullmatch(group) for group in groups)
            or any(first >= second for first, second in zip(groups, groups[1:]))):
        raise _refused("claims: groups is not a sorted list of at most %d distinct lowercase canonical UUIDs" % MAX_GROUPS)
    groups_at = claims["groups_at"]
    if not _epoch(groups_at):
        raise _refused("claims: groups_at is not whole seconds from 0 to 2**53 - 1")
    if groups_at > now + CLOCK_SKEW:
        raise _refused("claims: groups_at is in the future")
    name, name_dropped = claims.get("name"), False
    if "name" in claims:
        if type(name) is not str:
            raise _refused("claims: name is not a string")
        encoded = _utf8(name)
        if encoded is None:             # a lone surrogate; no other claim can hold one and pass
            raise _refused("claims: name is not valid text")
        if len(encoded) > MAX_NAME_BYTES or not _displayable(name):
            # The name is for display only. One that is not fit to display is
            # dropped; whom the statement speaks for does not depend on it.
            name, name_dropped = None, True
    iat, exp = claims["iat"], claims["exp"]
    if not _epoch(iat) or not _epoch(exp):
        raise _refused("claims: iat and exp are not both whole seconds from 0 to 2**53 - 1")
    if exp - iat != LIFETIME:
        raise _refused("claims: exp is not iat + %d" % LIFETIME)
    if iat > now + CLOCK_SKEW:
        raise _refused("claims: iat is in the future")
    if exp <= now:
        raise _refused("claims: expired")
    jti = claims["jti"]
    if type(jti) is not str or not _JTI.fullmatch(jti):
        raise _refused("claims: jti is not 22 base64url characters")
    htm = claims["htm"]
    if type(htm) is not str or not _METHOD.fullmatch(htm) or htm != method:
        raise _refused("claims: htm is not the request method in upper case")
    htu = claims["htu"]
    if type(htu) is not str or len(htu) > MAX_TARGET_BYTES or not _TARGET.fullmatch(htu):
        raise _refused("claims: htu is not / and at most %d further characters of printable ASCII other than #"
                       % (MAX_TARGET_BYTES - 1))
    if htu != target:
        raise _refused("claims: htu is not the request target as received")
    body_digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode("ascii")
    if type(claims["bdy"]) is not str or claims["bdy"] != body_digest:
        raise _refused("claims: bdy is not the SHA-256 of the request body")

    uuid = subject.group(1)
    stale = groups_at < now - max_groups_age        # older than the limit
    statement = Statement(
        sub=sub, uuid=uuid, reader="owui." + uuid,
        reader_hash=hashlib.sha256(b"\x00".join((READER_HASH_DOMAIN, issuer_bytes, sub.encode("ascii")))).hexdigest(),
        groups=() if stale else tuple(groups), groups_at=groups_at, groups_stale=stale,
        name=name, name_dropped=name_dropped, jti=jti, exp=exp, kid=kid)

    # 6. Once only. Last, so that nothing refused above is ever put on record.
    try:
        answer = replay.claim(jti, exp)
    except StatementError:
        raise
    except Exception as error:
        raise _refused("replay: the record of accepted statements failed") from error
    if answer is False:
        raise _refused("replay: this statement was already accepted")
    if answer is not True:
        raise _refused("replay: the record of accepted statements answered neither True nor False")
    return statement


def verify_request(*, header_values, keys, issuer, audience, method, target, body, now, max_groups_age,
                   replay) -> Statement:
    """Verify the statement of one request. This is what a broker calls.

    header_values
             every value of the `X-Nonlocally-Viewer` header, as the HTTP
             layer saw them: a list or tuple, for example
             `self.headers.get_all(HEADER_NAME)`. A request that carries the
             header more than once, or not at all, is refused; so is anything
             but a list or tuple holding one string. Values are never joined,
             split or trimmed

    The other arguments are those of verify_statement, and like them every one
    is required and passed by name. `keys` must be what load_keys returned:
    only the shape of the dictionary is checked here, and one built by hand
    skips every check that load_keys makes of a key and of its kid.

    Returns a Statement or raises StatementError. As with verify_statement, an
    argument left out is a TypeError, and what the caller's record raises that
    is not an Exception passes through.
    """
    if ((type(header_values) is not list and type(header_values) is not tuple) or len(header_values) != 1
            or type(header_values[0]) is not str):
        raise _refused("request: the statement header is not present exactly once")
    return verify_statement(header_values[0], keys=keys, method=method, target=target, body=body, now=now,
                            issuer=issuer, audience=audience, max_groups_age=max_groups_age, replay=replay)
