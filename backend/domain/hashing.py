"""Frame hashing.

The brief requires an XXH3_128 digest of every frame's raw payload. That obligation is
hard: 100 frames/s, no sampling, no skipping.

``xxhash.xxh128`` *is* XXH3 with a 128-bit output -- the ``xxhash`` package exposes the
same algorithm under both ``xxh128`` and ``xxh3_128`` aliases, and both agree with the
reference ``xxhsum -H128``. That equivalence is asserted by a test rather than trusted,
because shipping the wrong variant would silently invalidate every log line in the
primary deliverable and nothing in the UI would look wrong.

The :class:`FrameHasher` protocol exists so tests can inject a cheap stub instead of
hashing 80 kB buffers, and so the algorithm is one swappable line if the requirement
ever changes.
"""

from __future__ import annotations

from typing import Protocol

import xxhash

# XXH3_128 renders as 32 lowercase hex characters, as in the brief's example.
DIGEST_HEX_LENGTH = 32


class FrameHasher(Protocol):
    """Anything that can turn a payload into a hex digest string."""

    def hash(self, payload: bytes) -> str:  # pragma: no cover - structural only
        ...


class Xxh3_128Hasher:  # noqa: N801 - name mirrors the algorithm, XXH3_128
    """XXH3_128 over the raw bytes exactly as received.

    Hashing happens *before* and independently of validation: a short or malformed
    frame still gets a digest. A corrupted frame you cannot fingerprint is a corrupted
    frame you cannot diagnose, which defeats the purpose of logging it at all.
    """

    __slots__ = ()

    def hash(self, payload: bytes) -> str:
        return xxhash.xxh128(payload).hexdigest()
