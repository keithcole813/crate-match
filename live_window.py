#!/usr/bin/env python3
"""CrateMatch Live — always-on-top DJ companion window.

Watches Serato's history for the currently playing track, then shows a
ranked list of what to play next (pocket-aware: genre/era context +
audio similarity). Drag any result straight onto a Serato deck.

Dark Serato-style UI. Runs alongside Serato on the MacBook.
"""
import os
import queue
import tkinter as tk
from tkinter import ttk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except ImportError:
    from tkinter import Tk as _Tk
    HAVE_DND = False
    DND_FILES = None

from analyzer import Library
from matcher import similarity
from pocket import (classify_pocket, find_pocket_matches, format_key,
                    pocket_score)
from serato_watcher import SeratoWatcher
from cratetube_bridge import bridge_for_seed

DB_PATH = os.path.expanduser("~/.crate-match/library.db")

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
        self.status_var.set(f"{self.lib.count()} tracks indexed")

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
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "now_playing":
                    self._handle_now_playing(payload)
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def _handle_now_playing(self, now):
        if not now:
            self.np_var.set("Waiting for Serato…")
            return
        label = f"{now.get('artist') or '?'} — {now.get('title') or '?'}"
        self.np_var.set(label)
        seed = self._find_seed(now)
        if not seed:
            self.pocket_var.set("Track not in CrateMatch library — scan it first")
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
        self.matches = find_pocket_matches(self.seed, self.tracks, top_n=n)
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
    def _fill_pocket(self):
        import json
        import tempfile
        suggs = getattr(self, "_ct_suggestions", [])
        if not suggs:
            return
        path = os.path.join(tempfile.gettempdir(),
                            "cratematch_pocket_suggestions.json")
        with open(path, "w") as f:
            json.dump(suggs, f, indent=2)
        self.status_var.set(f"Wrote {len(suggs)} suggestions → {path} "
                            "(open in CrateTube)")

    def _quit(self):
        try:
            self.watcher.stop()
        except Exception:
            pass
        self.destroy()


def main():
    w = LiveWindow()
    w.mainloop()


if __name__ == "__main__":
    main()
