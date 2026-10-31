"""Audio analysis for CrateMatch: BPM, musical key, energy, spectral character.

Results are cached in SQLite so rescans are instant.
"""
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time

import numpy as np

# Krumhansl-Schmuckler key profiles
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Open Key / Camelot wheel
CAMELOT = {
    "C major": "8B", "G major": "9B", "D major": "10B", "A major": "11B",
    "E major": "12B", "B major": "1B", "F# major": "2B", "Db major": "3B",
    "Ab major": "4B", "Eb major": "5B", "Bb major": "6B", "F major": "7B",
    "A minor": "8A", "E minor": "9A", "B minor": "10A", "F# minor": "11A",
    "C# minor": "12A", "G# minor": "1A", "Eb minor": "2A", "Bb minor": "3A",
    "F minor": "4A", "C minor": "5A", "G minor": "6A", "D minor": "7A",
}
# normalize enharmonics to the spellings above
ENHARMONIC = {"D#": "Eb", "G#": "Ab", "A#": "Bb"}


def detect_key(chroma_mean):
    """Krumhansl template matching on a 12-bin chroma vector."""
    best, best_name, best_corr = -2, "?", -2
    for shift in range(12):
        root = NOTES[shift]
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = np.roll(profile, shift)
            corr = np.corrcoef(chroma_mean, rotated)[0, 1]
            if np.isnan(corr):
                continue
            if corr > best_corr:
                best_corr = corr
                disp_root = ENHARMONIC.get(root, root)
                best_name = f"{disp_root} {mode}"
    return best_name


def camelot_of(key_name):
    return CAMELOT.get(key_name, "?")




def _ffmpeg_bin():
    """Locate an ffmpeg executable: bundled-with-app first, then PATH."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "ffmpeg"))
        candidates.append(os.path.join(
            os.path.dirname(sys.executable), "..", "Frameworks", "ffmpeg"))
        candidates.append(os.path.join(
            os.path.dirname(sys.executable), "ffmpeg"))
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return shutil.which("ffmpeg")


def analyze_file(path):
    """Analyze an audio file. Returns dict or None on failure."""
    import subprocess
    import tempfile
    try:
        import librosa  # lazy: a broken audio stack must not kill app startup

        # Decode step 1: hand the file straight to librosa/soundfile.
        # Modern libsndfile reads wav/flac/mp3/ogg natively — no ffmpeg
        # needed. (The old code shelled out to a bare "ffmpeg" first,
        # which inside the .app bundle isn't on PATH, so EVERY analysis
        # silently failed and scans indexed 0 tracks.)
        y = sr = None
        try:
            y, sr = librosa.load(path, sr=22050, mono=True, duration=150)
        except Exception:
            y = sr = None
        if y is None:
            # Decode step 2 (m4a/aac and anything soundfile can't read):
            # convert with ffmpeg — bundled copy first, PATH copy second.
            ffmpeg = _ffmpeg_bin()
            if not ffmpeg:
                return None
            with tempfile.NamedTemporaryFile(suffix=".wav",
                                             delete=False) as tmp:
                wav_path = tmp.name
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", path,
                 "-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
                check=True)
            try:
                y, sr = librosa.load(wav_path, sr=22050, mono=True,
                                     duration=150)
            finally:
                os.unlink(wav_path)
        if len(y) < sr * 5:
            return None
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = round(float(np.asarray(tempo).flat[0]), 1)

        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        key_name = detect_key(chroma.mean(axis=1))

        rms = float(librosa.feature.rms(y=y).mean())
        centroid = float(librosa.feature.spectral_centroid(y=y, sr=sr).mean())
        zcr = float(librosa.feature.zero_crossing_rate(y).mean())
        duration = float(librosa.get_duration(y=y, sr=sr))

        return {
            "bpm": bpm,
            "key": key_name,
            "camelot": camelot_of(key_name),
            "energy": rms,
            "centroid": centroid,
            "zcr": zcr,
            "duration": duration,
        }
    except Exception:
        return None


# ---------- cache ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    path TEXT PRIMARY KEY,
    artist TEXT, title TEXT,
    bpm REAL, key_name TEXT, camelot TEXT,
    energy REAL, centroid REAL, zcr REAL, duration REAL,
    mtime REAL, analyzed_at REAL,
    genres TEXT DEFAULT '', year INTEGER
)"""


class Library:
    def __init__(self, db_path):
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False,
                                    timeout=30)
        self.conn.execute(SCHEMA)
        # migrate DBs created before genre/year support (the pocket
        # layer is blind without them)
        cols = [r[1] for r in
                self.conn.execute("PRAGMA table_info(tracks)").fetchall()]
        if "genres" not in cols:
            self.conn.execute(
                "ALTER TABLE tracks ADD COLUMN genres TEXT DEFAULT ''")
        if "year" not in cols:
            self.conn.execute(
                "ALTER TABLE tracks ADD COLUMN year INTEGER")
        self.conn.commit()

    def get(self, path):
        cur = self.conn.execute("SELECT * FROM tracks WHERE path = ?", (path,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def upsert(self, path, artist, title, a, genres="", year=None):
        self.conn.execute(
            """INSERT OR REPLACE INTO tracks
               (path, artist, title, bpm, key_name, camelot, energy,
                centroid, zcr, duration, mtime, analyzed_at, genres, year)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (path, artist, title, a["bpm"], a["key"], a["camelot"],
             a["energy"], a["centroid"], a["zcr"], a["duration"],
             os.path.getmtime(path), time.time(), genres or "", year))
        self.conn.commit()

    def delete(self, path):
        """Remove a track from the library (junk/mislabeled files)."""
        self.conn.execute("DELETE FROM tracks WHERE path = ?", (path,))
        self.conn.commit()

    def set_tags(self, path, genres, year):
        """Backfill genre/year on an already-analyzed row (cheap: no
        audio re-analysis, used when a rescan finds tags missing)."""
        if not genres and not year:
            return
        self.conn.execute(
            "UPDATE tracks SET genres = COALESCE(NULLIF(?, ''), genres), "
            "year = COALESCE(?, year) WHERE path = ?",
            (genres or "", year, path))
        self.conn.commit()

    def all_tracks(self):
        cur = self.conn.execute("SELECT * FROM tracks ORDER BY artist, title")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def remove_missing(self, existing_paths):
        """Drop rows whose files no longer exist."""
        existing = set(existing_paths)
        cur = self.conn.execute("SELECT path FROM tracks")
        for (p,) in cur.fetchall():
            if p not in existing:
                self.conn.execute("DELETE FROM tracks WHERE path = ?", (p,))
        self.conn.commit()


AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".wav", ".aac", ".aiff", ".ogg"}


# Folder names are a real genre signal in Keith's library — he files
# music in folders like "october 2026 rnb". Deepest matching folder
# name wins. Token-based so "rb" inside other words can't false-fire.
_FOLDER_GENRES = [
    ({"rnb", "r&b", "randb"}, "R&B"),
    ({"dancehall"}, "Dancehall"),
    ({"soca"}, "Soca"),
    ({"reggaeton"}, "Reggaeton"),
    ({"lovers", "rock"}, "Lovers Rock"),
    ({"reggae"}, "Reggae"),
    ({"hiphop", "rap"}, "Hip-Hop"),
    ({"afrobeats", "afrobeat"}, "Afrobeats"),
    ({"amapiano"}, "Amapiano"),
    ({"kompa", "zouk"}, "Kompa"),
    ({"house"}, "House"),
    ({"techno"}, "Techno"),
    ({"disco"}, "Disco"),
    ({"funk"}, "Funk"),
    ({"motown"}, "Motown"),
    ({"soul"}, "Soul"),
    ({"gospel"}, "Gospel"),
    ({"jazz"}, "Jazz"),
    ({"latin"}, "Latin"),
    ({"country"}, "Country"),
    ({"rock"}, "Rock"),
    ({"pop"}, "Pop"),
]


def folder_genre(path):
    """Genre implied by the file's folder names, or "" if none."""
    parts = os.path.dirname(path).split(os.sep)
    for part in reversed(parts):  # deepest folder first
        tokens = set(re.split(r"[^a-z0-9&]+", part.lower())) - {""}
        if "hip" in tokens and "hop" in tokens:
            return "Hip-Hop"
        for keys, genre in _FOLDER_GENRES:
            if keys & tokens:
                return genre
    return ""


def scan_folder(lib, folder, artist_title_fn, progress_cb=None):
    """Walk folder, analyze new/changed files.

    Returns (added, total_found, failed) — failed counts files that were
    found but could not be analyzed, so the UI can tell "empty folder"
    apart from "every file failed".
    """
    files = []
    for root, _, names in os.walk(folder):
        for n in names:
            if os.path.splitext(n)[1].lower() in AUDIO_EXTS:
                files.append(os.path.join(root, n))
    lib.remove_missing(files)
    added = 0
    failed = 0
    for i, path in enumerate(files):
        if progress_cb:
            progress_cb(i + 1, len(files), os.path.basename(path))
        row = lib.get(path)
        mtime = os.path.getmtime(path)
        # tag reader may return (artist, title) or
        # (artist, title, genres, year) — normalize
        meta = artist_title_fn(path)
        artist, title = meta[0], meta[1]
        genres = meta[2] if len(meta) > 2 else ""
        year = meta[3] if len(meta) > 3 else None
        if not genres:
            genres = folder_genre(path)  # his folders are genre-filed
        if row and abs(row["mtime"] - mtime) < 1:
            # unchanged audio: still backfill genre/year if the row
            # predates tag support, so a rescan fixes old libraries
            if not row.get("genres") and (genres or year):
                lib.set_tags(path, genres, year)
            continue
        a = analyze_file(path)
        if not a:
            failed += 1
            continue
        lib.upsert(path, artist, title, a, genres, year)
        added += 1
    return added, len(files), failed
