#!/usr/bin/env python3
"""CrateMatch Live — Qt edition (v1.3.0).

Same brain as the Tk build (library, pocket/crate/history matching,
Serato watcher, tag fill, crate send, Drop folder) — new window in Qt
because Tk's drag-out on macOS never offers files in a form Serato
accepts, while Qt uses the real NSDragging path with file URLs.
Drag any result straight onto a Serato deck; drop files onto the
window to add them to the library.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import traceback

from PySide6.QtCore import QMimeData, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDrag, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QMenu, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from analyzer import Library, scan_folder, analyze_file
from crate_io import write_crate
from crates import load_crate_index
from history import HistoryGraph
from pocket import classify_pocket, find_matches, format_key
from serato_watcher import SeratoWatcher
from cratetube_bridge import bridge_for_seed

VERSION = "1.3.1"
DB_PATH = os.path.expanduser("~/.crate-match/library.db")
SETTINGS_PATH = os.path.expanduser("~/.cratematch_settings.json")
ERROR_LOG = os.path.expanduser("~/cratematch-live-error.log")

# Serato-ish dark palette
BG = "#1a1a1a"
ROW_BG = "#2d2d2d"
ROW_ALT = "#262626"
FG = "#f5f5f5"
DIM = "#9a9a9a"
GOLD = "#f0cd6e"
HEADER_BG = "#111111"

APP_STYLE = f"""
QMainWindow, QDialog, QWidget {{ background: {BG}; color: {FG};
    font-family: Helvetica; font-size: 13px; }}
QLabel.dim {{ color: {DIM}; font-size: 11px; }}
QLabel.gold {{ color: {GOLD}; font-size: 14px; font-weight: bold; }}
QPushButton {{ background: {ROW_BG}; color: {FG}; border: 0;
    padding: 6px 10px; }}
QPushButton:hover {{ background: #3a3a3a; }}
QComboBox {{ background: {ROW_BG}; color: {FG}; border: 0;
    padding: 4px; }}
QTableWidget {{ background: {ROW_BG}; color: {FG}; border: 0;
    gridline-color: transparent; }}
QTableWidget::item:selected {{ background: #4a3f22; }}
QHeaderView::section {{ background: {HEADER_BG}; color: {GOLD};
    border: 0; padding: 4px; font-weight: bold; }}
"""


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def artist_title_from_file(path):
    """(artist, title, genres, year) from the file's own tags,
    filename fallback for artist/title."""
    artist = title = None
    genres, year = "", None
    try:
        from mutagen import File as MutagenFile
        m = MutagenFile(path, easy=True)
        if m:
            artist = (m.get("artist") or [None])[0]
            title = (m.get("title") or [None])[0]
            g = (m.get("genre") or [None])[0]
            if g:
                genres = str(g).strip()
            d = (m.get("date") or [None])[0]
            if d:
                digits = "".join(ch for ch in str(d) if ch.isdigit())
                if len(digits) >= 4:
                    year = int(digits[:4])
    except Exception:
        pass
    if artist and title:
        return artist, title, genres, year
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d+\s+", "", stem)
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip(), genres, year
    return "Unknown Artist", stem, genres, year


class MatchTable(QTableWidget):
    """Results table whose drags carry REAL file URLs (the whole
    point of the Qt rebuild: Serato accepts these)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path_for_row = lambda row: None  # set by the window

    def mimeData(self, items):
        paths = []
        seen_rows = set()
        for it in items:
            r = it.row()
            if r in seen_rows:
                continue
            seen_rows.add(r)
            p = self.path_for_row(r)
            if p:
                paths.append(p)
        md = QMimeData()
        if paths:
            md.setUrls([QUrl.fromLocalFile(p) for p in paths])
            md.setText("\n".join(paths))
        return md


class DownloadDialog(QDialog):
    """One-click YouTube MP3 download for unowned pocket suggestions."""

    def __init__(self, parent, suggestions, download_dir):
        super().__init__(parent)
        self.parent_win = parent
        self.suggestions = suggestions
        self.download_dir = download_dir
        self.setWindowTitle("Download missing tracks")
        self.resize(480, 440)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        lay = QVBoxLayout(self)
        hdr = QLabel(f"{len(suggestions)} tracks not in your library")
        hdr.setProperty("class", "gold")
        hdr.setStyleSheet(f"color: {GOLD}; font-weight: bold;")
        lay.addWidget(hdr)

        dir_row = QHBoxLayout()
        self.dir_label = QLabel(f"📁 {download_dir or '(no folder chosen)'}")
        self.dir_label.setStyleSheet(f"color: {DIM};")
        dir_row.addWidget(self.dir_label, 1)
        ch = QPushButton("Change…")
        ch.clicked.connect(self._change_dir)
        dir_row.addWidget(ch)
        lay.addLayout(dir_row)

        self.table = QTableWidget(len(suggestions), 3)
        self.table.setHorizontalHeaderLabels(["Track", "Status", ""])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 300)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 40)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._row_btns = []
        for i, s in enumerate(suggestions):
            self.table.setItem(i, 0, QTableWidgetItem(
                f"{s['artist']} — {s['title']}"))
            self.table.setItem(i, 1, QTableWidgetItem(""))
            btn = QPushButton("⬇")
            btn.clicked.connect(lambda _=False, i=i: self._download(i))
            self.table.setCellWidget(i, 2, btn)
            self._row_btns.append(btn)
        lay.addWidget(self.table, 1)

        all_btn = QPushButton("Download all")
        all_btn.clicked.connect(self._download_all)
        lay.addWidget(all_btn)

    def _change_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Choose download folder")
        if d:
            self.download_dir = d
            s = load_settings()
            s["download_dir"] = d
            save_settings(s)
            self.dir_label.setText(f"📁 {d}")

    def _safe_filename(self, artist, title):
        base = f"{artist} - {title}".strip(" -")
        for ch in '/\\:*?"<>|':
            base = base.replace(ch, "")
        return base[:120] or "track"

    def set_row_status(self, i, text):
        item = self.table.item(i, 1)
        if item:
            item.setText(text)

    def _download(self, i):
        if not self.download_dir or not os.path.isdir(self.download_dir):
            QMessageBox.warning(self, "No folder",
                                "Pick a download folder first.")
            return
        self._row_btns[i].setEnabled(False)
        self.set_row_status(i, "⏳ downloading…")
        threading.Thread(target=self._worker,
                         args=(i, self.suggestions[i]),
                         daemon=True).start()

    def _download_all(self):
        for i in range(len(self.suggestions)):
            item = self.table.item(i, 1)
            if item and item.text() in ("", "❌ failed"):
                self._download(i)

    def _worker(self, i, sugg):
        artist, title = sugg["artist"], sugg["title"]
        query = f"{artist} {title}".strip()
        fname = self._safe_filename(artist, title) + ".mp3"
        outpath = os.path.join(self.download_dir, fname)
        q = self.parent_win.msg_q
        try:
            r = subprocess.run(
                ["yt-dlp", f"ytsearch1:{query}", "--print", "%(id)s",
                 "--no-playlist", "--quiet"],
                capture_output=True, text=True, timeout=60)
            vid = (r.stdout or "").strip().split("\n")[0].strip()
            if not vid or r.returncode != 0:
                raise RuntimeError("YouTube search found nothing")
            r = subprocess.run(
                ["yt-dlp", f"https://youtube.com/watch?v={vid}",
                 "-x", "--audio-format", "mp3", "--audio-quality", "0",
                 "-o", outpath, "--no-playlist", "--quiet", "--no-warnings"],
                capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not os.path.exists(outpath):
                err = (r.stderr or "").strip().split("\n")[-1][:120]
                raise RuntimeError(err or "download failed")
            q.put(("download_done", (outpath, artist, title)))
            q.put(("dl_status", (i, "✅ in library")))
            q.put(("status_text",
                   f"Downloaded: {artist} — {title}"))
        except FileNotFoundError:
            q.put(("dl_status", (i, "❌ no yt-dlp")))
            q.put(("status_text", "yt-dlp not found — pip install yt-dlp"))
        except Exception as e:
            q.put(("dl_status", (i, "❌ failed")))
            q.put(("status_text",
                   f"Download failed ({title}): {str(e)[:60]}"))


class LiveWindowQt(QMainWindow):
    COLS = ("title", "artist", "bpm", "key", "energy", "score")
    LABELS = {"title": "Title", "artist": "Artist", "bpm": "BPM",
              "key": "Key", "energy": "Energy", "score": "Score"}

    def __init__(self, db_path=DB_PATH):
        super().__init__()
        self.setWindowTitle(f"CrateMatch Live v{VERSION}")
        self.resize(460, 680)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAcceptDrops(True)

        self.lib = Library(db_path)
        self.tracks = self.lib.all_tracks()
        self.matches = []
        self.seed = None
        self.seed_pocket = None
        self._scanning = False
        self._last_now = None
        self.msg_q = queue.Queue()
        self._sort_col = "score"
        self._sort_rev = True
        _s = load_settings()
        self._min_setting = str(_s.get("min_score", "60"))
        self.crate_index = None
        self._tag_filling = False
        self._analyzing_seeds = set()
        self._last_crate_sig = None
        self._last_drop_sig = None
        self._drop_announced = False
        self._dl_dialog = None

        self._build()
        self._refresh_count_label()

        self.watcher = SeratoWatcher(self._on_watcher, interval=2.0)
        self.watcher.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(150)
        QTimer.singleShot(5000, self._start_tag_fill)

        self.history = None
        threading.Thread(target=self._build_history, daemon=True).start()
        threading.Thread(target=self._load_crates, daemon=True).start()

    # ================= UI =================
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)
        lay.setContentsMargins(10, 10, 10, 10)

        np_title = QLabel("NOW PLAYING")
        np_title.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        lay.addWidget(np_title)
        self.np_label = QLabel("Waiting for Serato…")
        self.np_label.setStyleSheet(
            f"color: {GOLD}; font-size: 14px; font-weight: bold;")
        self.np_label.setWordWrap(True)
        lay.addWidget(self.np_label)
        self.pocket_label = QLabel("")
        self.pocket_label.setStyleSheet(f"color: {DIM};")
        self.pocket_label.setWordWrap(True)
        lay.addWidget(self.pocket_label)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Show:"))
        self.count_box = QComboBox()
        self.count_box.addItems(["10", "15", "20", "25"])
        self.count_box.setCurrentText("15")
        self.count_box.currentTextChanged.connect(lambda *_: self._rematch())
        ctl.addWidget(self.count_box)
        ctl.addWidget(QLabel("Key:"))
        self.key_box = QComboBox()
        self.key_box.addItems(["camelot", "classic", "openkey"])
        self.key_box.currentTextChanged.connect(
            lambda *_: (self._render(), self._refresh_header()))
        ctl.addWidget(self.key_box)
        ctl.addWidget(QLabel("Min:"))
        self.min_box = QComboBox()
        self.min_box.addItems(["Off", "50", "55", "60", "65"])
        self.min_box.setCurrentText(self._min_setting)
        self.min_box.currentTextChanged.connect(
            lambda *_: self._on_min_changed())
        ctl.addWidget(self.min_box)
        ctl.addStretch(1)
        self.ct_btn = QPushButton("⬇ Fill pocket")
        self.ct_btn.clicked.connect(self._fill_pocket)
        self.ct_btn.setVisible(False)
        ctl.addWidget(self.ct_btn)
        self.crate_btn = QPushButton("➡ Send list to crate")
        self.crate_btn.clicked.connect(
            lambda: self._send_list_to_crate(manual=True))
        ctl.addWidget(self.crate_btn)
        self.scan_btn = QPushButton("📁 Scan library")
        self.scan_btn.clicked.connect(self._scan_library)
        ctl.addWidget(self.scan_btn)
        lay.addLayout(ctl)

        self.table = MatchTable()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(
            [self.LABELS[c] for c in self.COLS])
        widths = {"title": 150, "artist": 110, "bpm": 52, "key": 52,
                  "energy": 58, "score": 52}
        for i, c in enumerate(self.COLS):
            self.table.setColumnWidth(i, widths[c])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(
            QTableWidget.DragDropMode.DragOnly)
        self.table.path_for_row = \
            lambda r: getattr(self, "_row_paths", {}).get(r)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_by)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        lay.addWidget(self.table, 1)

        self.status_label = QLabel(
            "Drag a track onto a Serato deck · drop files here to add them")
        self.status_label.setStyleSheet(f"color: {DIM};")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)

    def _set_status(self, text):
        self.status_label.setText(text)

    # ================= data / threads =================
    def _refresh_count_label(self):
        if self.lib.count() == 0:
            self._set_status(
                "No tracks indexed yet — click 📁 Scan library "
                "and pick the folder your music is in")

    def _build_history(self):
        try:
            hg = HistoryGraph(self.lib.db_path)
            parsed, total = hg.sync()
            self.msg_q.put(("history_ready", (hg, parsed, total)))
        except Exception:
            pass

    def _load_crates(self):
        try:
            idx = load_crate_index()
            self.msg_q.put(("crates_ready", idx))
        except Exception:
            pass

    def _min_score(self):
        v = self.min_box.currentText() if hasattr(self, "min_box") \
            else self._min_setting
        if v == "Off":
            return 0.0
        try:
            return int(v) / 100.0
        except ValueError:
            return 0.60

    def _on_min_changed(self):
        s = load_settings()
        s["min_score"] = self.min_box.currentText()
        save_settings(s)
        self._rematch()

    def _refresh_header(self):
        if not self.seed:
            return
        seed = self.seed
        pk = classify_pocket(seed)
        bits = [f"Pocket: {pk['label']}"]
        if seed.get("bpm"):
            bits.append(f"{seed['bpm']:.0f} BPM")
        k = format_key(seed.get("camelot"), self.key_box.currentText())
        if k and k != "?":
            bits.append(f"Key {k}")
        self.pocket_label.setText(" · ".join(bits))

    def _start_tag_fill(self):
        if self._tag_filling:
            return
        self._tag_filling = True

        def run():
            tagged = 0
            try:
                from genre_tagger import mb_search
                import time as _time
                rows = self.lib.conn.execute(
                    "SELECT path, artist, title FROM tracks "
                    "WHERE genres IS NULL OR genres = '' "
                    "ORDER BY artist, title LIMIT 300").fetchall()
                total = len(rows)
                for i, (path, artist, title) in enumerate(rows):
                    if i and i % 10 == 0:
                        self.msg_q.put(
                            ("tag_status",
                             f"Filling missing tags from the internet: "
                             f"{i}/{total}…"))
                    genres, year = mb_search(artist or "", title or "")
                    _time.sleep(1.0)
                    if genres or year:
                        self.lib.set_tags(path, genres or "", year)
                        tagged += 1
            except Exception:
                pass
            self.msg_q.put(("tag_done", (tagged,)))

        threading.Thread(target=run, daemon=True).start()

    def _analyze_seed_path(self, path):
        if path in self._analyzing_seeds:
            return
        self._analyzing_seeds.add(path)

        def run():
            try:
                a = analyze_file(path)
                if a:
                    artist, title, genres, year = \
                        artist_title_from_file(path)
                    self.lib.upsert(path, artist, title, a, genres, year)
                    self.msg_q.put(("seed_ready", path))
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    # ================= scan =================
    def _scan_library(self):
        if self._scanning:
            return
        folder = QFileDialog.getExistingDirectory(
            self, "Pick your music folder (the big one, e.g. Music)",
            os.path.expanduser("~/Music"))
        if not folder:
            return
        self._scanning = True
        self.scan_btn.setEnabled(False)
        self._set_status(f"Scanning {folder}…")

        def run():
            try:
                def prog(i, n, name):
                    self.msg_q.put(("scan_status",
                                    f"Analyzing {i}/{n}: {name[:40]}"))
                added, total, failed = scan_folder(
                    self.lib, folder, artist_title_from_file, prog)
                self.msg_q.put(("scan_done", (added, total, failed, None)))
            except Exception:
                tb = traceback.format_exc()
                try:
                    with open(ERROR_LOG, "a") as f:
                        f.write("SCAN FAILED\n" + tb + "\n---\n")
                except Exception:
                    pass
                last = tb.strip().splitlines()[-1] if tb.strip() else "error"
                self.msg_q.put(("scan_done", (0, 0, 0, last)))

        threading.Thread(target=run, daemon=True).start()

    # ================= matching =================
    def _find_seed(self, now):
        if not now:
            return None
        spath = (now.get("path") or "").strip()
        for t in self.tracks:
            if t.get("path") == spath:
                return t
        base = os.path.basename(spath).lower()
        for t in self.tracks:
            if os.path.basename(t.get("path", "")).lower() == base and base:
                return t
        na = (now.get("artist") or "").strip().lower()
        nt = (now.get("title") or "").strip().lower()
        for t in self.tracks:
            if ((t.get("artist") or "").strip().lower() == na and
                    (t.get("title") or "").strip().lower() == nt and na and nt):
                return t
        return None

    def _on_watcher(self, now):
        self.msg_q.put(("now_playing", now))

    def _drain(self):
        # One bad message is logged and skipped; the pump cannot die
        # (the Tk build's freeze bug taught us that).
        while True:
            try:
                kind, payload = self.msg_q.get_nowait()
            except queue.Empty:
                return
            try:
                self._dispatch_msg(kind, payload)
            except Exception:
                try:
                    with open(ERROR_LOG, "a") as f:
                        f.write(traceback.format_exc() + "\n---\n")
                except Exception:
                    pass

    def _dispatch_msg(self, kind, payload):
        if kind == "now_playing":
            self._handle_now_playing(payload)
        elif kind == "status_text":
            self._set_status(payload)
        elif kind == "dl_status":
            if self._dl_dialog is not None:
                i, text = payload
                self._dl_dialog.set_row_status(i, text)
        elif kind == "scan_status":
            self._set_status(payload)
        elif kind == "scan_done":
            added, total, failed, err = payload
            self._scanning = False
            self.scan_btn.setEnabled(True)
            if err:
                self._set_status(f"Scan failed: {err}")
            elif total == 0:
                self._set_status(
                    "Scan done: no audio files found in that folder — "
                    "pick the folder your music is in")
            else:
                self.tracks = self.lib.all_tracks()
                msg = (f"Scan done: {self.lib.count()} tracks indexed "
                       f"({added} new)")
                if failed:
                    msg += f" — {failed} of {total} files failed"
                self._set_status(msg)
                self._start_tag_fill()
                self.seed = None
                if self._last_now:
                    self._handle_now_playing(self._last_now)
        elif kind == "download_done":
            path, artist, title = payload
            self._on_track_downloaded(path, artist, title)
        elif kind == "crates_ready":
            self.crate_index = payload
            if payload and payload[2]:
                self._set_status(
                    f"Serato crates loaded: {payload[2]} — "
                    f"crate-mates rank first")
            if self.seed:
                self._rematch()
        elif kind == "seed_ready":
            self.tracks = self.lib.all_tracks()
            self.seed = None
            if self._last_now:
                self._handle_now_playing(self._last_now)
        elif kind == "drop_done":
            self.tracks = self.lib.all_tracks()
            self._set_status(
                f"Added {payload} dropped track(s) — "
                f"{self.lib.count()} in library")
            if self.seed:
                self._rematch()
        elif kind == "tag_status":
            if not self._scanning:
                self._set_status(payload)
        elif kind == "tag_done":
            self._tag_filling = False
            if payload and payload[0]:
                self.tracks = self.lib.all_tracks()
                self._set_status(
                    f"Tag fill done: {payload[0]} tracks got "
                    f"genre/year from the internet")
                if self.seed:
                    if self._last_now:
                        self.seed = self._find_seed(self._last_now)
                    self._refresh_header()
                    self._rematch()
        elif kind == "history_ready":
            hg, parsed, total = payload
            self.history = hg
            s, e = hg.stats()
            self._set_status(f"History ready: {s} past sets indexed")
            if self.seed:
                self._rematch()

    def _handle_now_playing(self, now):
        if not now:
            self.np_label.setText("Waiting for Serato…")
            return
        self._last_now = now
        self.np_label.setText(
            f"{now.get('artist') or '?'} — {now.get('title') or '?'}")
        seed = self._find_seed(now)
        if not seed:
            spath = (now.get("path") or "").strip()
            if spath and os.path.isfile(spath):
                self.pocket_label.setText(
                    "Not in library — analyzing this track now…")
                self._analyze_seed_path(spath)
            else:
                self.pocket_label.setText(
                    "Track not in library — click 📁 Scan library "
                    "and pick your music folder")
            self.matches = []
            self._render()
            return
        if self.seed and seed.get("path") == self.seed.get("path"):
            return
        self.seed = seed
        self.seed_pocket = classify_pocket(seed)
        self._refresh_header()
        self._rematch()

    def _rematch(self):
        if not self.seed:
            return
        try:
            n = int(self.count_box.currentText())
        except ValueError:
            n = 15
        exclude = None
        if self.history is not None:
            try:
                self.history.sync()
                exclude = self.history.tonight_keys()
            except Exception:
                pass
        self.matches = find_matches(
            self.seed, self.tracks, top_n=n,
            history_graph=self.history, exclude_keys=exclude,
            crate_index=self.crate_index, min_score=self._min_score())
        self._render()
        ms = self._min_score()
        if ms > 0:
            if not self.matches:
                self._set_status(
                    f"No confident matches (min "
                    f"{self.min_box.currentText()}) — lower Min to see more")
            elif len(self.matches) < n:
                self._set_status(
                    f"{len(self.matches)} confident matches "
                    f"(min {self.min_box.currentText()})")
        self._send_list_to_crate(manual=False)
        self._refresh_drop_folder()
        try:
            bridge = bridge_for_seed(self.seed, self.lib.db_path)
            if bridge["thin"] and bridge["suggestions"]:
                self.ct_btn.setVisible(True)
                self._ct_suggestions = bridge["suggestions"]
                self._set_status(
                    f"Pocket thin ({bridge['library_count']} in library) — "
                    f"{len(bridge['suggestions'])} suggestions available")
            else:
                self.ct_btn.setVisible(False)
        except Exception:
            pass

    # ================= rendering =================
    def _energy_display(self, t):
        e = t.get("energy") or 0
        return f"{max(1, min(10, round(e * 40)))}"

    def _sort_by(self, col_idx):
        col = self.COLS[col_idx]
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col, self._sort_rev = col, col == "score"
        self._render()

    def _row_values(self, score, t):
        return (
            t.get("title") or "",
            t.get("artist") or "",
            f"{t['bpm']:.1f}" if t.get("bpm") else "",
            format_key(t.get("camelot"), self.key_box.currentText()),
            self._energy_display(t),
            f"{score * 100:.0f}",
        )

    def _render(self):
        key_idx = self.COLS.index(self._sort_col)

        def sort_val(item):
            score, t, _ = item
            v = self._row_values(score, t)[key_idx]
            try:
                return float(v)
            except (ValueError, TypeError):
                return str(v).lower()

        rows = sorted(self.matches, key=sort_val, reverse=self._sort_rev)
        self.table.setRowCount(len(rows))
        self._row_tracks = []
        self._row_paths = {}
        for i, (score, t, _label) in enumerate(rows):
            for j, v in enumerate(self._row_values(score, t)):
                item = QTableWidgetItem(str(v))
                if j >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, j, item)
            self._row_tracks.append(t)
            self._row_paths[i] = t.get("path")

    # ================= crate send + drop folder =================
    @staticmethod
    def _crate_target_path():
        music = os.path.expanduser("~/Music")
        for sub in ("Subcrates", "Crates"):
            d = os.path.join(music, "_Serato_", sub)
            if os.path.isdir(d):
                return os.path.join(d, "CrateMatch Live.crate")
        d = os.path.join(music, "_Serato_", "Subcrates")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "CrateMatch Live.crate")

    def _send_list_to_crate(self, manual=False):
        paths = [t.get("path") for (_s, t, _l) in self.matches
                 if t.get("path") and os.path.isfile(t.get("path"))]
        sig = tuple(paths)
        if not manual and sig == self._last_crate_sig:
            return
        self._last_crate_sig = sig
        try:
            write_crate(self._crate_target_path(), paths)
        except Exception as e:
            if manual:
                self._set_status(f"Crate send failed: {e}")
            return
        if manual:
            self._set_status(
                f"Sent {len(paths)} tracks to the "
                f"'CrateMatch Live' crate in Serato")

    @staticmethod
    def _drop_dir_for(seed_path):
        if seed_path and seed_path.startswith("/Volumes/"):
            vol = seed_path.split("/")[2]
            return os.path.join("/Volumes", vol, "CrateMatch Drop")
        return os.path.expanduser("~/Music/CrateMatch Drop")

    def _refresh_drop_folder(self):
        if not self.seed:
            return
        drop_dir = self._drop_dir_for(self.seed.get("path"))
        paths = [t.get("path") for (_s, t, _l) in self.matches
                 if t.get("path") and os.path.isfile(t.get("path"))]
        sig = (drop_dir, tuple(paths))
        if sig == self._last_drop_sig:
            return
        self._last_drop_sig = sig
        try:
            os.makedirs(drop_dir, exist_ok=True)
            manifest = os.path.join(drop_dir, ".cratematch_manifest.json")
            old = []
            if os.path.isfile(manifest):
                try:
                    with open(manifest) as f:
                        old = json.load(f)
                except Exception:
                    old = []
            new_names = []
            by_path = {t.get("path"): t for (_s, t, _l) in self.matches}
            for i, p in enumerate(paths):
                t = by_path.get(p, {})
                base = f"{t.get('artist') or 'Unknown'} - " \
                       f"{t.get('title') or os.path.basename(p)}"
                base = re.sub(r'[\\/:*?"<>|]', "", base)[:110].strip()
                ext = os.path.splitext(p)[1]
                name = f"{i + 1:02d} - {base}{ext}"
                dst = os.path.join(drop_dir, name)
                new_names.append(name)
                if os.path.lexists(dst):
                    os.unlink(dst)
                try:
                    os.link(p, dst)
                except OSError:
                    try:
                        os.symlink(p, dst)
                    except OSError:
                        pass
            for name in old:
                if name not in new_names:
                    stale = os.path.join(drop_dir, name)
                    if os.path.lexists(stale):
                        os.unlink(stale)
            with open(manifest, "w") as f:
                json.dump(new_names, f)
            if not self._drop_announced:
                self._drop_announced = True
                self._set_status(
                    f"Drop folder live: {drop_dir} "
                    f"({len(new_names)} tracks)")
        except Exception:
            pass

    # ================= drop files in =================
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls()]
        todo = [p for p in paths
                if os.path.isfile(p) and
                os.path.splitext(p)[1].lower() in
                (".mp3", ".wav", ".flac", ".m4a", ".aiff", ".aif",
                 ".ogg")]
        if not todo:
            return
        self._set_status(f"Adding {len(todo)} dropped file(s)…")

        def run():
            n = 0
            for p in todo:
                try:
                    a = analyze_file(p)
                    if a:
                        artist, title, genres, year = \
                            artist_title_from_file(p)
                        self.lib.upsert(p, artist, title, a, genres, year)
                        n += 1
                except Exception:
                    pass
            self.msg_q.put(("drop_done", n))

        threading.Thread(target=run, daemon=True).start()

    # ================= remove junk =================
    def _row_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        menu = QMenu(self)
        act = QAction("Remove from library", self)
        act.triggered.connect(self._remove_selected)
        menu.addAction(act)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _remove_selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(getattr(self, "_row_tracks", [])):
            return
        t = self._row_tracks[row]
        self.lib.delete(t.get("path"))
        self.tracks = self.lib.all_tracks()
        self._set_status(
            f"Removed: {t.get('artist')} — {t.get('title')}")
        self._rematch()

    # ================= cratetube fill =================
    def _get_download_dir(self):
        s = load_settings()
        d = s.get("download_dir")
        if d and os.path.isdir(d):
            return d
        d = QFileDialog.getExistingDirectory(
            self, "Choose download folder for new tracks")
        if d:
            s["download_dir"] = d
            save_settings(s)
            return d
        return None

    def _fill_pocket(self):
        suggs = getattr(self, "_ct_suggestions", [])
        if not suggs:
            return
        d = self._get_download_dir()
        self._dl_dialog = DownloadDialog(self, suggs, d)
        self._dl_dialog.show()

    def _on_track_downloaded(self, path, artist, title):
        try:
            a = analyze_file(path)
            if a:
                self.lib.upsert(path, artist, title, a)
                self.tracks = self.lib.all_tracks()
                self._rematch()
        except Exception as e:
            self._set_status(f"Analyze failed for {title}: {e}")

    # ================= lifecycle =================
    def closeEvent(self, event):
        try:
            self.watcher.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CrateMatch Live")
    app.setApplicationVersion(VERSION)
    app.setStyleSheet(APP_STYLE)
    try:
        w = LiveWindowQt()
    except Exception:
        try:
            with open(ERROR_LOG, "a") as f:
                f.write(traceback.format_exc() + "\n---\n")
        except Exception:
            pass
        traceback.print_exc()
        raise
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
