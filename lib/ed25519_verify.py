#!/usr/bin/env python3
"""Ed25519 signature verification (RFC 8032), standard library only.

Verification only. This module cannot sign, cannot make keys and never sees a
secret; every value it handles is public, so nothing here needs to run in
constant time.

It is stricter than RFC 8032 requires, and never repairs an input:

- the key must be 32 bytes and the signature 64 bytes;
- S must be canonical, 0 <= S < L (section 5.1.7), which removes the S + L
  malleability;
- R and the public key A must be canonical encodings of curve points
  (section 5.1.3): y < p, an x that exists, and never x = 0 with the sign bit;
- A must not be one of the eight points of small order. Keys are pinned by an
  operator, and such a key verifies forgeries for a large share of messages;
- R must not be of small order either. For an honest key this only excludes
  R = neutral element, a nonce of zero that no signer emits;
- the equation is the one without the cofactor, [S]B = R + [k]A, with k
  reduced modulo L.

A key is pinned through load_public_key, which refuses one more kind: a key
outside the subgroup of prime order, that is an honest key with a point of
small order added to it. No key generator makes one, and for such a key the
verdict on a signature would depend on details that implementations do not
share. verify does not repeat that test, which costs as much as half a
verification. It expects a key that load_public_key has passed.
"""
import base64
import hashlib
import re

__all__ = ["load_public_key", "verify"]

# Section 5.1: the curve -x^2 + y^2 = 1 + d x^2 y^2 over GF(p).
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493    # order of the base point
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)                       # a square root of -1

_KEY_TEXT = re.compile(r"[A-Za-z0-9_-]{43}")


def _add(p1, p2):
    """Section 5.1.4 addition in extended coordinates (X, Y, Z, T).

    x = X/Z, y = Y/Z and xy = T/Z. The formula is complete on this curve: it
    also doubles, and it is correct for the neutral element and for points of
    small order, so no input needs a special case.
    """
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = t1 * 2 * D * t2 % P
    d = z1 * 2 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(scalar, point):
    """[scalar]point by double-and-add; variable time, all inputs are public."""
    result = _NEUTRAL
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(p1, p2):
    """Same affine point: x1/z1 = x2/z2 and y1/z1 = y2/z2."""
    x1, y1, z1, _ = p1
    x2, y2, z2, _ = p2
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _has_small_order(point):
    """True for the eight points whose order divides the cofactor 8."""
    for _ in range(3):
        point = _add(point, point)
    return _equal(point, _NEUTRAL)


def _decode_point(encoded):
    """Section 5.1.3. None unless `encoded` canonically encodes a curve point."""
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= P:
        return None                     # non-canonical y; it is never reduced
    # x^2 = (y^2 - 1) / (d y^2 + 1). The denominator cannot be zero: that
    # would need y^2 = -1/d, and -1/d is not a square.
    xx = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(xx, (P + 3) // 8, P)        # a root, if one exists, since p = 5 mod 8
    if (x * x - xx) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - xx) % P != 0:
        return None                     # not a square: no point has this y
    if x == 0 and sign:
        return None                     # x = 0 has no negative; x_0 = 1 is invalid
    if x & 1 != sign:
        x = P - x
    return (x, y, 1, x * y % P)


_NEUTRAL = (0, 1, 1, 0)
_BASE = _decode_point((4 * pow(5, P - 2, P) % P).to_bytes(32, "little"))    # y = 4/5, x even


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """True only if `signature` is a strict Ed25519 signature of `message`.

    Section 5.1.7. Never raises: a wrong type, a wrong length or any malformed
    value returns False. The three arguments must be exactly `bytes`; a
    subclass is refused like any other type, since it could answer for itself.

    The key is refused here if it is malformed or of small order. Whether it
    lies in the subgroup of prime order is tested once, by load_public_key,
    and not again here.
    """
    if type(public_key) is not bytes or type(message) is not bytes or type(signature) is not bytes:
        return False
    if len(public_key) != 32 or len(signature) != 64:
        return False
    # Step 1: decode A, R and S; any failure makes the signature invalid.
    a = _decode_point(public_key)
    r = _decode_point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= L:
        return False
    if _has_small_order(a) or _has_small_order(r):
        return False
    # Step 2: k = SHA-512(R || A || M), read as a little-endian integer.
    k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % L
    # Step 3, without the cofactor: [S]B = R + [k]A.
    return _equal(_mul(s, _BASE), _add(r, _mul(k, a)))


def load_public_key(b64url: str) -> bytes:
    """Decode and validate a pinned public key; return its 32 raw bytes.

    The text must be the canonical unpadded base64url of 32 bytes that encode
    canonically a curve point of the subgroup of prime order other than the
    neutral element. Raises ValueError otherwise; the message never contains
    any part of the input.
    """
    if type(b64url) is not str or not _KEY_TEXT.fullmatch(b64url):
        raise ValueError("Ed25519 public key must be 43 characters of unpadded base64url")
    raw = base64.urlsafe_b64decode(b64url + "=")
    if base64.urlsafe_b64encode(raw) != (b64url + "=").encode("ascii"):
        raise ValueError("Ed25519 public key is not canonical base64url")
    point = _decode_point(raw)
    if point is None:
        raise ValueError("Ed25519 public key is not a canonical curve point")
    if _has_small_order(point):         # the neutral element would pass the test below
        raise ValueError("Ed25519 public key has small order")
    if not _equal(_mul(L, point), _NEUTRAL):
        raise ValueError("Ed25519 public key is not in the subgroup of prime order")
    return raw
