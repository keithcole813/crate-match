"""Serato .crate read/write for CrateMatch.

Format: sequence of chunks; each chunk = 4-byte ASCII tag,
4-byte big-endian length, payload. Track paths live in
otrk > ptrk chunks as UTF-16BE strings.
"""
import os
import struct


def _chunk(tag, payload):
    return tag.encode("ascii") + struct.pack(">I", len(payload)) + payload


def _u16(s):
    return s.encode("utf-16-be")


def parse_crate(path):
    """Return list of track paths from a .crate file."""
    data = open(path, "rb").read()
    tracks = []

    def walk(buf):
        pos = 0
        while pos + 8 <= len(buf):
            tag = buf[pos:pos + 4]
            if not all(32 <= b < 127 for b in tag):
                break
            length = struct.unpack(">I", buf[pos + 4:pos + 8])[0]
            payload = buf[pos + 8:pos + 8 + length]
            tag_s = tag.decode("ascii")
            if tag_s == "ptrk":
                tracks.append(payload.decode("utf-16-be", errors="replace"))
            elif tag_s in ("otrk", "ovct", "osrt"):
                walk(payload)
            pos += 8 + length

    walk(data)
    return tracks


def write_crate(path, track_paths):
    """Write a Serato .crate file listing track_paths.

    Serato stores Mac paths like 'Users/keith/Music/track.mp3'
    (no leading slash in the ptrk payload).
    """
    out = _chunk("vrsn", _u16("1.0/Serato ScratchLive Crate"))
    for p in track_paths:
        # normalize to Serato-style relative-from-root path
        norm = p.replace("\\", "/")
        if norm.startswith("/"):
            norm = norm[1:]
        out += _chunk("otrk", _chunk("ptrk", _u16(norm)))
    with open(path, "wb") as f:
        f.write(out)
    return path


def verify_crate(path):
    """Re-parse what we wrote; return track count (0 = broken)."""
    try:
        return len(parse_crate(path))
    except Exception:
        return 0
