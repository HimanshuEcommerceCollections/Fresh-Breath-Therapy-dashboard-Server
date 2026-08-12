"""Application-minted, time-ordered identifiers.

Two reasons the import mints ids in Python rather than letting Postgres'
`gen_random_uuid()` default do it:

  * **No RETURNING round trip.** A parent's id has to be known before a child
    row referencing it can be built. Waiting for the database to hand it back
    costs a round trip per family, and at ~500ms each that is the difference
    between a batched insert and a per-row conversation.

  * **v7, not v4.** Random ids scatter inserts across the whole B-tree, so
    every insert dirties a different page — page splits, WAL amplification,
    and an index that no longer fits usefully in cache. A v7 has a 48-bit
    millisecond timestamp in front, so a bulk insert appends to the rightmost
    page instead.

The `gen_random_uuid()` column defaults stay exactly as they are. This changes
who supplies the id when the importer writes, not the schema.
"""
from __future__ import annotations

import os
import time
import uuid

# Guards against two ids minted inside the same millisecond coming out
# unordered — the random tail would decide, and monotonicity is the whole
# point of using v7.
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    """A time-ordered UUID (RFC 9562 version 7).

    Layout: 48 bits of Unix milliseconds, 4 bits version, 12 bits of a
    per-millisecond counter, 2 bits variant, 62 bits random.
    """
    global _last_ms, _counter

    ms = int(time.time() * 1000)
    if ms == _last_ms:
        _counter += 1
    else:
        _last_ms, _counter = ms, 0

    # 12 bits of counter live in rand_a; if a single millisecond somehow
    # produces more than 4096 ids, fall back to random rather than colliding.
    seq = _counter if _counter < 0x1000 else int.from_bytes(os.urandom(2), "big") & 0x0FFF

    raw = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    raw[6] = 0x70 | (seq >> 8)           # version 7 + high nibble of counter
    raw[7] = seq & 0xFF                  # low byte of counter
    raw[8] = (raw[8] & 0x3F) | 0x80      # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))
