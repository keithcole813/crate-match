"""Serato crate membership index for CrateMatch.

Keith's crates are hand-curated by vibe, so "shares a crate with the
track that's playing" is the strongest match signal the app has —
stronger than any tag. This module finds his .crate files and builds
lookup maps from track path -> set of crate names.

Serato keeps crates in <Music>/_Serato_/Subcrates (older: /Crates),
and per-volume _Serato_ folders on external drives.
"""
import os

from crate_io import parse_crate


def _default_roots():
    music = os.path.expanduser("~/Music")
    roots = [
        os.path.join(music, "_Serato_", "Subcrates"),
        os.path.join(music, "_Serato_", "Crates"),
    ]
    # Keith's music (and Serato library) live on an external drive —
    # Serato keeps a _Serato_ folder at the root of every volume.
    import glob as _glob
    for pat in ("/Volumes/*/_Serato_/Subcrates",
                "/Volumes/*/_Serato_/Crates"):
        roots.extend(sorted(_glob.glob(pat)))
    return roots


def crate_name_from_filename(fn):
    name = fn[:-len(".crate")] if fn.lower().endswith(".crate") else fn
    # Serato encodes subcrate nesting as Parent%%Child
    return name.replace("%%", " / ").strip()


def _norm(p):
    return os.path.normpath(p).lower() if p else ""


def load_crate_index(extra_roots=None):
    """Return (by_path, by_base, crate_count).

    by_path: {normalized absolute path: set(crate names)}
    by_base: {basename.lower(): set(crate names)} — fallback when the
    crate's stored path differs from the scanned path (moved files).
    """
    roots = _default_roots() + list(extra_roots or [])
    by_path, by_base = {}, {}
    crates = 0
    seen_files = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for fn in os.listdir(root):
            if not fn.lower().endswith(".crate"):
                continue
            full = os.path.join(root, fn)
            if full in seen_files:
                continue
            seen_files.add(full)
            try:
                paths = parse_crate(full)
            except Exception:
                continue
            crates += 1
            name = crate_name_from_filename(fn)
            for p in paths:
                if not p:
                    continue
                # Serato stores paths without the leading slash
                # ('Users/keith/...'); library paths have it — align
                if not p.startswith("/"):
                    p = "/" + p
                by_path.setdefault(_norm(p), set()).add(name)
                by_base.setdefault(os.path.basename(p).lower(),
                                   set()).add(name)
    return by_path, by_base, crates


def crates_for(index, path):
    """Set of crate names a track belongs to (empty set if none)."""
    if not index or not path:
        return set()
    by_path, by_base = index[0], index[1]
    hit = by_path.get(_norm(path))
    if hit:
        return hit
    return by_base.get(os.path.basename(path).lower(), set())
