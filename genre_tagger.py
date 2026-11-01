"""Batch-tag the CrateMatch library with genres from MusicBrainz.

MusicBrainz is free and needs no API key. Rate limit is 1 request/second,
which this script respects. Results are cached in the SQLite library so
re-runs are instant and only new/untagged tracks are queried.

Usage:
    python genre_tagger.py [--limit N] [--force] [--db PATH]

Adds `genres` ("rock;classic rock") and `year` columns to the tracks table.
"""
import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request

DB_DEFAULT = None  # resolved from analyzer default below
UA = "CrateMatch/1.0 ( https://github.com/keithcole813/crate-match )"


def ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()}
    if "genres" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN genres TEXT DEFAULT ''")
    if "year" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN year INTEGER")
    conn.commit()


def mb_search(artist, title, timeout=15):
    """Query MusicBrainz recording search. Returns (genres, year) or (None, None)."""
    q = f'recording:"{title}" AND artist:"{artist}"'
    url = ("https://musicbrainz.org/ws/2/recording/?query=" +
           urllib.parse.quote(q) + "&fmt=json&limit=5")
    try:
        from netutil import get_json
        data = get_json(url, UA, timeout=timeout)
    except Exception:
        return None, None
    recs = data.get("recordings") or []
    if not recs:
        return None, None
    # aggregate tags across top hits, weighted by score
    tag_counts = {}
    year = None
    for r in recs[:3]:
        for t in r.get("tags") or []:
            name = (t.get("name") or "").strip().lower()
            if name:
                tag_counts[name] = tag_counts.get(name, 0) + int(t.get("count", 1))
        if year is None:
            frd = r.get("first-release-date") or ""
            if len(frd) >= 4 and frd[:4].isdigit():
                year = int(frd[:4])
    genres = sorted(tag_counts, key=lambda k: -tag_counts[k])[:6]
    return (";".join(genres) if genres else None), year


def tag_library(db_path, limit=None, force=False, progress_cb=None):
    """Tag untagged tracks. Returns (tagged, skipped, failed)."""
    from analyzer import Library
    lib = Library(db_path)
    ensure_columns(lib.conn)
    cur = lib.conn.execute(
        "SELECT path, artist, title FROM tracks ORDER BY artist, title")
    rows = cur.fetchall()
    tagged, skipped, failed = 0, 0, 0
    for i, (path, artist, title) in enumerate(rows):
        if limit and i >= limit:
            break
        if not force:
            cur2 = lib.conn.execute(
                "SELECT genres FROM tracks WHERE path = ?", (path,))
            g = cur2.fetchone()[0]
            if g:
                skipped += 1
                continue
        if progress_cb:
            progress_cb(i + 1, len(rows), f"{artist} - {title}")
        genres, year = mb_search(artist or "", title or "")
        time.sleep(1.0)  # MusicBrainz rate limit
        if genres or year:
            lib.conn.execute(
                "UPDATE tracks SET genres = COALESCE(?, genres), year = COALESCE(?, year) WHERE path = ?",
                (genres or "", year, path))
            lib.conn.commit()
            tagged += 1
        else:
            # mark as attempted so we don't hammer MB every run
            lib.conn.execute(
                "UPDATE tracks SET genres = '' WHERE path = ?", (path,))
            lib.conn.commit()
            failed += 1
    return tagged, skipped, failed


def main():
    import os
    ap = argparse.ArgumentParser(description="Tag CrateMatch library with MusicBrainz genres")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="re-tag even already-tagged tracks")
    ap.add_argument("--db", default=os.path.expanduser("~/.crate-match/library.db"))
    args = ap.parse_args()

    def prog(i, n, name):
        print(f"[{i}/{n}] {name[:60]}", flush=True)

    tagged, skipped, failed = tag_library(args.db, args.limit, args.force, prog)
    print(f"done: {tagged} tagged, {skipped} already tagged, {failed} no match")


if __name__ == "__main__":
    main()
