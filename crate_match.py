#!/usr/bin/env python3
"""
CrateMatch — djay-style "find similar songs" for your Serato offline library.

Scans your music folder, analyzes every track (BPM, musical key as Camelot
code, energy, spectral character), then for any track shows a ranked list
of similar tracks. Preview them in the app, export the set as a Serato
.crate to drop straight into your DJ workflow.

macOS-first (uses afplay for preview playback).
"""
import os
import platform
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from analyzer import Library, scan_folder
from crate_io import verify_crate, write_crate
from matcher import find_similar

APP_TITLE = "CrateMatch"
DB_PATH = os.path.expanduser("~/.crate-match/library.db")
IS_MAC = platform.system() == "Darwin"


def artist_title_from_file(path):
    """Tags first, filename fallback."""
    try:
        from mutagen import File as MutagenFile
        m = MutagenFile(path, easy=True)
        if m:
            artist = (m.get("artist") or [None])[0]
            title = (m.get("title") or [None])[0]
            if artist and title:
                return artist, title
    except Exception:
        pass
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d+\s+", "", stem)
    if " - " in stem:
        a, t = stem.split(" - ", 1)
        return a.strip(), t.strip()
    return "Unknown Artist", stem


class Player:
    """Preview playback: afplay on mac, ffplay elsewhere."""

    def __init__(self):
        self.proc = None

    def play(self, path):
        self.stop()
        try:
            if IS_MAC:
                self.proc = subprocess.Popen(
                    ["afplay", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                self.proc = subprocess.Popen(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("960x640")
        self.lib = Library(DB_PATH)
        self.player = Player()
        self.msg_q = queue.Queue()
        self.tracks = []
        self.matches = []
        self.seed = None

        self._build()
        self._refresh_tracks()
        self.after(120, self._drain)

    # ================= UI =================
    def _build(self):
        # folder row
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Music folder:").pack(side="left")
        self.folder_var = tk.StringVar(value=os.path.expanduser("~/Music"))
        ttk.Entry(top, textvariable=self.folder_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="Browse…", command=self._browse_folder).pack(side="left")
        ttk.Button(top, text="Scan", command=self._scan).pack(side="left", padx=(6, 0))
        self.scan_status = tk.StringVar(value=f"{self.lib.count()} tracks indexed")
        ttk.Label(top, textvariable=self.scan_status).pack(side="left", padx=10)

        # main panes
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(paned)
        ttk.Label(left, text="Tracks (type to filter):").pack(anchor="w")
        self.filter_var = tk.StringVar()
        fentry = ttk.Entry(left, textvariable=self.filter_var)
        fentry.pack(fill="x", pady=(0, 4))
        fentry.bind("<KeyRelease>", lambda e: self._refresh_tracks())
        self.track_list = tk.Listbox(left, font=("Segoe UI", 10))
        self.track_list.pack(fill="both", expand=True)
        self.track_list.bind("<<ListboxSelect>>", self._on_seed)
        paned.add(left, weight=1)

        right = ttk.Frame(paned)
        self.seed_label = tk.StringVar(value="Pick a track to find its matches")
        ttk.Label(right, textvariable=self.seed_label, font=("Segoe UI", 11, "bold"),
                  wraplength=420).pack(anchor="w", pady=(0, 4))

        cols = ("score", "title", "artist", "bpm", "key")
        self.match_tree = ttk.Treeview(right, columns=cols, show="headings", height=18)
        for c, w in zip(cols, (60, 220, 140, 60, 60)):
            self.match_tree.heading(c, text=c.title())
            self.match_tree.column(c, width=w)
        self.match_tree.pack(fill="both", expand=True)
        self.match_tree.bind("<Double-1>", lambda e: self._play_match())
        paned.add(right, weight=2)

        # bottom bar
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=8, pady=8)
        ttk.Button(bottom, text="▶ Play", command=self._play_match).pack(side="left")
        ttk.Button(bottom, text="⏹ Stop", command=self._stop).pack(side="left", padx=6)
        self.now_playing = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.now_playing).pack(side="left", padx=10)
        ttk.Button(bottom, text="Export matches as Serato crate…",
                   command=self._export_crate).pack(side="right")

    def _drain(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "scan_status":
                    self.scan_status.set(payload)
                elif kind == "tracks":
                    self._refresh_tracks()
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # ================= library =================
    def _browse_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get())
        if d:
            self.folder_var.set(d)

    def _scan(self):
        folder = self.folder_var.get()
        if not os.path.isdir(folder):
            messagebox.showwarning(APP_TITLE, "Pick a valid music folder first.")
            return
        self.scan_status.set("Scanning…")

        def run():
            def prog(i, n, name):
                self.msg_q.put(("scan_status", f"Analyzing {i}/{n}: {name[:40]}"))
            added, _ = scan_folder(self.lib, folder, artist_title_from_file, prog)
            self.msg_q.put(("scan_status",
                            f"{self.lib.count()} tracks indexed ({added} new)"))
            self.msg_q.put(("tracks", None))

        threading.Thread(target=run, daemon=True).start()

    def _refresh_tracks(self):
        filt = self.filter_var.get().lower() if hasattr(self, "filter_var") else ""
        self.tracks = self.lib.all_tracks()
        self.track_list.delete(0, "end")
        self._track_idx = []
        for t in self.tracks:
            label = f"{t['artist']} - {t['title']}"
            if filt and filt not in label.lower():
                continue
            self.track_list.insert("end", label)
            self._track_idx.append(t)

    def _on_seed(self, _e=None):
        sel = self.track_list.curselection()
        if not sel:
            return
        self.seed = self._track_idx[sel[0]]
        self.seed_label.set(
            f"Matches for: {self.seed['artist']} - {self.seed['title']} "
            f"({self.seed['bpm']} BPM, {self.seed['camelot']})")
        self.matches = find_similar(self.seed, self.tracks, top_n=30)
        self.match_tree.delete(*self.match_tree.get_children())
        for score, t in self.matches:
            self.match_tree.insert("", "end", values=(
                f"{score:.2f}", t["title"], t["artist"],
                t["bpm"], t["camelot"]))

    def _selected_match(self):
        sel = self.match_tree.selection()
        if not sel:
            return None
        vals = self.match_tree.item(sel[0])["values"]
        for _, t in self.matches:
            if t["title"] == vals[1] and t["artist"] == vals[2]:
                return t
        return None

    def _play_match(self):
        t = self._selected_match()
        if not t:
            messagebox.showinfo(APP_TITLE, "Pick a match from the list first.")
            return
        if self.player.play(t["path"]):
            self.now_playing.set(f"▶ {t['artist']} - {t['title']}")
        else:
            messagebox.showwarning(
                APP_TITLE, "Couldn't start playback (afplay/ffplay missing?).")

    def _stop(self):
        self.player.stop()
        self.now_playing.set("")

    def _export_crate(self):
        if not self.matches:
            messagebox.showinfo(APP_TITLE, "Find matches first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".crate",
            filetypes=[("Serato crate", "*.crate")],
            initialfile="CrateMatch.crate")
        if not path:
            return
        track_paths = [self.seed["path"]] + [t["path"] for _, t in self.matches[:25]]
        write_crate(path, track_paths)
        n = verify_crate(path)
        messagebox.showinfo(
            APP_TITLE,
            f"Crate written with {n} tracks.\nDrop it into Serato's _Serato_/Subcrates folder.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
