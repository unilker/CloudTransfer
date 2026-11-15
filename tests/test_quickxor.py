"""QuickXorHash correctness, including streaming equivalence."""

from __future__ import annotations

import base64
import os

import pytest

from cloudtransfer.quickxor import QuickXorHash, quickxorhash

MASK = (1 << 160) - 1


def naive_quickxor(data: bytes) -> str:
    """A second, independent implementation.

    Written straight from the algorithm - a flat 160-bit register where byte
    ``i`` is XORed in rotated left by ``(i * 11) mod 160`` - sharing no code
    with the production port. Agreement between the two is the real test.
    """
    acc = 0
    for i, byte in enumerate(data):
        shifted = byte << ((i * 11) % 160)
        acc ^= (shifted & MASK) | (shifted >> 160)
    out = bytearray((acc & MASK).to_bytes(20, "little"))
    for i, byte in enumerate((len(data) & 0xFFFF_FFFF_FFFF_FFFF).to_bytes(8, "little")):
        out[12 + i] ^= byte
    return base64.b64encode(bytes(out)).decode("ascii")


def test_empty_input_is_all_zeroes():
    assert quickxorhash(b"") == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_digest_is_160_bits():
    h = QuickXorHash()
    h.update(b"anything")
    assert len(h.digest()) == 20


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"a",
        b"hello world",
        bytes(range(256)),
        b"x" * 159,   # one byte short of a full rotation
        b"y" * 160,   # exactly one rotation
        b"z" * 161,   # one byte past it
        b"\x00" * 64,
        b"\xff" * 200,
    ],
)
def test_matches_independent_implementation(data):
    assert quickxorhash(data) == naive_quickxor(data)


@pytest.mark.parametrize("size", [1, 7, 63, 64, 65, 159, 160, 161, 320, 1000, 65537])
def test_matches_independent_implementation_on_random_sizes(size):
    data = os.urandom(size)
    assert quickxorhash(data) == naive_quickxor(data)


def test_streaming_matches_one_shot():
    """Chunk boundaries must not change the result; the relay hashes in pieces."""
    import random

    random.seed(7)
    data = os.urandom(250_000)

    streamed = QuickXorHash()
    position = 0
    while position < len(data):
        step = random.randint(1, 40_000)
        streamed.update(data[position : position + step])
        position += step

    assert streamed.b64digest() == quickxorhash(data)
    assert streamed.b64digest() == naive_quickxor(data)


def test_length_is_part_of_the_hash():
    """Trailing zero bytes must change the digest, or padding would be invisible."""
    assert quickxorhash(b"abc") != quickxorhash(b"abc\x00")


def test_empty_updates_are_ignored():
    h = QuickXorHash()
    h.update(b"")
    h.update(b"data")
    h.update(b"")
    assert h.b64digest() == quickxorhash(b"data")


def test_hexdigest_agrees_with_digest():
    h = QuickXorHash()
    h.update(b"consistency")
    assert h.hexdigest() == h.digest().hex()
    assert base64.b64decode(h.b64digest()) == h.digest()
