"""Hashing tests, including the phase's correctness gate.

The gate is the first two tests. Everything else in this project is verifiable by
looking at the screen; a wrong hash variant is not. Every log line would still render,
still be 32 hex characters, still look plausible -- and be wrong. So the digest is
pinned against the reference implementation on fixed vectors.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from backend.domain.hashing import DIGEST_HEX_LENGTH, Xxh3_128Hasher

# Digests produced by the reference CLI `xxhsum -H128` (xxHash 0.8.2) on these exact
# byte vectors. Hard-coded so the gate still runs where xxhsum is not installed.
REFERENCE_VECTORS: list[tuple[bytes, str]] = [
    (b"", "99aa06d3014798d86001c324468d497f"),
    (b"SignalScope", "fa121aee6ddc6006a34dcfe79a9e1d0b"),
    (bytes(((i * 37 + 11) % 256) for i in range(1000)) * 80, "fe1e1dcc59af93f02bb52a6ae97ed9cd"),
]


@pytest.fixture
def hasher() -> Xxh3_128Hasher:
    return Xxh3_128Hasher()


@pytest.mark.parametrize(("payload", "expected"), REFERENCE_VECTORS)
def test_matches_reference_xxh3_128_digests(hasher, payload, expected):
    """CORRECTNESS GATE: our digest is XXH3_128, not some neighbouring variant."""
    assert hasher.hash(payload) == expected


@pytest.mark.skipif(shutil.which("xxhsum") is None, reason="xxhsum CLI not installed")
def test_cross_checked_against_live_xxhsum_cli(hasher, tmp_path):
    """Same gate, run against the actual reference binary when it is available.

    This is what keeps the hard-coded vectors above honest: if someone regenerates them
    from our own implementation, this test still compares against an outside authority.
    """
    payload = bytes(range(256)) * 313  # 80 128 bytes, close to a real frame
    target = tmp_path / "frame.bin"
    target.write_bytes(payload)

    result = subprocess.run(
        ["xxhsum", "-H128", str(target)],
        capture_output=True,
        text=True,
        check=True,
    )
    reference_digest = result.stdout.split()[0].strip().lower()

    assert hasher.hash(payload) == reference_digest


def test_digest_is_32_lowercase_hex_chars(hasher):
    """The brief's example log line shows a 32-character digest."""
    digest = hasher.hash(b"\x01\x02\x03\x04" * 20_000)
    assert len(digest) == DIGEST_HEX_LENGTH
    assert digest == digest.lower()
    assert all(c in "0123456789abcdef" for c in digest)


def test_hashes_short_and_malformed_payloads_without_complaint(hasher):
    """Assumption #3: hashing happens before and independently of validation, so a
    corrupted frame is still fingerprintable and therefore still diagnosable."""
    assert len(hasher.hash(b"\x01\x02\x03")) == DIGEST_HEX_LENGTH  # not a whole sample
    assert len(hasher.hash(b"\x00" * 79_996)) == DIGEST_HEX_LENGTH  # short frame


def test_is_deterministic_and_sensitive_to_a_single_bit(hasher):
    payload = bytearray(b"\x00\x00\x00\x01" * 20_000)
    first = hasher.hash(bytes(payload))
    assert first == hasher.hash(bytes(payload))

    payload[40_000] ^= 0x01
    assert hasher.hash(bytes(payload)) != first
