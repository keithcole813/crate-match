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
    QApplication, QComboBox, QDialog, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSlider, QTableWidget, QTableWidgetItem,
    QToolButton, QVBoxLayout, QWidget,
)

from analyzer import Library, scan_files, scan_folder, analyze_file
from crates import list_crates, load_crate_index
from history import HistoryGraph
from pocket import classify_pocket, find_matches, format_key
from serato_watcher import SeratoWatcher
from cratetube_bridge import bridge_for_seed

VERSION = "1.6.1"
DB_PATH = os.path.expanduser("~/.crate-match/library.db")
SETTINGS_PATH = os.path.expanduser("~/.cratematch_settings.json")
ERROR_LOG = os.path.expanduser("~/cratematch-live-error.log")

# CrateMatch identity palette — near-black + signature gold. Applied
# over Qt's Fusion style (set in main) so NOTHING renders with the
# host OS theme: every control is drawn from this sheet (Keith:
# "make it look unique instead of matching the os look").
BG = "#141414"
ROW_BG = "#2b2b2b"
ROW_ALT = "#232323"
FG = "#f5f5f5"
DIM = "#9a9a9a"
GOLD = "#f0cd6e"
GOLD_DARK = "#4a3f22"
HEADER_BG = "#0d0d0d"
EDGE = "#3d3d3d"

APP_STYLE = f"""
QMainWindow, QDialog, QWidget {{ background: {BG}; color: {FG};
    font-family: Helvetica; font-size: 13px; }}
QLabel.dim {{ color: {DIM}; font-size: 11px; }}
QLabel.gold {{ color: {GOLD}; font-size: 14px; font-weight: bold; }}
QPushButton {{ background: {ROW_BG}; color: {FG};
    border: 1px solid {EDGE}; border-radius: 6px;
    padding: 6px 12px; }}
QPushButton:hover {{ border-color: {GOLD}; color: {GOLD}; }}
QPushButton:pressed {{ background: {GOLD_DARK}; }}
QPushButton:disabled {{ color: {DIM}; border-color: {ROW_BG}; }}
QComboBox {{ background: {ROW_BG}; color: {FG};
    border: 1px solid {EDGE}; border-radius: 6px; padding: 4px 6px; }}
QComboBox:hover {{ border-color: {GOLD}; }}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{ background: {ROW_BG}; color: {FG};
    border: 1px solid {GOLD_DARK}; selection-background-color: {GOLD_DARK};
    selection-color: {FG}; outline: 0; }}
QComboBox QAbstractItemView::item {{ padding: 4px 8px; }}
QCheckBox {{ color: {FG}; spacing: 6px; }}
QCheckBox::indicator {{ width: 16px; height: 16px;
    border: 1px solid {EDGE}; border-radius: 4px; background: {ROW_BG}; }}
QCheckBox::indicator:hover {{ border-color: {GOLD}; }}
QCheckBox::indicator:checked {{ background: {GOLD};
    border-color: {GOLD}; }}
QToolButton {{ background: {ROW_BG}; color: {FG};
    border: 1px solid {EDGE}; border-radius: 6px;
    padding: 6px 12px; }}
QToolButton:hover {{ border-color: {GOLD}; color: {GOLD}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QMenu {{ background: {ROW_BG}; color: {FG};
    border: 1px solid {GOLD_DARK}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {GOLD_DARK}; }}
QMenu::item:disabled {{ color: {DIM}; }}
QMenu::separator {{ height: 1px; background: {EDGE};
    margin: 4px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; }}
QSlider::groove:horizontal {{ height: 6px; background: {ROW_ALT};
    border: 1px solid {EDGE}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {GOLD_DARK};
    border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {GOLD}; width: 14px;
    margin: -5px 0; border-radius: 7px;
    border: 1px solid {GOLD_DARK}; }}
QProgressBar {{ background: {ROW_ALT}; border: 1px solid {EDGE};
    border-radius: 5px; height: 10px; }}
QProgressBar::chunk {{ background: {GOLD}; border-radius: 4px; }}
QTableWidget {{ background: {ROW_BG}; color: {FG}; border: 0;
    gridline-color: transparent; alternate-background-color: {ROW_ALT}; }}
QTableWidget::item {{ padding: 2px 4px; }}
QTableWidget::item:selected {{ background: {GOLD_DARK}; }}
QHeaderView::section {{ background: {HEADER_BG}; color: {GOLD};
    border: 0; padding: 4px; font-weight: bold; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #55491f; border-radius: 5px;
    min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {GOLD}; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #55491f; border-radius: 5px;
    min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {GOLD}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QToolTip {{ background: {ROW_BG}; color: {FG};
    border: 1px solid {GOLD_DARK}; padding: 4px; }}
"""


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


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

    def __init__(self, parent, suggestions, download_dir, seed_label=""):
        super().__init__(parent)
        self.parent_win = parent
        self.suggestions = suggestions
        self.download_dir = download_dir
        self.setWindowTitle("Download missing tracks")
        self.resize(480, 440)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        lay = QVBoxLayout(self)
        hdr = QLabel(f"{len(suggestions)} tracks not in your library"
                     + (f" — like {seed_label}" if seed_label else ""))
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

        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        prow.addWidget(self.progress, 1)
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {DIM};")
        prow.addWidget(self.progress_label)
        lay.addLayout(prow)

        all_btn = QPushButton("Download all")
        all_btn.clicked.connect(self._download_all)
        lay.addWidget(all_btn)
        self._active_row = None

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
        if text.startswith("✅"):
            if self._active_row == i:
                self.progress.setRange(0, 100)
                self.progress.setValue(100)
                self.progress_label.setText("done")
        elif text.startswith("❌"):
            if self._active_row == i:
                self.progress.setRange(0, 100)
                self.progress.setValue(0)
                self.progress_label.setText("failed")

    def set_progress(self, i, pct):
        self._active_row = i
        self.set_row_status(i, f"⬇ {pct}%" if pct >= 0 else "⬇ …")
        s = self.suggestions[i]
        self.progress_label.setText(
            f"{s['artist']} — {s['title']}"[:42])
        if pct >= 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(pct)
        else:
            self.progress.setRange(0, 0)

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
        """Download via the yt-dlp LIBRARY (bundled in the app — the
        old code shelled out to a 'yt-dlp' command that doesn't exist
        inside the frozen app, hence Keith's 'no yt-dlp' status).
        Progress hooks feed the dialog's progress bar."""
        artist, title = sugg["artist"], sugg["title"]
        query = f"{artist} {title}".strip()
        base = self._safe_filename(artist, title)
        outpath = os.path.join(self.download_dir, base + ".mp3")
        q = self.parent_win.msg_q

        def hook(d):
            if d.get("status") == "downloading":
                total = (d.get("total_bytes") or
                         d.get("total_bytes_estimate") or 0)
                done = d.get("downloaded_bytes") or 0
                pct = int(done * 100 / total) if total else -1
                q.put(("dl_progress", (i, pct)))
            elif d.get("status") == "finished":
                q.put(("dl_status", (i, "⏳ converting…")))

        try:
            import yt_dlp
            opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(self.download_dir,
                                        base + ".%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "0",
                }],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [hook],
            }
            try:
                from analyzer import _ffmpeg_bin
                ff = _ffmpeg_bin()
                if ff:
                    opts["ffmpeg_location"] = ff
            except Exception:
                pass
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([f"ytsearch1:{query}"])
            if not os.path.exists(outpath):
                raise RuntimeError("download produced no MP3")
            q.put(("download_done", (outpath, artist, title)))
            q.put(("dl_status", (i, "✅ in library")))
            q.put(("status_text",
                   f"Downloaded: {artist} — {title}"))
        except Exception as e:
            q.put(("dl_status", (i, "❌ failed")))
            q.put(("status_text",
                   f"Download failed ({title}): {str(e)[:60]}"))


class LiveWindowQt(QMainWindow):
    COLS = ("title", "artist", "bpm", "key", "energy", "dance", "score")
    LABELS = {"title": "Title", "artist": "Artist", "bpm": "BPM",
              "key": "Key", "energy": "E%", "dance": "D%",
              "score": "Score"}

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
        self._ct_suggestions = []
        self._bridge = None
        self._bridge_key = None
        self._want_fill = False
        self._scanning = False
        self._last_now = None
        self.msg_q = queue.Queue()
        self._sort_col = "score"
        self._sort_rev = True
        _s = load_settings()
        self._min_value = self._parse_min_setting(
            _s.get("min_score", "60"))
        # OBS-style window elements (Keith, v1.6.1): what shows in the
        # window is his choice, set in Settings → Window elements.
        self._elements = {"show": True, "key": True, "threshold": True,
                          "download": True, "library": True,
                          "energy": True, "dance": True}
        _saved_el = _s.get("ui_elements")
        if isinstance(_saved_el, dict):
            for _k in self._elements:
                if _k in _saved_el:
                    self._elements[_k] = bool(_saved_el[_k])
        self.crate_index = None
        self._tag_filling = False
        self._baking = False
        self._closing = False
        self._collapsed = False
        self._pre_collapse_size = None
        self._analyzing_seeds = set()
        self._last_drop_sig = None
        self._drop_announced = False
        self._dl_dialog = None

        self._build()
        self._apply_elements()
        self._refresh_count_label()

        self.watcher = SeratoWatcher(self._on_watcher, interval=2.0)
        self.watcher.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(150)
        QTimer.singleShot(5000, self._start_tag_fill)
        QTimer.singleShot(9000, self._start_bake)

        self.history = None
        threading.Thread(target=self._build_history, daemon=True).start()
        threading.Thread(target=self._load_crates, daemon=True).start()

    # ================= UI =================
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)

        # ---- collapsed strip (hidden until Collapse is pressed) ----
        # Keith's rule: collapse, NOT minimize — the strip keeps the
        # pocket + now-playing + numbers visible and live, stays on
        # top, and restores in place.
        self.strip_view = QWidget()
        strip = QHBoxLayout(self.strip_view)
        strip.setContentsMargins(2, 0, 2, 0)
        self.strip_pocket = QLabel("◆ —")
        self.strip_pocket.setStyleSheet(
            f"color: {GOLD}; font-size: 13px; font-weight: bold;")
        strip.addWidget(self.strip_pocket)
        self.strip_np = QLabel("")
        self.strip_np.setStyleSheet(f"color: {DIM}; font-size: 12px;")
        strip.addWidget(self.strip_np)
        self.strip_stats = QLabel("")
        self.strip_stats.setStyleSheet(f"font-size: 12px;")
        strip.addWidget(self.strip_stats)
        strip.addStretch(1)
        restore_btn = QPushButton("Restore ⤢")
        restore_btn.clicked.connect(lambda: self._set_collapsed(False))
        strip.addWidget(restore_btn)
        self.strip_view.setVisible(False)
        root.addWidget(self.strip_view)

        # ---- full view ----
        self.full_view = QWidget()
        root.addWidget(self.full_view, 1)
        lay = QVBoxLayout(self.full_view)
        lay.setContentsMargins(0, 0, 0, 0)

        # brand wordmark — the identity moment at the top of the app
        wm_row = QHBoxLayout()
        wm = QLabel("◆ CRATEMATCH LIVE")
        wm.setStyleSheet(
            f"color: {GOLD}; font-size: 16px; font-weight: bold; "
            f"letter-spacing: 2px;")
        wm_row.addWidget(wm)
        wm_row.addStretch(1)
        # ⚙ Settings (v1.6.1, Keith): ALL setup lives here — adding
        # music, rescans, API keys, which elements the window shows.
        # The main window keeps only live controls.
        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙ Settings")
        self.settings_btn.setToolTip(
            "Setup: add music, rescan, API keys, window elements")
        _menu = QMenu(self.settings_btn)
        self.act_add_crate = _menu.addAction("Add from crate…")
        self.act_add_crate.triggered.connect(self._add_from_crate)
        self.act_add_folder = _menu.addAction("Add from folder…")
        self.act_add_folder.triggered.connect(self._scan_library)
        self.act_rescan = _menu.addAction("Rescan my folders")
        self.act_rescan.triggered.connect(self._rescan_folders)
        self._scan_actions = [self.act_add_crate, self.act_add_folder,
                              self.act_rescan]
        _menu.addSeparator()
        _act_keys = _menu.addAction("API keys…")
        _act_keys.triggered.connect(self._open_keys_dialog)
        _elem_menu = _menu.addMenu("Window elements")
        self._element_actions = {}
        _elem_labels = {"show": "Show count", "key": "Key format",
                        "threshold": "Threshold slider",
                        "download": "Download more button",
                        "library": "Library count",
                        "energy": "E% column", "dance": "D% column"}
        for _k in ("show", "key", "threshold", "download", "library",
                   "energy", "dance"):
            _a = _elem_menu.addAction(_elem_labels[_k])
            _a.setCheckable(True)
            _a.setChecked(bool(self._elements.get(_k, True)))
            _a.toggled.connect(
                lambda on, k=_k: self._toggle_element(k, on))
            self._element_actions[_k] = _a
        self.settings_btn.setMenu(_menu)
        self.settings_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        wm_row.addWidget(self.settings_btn)
        collapse_btn = QPushButton("▼ Collapse")
        collapse_btn.setToolTip(
            "Shrink to a slim strip that still shows your pocket — "
            "not a minimize; it stays on top and updates live")
        collapse_btn.clicked.connect(lambda: self._set_collapsed(True))
        wm_row.addWidget(collapse_btn)
        lay.addLayout(wm_row)
        rule = QWidget()
        rule.setFixedHeight(2)
        rule.setStyleSheet(f"background: {GOLD_DARK};")
        lay.addWidget(rule)

        np_title = QLabel("NOW PLAYING")
        np_title.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        lay.addWidget(np_title)
        self.np_label = QLabel("Waiting for Serato…")
        self.np_label.setStyleSheet(
            f"color: {GOLD}; font-size: 14px; font-weight: bold;")
        self.np_label.setWordWrap(True)
        lay.addWidget(self.np_label)
        pk_title = QLabel("POCKET")
        pk_title.setStyleSheet(f"color: {DIM}; font-size: 11px;")
        lay.addWidget(pk_title)
        self.pocket_headline = QLabel("")
        self.pocket_headline.setStyleSheet(
            f"color: {GOLD}; font-size: 17px; font-weight: bold; "
            f"letter-spacing: 1px;")
        self.pocket_headline.setWordWrap(True)
        lay.addWidget(self.pocket_headline)
        self.pocket_label = QLabel("")
        self.pocket_label.setStyleSheet(f"color: {DIM};")
        self.pocket_label.setWordWrap(True)
        lay.addWidget(self.pocket_label)

        ctl = QHBoxLayout()
        self.show_label = QLabel("Show:")
        ctl.addWidget(self.show_label)
        self.count_box = QComboBox()
        self.count_box.addItems(["10", "15", "20", "25"])
        self.count_box.setCurrentText("15")
        self.count_box.setMinimumWidth(56)
        self.count_box.currentTextChanged.connect(lambda *_: self._rematch())
        ctl.addWidget(self.count_box)
        self.key_label = QLabel("Key:")
        ctl.addWidget(self.key_label)
        self.key_box = QComboBox()
        self.key_box.addItems(["camelot", "classic", "openkey"])
        self.key_box.setMinimumWidth(108)
        self.key_box.currentTextChanged.connect(
            lambda *_: (self._render(), self._refresh_header()))
        ctl.addWidget(self.key_box)
        self.thr_label = QLabel("Threshold:")
        ctl.addWidget(self.thr_label)
        # v1.6.1 (Keith): a slider with 1-point steps, not the old
        # preset menu (Off/50/55/60/65). All the way left = Off.
        self.min_slider = QSlider(Qt.Orientation.Horizontal)
        self.min_slider.setRange(0, 95)
        self.min_slider.setSingleStep(1)
        self.min_slider.setPageStep(5)
        self.min_slider.setValue(self._min_value)
        self.min_slider.setFixedWidth(104)
        self.min_slider.setToolTip(
            "Lowest match score to show — 1-point steps; "
            "all the way left is Off")
        self.min_slider.valueChanged.connect(
            lambda *_: self._on_min_changed())
        ctl.addWidget(self.min_slider)
        self.min_value_label = QLabel(self._min_text())
        self.min_value_label.setMinimumWidth(26)
        ctl.addWidget(self.min_value_label)
        ctl.addStretch(1)
        self.lib_count_label = QLabel("")
        self.lib_count_label.setStyleSheet(f"color: {DIM};")
        ctl.addWidget(self.lib_count_label)
        lay.addLayout(ctl)

        ctl2 = QHBoxLayout()
        # v1.6.0 (Keith): no Downloads toggle — "Download more" is
        # always here. It only does anything with internet; offline
        # it just finds nothing new, the library matches never stop.
        self.ct_btn = QPushButton("⬇ Download more")
        self.ct_btn.setStyleSheet(
            f"color: {GOLD}; border-color: {GOLD_DARK}; "
            f"font-weight: bold;")
        self.ct_btn.setToolTip(
            "Songs like this one that you don't own yet")
        self.ct_btn.clicked.connect(self._fill_pocket)
        ctl2.addWidget(self.ct_btn)
        # v1.6.1 (Keith): 'Send list to crate' is GONE — drag-and-drop
        # replaced it. Scan library and the API-keys button moved up
        # into ⚙ Settings; this row is live actions only.
        lay.addLayout(ctl2)

        self.table = MatchTable()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(
            [self.LABELS[c] for c in self.COLS])
        widths = {"title": 140, "artist": 100, "bpm": 50, "key": 48,
                  "energy": 44, "dance": 44, "score": 50}
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
        n = self.lib.count()
        self.lib_count_label.setText(f"Library: {n:,} songs")
        if n == 0:
            self._set_status(
                "No tracks indexed yet — open ⚙ Settings → "
                "Add from folder and pick the folder your music is in")

    def _set_collapsed(self, collapsed):
        """Collapse to the slim strip (NOT minimize): the strip keeps
        watching Serato and updates live; Restore returns the window
        to its previous size and place."""
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self._pre_collapse_size = self.size()
            self.full_view.setVisible(False)
            self.strip_view.setVisible(True)
            self.resize(self.width(), 56)
        else:
            self.strip_view.setVisible(False)
            self.full_view.setVisible(True)
            if self._pre_collapse_size is not None:
                self.resize(self._pre_collapse_size)

    def _open_keys_dialog(self):
        s = load_settings()
        dlg = QDialog(self)
        dlg.setWindowTitle("API keys — entered once")
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(dlg)
        note = QLabel(
            "These power the offline numbers bake-in. They stay on "
            "this Mac only.\n\n"
            "Discogs token: discogs.com → Settings → Developers → "
            "Generate new token (free). Powers the specific pocket "
            "names (styles like Trap · Gangsta).\n\n"
            "TheAudioDB key: optional — the shared test key works, "
            "a premium key (Patreon) makes the first-scan bake "
            "faster.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {DIM};")
        lay.addWidget(note)
        lay.addWidget(QLabel("Discogs token:"))
        dg = QLineEdit(s.get("discogs_token", ""))
        dg.setEchoMode(QLineEdit.EchoMode.Password)
        lay.addWidget(dg)
        lay.addWidget(QLabel("TheAudioDB key:"))
        adb = QLineEdit(s.get("audiodb_key", ""))
        adb.setEchoMode(QLineEdit.EchoMode.Password)
        lay.addWidget(adb)
        row = QHBoxLayout()
        row.addStretch(1)
        save = QPushButton("Save")
        row.addWidget(save)
        lay.addLayout(row)

        def do_save():
            s2 = load_settings()
            s2["discogs_token"] = dg.text().strip()
            s2["audiodb_key"] = adb.text().strip()
            save_settings(s2)
            dlg.accept()
            self._set_status(
                "Keys saved — the bake-in uses them from here on")
            self._start_bake()

        save.clicked.connect(do_save)
        dlg.exec()

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

    @staticmethod
    def _parse_min_setting(v):
        """Settings migration: through v1.6.0 min_score was text
        ('Off'/'50'…'65'); the v1.6.1 slider stores a number 0..95."""
        if isinstance(v, (int, float)):
            n = int(v)
        elif str(v).strip().lower() == "off":
            return 0
        else:
            try:
                n = int(float(str(v).strip()))
            except (TypeError, ValueError):
                return 60
        return max(0, min(95, n))

    def _min_score(self):
        if hasattr(self, "min_slider"):
            return self.min_slider.value() / 100.0
        return self._min_value / 100.0

    def _min_text(self):
        v = self.min_slider.value() if hasattr(self, "min_slider") \
            else self._min_value
        return "Off" if v == 0 else str(v)

    def _on_min_changed(self):
        v = self.min_slider.value()
        self.min_value_label.setText("Off" if v == 0 else str(v))
        s = load_settings()
        s["min_score"] = v
        save_settings(s)
        self._rematch()

    # ================= window elements (OBS-style) =================
    def _apply_elements(self):
        el = self._elements
        self.show_label.setVisible(el["show"])
        self.count_box.setVisible(el["show"])
        self.key_label.setVisible(el["key"])
        self.key_box.setVisible(el["key"])
        self.thr_label.setVisible(el["threshold"])
        self.min_slider.setVisible(el["threshold"])
        self.min_value_label.setVisible(el["threshold"])
        self.ct_btn.setVisible(el["download"])
        self.lib_count_label.setVisible(el["library"])
        self.table.setColumnHidden(self.COLS.index("energy"),
                                   not el["energy"])
        self.table.setColumnHidden(self.COLS.index("dance"),
                                   not el["dance"])

    def _toggle_element(self, key, on):
        self._elements[key] = bool(on)
        s = load_settings()
        s["ui_elements"] = dict(self._elements)
        save_settings(s)
        self._apply_elements()

    @staticmethod
    def _energy_pct(t):
        """Energy % for display: the baked database number when the
        track has one; the local loudness mapped onto % until then."""
        db = t.get("db_energy")
        if db is not None:
            try:
                return f"{round(float(db) * 100):.0f}%"
            except (TypeError, ValueError):
                pass
        e = t.get("energy") or 0
        return f"{max(1, min(10, round(e * 40))) * 10:.0f}%"

    @staticmethod
    def _dance_pct(t):
        db = t.get("db_dance")
        if db is None:
            return "—"
        try:
            return f"{round(float(db) * 100):.0f}%"
        except (TypeError, ValueError):
            return "—"

    def _refresh_header(self):
        if not self.seed:
            return
        seed = self.seed
        pk = classify_pocket(seed)
        self.pocket_headline.setText(pk["headline"])
        stats = []
        if seed.get("bpm"):
            stats.append(f"{seed['bpm']:.0f} BPM")
        k = format_key(seed.get("camelot"), self.key_box.currentText())
        if k and k != "?":
            stats.append(f"Key {k}")
        stats.append(f"E {self._energy_pct(seed)}")
        stats.append(f"D {self._dance_pct(seed)}")
        detail = pk["detail"]
        self.pocket_label.setText(
            " · ".join(x for x in (detail, " · ".join(stats)) if x))
        # the collapsed strip mirrors the same live numbers
        self.strip_pocket.setText(f"◆ {pk['headline']}")
        self.strip_stats.setText(" · ".join(stats))
        if self._last_now:
            self.strip_np.setText(
                f"{self._last_now.get('title') or ''}"[:28])

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

    def _start_bake(self):
        """First-scan bake-in: fetch database Energy/Danceability %
        (and Discogs pocket styles) for every track that lacks them,
        store locally, done — DJing is offline after that. Throttled
        + resumable in meta_bake; safe to call any time (baked rows
        are skipped)."""
        if self._baking or self._closing:
            return
        self._baking = True

        def run():
            filled = attempted = 0
            try:
                import meta_bake
                s = load_settings()

                def prog(i, n, name):
                    if i == 1 or i % 25 == 0:
                        self.msg_q.put(
                            ("bake_status",
                             f"Baking offline numbers: {i}/{n}…"))

                filled, attempted = meta_bake.bake_library(
                    self.lib,
                    audiodb_key=s.get("audiodb_key") or "123",
                    discogs_token=s.get("discogs_token") or "",
                    progress_cb=prog,
                    should_stop=lambda: self._closing)
            except Exception:
                pass
            self.msg_q.put(("bake_done", (filled, attempted)))

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
    def _set_scan_ui_enabled(self, on):
        for a in getattr(self, "_scan_actions", []):
            a.setEnabled(on)

    def _remember_folder(self, folder):
        s = load_settings()
        folders = [f for f in (s.get("music_folders") or [])
                   if f != folder]
        folders.insert(0, folder)
        s["music_folders"] = folders[:8]
        save_settings(s)

    def _scan_library(self):
        """Settings → Add from folder… (was the Scan library button)."""
        if self._scanning:
            return
        start = (load_settings().get("music_folders") or
                 [os.path.expanduser("~/Music")])[0]
        folder = QFileDialog.getExistingDirectory(
            self, "Pick your music folder (the big one, e.g. Music)",
            start)
        if not folder:
            return
        self._remember_folder(folder)
        self._run_scan([folder])

    def _rescan_folders(self):
        """Settings → Rescan my folders: one click over every folder
        ever added. Unchanged songs are skipped, so this is the
        append flow — only new files analyze."""
        if self._scanning:
            return
        folders = [f for f in
                   (load_settings().get("music_folders") or [])
                   if os.path.isdir(f)]
        if not folders:
            self._set_status(
                "No folders remembered yet — use Settings → "
                "Add from folder once and I'll remember it")
            return
        self._run_scan(folders)

    def _run_scan(self, folders):
        self._scanning = True
        self._set_scan_ui_enabled(False)
        if len(folders) == 1:
            self._set_status(f"Scanning {folders[0]}…")
        else:
            self._set_status(f"Scanning {len(folders)} folders…")

        def run():
            try:
                added = total = failed = 0
                for folder in folders:
                    def prog(i, n, name):
                        self.msg_q.put(("scan_status",
                                        f"Analyzing {i}/{n}: "
                                        f"{name[:40]}"))
                    a, t, fl = scan_folder(
                        self.lib, folder, artist_title_from_file, prog)
                    added += a
                    total += t
                    failed += fl
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

    def _add_from_crate(self):
        """Settings → Add from crate…: Keith's Serato intake flow —
        he drops new songs into a crate; this analyzes and adds just
        that crate's songs, skipping ones already in the library."""
        if self._scanning:
            return
        try:
            crates = [(n, ps) for (n, ps) in list_crates() if ps]
        except Exception:
            crates = []
        if not crates:
            self._set_status(
                "No Serato crates found — crates live in the _Serato_ "
                "folders on your drives")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Add from crate")
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        lay = QVBoxLayout(dlg)
        note = QLabel(
            "Adds the songs in one Serato crate to the library. "
            "Songs already in are skipped, and nothing else in the "
            "library is touched.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {DIM};")
        lay.addWidget(note)
        box = QComboBox()
        for name, paths in crates:
            box.addItem(f"{name} ({len(paths)})", paths)
        lay.addWidget(box)
        row = QHBoxLayout()
        row.addStretch(1)
        add = QPushButton("Add crate")
        row.addWidget(add)
        lay.addLayout(row)
        chosen = {}

        def do_add():
            chosen["paths"] = box.currentData()
            dlg.accept()

        add.clicked.connect(do_add)
        if not dlg.exec() or not chosen.get("paths"):
            return
        paths = chosen["paths"]
        self._scanning = True
        self._set_scan_ui_enabled(False)
        self._set_status(f"Adding crate: {len(paths)} songs listed…")

        def run():
            try:
                def prog(i, n, name):
                    self.msg_q.put(("scan_status",
                                    f"Analyzing {i}/{n}: {name[:40]}"))
                added, total, failed, missing = scan_files(
                    self.lib, paths, artist_title_from_file, prog)
                self.msg_q.put(("scan_done",
                                (added, total, failed + missing, None)))
            except Exception:
                tb = traceback.format_exc()
                try:
                    with open(ERROR_LOG, "a") as f:
                        f.write("CRATE ADD FAILED\n" + tb + "\n---\n")
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
        elif kind == "dl_progress":
            if self._dl_dialog is not None:
                i, pct = payload
                self._dl_dialog.set_progress(i, pct)
        elif kind == "scan_status":
            self._set_status(payload)
        elif kind == "scan_done":
            added, total, failed, err = payload
            self._scanning = False
            self._set_scan_ui_enabled(True)
            if err:
                self._set_status(f"Scan failed: {err}")
            elif total == 0:
                self._set_status(
                    "Scan done: no audio files found in that folder — "
                    "pick the folder your music is in")
            else:
                self.tracks = self.lib.all_tracks()
                self._refresh_count_label()
                msg = (f"Scan done: {self.lib.count()} tracks indexed "
                       f"({added} new)")
                if failed:
                    msg += f" — {failed} of {total} files failed"
                self._set_status(msg)
                self._start_tag_fill()
                self._start_bake()
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
            # a just-analyzed seed usually has no genre yet — let the
            # internet tag fill give it one so its pocket (and any
            # download suggestions) are based on a real genre
            self._start_tag_fill()
        elif kind == "seed_tagged":
            self.tracks = self.lib.all_tracks()
            if self._last_now:
                seed = self._find_seed(self._last_now)
                if seed:
                    self.seed = seed
                    self.seed_pocket = classify_pocket(seed)
                    self._refresh_header()
                    self._rematch()
        elif kind == "bridge":
            key, payload = payload
            if key != self._current_bridge_key():
                return  # stale lookup for a previous song — drop it
            self._bridge = payload
            suggs = (payload or {}).get("suggestions") or []
            self._ct_suggestions = suggs
            if payload and payload.get("online_error"):
                err = str(payload["online_error"])
                if len(err) > 64:
                    err = err[:64] + "…"
                self._set_status(
                    f"⚠ Online lookup failed ({err}) — this list "
                    f"came from my built-in book")
            if self._want_fill:
                self._want_fill = False
                if suggs:
                    self._open_fill_dialog(suggs)
                else:
                    self._fill_empty_status()
            elif payload and payload.get("thin") and suggs:
                self._set_status(
                    f"Pocket thin ({payload['library_count']} in "
                    f"library) — ⬇ Download more has {len(suggs)} "
                    f"songs like this one")
        elif kind == "drop_done":
            self.tracks = self.lib.all_tracks()
            self._refresh_count_label()
            self._set_status(
                f"Added {payload} dropped track(s) — "
                f"{self.lib.count()} in library")
            if self.seed:
                self._rematch()
        elif kind == "bake_status":
            if not self._scanning:
                self._set_status(payload)
        elif kind == "bake_done":
            self._baking = False
            filled, attempted = payload
            if filled:
                self.tracks = self.lib.all_tracks()
                self._set_status(
                    f"Offline numbers baked in: {filled} songs got "
                    f"Energy/Dance % — no internet needed at DJ time")
                if self.seed:
                    if self._last_now:
                        self.seed = self._find_seed(self._last_now) \
                            or self.seed
                    self._refresh_header()
                    self._rematch()
            elif attempted:
                self._set_status(
                    f"Bake-in pass done — {attempted} songs checked, "
                    f"no database numbers found for them yet")
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
        self.strip_np.setText(f"{now.get('title') or ''}"[:28])
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
        # song changed: an open download list belongs to the old
        # song — close it so stale suggestions never linger
        if getattr(self, "_dl_dialog", None) is not None:
            try:
                self._dl_dialog.close()
            except Exception:
                pass
            self._dl_dialog = None
        self.seed = seed
        self.seed_pocket = classify_pocket(seed)
        self._refresh_header()
        if (not (seed.get("genres") or "").strip()
                or seed.get("db_energy") is None):
            self._tag_seed(seed)
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
                    f"No confident matches (threshold "
                    f"{self._min_text()}) — lower Threshold "
                    f"to see more")
            elif len(self.matches) < n:
                self._set_status(
                    f"{len(self.matches)} confident matches "
                    f"(threshold {self._min_text()})")
        self._refresh_drop_folder()
        # Download-more suggestions come from an async online lookup
        # (never block the UI on the network — v1.2.3 lesson). Keyed
        # by seed + genres so a fresh genre tag re-triggers it.
        if self._current_bridge_key() != self._bridge_key:
            self._bridge_key = self._current_bridge_key()
            self._kick_bridge()

    def _current_bridge_key(self):
        if not self.seed:
            return None
        return (norm(self.seed.get("artist")),
                norm(self.seed.get("title")),
                (self.seed.get("genres") or "").strip().lower())

    def _kick_bridge(self):
        seed = dict(self.seed)
        key = self._current_bridge_key()

        def run():
            try:
                bridge = bridge_for_seed(seed, self.lib.db_path,
                                         online=True)
            except Exception:
                bridge = None
            self.msg_q.put(("bridge", (key, bridge)))

        threading.Thread(target=run, daemon=True).start()

    def _tag_seed(self, seed):
        """Enrich JUST the now-playing track first — the general
        fills take minutes; the seed is what matters now. Bakes its
        database Energy/Dance % (meta_bake.bake_one) and fills its
        MusicBrainz genres. (Pre-1.6.0 this called a Library method
        that never existed, so the seed-first tag silently failed
        every time and only the slow general fill ever landed.)"""
        def run():
            changed = False
            try:
                import meta_bake
                s = load_settings()
                row = self.lib.get(seed["path"]) or seed
                if meta_bake.bake_one(
                        self.lib, row,
                        audiodb_key=s.get("audiodb_key") or "123",
                        discogs_token=s.get("discogs_token") or ""):
                    changed = True
            except Exception:
                pass
            try:
                if not (seed.get("genres") or "").strip():
                    from genre_tagger import mb_search
                    g, y = mb_search(seed.get("artist"),
                                     seed.get("title"))
                    if g and seed.get("path"):
                        self.lib.set_tags(seed["path"], g, y)
                        changed = True
            except Exception:
                pass
            if changed:
                self.msg_q.put(("seed_tagged", None))

        threading.Thread(target=run, daemon=True).start()

    # ================= rendering =================
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
            self._energy_pct(t).rstrip("%"),
            self._dance_pct(t).rstrip("%"),
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

    # ================= drop folder =================
    # (Send-list-to-crate was removed in v1.6.1 at Keith's ruling:
    # drag-and-drop to the deck replaced it, button and auto-write
    # alike. The Drop folder below stays — it's the Files-panel path.)
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
        self._refresh_count_label()
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
        if not self.seed:
            self._set_status("Play a song first — then I can check "
                             "its pocket")
            return
        if self._ct_suggestions:
            self._open_fill_dialog(self._ct_suggestions)
            return
        if (self._bridge is not None and
                self._bridge_key == self._current_bridge_key()):
            self._fill_empty_status()
            return
        # nothing cached yet — look online now, open when it lands
        self._want_fill = True
        self._bridge_key = self._current_bridge_key()
        self._kick_bridge()
        self._set_status("Looking up songs like this one online…")

    def _fill_empty_status(self):
        b = self._bridge or {}
        if b.get("thin"):
            self._set_status(
                f"Pocket's thin — only {b.get('library_count', 0)} "
                f"in your library — and no similar songs turned up "
                f"online for '{b.get('pocket', '')}' either")
        elif b:
            self._set_status(
                f"No new songs to suggest — your library covers "
                f"this pocket ({b.get('library_count', 0)} songs)")
        else:
            self._set_status(
                "Couldn't look up suggestions — check the internet "
                "connection and try again")

    def _open_fill_dialog(self, suggs):
        d = self._get_download_dir()
        if not d:
            self._set_status(
                "No download folder picked — click ⬇ Download more "
                "again when you're ready to choose one")
            return
        seed_label = ""
        if self.seed:
            seed_label = (f"{self.seed.get('artist') or ''} — "
                          f"{self.seed.get('title') or ''}")
        self._dl_dialog = DownloadDialog(self, suggs, d, seed_label)
        self._dl_dialog.show()

    def _on_track_downloaded(self, path, artist, title):
        try:
            a = analyze_file(path)
            if a:
                self.lib.upsert(path, artist, title, a)
                self.tracks = self.lib.all_tracks()
                self._refresh_count_label()
                self._rematch()
        except Exception as e:
            self._set_status(f"Analyze failed for {title}: {e}")

    # ================= lifecycle =================
    def closeEvent(self, event):
        self._closing = True
        try:
            self.watcher.stop()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CrateMatch Live")
    app.setApplicationVersion(VERSION)
    # Fusion draws controls itself (no host-OS theme), so the
    # identity stylesheet below fully owns the look.
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    try:
        from PySide6.QtGui import QIcon
        icon_path = os.path.join(
            getattr(sys, "_MEIPASS", os.path.dirname(
                os.path.abspath(__file__))), "icon.png")
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
    except Exception:
        pass
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
