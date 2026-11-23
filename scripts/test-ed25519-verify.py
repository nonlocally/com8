#!/usr/bin/env python3
"""Offline tests for lib/ed25519_verify.py.

Known answers come from RFC 8032 section 7.1. The forged cases, those that
satisfy the bare equation and must be refused all the same, are checked
against a small affine reference written here on purpose with different
formulas from the module, so that "a permissive verifier accepts this" is a
demonstrated property of each of them and not an assumption. Random
(key, message, signature) triples come from independent signers: node's
crypto module and, when it supports raw Ed25519 signing, the openssl command.
A leg whose tool is missing reports itself skipped and unqualified.

The last line is PASS only for the whole file with the node leg run. A
selection of tests ends in PARTIAL, a run without node in UNQUALIFIED and a
failing exit status, and each names the legs that did not run.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import ed25519_verify
from ed25519_verify import load_public_key, verify

# Filled in by the legs below and printed in the final line.
LEGS = {"node": None, "openssl": None}
TIMING = {}


def unhex(*parts):
    return bytes.fromhex("".join(parts))


# RFC 8032 section 7.1: (public key, message, signature). The secret keys of
# these vectors are not needed to verify and are left out.
RFC8032 = {
    "TEST 1": (
        unhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"),
        unhex(""),
        unhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
              "d25bf5f0595bbe24655141438e7a100b"),
    ),
    "TEST 2": (
        unhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"),
        unhex("72"),
        unhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
              "387b2eaeb4302aeeb00d291612bb0c00"),
    ),
    "TEST 3": (
        unhex("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"),
        unhex("af82"),
        unhex("6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc659"
              "4a7c15e9716ed28dc027beceea1ec40a"),
    ),
    "TEST 1024": (
        unhex("278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e"),
        unhex("08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98fa6e264bf09efe12ee50f8f54e9f77b1"
              "e355f6c50544e23fb1433ddf73be84d879de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
              "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc1a858efcb8550ee3a5e1998bd177e93a"
              "7363c344fe6b199ee5d02e82d522c4feba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
              "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbefefd75499da96bd68a8a97b928a8bbc10"
              "3b6621fcde2beca1231d206be6cd9ec7aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
              "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2d17ba70eb6248e594e1a2297acbbb39d"
              "502f1a8c6eb6f1ce22b3de1a1f40cc24554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
              "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc2732e69485bbc9c90bfbd62481d9089b"
              "eccf80cfe2df16a2cf65bd92dd597b0707e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
              "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51addd3b75a2b1ae0784504df543af8969b"
              "e3ea7082ff7fc9888c144da2af58429ec96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
              "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c42f58c30c04aafdb038dda0847dd988d"
              "cda6f3bfd15c4b4c4525004aa06eeff8ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
              "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08d78ff4ef5d63653a65040cf9bfd4aca7"
              "984a74d37145986780fc0b16ac451649de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
              "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a32ef5b52a1ecc820e308aa342721aac09"
              "43bf6686b64b2579376504ccc493d97e6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
              "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b50d334ba77c225bc307ba537152f3f161"
              "0e4eafe595f6d9d90d11faa933a15ef1369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
              "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c0618983f8741c5ef68d3a101e8a3b8ca"
              "c60c905c15fc910840b94c00a0b9d0"),
        unhex("0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350aa5371b1508f9f4528ecea23c436d94b"
              "5e8fcd4f681e30a6ac00a9704a188a03"),
    ),
    "TEST SHA(abc)": (
        unhex("ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf"),
        unhex("ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd"
              "454d4423643ce80e2a9ac94fa54ca49f"),
        unhex("dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e030"
              "3dca179c138ac17ad9bef1177331a704"),
    ),
}

# ---------------------------------------------------------------------------
# Independent reference: affine twisted Edwards arithmetic, one inversion per
# coordinate. Slow, and deliberately permissive where the module is strict.
# ---------------------------------------------------------------------------
P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, -1, P) % P
IDENTITY = (0, 1)


def ref_add(p1, p2):
    (x1, y1), (x2, y2) = p1, p2
    k = D * x1 * x2 * y1 * y2 % P
    return ((x1 * y2 + x2 * y1) * pow(1 + k, -1, P) % P,
            (y1 * y2 + x1 * x2) * pow(1 - k, -1, P) % P)


def ref_mul(n, point):
    total = IDENTITY
    while n:
        if n & 1:
            total = ref_add(total, point)
        point = ref_add(point, point)
        n >>= 1
    return total


def ref_decode(encoded, strict=False):
    """Permissive unless strict: y is reduced mod p and x = 0 takes either sign."""
    y = int.from_bytes(encoded, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if strict and y >= P:
        return None
    y %= P
    xx = (y * y - 1) * pow(D * y * y + 1, -1, P) % P
    if pow(xx, (P - 1) // 2, P) not in (0, 1):      # Euler's criterion
        return None
    x = pow(xx, (P + 3) // 8, P)
    if (x * x - xx) % P:
        x = x * pow(2, (P - 1) // 4, P) % P
    if strict and x == 0 and sign:
        return None
    if x & 1 != sign:
        x = (P - x) % P
    return (x, y)


def ref_encode(point):
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


BASE = ref_decode((4 * pow(5, -1, P) % P).to_bytes(32, "little"))


def ref_k(r, a, message):
    return int.from_bytes(hashlib.sha512(r + a + message).digest(), "little") % L


def ref_permissive(public_key, message, signature, cofactor=1, reduce=True):
    """The bare group equation: no length, range, canonical or order checks.

    With reduce=False the hash multiplies the key as the 512-bit number it is,
    not reduced modulo L. The two agree for every key of prime order.
    """
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a, r = ref_decode(public_key), ref_decode(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    k = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little")
    if reduce:
        k %= L
    return ref_mul(cofactor * s, BASE) == ref_add(ref_mul(cofactor, r), ref_mul(cofactor * k, a))


def ref_secret_scalar(seed):
    """RFC 8032 section 5.1.5, used only with seeds the RFC itself publishes."""
    h = bytearray(hashlib.sha512(seed).digest()[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return int.from_bytes(h, "little")


def le(number):
    return number.to_bytes(32, "little")


def flip(data, bit):
    out = bytearray(data)
    out[bit >> 3] ^= 1 << (bit & 7)
    return bytes(out)


def random_bytes(rng, size):
    return bytes(rng.getrandbits(8) for _ in range(size))


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# The eight points of small order, in canonical encoding, with their orders.
SMALL_ORDER = [
    (1, "0100000000000000000000000000000000000000000000000000000000000000"),
    (2, "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f"),
    (4, "0000000000000000000000000000000000000000000000000000000000000000"),
    (4, "0000000000000000000000000000000000000000000000000000000000000080"),
    (8, "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
    (8, "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85"),
    (8, "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
    (8, "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"),
]

# The seed RFC 8032 publishes for TEST 1. Knowing it lets a test build
# signatures that satisfy the verification equation with a degenerate R.
RFC8032_TEST_1_SEED = unhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")

# The seven keys of mixed order A + T: A is the public key of RFC 8032 TEST 1
# and T runs over the points of order 2, 4 and 8. Each is a canonical encoding
# of a curve point that is neither of small order nor in the subgroup of prime
# order, so that only the subgroup check of load_public_key refuses it.
#
# With each go two signatures made with the secret of TEST 1, R = [r]B and
# S = r + k a mod L, over a message chosen for its hash k:
#   - the first satisfies [S]B = R + [k]A' when k is reduced modulo L before
#     it multiplies the key, as RFC 8032 implementations do, and fails when
#     the 512-bit hash is used as it stands;
#   - the second is the other way round.
# A verifier's answers to the pair show which of the two it computes.
#
# (order of T, T, the key A + T, (message, signature) for reduced k, (message, signature) for unreduced k)
MIXED_ORDER_KEYS = [
    (2, "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
     "16a567fe7d4ef5482ab4012c369bf8c5f11e8d0c2559dcda50fde59708f8aee5",
     ("6d69786564206f7264657220322c206d6573736167652031",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651661d5e647ca6f38e4fda89718d8c8f9b"
      "f8ebc55f77d7f3aabf605e9c2b3e9803"),
     ("6d69786564206f7264657220322c206d6573736167652035",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b66516818d41fb4d59f37c74055d7e05defc3"
      "605a2e4b51f8b1694c90cf281867290b")),
    (4, "0000000000000000000000000000000000000000000000000000000000000000",
     "40c7570f4dd54835b9131184410ed4a0cc93e7d9ad053cbc6d07a62426999582",
     ("6d69786564206f7264657220342c206d657373616765203132",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651165e5602c8169a5751dd271a6bd8dc7a"
      "2e3287ac5baa31614d3754edff4c9100"),
     ("6d69786564206f7264657220342c206d6573736167652030",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b66516b3ae7a7967ff920d8911cf7847a4238"
      "2a0c36ca4d81283f2b35f1cb6d182f0f")),
    (4, "0000000000000000000000000000000000000000000000000000000000000080",
     "ad38a8f0b22ab7ca46ecee7bbef12b5f336c182652fac34392f859dbd9666a7d",
     ("6d69786564206f7264657220342c206d6573736167652037",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651610c8e52c8964a109765776845ea59a4"
      "9cc63df0c938ad5d4307105bf7ba9a0e"),
     ("6d69786564206f7264657220342c206d6573736167652031",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651fefcc8c949206de6d8a25ae0f2b932fd"
      "dd1595ebad2687c29262e2247394ba0d")),
    (8, "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
     "3b5b475c4b82dd1572799fc546f4c6c03e478c6654aa4c7f945b347ea32af60d",
     ("6d69786564206f7264657220382c206d6573736167652033",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b665143e4aecf50d880264cba7e0c9c94fe7f"
      "4574398ded08cfb9a26acb1c48911d0a"),
     ("6d69786564206f7264657220382c206d657373616765203131",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651fbf1d432660054dfd0b1c5af44468cb5"
      "9516d4622a08d01474d11e5ef07fe908")),
    (8, "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
     "5ca7ced5657291c4cb376e2929ebbb0747dee3aee81452ea424f42974f81fdba",
     ("6d69786564206f7264657220382c206d6573736167652038",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651d92c777b0ab1aa0966fd2491995d3ddc"
      "c2c2e28904ee73b20c4db53e0e7e6b0a"),
     ("6d69786564206f7264657220382c206d657373616765203134",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b66512a833c9cd73e0660ca745c61c05fcf62"
      "e180fdf8dee7f327a403f6e6dae04f0d")),
    (8, "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
     "9158312a9a8d6e3b34c891d6d61444f8b8211c5117ebad15bdb0bd68b07e0245",
     ("6d69786564206f7264657220382c206d6573736167652030",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b665103e4c7f1cfaddbbe7aa4a77487f7a38c"
      "3297dd038c42f08052bd0c2129c01203"),
     ("6d69786564206f7264657220382c206d6573736167652033",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651d732810ce6fdae344f67a0e7489ce11c"
      "89c8baba844aa966948191530f1c9e0e")),
    (8, "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
     "b2a4b8a3b47d22ea8d86603ab90b393fc1b87399ab55b3806ba4cb815cd509f2",
     ("6d69786564206f7264657220382c206d6573736167652031",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b6651851a4564d2b548e009735756f49a6229"
      "5bd3e7cb367d51b2378239168e84f60a"),
     ("6d69786564206f7264657220382c206d6573736167652032",
      "89735cc0223ef615eae81a4d5e32a4e394d2c2e0f88a3ae4bfc5e7c4673b66513d6c9962b150e99ac9170c9c90d460b2"
      "ef4d4f0ddf36cb0d2eebe0869c652f0b")),
]

# Curated cases in the style of Wycheproof's EdDSA set, written for this file:
# (tcId, result, flag, comment, public key, message, signature), all hex.
# PERMISSIVE lists the invalid cases that nevertheless satisfy the bare group
# equation [S]B = R + [k]A, which is what makes them worth having.
CASES = [
    (1, "valid", "",
     "RFC 8032 TEST 2, unmodified",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (2, "valid", "",
     "independent signer, 47-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e",
     "472f76bd49c66d959d16254fa89a75db1b08c75a919407fd7b2a9c40427aa3c3eb5743d25439e1f89d7d44e4eebb38ef"
     "d0fecc937bf5058952faf87bf1da970c"),
    (3, "valid", "",
     "independent signer, 48-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f",
     "e4075ade2ef5426805ae8444897c1a92802aba7ea069f4e780bca4a75e552a1122e20fd6488ca12dcd7bd5cc66a5435c"
     "2070c3db478506acd3c98bfe44b69a01"),
    (4, "valid", "",
     "independent signer, 64-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"
     "303132333435363738393a3b3c3d3e3f",
     "b659bf817bea8e96dd87af52fe5904aa5ac471a6caf95d9f978255f678ed63c9731848d5c419a71b53b38e883b407f65"
     "89c6d8a98abdbe43ff2d446c61a3560c"),
    (5, "valid", "",
     "independent signer, 111-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"
     "303132333435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
     "606162636465666768696a6b6c6d6e",
     "e55fe47e986455d633048c275389792188dbbfca58f286dbd93774f0f92ac1f6de98adeb6a7c6f82a4cb6a34a5618e81"
     "9dc845718d5d3c0b38582d33fdb89303"),
    (6, "valid", "",
     "independent signer, 112-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"
     "303132333435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
     "606162636465666768696a6b6c6d6e6f",
     "f533575fbe6e94b9633fe5b7acb157aca16e2c8df56712b3cd7505a3f253a4d585b50eb3ce13bba68ab78179bf09d8e1"
     "556db6ad39c32ca3bfa3e83dfbbc5a09"),
    (7, "valid", "",
     "independent signer, 175-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"
     "303132333435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
     "606162636465666768696a6b6c6d6e6f707172737475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f"
     "909192939495969798999a9b9c9d9e9fa0a1a2a3a4a5a6a7a8a9aaabacadae",
     "2dbe1549d2f695db5f5dd2c3533929f8a7528b49840963c144b2f7d3d259d22ed602bf8aedb0b1b74ac3af3420988bb4"
     "7a0e97125abb48bc01c4d76e3f49cc0f"),
    (8, "valid", "",
     "independent signer, 176-byte message (SHA-512 block boundary of R || A || M)",
     "6a830fc108b995230a4bfd19efef0995faca5e001f59c12a6b6b801093976b1b",
     "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f"
     "303132333435363738393a3b3c3d3e3f404142434445464748494a4b4c4d4e4f505152535455565758595a5b5c5d5e5f"
     "606162636465666768696a6b6c6d6e6f707172737475767778797a7b7c7d7e7f808182838485868788898a8b8c8d8e8f"
     "909192939495969798999a9b9c9d9e9fa0a1a2a3a4a5a6a7a8a9aaabacadaeaf",
     "02fa456612ad586ca8898b2ca0924cbce335c27de99223ca1d90a981c178e97475c9ccae19c561b870cc1beed12124a7"
     "cbcb4420243f823799da9e1b76826308"),
    (9, "invalid", "SignatureMalleability",
     "S replaced by S + L",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69daf52db7415978abc61b2c2eb6aeebfca0"
     "387b2eaeb4302aeeb00d291612bb0c10"),
    (10, "invalid", "SignatureMalleability",
     "S replaced by S + 2L",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69dae201ad9e73dbbd1ef2c825598de5dbb5"
     "387b2eaeb4302aeeb00d291612bb0c20"),
    (11, "invalid", "SignatureMalleability",
     "S replaced by S + 2^255 (top bit set)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c80"),
    (12, "invalid", "SignatureMalleability",
     "S replaced by S + 2^253",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c20"),
    (13, "invalid", "SpecialS",
     "S = 0",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da00000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (14, "invalid", "SpecialS",
     "S = 1",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da01000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (15, "invalid", "SpecialS",
     "S = L - 1",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69daecd3f55c1a631258d69cf7a2def9de14"
     "00000000000000000000000000000010"),
    (16, "invalid", "SpecialS",
     "S = L (non-canonical zero)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69daedd3f55c1a631258d69cf7a2def9de14"
     "00000000000000000000000000000010"),
    (17, "invalid", "SpecialS",
     "S = 2^256 - 1",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69daffffffffffffffffffffffffffffffff"
     "ffffffffffffffffffffffffffffffff"),
    (18, "invalid", "SpecialR",
     "all-zero signature",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (19, "invalid", "SpecialR",
     "R = identity, S = 0",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (20, "invalid", "SpecialR",
     "R = identity, S = 1",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "010000000000000000000000000000000000000000000000000000000000000001000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (21, "invalid", "SpecialR",
     "R = identity, S = k = SHA-512(R || A || M) mod L (crafted without the secret)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "0100000000000000000000000000000000000000000000000000000000000000dd9869840aba7621a392101b07265bc1"
     "8d4204659273fb08cde9dd98212c5709"),
    (22, "invalid", "SmallOrderR",
     "R = identity, S = k * a mod L (zero nonce; satisfies [S]B = R + [k]A, refused for small-order R)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "0100000000000000000000000000000000000000000000000000000000000000675d054595fa17cb6a44e3ada6ca385e"
     "55667e41a63fdab0ac22d10000354002"),
    (23, "invalid", "SpecialR",
     "R = base point, S = 1",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "586666666666666666666666666666666666666666666666666666666666666601000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (24, "invalid", "SpecialR",
     "R negated (sign bit of x flipped)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb695a085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (25, "invalid", "SpecialR",
     "R and S swapped",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c0092a009a9f0d4cab8720e820b5f642540"
     "a2b27b5416503f8fb3762223ebdb69da"),
    (26, "invalid", "Cofactor",
     "R = [r]B + (order-8 point), S = r + k * a: holds only for the cofactored equation",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "4b9ebf7b8670a5ade023c24054a8ea0c64a0b697b67f5cbdedaa912873e7579b049b12812a84ac8de6c7c14edd1c964b"
     "d59d13ef2018f37c8a9630ea1766ab06"),
    (27, "invalid", "NonCanonicalR",
     "R = y = p + 1 (non-canonical identity), S = k * a for that encoding",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f0a0403c78756704394a791302d07acbd"
     "f846e02e6371010b73f9c021313bde01"),
    (28, "invalid", "NonCanonicalR",
     "R = y = p (non-canonical y = 0)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (29, "invalid", "NonCanonicalR",
     "R = y = 2^255 - 1",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (30, "invalid", "NonCanonicalR",
     "R = 2^256 - 1 (all ones)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (31, "invalid", "InvalidR",
     "R = identity with the sign bit set (x = 0, x_0 = 1)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "0100000000000000000000000000000000000000000000000000000000000080bd498620dc071c7bd37de37a8b3208e5"
     "475dd11db9151270fb200f690ae0d009"),
    (32, "invalid", "InvalidR",
     "R = (0, -1) with the sign bit set (x = 0, x_0 = 1)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (33, "invalid", "InvalidR",
     "R = y = 2, not on the curve",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "0200000000000000000000000000000000000000000000000000000000000000085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (34, "invalid", "Length",
     "empty signature",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     ""),
    (35, "invalid", "Length",
     "R only (S missing)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"),
    (36, "invalid", "Length",
     "signature truncated by one byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c"),
    (37, "invalid", "Length",
     "signature with a trailing zero byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c0000"),
    (38, "invalid", "Length",
     "signature with a leading zero byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "0092a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d"
     "8c387b2eaeb4302aeeb00d291612bb0c00"),
    (39, "invalid", "Length",
     "signature repeated twice",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c0092a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    (40, "invalid", "Length",
     "public key truncated by one byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af466",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (41, "invalid", "Length",
     "public key with a trailing zero byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c00",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (42, "invalid", "Length",
     "empty public key",
     "",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (43, "invalid", "WrongMessage",
     "RFC 8032 TEST 2 signature over the TEST 3 message",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "af82",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (44, "invalid", "WrongMessage",
     "RFC 8032 TEST 2 signature over the empty message",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (45, "invalid", "WrongMessage",
     "message with a trailing zero byte",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "7200",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (46, "invalid", "WrongKey",
     "RFC 8032 TEST 2 signature under the TEST 1 key",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (47, "invalid", "WrongKey",
     "public key negated (sign bit of x flipped)",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4668c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (48, "invalid", "SmallOrderKey",
     "public key of order 1; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 1 divides k",
     "0100000000000000000000000000000000000000000000000000000000000000",
     "666f726765642030",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (49, "invalid", "SmallOrderKey",
     "public key of order 2; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 2 divides k",
     "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
     "666f726765642032",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (50, "invalid", "SmallOrderKey",
     "public key of order 4; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 4 divides k",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "666f726765642030",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (51, "invalid", "SmallOrderKey",
     "public key of order 4; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 4 divides k",
     "0000000000000000000000000000000000000000000000000000000000000080",
     "666f726765642039",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (52, "invalid", "SmallOrderKey",
     "public key of order 8; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 8 divides k",
     "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
     "666f726765642032",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (53, "invalid", "SmallOrderKey",
     "public key of order 8; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 8 divides k",
     "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85",
     "666f726765642032",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (54, "invalid", "SmallOrderKey",
     "public key of order 8; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 8 divides k",
     "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
     "666f726765642037",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (55, "invalid", "SmallOrderKey",
     "public key of order 8; R = [7]B, S = 7 satisfies [S]B = R + [k]A because 8 divides k",
     "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa",
     "666f726765642033",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (56, "invalid", "SmallOrderKey",
     "all-zero public key with the all-zero signature; satisfies [S]B = R + [k]A because k = 3 mod 4",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "666f726765642038",
     "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (57, "invalid", "NonCanonicalKey",
     "public key y = p + 1 (non-canonical identity); R = [7]B, S = 7 passes a permissive verifier",
     "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
     "666f726765642030",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (58, "invalid", "NonCanonicalKey",
     "public key y = p (non-canonical y = 0); R = [7]B, S = 7 passes a permissive verifier",
     "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
     "666f726765642030",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (59, "invalid", "NonCanonicalKey",
     "public key y = p + 1 with the sign bit set; R = [7]B, S = 7 passes a permissive verifier",
     "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
     "666f726765642030",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (60, "invalid", "InvalidKey",
     "public key identity with the sign bit set (x = 0, x_0 = 1); R = [7]B, S = 7 passes a permissive verifier",
     "0100000000000000000000000000000000000000000000000000000000000080",
     "666f726765642030",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (61, "invalid", "InvalidKey",
     "public key (0, -1) with the sign bit set (x = 0, x_0 = 1); R = [7]B, S = 7 passes a permissive verifier",
     "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
     "666f726765642031",
     "b862409fb5c4c4123df2abf7462b88f041ad36dd6864ce872fd5472be363c5b107000000000000000000000000000000"
     "00000000000000000000000000000000"),
    (62, "invalid", "NonCanonicalKey",
     "public key y = p + 3 (non-canonical encoding of a point of large order)",
     "f0ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (63, "invalid", "NonCanonicalKey",
     "public key y = 2^255 - 1",
     "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (64, "invalid", "NonCanonicalKey",
     "public key 2^256 - 1 (all ones)",
     "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (65, "invalid", "InvalidKey",
     "public key y = 2, not on the curve",
     "0200000000000000000000000000000000000000000000000000000000000000",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
    (66, "invalid", "InvalidKey",
     "public key y = 7, not on the curve",
     "0700000000000000000000000000000000000000000000000000000000000000",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c"
     "387b2eaeb4302aeeb00d291612bb0c00"),
]
PERMISSIVE = {9, 10, 22, 27, 31, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61}


class MixedOrderKeys(unittest.TestCase):
    """Keys outside the subgroup of prime order: refused when loaded; and what verify computes with one."""

    def test_the_vectors_are_what_they_claim(self):
        a = ref_secret_scalar(RFC8032_TEST_1_SEED)
        honest = ref_decode(RFC8032["TEST 1"][0], strict=True)
        self.assertEqual([(order, torsion) for order, torsion, _, _, _ in MIXED_ORDER_KEYS],
                         [(order, torsion) for order, torsion in SMALL_ORDER if order > 1])
        self.assertEqual(len({key for _, _, key, _, _ in MIXED_ORDER_KEYS}), 7)
        for order, torsion, key, reduced, unreduced in MIXED_ORDER_KEYS:
            key = unhex(key)
            point = ref_decode(key, strict=True)
            with self.subTest(key=key.hex()):
                self.assertEqual(point, ref_add(honest, ref_decode(unhex(torsion), strict=True)))
                self.assertEqual(ref_encode(point), key, "a canonical encoding")
                self.assertNotEqual(ref_mul(8, point), IDENTITY, "not of small order")
                self.assertNotEqual(ref_mul(L, point), IDENTITY, "not in the subgroup of prime order")
                for label, (message, signature) in (("reduced", reduced), ("unreduced", unreduced)):
                    message, signature = unhex(message), unhex(signature)
                    with self.subTest(label):
                        self.assertEqual(
                            (ref_permissive(key, message, signature), ref_permissive(key, message, signature, reduce=False)),
                            (label == "reduced", label == "unreduced"))
                        # Made as described: an honest R, and S = r + k a with the secret of TEST 1.
                        full = int.from_bytes(hashlib.sha512(signature[:32] + key + message).digest(), "little")
                        s = int.from_bytes(signature[32:], "little")
                        self.assertLess(s, L)
                        self.assertEqual(ref_mul((s - full * a) % L, BASE), ref_decode(signature[:32], strict=True))
                        self.assertEqual((full % L % order == 0, full % order == 0),
                                         (label == "reduced", label == "unreduced"))

    def test_load_public_key_refuses_each(self):
        for _, _, key, _, _ in MIXED_ORDER_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    load_public_key(b64url(unhex(key)))
                self.assertEqual(str(caught.exception), "Ed25519 public key is not in the subgroup of prime order")
        # The honest key each was made from loads, and so does every key of the RFC.
        for name, (key, _, _) in RFC8032.items():
            with self.subTest(name):
                self.assertEqual(load_public_key(b64url(key)), key)

    def test_the_neutral_element_is_in_the_subgroup_and_is_refused_as_small(self):
        self.assertEqual(ref_mul(L, IDENTITY), IDENTITY)
        with self.assertRaises(ValueError) as caught:
            load_public_key(b64url(le(1)))
        self.assertEqual(str(caught.exception), "Ed25519 public key has small order")
        for order, encoding in SMALL_ORDER:
            with self.subTest(encoding):
                with self.assertRaises(ValueError) as caught:
                    load_public_key(b64url(unhex(encoding)))
                self.assertEqual(str(caught.exception), "Ed25519 public key has small order")

    def test_verify_reduces_the_hash_modulo_l(self):
        # verify is not where such a key is refused; given one, it computes
        # what every RFC 8032 implementation computes.
        for order, _, key, reduced, unreduced in MIXED_ORDER_KEYS:
            with self.subTest(key=key):
                self.assertIs(verify(unhex(key), unhex(reduced[0]), unhex(reduced[1])), True)
                self.assertIs(verify(unhex(key), unhex(unreduced[0]), unhex(unreduced[1])), False)

    def test_the_module_says_where_such_a_key_is_refused(self):
        self.assertIn("subgroup of prime order", " ".join(ed25519_verify.__doc__.split()))
        self.assertIn("load_public_key", " ".join(ed25519_verify.verify.__doc__.split()))
        self.assertNotIn("is not detected", ed25519_verify.__doc__)


class RFC8032Vectors(unittest.TestCase):
    def test_reference_agrees_with_the_rfc(self):
        self.assertEqual(ref_encode(BASE).hex(), "58" + "66" * 31)
        self.assertEqual(ref_mul(L, BASE), IDENTITY)
        self.assertEqual(ref_encode(ref_mul(ref_secret_scalar(RFC8032_TEST_1_SEED), BASE)),
                         RFC8032["TEST 1"][0])

    def test_section_7_1_vectors_verify(self):
        self.assertEqual(list(RFC8032), ["TEST 1", "TEST 2", "TEST 3", "TEST 1024", "TEST SHA(abc)"])
        self.assertEqual([len(RFC8032[name][1]) for name in RFC8032], [0, 1, 2, 1023, 64])
        self.assertEqual(RFC8032["TEST SHA(abc)"][1], hashlib.sha512(b"abc").digest())
        for name, (key, message, signature) in RFC8032.items():
            with self.subTest(name):
                self.assertIs(verify(key, message, signature), True)

    def bits(self, name, total):
        # Every bit for the smallest vector with a message, a spread for the rest.
        return range(total) if name == "TEST 2" else range(0, total, max(1, total // 12))

    def test_flipped_message_bit(self):
        for name, (key, message, signature) in RFC8032.items():
            for bit in self.bits(name, len(message) * 8):
                with self.subTest(name, bit=bit):
                    self.assertIs(verify(key, flip(message, bit), signature), False)
            with self.subTest(name, change="appended byte"):
                self.assertIs(verify(key, message + b"\0", signature), False)

    def test_flipped_R_bit(self):
        for name, (key, message, signature) in RFC8032.items():
            for bit in self.bits(name, 256):
                with self.subTest(name, bit=bit):
                    self.assertIs(verify(key, message, flip(signature, bit)), False)

    def test_flipped_S_bit(self):
        for name, (key, message, signature) in RFC8032.items():
            for bit in self.bits(name, 256):
                with self.subTest(name, bit=bit):
                    self.assertIs(verify(key, message, flip(signature, 256 + bit)), False)

    def test_flipped_key_bit(self):
        for name, (key, message, signature) in RFC8032.items():
            for bit in self.bits(name, 256):
                with self.subTest(name, bit=bit):
                    self.assertIs(verify(flip(key, bit), message, signature), False)

    def test_s_plus_l_is_rejected(self):
        for name, (key, message, signature) in RFC8032.items():
            s = int.from_bytes(signature[32:], "little")
            self.assertLess(s, L)
            for multiple in (1, 2):
                forged = signature[:32] + le(s + multiple * L)
                with self.subTest(name, multiple=multiple):
                    self.assertTrue(ref_permissive(key, message, forged), "S + L must satisfy the bare equation")
                    self.assertIs(verify(key, message, forged), False)


class Strictness(unittest.TestCase):
    key, message, signature = RFC8032["TEST 3"]

    def test_wrong_lengths(self):
        for size in (0, 1, 31, 33, 64):
            with self.subTest(key_bytes=size):
                self.assertIs(verify((self.key * 2)[:size], self.message, self.signature), False)
        for size in (0, 1, 32, 63, 65, 96, 128):
            with self.subTest(signature_bytes=size):
                self.assertIs(verify(self.key, self.message, (self.signature * 2)[:size]), False)

    def test_overlong_key_is_refused_even_with_a_signature_made_for_it(self):
        # A || 00 names the same point to a decoder that does not check the
        # length, and whoever holds the secret can sign for that spelling.
        key, message, _ = RFC8032["TEST 1"]
        a = ref_secret_scalar(RFC8032_TEST_1_SEED)
        r = 0x0F1E2D3C4B5A69780F1E2D3C4B5A69780F1E2D3C4B5A69780F1E2D3C4B5A6978 % L
        encoded_r = ref_encode(ref_mul(r, BASE))

        def sign_for(spelling):
            return encoded_r + le((r + ref_k(encoded_r, spelling, message) * a) % L)
        self.assertIs(verify(key, message, sign_for(key)), True, "the construction itself signs correctly")
        self.assertIs(verify(key + b"\0", message, sign_for(key + b"\0")), False)

    def test_malformed_arguments_return_false_without_raising(self):
        good = (self.key, self.message, self.signature)
        self.assertIs(verify(*good), True)
        for position in range(3):
            for bad in (None, "text", 0, 1.5, [], {}, object(), bytearray(good[position]),
                        memoryview(good[position]), good[position].hex()):
                arguments = list(good)
                arguments[position] = bad
                with self.subTest(position=position, bad=type(bad).__name__):
                    self.assertIs(verify(*arguments), False)

    def test_arguments_that_answer_for_themselves_are_never_asked(self):
        # Not an Exception: were any argument compared, measured or sliced
        # before its type was known, this would pass every `except Exception`.
        class Trap(BaseException):
            pass

        def trap(*arguments, **named):
            raise Trap()

        trapped = ("__eq__ __ne__ __hash__ __bool__ __len__ __iter__ __getitem__ __add__ __radd__ __bytes__ "
                   "__str__ __repr__ __index__").split()

        def hostile():
            meta = type("HostileMeta", (type,), {name: trap for name in ("__eq__", "__ne__", "__hash__")})
            return meta("Hostile", (), {name: trap for name in trapped + ["__getattribute__"]})()

        def lookalike(value):
            return type("Hostile" + type(value).__name__, (type(value),), {name: trap for name in trapped})(value)

        self.assertRaises(Trap, isinstance, hostile(), bytes)
        self.assertRaises(Trap, len, lookalike(self.key))
        good = (self.key, self.message, self.signature)
        for position in range(3):
            for label, bad in (("an object of no type", hostile()), ("a bytes subclass", lookalike(good[position]))):
                arguments = list(good)
                arguments[position] = bad
                with self.subTest(position=position, bad=label):
                    self.assertIs(verify(*arguments), False)
        text = b64url(self.key)
        self.assertEqual(load_public_key(text), self.key)
        for label, bad in (("an object of no type", hostile()), ("a str subclass", lookalike(text))):
            with self.subTest(bad=label):
                self.assertRaises(ValueError, load_public_key, bad)

    def test_random_garbage_never_raises(self):
        rng = random.Random(8032)
        for _ in range(2000):
            key = random_bytes(rng, rng.choice((0, 31, 32, 32, 32, 33)))
            signature = random_bytes(rng, rng.choice((0, 63, 64, 64, 64, 65)))
            self.assertIs(verify(key, random_bytes(rng, rng.randrange(64)), signature), False)

    def test_all_zero_signature(self):
        for name, (key, message, _) in RFC8032.items():
            with self.subTest(name):
                self.assertIs(verify(key, message, bytes(64)), False)

    def test_all_zero_signature_under_the_all_zero_key(self):
        # R = A = a point of order 4 and S = 0: the bare equation holds whenever
        # k = 3 mod 4, so one message in four is "signed" for free.
        message = next(b"zero %d" % n for n in range(1000) if ref_k(bytes(32), bytes(32), b"zero %d" % n) % 4 == 3)
        self.assertTrue(ref_permissive(bytes(32), message, bytes(64)))
        self.assertIs(verify(bytes(32), message, bytes(64)), False)

    def test_point_decoding_matches_the_reference(self):
        decode = ed25519_verify._decode_point
        rng = random.Random(5173)
        samples = [random_bytes(rng, 32) for _ in range(300)]
        samples += [ref_encode(ref_mul(rng.randrange(1, L), BASE)) for _ in range(20)]
        samples += [le(y | (sign << 255)) for y in range(40) for sign in (0, 1)]
        on_curve = 0
        for encoded in samples:
            expected, point = ref_decode(encoded, strict=True), decode(encoded)
            with self.subTest(encoded=encoded.hex()):
                if expected is None:
                    self.assertIsNone(point)
                    continue
                self.assertIsNotNone(point)
                on_curve += 1
                x, y, z, t = point
                inverse = pow(z, P - 2, P)
                self.assertEqual((x * inverse % P, y * inverse % P), expected)
                self.assertEqual(x * y % P, z * t % P, "extended coordinate T must equal XY/Z")
        self.assertGreater(on_curve, 100)
        self.assertGreater(len(samples) - on_curve, 100)

    def test_non_canonical_y_is_rejected(self):
        # The nineteen values p <= y < 2^255, with either sign bit.
        reduced_on_curve = 0
        for excess in range(19):
            for sign in (0, 1):
                encoded = le((P + excess) | (sign << 255))
                reduced_on_curve += ref_decode(encoded) is not None
                with self.subTest(excess=excess, sign=sign):
                    self.assertIsNone(ed25519_verify._decode_point(encoded))
                    self.assertIs(verify(encoded, self.message, self.signature), False)
                    self.assertIs(verify(self.key, self.message, encoded + self.signature[32:]), False)
                    self.assertRaises(ValueError, load_public_key, b64url(encoded))
        self.assertGreater(reduced_on_curve, 8, "several of these do name real points once reduced")

    def test_x_zero_with_sign_bit_is_rejected(self):
        for y in (1, P - 1):
            encoded = le(y | (1 << 255))
            with self.subTest(y=y):
                self.assertIsNotNone(ref_decode(encoded), "a permissive decoder accepts it")
                self.assertIsNone(ed25519_verify._decode_point(encoded))
                self.assertIs(verify(encoded, self.message, self.signature), False)
                self.assertRaises(ValueError, load_public_key, b64url(encoded))

    def test_small_order_public_keys_are_rejected(self):
        # R = [7]B with S = 7 needs no secret and is an honest-looking R, so only
        # the check on the key stands between these keys and a forgery.
        forged = ref_encode(ref_mul(7, BASE)) + le(7)
        points = set()
        for order, encoding in SMALL_ORDER:
            key = unhex(encoding)
            point = ref_decode(key, strict=True)
            with self.subTest(key=encoding):
                self.assertIsNotNone(point)
                self.assertEqual(ref_encode(point), key)
                self.assertEqual([n for n in (1, 2, 4, 8) if ref_mul(n, point) == IDENTITY][0], order)
                points.add(point)
                message = next(b"forged %d" % n for n in range(1000)
                               if ref_k(forged[:32], key, b"forged %d" % n) % order == 0)
                self.assertTrue(ref_permissive(key, message, forged), "the bare equation accepts this forgery")
                self.assertIs(verify(key, message, forged), False)
                self.assertRaises(ValueError, load_public_key, b64url(key))
        self.assertEqual(len(points), 8)

    def test_identity_as_R_with_crafted_S(self):
        key, message, _ = RFC8032["TEST 1"]
        identity = le(1)
        k = ref_k(identity, key, message)
        for label, s in (("zero", 0), ("one", 1), ("k", k), ("L - 1", L - 1)):
            with self.subTest(S=label):
                self.assertIs(verify(key, message, identity + le(s)), False)
        # With the secret scalar a, S = k * a is the signature a zero nonce would
        # produce. It satisfies [S]B = R + [k]A; a small-order R is refused anyway.
        a = ref_secret_scalar(RFC8032_TEST_1_SEED)
        zero_nonce = identity + le(k * a % L)
        self.assertTrue(ref_permissive(key, message, zero_nonce))
        self.assertIs(verify(key, message, zero_nonce), False)

    def test_equation_is_cofactorless(self):
        # R = [r]B + T with T of order 8 and S = r + k * a holds only after
        # multiplying through by 8. RFC 8032 permits either form; this module
        # uses the stricter one without the cofactor.
        key, message, _ = RFC8032["TEST 1"]
        a = ref_secret_scalar(RFC8032_TEST_1_SEED)
        r = 0x1F2E3D4C5B6A79881F2E3D4C5B6A79881F2E3D4C5B6A79881F2E3D4C5B6A7988 % L
        encoded_r = ref_encode(ref_add(ref_mul(r, BASE), ref_decode(unhex(SMALL_ORDER[4][1]))))
        signature = encoded_r + le((r + ref_k(encoded_r, key, message) * a) % L)
        self.assertTrue(ref_permissive(key, message, signature, cofactor=8))
        self.assertFalse(ref_permissive(key, message, signature))
        self.assertIs(verify(key, message, signature), False)


class CuratedCases(unittest.TestCase):
    def test_table_is_well_formed(self):
        self.assertEqual([case[0] for case in CASES], list(range(1, len(CASES) + 1)))
        self.assertGreaterEqual(len(CASES), 60)
        self.assertTrue(PERMISSIVE <= {case[0] for case in CASES if case[1] == "invalid"})
        self.assertEqual({case[1] for case in CASES}, {"valid", "invalid"})

    def test_cases(self):
        for number, result, flag, comment, key, message, signature in CASES:
            key, message, signature = unhex(key), unhex(message), unhex(signature)
            with self.subTest(tcId=number, flag=flag, comment=comment):
                self.assertIs(verify(key, message, signature), result == "valid")

    def test_cases_are_what_their_comments_claim(self):
        # Guards the literals: a mistyped forgery would still be "invalid", only
        # for a dull reason. Each case's verdict under the bare equation is pinned.
        for number, result, flag, comment, key, message, signature in CASES:
            key, message, signature = unhex(key), unhex(message), unhex(signature)
            with self.subTest(tcId=number, comment=comment):
                self.assertEqual(ref_permissive(key, message, signature),
                                 result == "valid" or number in PERMISSIVE)


class LoadPublicKey(unittest.TestCase):
    key = RFC8032["TEST 1"][0]

    def test_returns_the_raw_key(self):
        for name, (key, _, _) in RFC8032.items():
            with self.subTest(name):
                loaded = load_public_key(b64url(key))
                self.assertIs(type(loaded), bytes)
                self.assertEqual(loaded, key)

    def test_rejects_anything_but_canonical_unpadded_base64url(self):
        text = b64url(self.key)
        self.assertEqual(len(text), 43)
        standard = base64.b64encode(self.key).decode("ascii").rstrip("=")
        self.assertNotEqual(standard, text, "this key's spelling uses a character outside the URL-safe alphabet")
        last = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(text[-1])
        self.assertEqual(last & 3, 0)
        loose = text[:-1] + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[last | 1]
        self.assertEqual(base64.urlsafe_b64decode(loose + "="), self.key, "same bytes to a lenient decoder")
        for label, bad in (("padded", text + "="), ("short", text[:-1]), ("long", text + "A"), ("empty", ""),
                           ("trailing newline", text + "\n"), ("leading space", " " + text),
                           ("inner space", text[:10] + " " + text[10:]), ("standard alphabet", standard),
                           ("non-zero trailing bits", loose), ("hex", self.key.hex()),
                           ("bytes", text.encode("ascii")), ("raw key", self.key), ("none", None),
                           ("non-ascii", text[:-1] + "\u00e9")):
            with self.subTest(label):
                with self.assertRaises(ValueError) as caught:
                    load_public_key(bad)
                self.assertNotIn(text[:16], str(caught.exception))

    def test_error_does_not_echo_the_key(self):
        bad = b64url(unhex(SMALL_ORDER[4][1]))
        with self.assertRaises(ValueError) as caught:
            load_public_key(bad)
        self.assertNotIn(bad[:8], str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_rejects_points_off_the_curve(self):
        for y in (2, 7):
            with self.subTest(y=y):
                self.assertIsNone(ref_decode(le(y)))
                self.assertRaises(ValueError, load_public_key, b64url(le(y)))


class Performance(unittest.TestCase):
    def test_one_verification_is_well_under_50_ms(self):
        # Judged on processor time, so that a busy machine does not turn this
        # into a test of its scheduler; the elapsed time is reported beside it.
        key, message, signature = RFC8032["TEST 1024"]
        cpu, elapsed = [], []
        for _ in range(40):
            started = time.process_time(), time.perf_counter()
            accepted = verify(key, message, signature)
            cpu.append((time.process_time() - started[0]) * 1000)
            elapsed.append((time.perf_counter() - started[1]) * 1000)
            self.assertIs(accepted, True)
        TIMING.update(cpu=statistics.median(cpu), elapsed=statistics.median(elapsed))
        self.assertLess(TIMING["cpu"], 50.0)


NODE_PROBE = """
const crypto = require('crypto');
const pair = crypto.generateKeyPairSync('ed25519');
const signature = crypto.sign(null, Buffer.from('probe'), pair.privateKey);
if (signature.length !== 64 || !crypto.verify(null, Buffer.from('probe'), pair.publicKey, signature)) process.exit(3);
process.stdout.write('ed25519-ok');
"""

# One fresh key per triple; the private half never leaves the node process.
NODE_TRIPLES = """
const crypto = require('crypto');
const fixed = [0, 1, 2, 3, 31, 32, 33, 47, 48, 63, 64, 65, 111, 112, 127, 128, 129, 255, 256, 1023, 1024, 4095, 4096];
for (let i = 0; i < COUNT; i++) {
  const pair = crypto.generateKeyPairSync('ed25519');
  const message = crypto.randomBytes(i < fixed.length ? fixed[i] : crypto.randomBytes(2).readUInt16BE(0) % 4097);
  const key = pair.publicKey.export({type: 'spki', format: 'der'}).subarray(-32);
  const signature = crypto.sign(null, message, pair.privateKey);
  console.log(JSON.stringify([key.toString('hex'), message.toString('hex'), signature.toString('hex')]));
}
"""


def node_with_ed25519():
    """(path, None) for a node that signs Ed25519, else (None, reason)."""
    node = shutil.which("node")
    if node is None:
        return None, "node is not installed"
    try:
        probe = subprocess.run([node, "-e", NODE_PROBE], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, "node could not be run (%s)" % type(error).__name__
    if probe.returncode != 0 or probe.stdout != "ed25519-ok":
        return None, "node has no Ed25519 support"
    return node, None


class Differential(unittest.TestCase):
    """Shared assertions: every triple verifies, every one-bit mutation does not."""

    def check(self, triples, rng):
        for index, (key, message, signature) in enumerate(triples):
            detail = "key=%s message=%s signature=%s" % (key.hex(), message.hex(), signature.hex())
            self.assertIs(verify(key, message, signature), True, detail)
            # A key an honest signer made is in the subgroup of prime order and can be pinned.
            self.assertEqual(load_public_key(b64url(key)), key, detail)
            if message and rng.random() < 0.5:
                bit = rng.randrange(len(message) * 8)
                mutated = (key, flip(message, bit), signature)
                where = "message bit %d" % bit
            else:
                bit = rng.randrange(512)
                mutated = (key, message, flip(signature, bit))
                where = "signature bit %d" % bit
            self.assertIs(verify(*mutated), False, "accepted after flipping %s: %s" % (where, detail))
            bit = rng.randrange(256)
            self.assertIs(verify(flip(key, bit), message, signature), False,
                          "accepted after flipping key bit %d: %s" % (bit, detail))


class NodeDifferential(Differential):
    COUNT = 200

    def test_node_signatures(self):
        node, reason = node_with_ed25519()
        if node is None:
            LEGS["node"] = "SKIPPED (%s)" % reason
            self.skipTest("UNQUALIFIED: " + reason)
        output = subprocess.run([node, "-e", NODE_TRIPLES.replace("COUNT", str(self.COUNT))],
                                capture_output=True, text=True, timeout=120, check=True).stdout
        triples = [tuple(unhex(field) for field in json.loads(line)) for line in output.splitlines()]
        self.assertEqual(len(triples), self.COUNT)
        self.assertGreaterEqual(len(triples), 200)
        lengths = [len(message) for _, message, _ in triples]
        self.assertIn(0, lengths)
        self.assertIn(4096, lengths)
        self.assertGreater(len(set(lengths)), 100)
        self.assertEqual(len({key for key, _, _ in triples}), self.COUNT)
        seed = int.from_bytes(os.urandom(8), "big")
        self.check(triples, random.Random(seed))
        LEGS["node"] = "ran (%d triples, %d mutations)" % (self.COUNT, 2 * self.COUNT)


class OpenSSLDifferential(Differential):
    LENGTHS = (32, 0, 1, 63, 64, 65, 127, 128, 1000, 4096)     # the first is the probe for support
    SPKI_PREFIX = unhex("302a300506032b6570032100")

    def run_tool(self, *arguments):
        return subprocess.run(arguments, capture_output=True, timeout=60)

    def test_openssl_signatures(self):
        openssl = shutil.which("openssl")
        if openssl is None:
            LEGS["openssl"] = "SKIPPED (openssl is not installed)"
            self.skipTest("UNQUALIFIED: openssl is not installed")
        triples, notes = [], ""
        with tempfile.TemporaryDirectory(prefix="com8-ed25519-") as tmp:
            for index, length in enumerate(self.LENGTHS):
                private, data, out = (os.path.join(tmp, name) for name in ("key-%d.pem" % index, "message", "signature"))
                generated = self.run_tool(openssl, "genpkey", "-algorithm", "ED25519", "-out", private)
                public = self.run_tool(openssl, "pkey", "-in", private, "-pubout", "-outform", "DER")
                message = os.urandom(length)
                Path(data).write_bytes(message)
                Path(out).unlink(missing_ok=True)
                signed = self.run_tool(openssl, "pkeyutl", "-sign", "-inkey", private, "-rawin", "-in", data, "-out", out)
                supported = (generated.returncode == 0 and public.returncode == 0 and signed.returncode == 0
                             and len(public.stdout) == 44 and public.stdout.startswith(self.SPKI_PREFIX)
                             and os.path.exists(out) and os.path.getsize(out) == 64)
                if not supported and index == 0:
                    version = self.run_tool(openssl, "version").stdout.decode("utf-8", "replace").strip()
                    LEGS["openssl"] = "SKIPPED (%s cannot sign raw Ed25519 with pkeyutl)" % (version or "this openssl")
                    self.skipTest("UNQUALIFIED: " + LEGS["openssl"])
                if not supported and length == 0:
                    # Some builds cannot read an empty input; that is not a missing algorithm.
                    notes = "; the empty message was not signed by this openssl"
                    continue
                self.assertTrue(supported, "openssl signed the first message and then failed at length %d" % length)
                triples.append((public.stdout[12:], message, Path(out).read_bytes()))
        self.check(triples, random.Random(int.from_bytes(os.urandom(8), "big")))
        LEGS["openssl"] = "ran (%d triples, %d mutations%s)" % (len(triples), 2 * len(triples), notes)


class FinalLine(unittest.TestCase):
    """The last line of this file says how much of it ran: PASS is for all of it, signed by node."""
    NODE = "ran (200 triples, 400 mutations)"
    OPENSSL = "ran (10 triples, 20 mutations)"
    TIMED = {"cpu": 2.0, "elapsed": 2.5}
    TIMING_TEXT = "one verification: median 2.00 ms processor time, 2.50 ms elapsed"

    def run_file(self, *selection, path=None):
        """Run this file in a child process; its exit status and the last line it printed."""
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        if path is not None:
            environment["PATH"] = path
        child = subprocess.run([sys.executable, str(Path(__file__).resolve())] + list(selection),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300,
                               env=environment)
        return child.returncode, child.stdout.strip().splitlines()[-1], child.stdout

    def test_all_of_it_with_node_is_a_pass(self):
        self.assertEqual(final_line(40, 40, {"node": self.NODE, "openssl": self.OPENSSL}, self.TIMED), (
            0, "PASS: Ed25519 verification (node differential %s; openssl differential %s; %s)"
            % (self.NODE, self.OPENSSL, self.TIMING_TEXT)))

    def test_an_openssl_that_cannot_sign_is_named_and_the_file_still_passes(self):
        skipped = "SKIPPED (LibreSSL 3.3.6 cannot sign raw Ed25519 with pkeyutl)"
        self.assertEqual(final_line(40, 40, {"node": self.NODE, "openssl": skipped}, self.TIMED), (
            0, "PASS: Ed25519 verification (node differential %s; openssl differential %s; %s)"
            % (self.NODE, skipped, self.TIMING_TEXT)))

    def test_a_selection_is_partial_however_well_it_went(self):
        self.assertEqual(final_line(4, 40, {"node": None, "openssl": None}, {}), (
            0, "PARTIAL: Ed25519 verification, 4 of 40 tests selected (node differential not selected; "
               "openssl differential not selected; timing not selected)"))
        # Even the node leg alone is not the file.
        status, line = final_line(1, 40, {"node": self.NODE, "openssl": None}, {})
        self.assertEqual((status, line[:44]), (0, "PARTIAL: Ed25519 verification, 1 of 40 tests"))
        self.assertIn("openssl differential not selected", line)
        # Nor is every test of a file that has lost its node leg.
        self.assertEqual(final_line(40, 40, {"node": None, "openssl": self.OPENSSL}, self.TIMED)[1][:8], "PARTIAL:")
        # And one test left out is enough.
        self.assertTrue(final_line(39, 40, {"node": self.NODE, "openssl": self.OPENSSL}, self.TIMED)[1].startswith(
            "PARTIAL: Ed25519 verification, 39 of 40 tests selected (node differential ran"))

    def test_without_node_it_is_unqualified_and_fails(self):
        missing = "SKIPPED (node is not installed)"
        for ran in (40, 1):
            status, line = final_line(ran, 40, {"node": missing, "openssl": self.OPENSSL}, self.TIMED)
            self.assertEqual(status, 1)
            self.assertTrue(line.startswith("UNQUALIFIED: Ed25519 verification (node differential " + missing), line)

    def test_this_file_run_in_part_says_partial(self):
        status, line, output = self.run_file("LoadPublicKey")
        self.assertEqual(status, 0, output)
        counted = re.fullmatch(r"PARTIAL: Ed25519 verification, (\d+) of (\d+) tests selected \(node differential "
                               r"not selected; openssl differential not selected; timing not selected\)", line)
        self.assertIsNotNone(counted, line)
        self.assertLess(int(counted.group(1)), int(counted.group(2)))
        self.assertEqual(int(counted.group(2)), whole_file(), "of every test the file holds")
        self.assertNotIn("PASS", output)

    def test_this_file_run_with_a_pattern_says_partial(self):
        # -k narrows the very loader unittest.main uses, so the whole file is
        # counted with a loader of its own.
        status, line, output = self.run_file("-k", "LoadPublicKey")
        self.assertEqual(status, 0, output)
        counted = re.fullmatch(r"PARTIAL: Ed25519 verification, (\d+) of (\d+) tests selected \(node differential "
                               r"not selected; openssl differential not selected; timing not selected\)", line)
        self.assertIsNotNone(counted, line)
        self.assertLess(int(counted.group(1)), int(counted.group(2)))
        self.assertEqual(int(counted.group(2)), whole_file(), "of every test the file holds")
        self.assertNotIn("PASS", output)

    def test_this_file_run_where_node_is_missing_says_unqualified(self):
        with tempfile.TemporaryDirectory(prefix="com8-ed25519-no-tools-") as empty:
            status, line, output = self.run_file("NodeDifferential", path=empty)
        self.assertEqual(status, 1, output)
        self.assertTrue(line.startswith("UNQUALIFIED: Ed25519 verification (node differential SKIPPED (node is not "
                                        "installed); openssl differential not selected"), line)
        self.assertNotIn("PASS", output)


def final_line(ran, total, legs, timing):
    """The exit status and last line of a run in which no test failed.

    PASS is for the whole file with the node leg run. A selection of its
    tests is PARTIAL, and a run in which node was wanted and missing is
    UNQUALIFIED and fails. Either way the line names the legs that did not run.
    """
    detail = "(%s; %s)" % (
        "; ".join("%s differential %s" % (name, state or "not selected") for name, state in legs.items()),
        "one verification: median %.2f ms processor time, %.2f ms elapsed" % (timing["cpu"], timing["elapsed"])
        if timing else "timing not selected")
    if (legs["node"] or "").startswith("SKIPPED"):
        # The node signer is the required independent implementation.
        return 1, "UNQUALIFIED: Ed25519 verification " + detail
    if ran < total or legs["node"] is None:
        return 0, "PARTIAL: Ed25519 verification, %d of %d tests selected %s" % (ran, total, detail)
    return 0, "PASS: Ed25519 verification " + detail


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
    status, line = final_line(program.result.testsRun, whole_file(), LEGS, TIMING)
    if status:
        sys.exit(line)
    print(line)


if __name__ == "__main__":
    main()
