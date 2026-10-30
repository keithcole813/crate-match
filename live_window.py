#!/usr/bin/env python3
"""CrateMatch Live — always-on-top DJ companion window.

Watches Serato's history for the currently playing track, then shows a
ranked list of what to play next (pocket-aware: genre/era context +
audio similarity). Drag any result straight onto a Serato deck.

Dark Serato-style UI. Runs alongside Serato on the MacBook.
"""
import json
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except ImportError:
    from tkinter import Tk as _Tk
    HAVE_DND = False
    DND_FILES = None

if HAVE_DND:
    # The import can succeed while the native tkdnd library still fails to
    # load on this machine (e.g. wrong CPU arch) — that failure only shows
    # when a DnD window is created, and it kills the app silently. Probe
    # once here; if tkdnd can't really load, fall back to a plain window
    # (everything except drag-to-deck still works).
    try:
        _probe = TkinterDnD.Tk()
        _probe.withdraw()
        _probe.destroy()
    except Exception:
        HAVE_DND = False
        DND_FILES = None

from analyzer import Library, scan_folder
from history import HistoryGraph
from matcher import similarity
from pocket import (classify_pocket, find_matches, find_pocket_matches,
                    format_key, pocket_score)
from serato_watcher import SeratoWatcher
from cratetube_bridge import bridge_for_seed

DB_PATH = os.path.expanduser("~/.crate-match/library.db")
SETTINGS_PATH = os.path.expanduser("~/.cratematch_settings.json")


def artist_title_from_file(path):
    """(artist, title, genres, year) from the file's own tags, filename
    fallback for artist/title. Genre/year feed the pocket layer — without
    them every pocket is "Unknown" and matches are pure BPM+key."""
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

# Serato-ish dark palette
BG = "#1a1a1a"
ROW_BG = "#2d2d2d"
ROW_ALT = "#262626"
FG = "#f5f5f5"
DIM = "#9a9a9a"
GOLD = "#f0cd6e"
ACCENT = "#38bdf8"
HEADER_BG = "#111111"


class LiveWindow(TkinterDnD.Tk if HAVE_DND else tk.Tk):
    def __init__(self, db_path=DB_PATH):
        super().__init__()
        self.title("CrateMatch Live")
        self.geometry("440x640")
        self.attributes("-topmost", True)
        self.configure(bg=BG)

        self.lib = Library(db_path)
        self.tracks = self.lib.all_tracks()
        self.matches = []          # [(score, track, pocket_label)]
        self.seed = None
        self.seed_pocket = None
        self._scanning = False
        self._last_now = None
        self.msg_q = queue.Queue()
        self._sort_col = "score"
        self._sort_rev = True

        self.count_var = tk.StringVar(value="15")
        self.keyfmt_var = tk.StringVar(value="camelot")

        self._style()
        self._build()
        self._refresh_count_label()

        self.watcher = SeratoWatcher(self._on_watcher, interval=2.0)
        self.watcher.start()
        self.after(150, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._quit)

        # history graph: built in background so years of sessions
        # don't freeze startup; recommendations fall back to
        # audio+pocket until it's ready
        self.history = None
        threading.Thread(target=self._build_history,
                         daemon=True).start()

    def _build_history(self):
        try:
            hg = HistoryGraph(self.lib.db_path)
            parsed, total = hg.sync()
            self.msg_q.put(("history_ready", (hg, parsed, total)))
        except Exception:
            pass  # history stays unavailable; audio+pocket still work

    # ================= theme =================
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Dark.TFrame", background=BG)
        s.configure("Dark.TLabel", background=BG, foreground=FG,
                    font=("Helvetica", 10))
        s.configure("Gold.TLabel", background=BG, foreground=GOLD,
                    font=("Helvetica", 11, "bold"))
        s.configure("Dim.TLabel", background=BG, foreground=DIM,
                    font=("Helvetica", 9))
        s.configure("Dark.TButton", background=ROW_BG, foreground=FG,
                    borderwidth=0, padding=6, font=("Helvetica", 10))
        s.map("Dark.TButton", background=[("active", "#3a3a3a")])
        s.configure("Dark.TCombobox", fieldbackground=ROW_BG,
                    background=ROW_BG, foreground=FG, arrowcolor=GOLD)
        s.configure("Treeview", background=ROW_BG, fieldbackground=ROW_BG,
                    foreground=FG, borderwidth=0, rowheight=26,
                    font=("Helvetica", 10))
        s.configure("Treeview.Heading", background=HEADER_BG,
                    foreground=GOLD, borderwidth=0,
                    font=("Helvetica", 10, "bold"))
        s.map("Treeview", background=[("selected", "#4a3f22")])

    # ================= UI =================
    def _build(self):
        # --- now playing header ---
        hdr = ttk.Frame(self, style="Dark.TFrame")
        hdr.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(hdr, text="NOW PLAYING", style="Dim.TLabel").pack(anchor="w")
        self.np_var = tk.StringVar(value="Waiting for Serato…")
        ttk.Label(hdr, textvariable=self.np_var, style="Gold.TLabel",
                  wraplength=400).pack(anchor="w")
        self.pocket_var = tk.StringVar(value="")
        ttk.Label(hdr, textvariable=self.pocket_var, style="Dim.TLabel",
                  wraplength=400).pack(anchor="w", pady=(2, 0))

        # --- controls ---
        ctl = ttk.Frame(self, style="Dark.TFrame")
        ctl.pack(fill="x", padx=10, pady=4)
        ttk.Label(ctl, text="Show:", style="Dark.TLabel").pack(side="left")
        self.count_box = ttk.Combobox(
            ctl, textvariable=self.count_var, width=4, state="readonly",
            values=["10", "15", "20", "25"], style="Dark.TCombobox")
        self.count_box.pack(side="left", padx=4)
        self.count_box.bind("<<ComboboxSelected>>",
                            lambda e: self._rematch())
        ttk.Label(ctl, text="Key:", style="Dark.TLabel").pack(
            side="left", padx=(10, 0))
        self.key_box = ttk.Combobox(
            ctl, textvariable=self.keyfmt_var, width=9, state="readonly",
            values=["camelot", "classic", "openkey"], style="Dark.TCombobox")
        self.key_box.pack(side="left", padx=4)
        self.key_box.bind("<<ComboboxSelected>>",
                          lambda e: self._render())
        self.ct_btn = ttk.Button(ctl, text="⬇ Fill pocket",
                                 command=self._fill_pocket,
                                 style="Dark.TButton")
        # shown only when pocket is thin
        self.scan_btn = ttk.Button(ctl, text="📁 Scan library",
                                   command=self._scan_library,
                                   style="Dark.TButton")
        self.scan_btn.pack(side="right", padx=4)

        # --- results tree ---
        cols = ("title", "artist", "bpm", "key", "energy", "score")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 height=20)
        widths = {"title": 150, "artist": 110, "bpm": 52, "key": 52,
                  "energy": 58, "score": 52}
        labels = {"title": "Title", "artist": "Artist", "bpm": "BPM",
                  "key": "Key", "energy": "Energy", "score": "Score"}
        for c in cols:
            self.tree.heading(c, text=labels[c],
                              command=lambda c=c: self._sort_by(c))
            self.tree.column(c, width=widths[c], anchor="w"
                             if c in ("title", "artist") else "center")
        self.tree.pack(fill="both", expand=True, padx=10, pady=4)
        # zebra stripes
        self.tree.tag_configure("odd", background=ROW_ALT)
        self.tree.tag_configure("even", background=ROW_BG)

        if HAVE_DND:
            self.tree.drag_source_register(DND_FILES)
            self.tree.dnd_bind("<<DragInitCmd>>", self._drag_init)
            hint = "Drag a track onto a Serato deck"
        else:
            hint = "Install tkinterdnd2 for drag-to-deck"
        # --- status bar ---
        self.status_var = tk.StringVar(value=hint)
        ttk.Label(self, textvariable=self.status_var, style="Dim.TLabel",
                  wraplength=420).pack(fill="x", padx=10, pady=(0, 10))

    # ================= data =================
    def _refresh_count_label(self):
        n = self.lib.count()
        if n == 0:
            self.status_var.set(
                "No tracks indexed yet — click 📁 Scan library "
                "and pick your music folder")
        else:
            self.status_var.set(f"{n} tracks indexed")

    # ================= library scan =================
    def _scan_library(self):
        if self._scanning:
            return
        folder = filedialog.askdirectory(
            title="Pick your music folder (the big one, e.g. Music)",
            initialdir=os.path.expanduser("~/Music"))
        if not folder:
            return
        self._scanning = True
        self.scan_btn.configure(state="disabled")
        self.status_var.set(f"Scanning {folder}…")

        def run():
            # Use the app's one shared connection (check_same_thread=False
            # + busy timeout): opening a second connection from this
            # thread is what failed on Keith's Mac with
            # "unable to open database file".
            try:
                def prog(i, n, name):
                    self.msg_q.put(("scan_status",
                                    f"Analyzing {i}/{n}: {name[:40]}"))
                added, total, failed = scan_folder(
                    self.lib, folder, artist_title_from_file, prog)
                self.msg_q.put(("scan_done", (added, total, failed, None)))
            except Exception:
                import traceback
                tb = traceback.format_exc()
                try:
                    with open(os.path.expanduser(
                            "~/cratematch-live-error.log"), "a") as f:
                        f.write("SCAN FAILED\n" + tb + "\n---\n")
                except Exception:
                    pass
                last = tb.strip().splitlines()[-1] if tb.strip() else "error"
                self.msg_q.put(("scan_done", (0, 0, 0, last)))

        threading.Thread(target=run, daemon=True).start()

    def _find_seed(self, now):
        """Match the Serato now-playing entry to a library track."""
        if not now:
            return None
        spath = (now.get("path") or "").strip()
        # 1) exact path match
        for t in self.tracks:
            if t.get("path") == spath:
                return t
        # 2) basename match
        base = os.path.basename(spath).lower()
        for t in self.tracks:
            if os.path.basename(t.get("path", "")).lower() == base and base:
                return t
        # 3) artist+title match
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
        # The reschedule lives in `finally`: if any single message handler
        # raises, the old code never called self.after() again and live
        # updates froze forever (looked like "stuck on song 1"). Now one
        # bad message is logged and skipped; the loop cannot die.
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                try:
                    self._dispatch_msg(kind, payload)
                except Exception:
                    import traceback
                    try:
                        with open(os.path.expanduser(
                                "~/cratematch-live-error.log"), "a") as f:
                            f.write(traceback.format_exc() + "\n---\n")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        finally:
            self.after(150, self._drain)

    def _dispatch_msg(self, kind, payload):
                if kind == "now_playing":
                    self._handle_now_playing(payload)
                elif kind == "scan_status":
                    self.status_var.set(payload)
                elif kind == "scan_done":
                    added, total, failed, err = payload
                    self._scanning = False
                    self.scan_btn.configure(state="normal")
                    if err:
                        self.status_var.set(f"Scan failed: {err}")
                    elif total == 0:
                        self.status_var.set(
                            "Scan done: no audio files found in that "
                            "folder — pick the folder your music is in")
                    else:
                        self.tracks = self.lib.all_tracks()
                        self._refresh_count_label()
                        msg = (f"Scan done: {self.lib.count()} tracks "
                               f"indexed ({added} new)")
                        if failed:
                            msg += f" — {failed} of {total} files failed"
                        self.status_var.set(msg)
                        # re-match whatever Serato is playing right now
                        self.seed = None
                        if self._last_now:
                            self._handle_now_playing(self._last_now)
                elif kind == "download_done":
                    path, artist, title = payload
                    self._on_track_downloaded(path, artist, title)
                elif kind == "history_ready":
                    hg, parsed, total = payload
                    self.history = hg
                    s, e = hg.stats()
                    self.status_var.set(
                        f"History ready: {s} past sets indexed")
                    if self.seed:
                        self._rematch()

    def _handle_now_playing(self, now):
        if not now:
            self.np_var.set("Waiting for Serato…")
            return
        self._last_now = now
        label = f"{now.get('artist') or '?'} — {now.get('title') or '?'}"
        self.np_var.set(label)
        seed = self._find_seed(now)
        if not seed:
            self.pocket_var.set("Track not in library — click 📁 Scan library and pick your music folder")
            self.matches = []
            self._render()
            return
        if self.seed and seed.get("path") == self.seed.get("path"):
            return  # same track, nothing to do
        self.seed = seed
        self.seed_pocket = classify_pocket(seed)
        self.pocket_var.set(f"Pocket: {self.seed_pocket['label']}")
        self._rematch()

    def _rematch(self):
        if not self.seed:
            return
        try:
            n = int(self.count_var.get())
        except ValueError:
            n = 15
        # three-layer scoring: audio + pocket + history.
        # history=None until background sync finishes -> graceful fallback.
        exclude = None
        if self.history is not None:
            try:
                self.history.sync()  # incremental: only new/changed files
                exclude = self.history.tonight_keys()
            except Exception:
                pass
        self.matches = find_matches(self.seed, self.tracks, top_n=n,
                                    history_graph=self.history,
                                    exclude_keys=exclude)
        self._render()
        # thin pocket? offer CrateTube fill
        try:
            bridge = bridge_for_seed(self.seed, self.lib.db_path)
            if bridge["thin"] and bridge["suggestions"]:
                self.ct_btn.pack(side="right", padx=4)
                self._ct_suggestions = bridge["suggestions"]
                self.status_var.set(
                    f"Pocket thin ({bridge['library_count']} in library) — "
                    f"{len(bridge['suggestions'])} suggestions available")
            else:
                self.ct_btn.pack_forget()
        except Exception:
            pass

    # ================= rendering =================
    def _energy_display(self, t):
        e = t.get("energy") or 0
        # normalize RMS (~0.0-0.3 typical) to 1-10
        return f"{max(1, min(10, round(e * 40)))}"

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col, self._sort_rev = col, col == "score"
        self._render()

    def _row_values(self, score, t):
        keyfmt = self.keyfmt_var.get()
        return (
            t.get("title") or "",
            t.get("artist") or "",
            t.get("bpm") or "",
            format_key(t.get("camelot"), keyfmt),
            self._energy_display(t),
            f"{score * 100:.0f}",
        )

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        key = {"title": 0, "artist": 1, "bpm": 2, "key": 3,
               "energy": 4, "score": 5}[self._sort_col]

        def sort_val(item):
            score, t, _ = item
            v = self._row_values(score, t)[key]
            try:
                return float(v)
            except (ValueError, TypeError):
                return str(v).lower()

        rows = sorted(self.matches, key=sort_val, reverse=self._sort_rev)
        self._row_tracks = []
        for i, (score, t, _plabel) in enumerate(rows):
            tag = "even" if i % 2 == 0 else "odd"
            iid = self.tree.insert("", "end", values=self._row_values(score, t),
                                   tags=(tag,))
            self._row_tracks.append((iid, t))

    # ================= drag to Serato =================
    def _drag_init(self, event):
        sel = self.tree.selection()
        if not sel:
            return ""
        vals = self.tree.item(sel[0])["values"]
        for iid, t in getattr(self, "_row_tracks", []):
            if iid == sel[0]:
                path = t.get("path", "")
                # DND_FILES expects a list; braces protect spaces
                return ("{" + path + "}",)
        return ""

    # ================= cratetube fill =================
    def _get_download_dir(self):
        """Return download dir, prompting first time."""
        s = load_settings()
        d = s.get("download_dir")
        if d and os.path.isdir(d):
            return d
        d = filedialog.askdirectory(
            title="Choose download folder for new tracks",
            mustexist=True)
        if d:
            s["download_dir"] = d
            save_settings(s)
            return d
        return None

    def _fill_pocket(self):
        suggs = getattr(self, "_ct_suggestions", [])
        if not suggs:
            return
        DownloadPopup(self, suggs, self._get_download_dir(),
                      self._on_track_downloaded)

    def _on_track_downloaded(self, path, artist, title):
        """Called (via msg queue) when a download finishes."""
        from analyzer import analyze_file
        try:
            a = analyze_file(path)
            if a:
                self.lib.upsert(path, artist, title, a)
                self.tracks = self.lib.all_tracks()
                self._refresh_count_label()
                self._rematch()
        except Exception as e:
            self.status_var.set(f"Analyze failed for {title}: {e}")

    def _quit(self):
        try:
            self.watcher.stop()
        except Exception:
            pass
        self.destroy()


class DownloadPopup(tk.Toplevel):
    """One-click YouTube MP3 download for unowned pocket suggestions."""

    def __init__(self, parent, suggestions, download_dir, on_done_cb):
        super().__init__(parent)
        self.parent_win = parent
        self.suggestions = suggestions
        self.download_dir = download_dir
        self.on_done_cb = on_done_cb
        self.title("Download missing tracks")
        self.geometry("460x420")
        self.configure(bg=BG)
        self.attributes("-topmost", True)

        hdr = ttk.Frame(self, style="Dark.TFrame")
        hdr.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(hdr, text=f"{len(suggestions)} tracks not in your library",
                  style="Gold.TLabel", wraplength=420).pack(anchor="w")
        dir_txt = download_dir or "(no folder chosen)"
        self.dir_var = tk.StringVar(value=f"📁 {dir_txt}")
        row = ttk.Frame(self, style="Dark.TFrame")
        row.pack(fill="x", padx=10, pady=2)
        ttk.Label(row, textvariable=self.dir_var, style="Dim.TLabel",
                  wraplength=340).pack(side="left")
        ttk.Button(row, text="Change…", style="Dark.TButton",
                   command=self._change_dir).pack(side="right")

        # suggestion rows
        body = ttk.Frame(self, style="Dark.TFrame")
        body.pack(fill="both", expand=True, padx=10, pady=4)
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Dark.TFrame")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.rows = []  # (sugg_dict, status_var, dl_button)
        for i, s in enumerate(suggestions):
            fr = ttk.Frame(inner, style="Dark.TFrame")
            fr.pack(fill="x", pady=2)
            bg = ROW_ALT if i % 2 else ROW_BG
            lbl = tk.Label(fr, text=f"{s['artist']} — {s['title']}",
                           bg=bg, fg=FG, font=("Helvetica", 10),
                           anchor="w", wraplength=330, justify="left")
            lbl.pack(side="left", fill="x", expand=True, padx=(4, 2),
                     ipady=4)
            st = tk.StringVar(value="")
            tk.Label(fr, textvariable=st, bg=bg, fg=DIM,
                     font=("Helvetica", 9), width=14).pack(side="left")
            btn = tk.Button(fr, text="⬇", bg=ROW_BG, fg=GOLD, bd=0,
                            font=("Helvetica", 12),
                            activebackground="#3a3a3a")
            btn.configure(command=lambda s=s, st=st, btn=btn:
                          self._download(s, st, btn))
            btn.pack(side="right", padx=4)
            self.rows.append((s, st, btn))

        ttk.Button(self, text="Download all",
                   command=self._download_all,
                   style="Dark.TButton").pack(pady=(4, 10))

    def _change_dir(self):
        d = filedialog.askdirectory(title="Choose download folder",
                                    mustexist=True)
        if d:
            self.download_dir = d
            s = load_settings()
            s["download_dir"] = d
            save_settings(s)
            self.dir_var.set(f"📁 {d}")

    def _safe_filename(self, artist, title):
        base = f"{artist} - {title}".strip(" -")
        for ch in '/\\:*?"<>|':
            base = base.replace(ch, "")
        return base[:120] or "track"

    def _download(self, sugg, status_var, btn):
        if not self.download_dir or not os.path.isdir(self.download_dir):
            messagebox.showwarning(
                "No folder", "Pick a download folder first.")
            return
        btn.configure(state="disabled")
        status_var.set("⏳ downloading…")
        t = threading.Thread(target=self._worker, args=(sugg, status_var),
                             daemon=True)
        t.start()

    def _download_all(self):
        for s, st, btn in self.rows:
            if st.get() in ("", "❌ failed"):
                self._download(s, st, btn)

    def _worker(self, sugg, status_var):
        artist, title = sugg["artist"], sugg["title"]
        query = f"{artist} {title}".strip()
        fname = self._safe_filename(artist, title) + ".mp3"
        outpath = os.path.join(self.download_dir, fname)
        try:
            # 1) find the YouTube video id
            r = subprocess.run(
                ["yt-dlp", f"ytsearch1:{query}", "--print", "%(id)s",
                 "--no-playlist", "--quiet"],
                capture_output=True, text=True, timeout=60)
            vid = (r.stdout or "").strip().split("\n")[0].strip()
            if not vid or r.returncode != 0:
                raise RuntimeError("YouTube search found nothing")
            # 2) download as MP3 with a clean filename
            r = subprocess.run(
                ["yt-dlp", f"https://youtube.com/watch?v={vid}",
                 "-x", "--audio-format", "mp3", "--audio-quality", "0",
                 "-o", outpath, "--no-playlist", "--quiet", "--no-warnings"],
                capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not os.path.exists(outpath):
                err = (r.stderr or "").strip().split("\n")[-1][:120]
                raise RuntimeError(err or "download failed")
            # tell the main window (thread-safe via queue)
            self.parent_win.msg_q.put(
                ("download_done", (outpath, artist, title)))
            self.after(0, lambda: status_var.set("✅ in library"))
            self.after(0, lambda: self.parent_win.status_var.set(
                f"Downloaded: {artist} — {title}"))
        except FileNotFoundError:
            self.after(0, lambda: status_var.set("❌ no yt-dlp"))
            self.after(0, lambda: self.parent_win.status_var.set(
                "yt-dlp not found — pip install yt-dlp"))
        except Exception as e:
            msg = str(e)[:60]
            self.after(0, lambda m=msg: status_var.set("❌ failed"))
            self.after(0, lambda m=msg: self.parent_win.status_var.set(
                f"Download failed ({title}): {m}"))


def main():
    try:
        w = LiveWindow()
        w.mainloop()
    except Exception:
        # A windowed .app shows no console — without this, a startup crash
        # looks like "nothing happens". Log the traceback where Keith (and
        # Amy) can actually read it.
        import traceback
        log_path = os.path.expanduser("~/cratematch-live-error.log")
        try:
            with open(log_path, "a") as f:
                f.write(traceback.format_exc() + "\n---\n")
        except Exception:
            pass
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
