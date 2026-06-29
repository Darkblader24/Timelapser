# -*- coding: utf-8 -*-
"""Timelapser – GUI zur Erstellung von Zeitraffer-Videos"""

import os
import io
import re
import shutil
import hashlib
import tempfile
import threading
import subprocess
import pathlib
import queue
import winsound
import ctypes
from ctypes import wintypes
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import tkinter
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image, ImageOps, ImageTk
from PIL.ExifTags import TAGS, IFD

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

THUMB_W, THUMB_H = 150, 100
THUMB_BG = (38, 38, 42)
CARD_COLS = 4
CARD_PAD = 5  # gap (px) around each card; cards are place()d, not gridded
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

# Parallel decode: PIL releases the GIL while decoding, so a small pool of
# threads gives a real speedup. Capped low so it stays kind to old hardware.
THUMB_WORKERS = max(2, min(4, (os.cpu_count() or 2)))

# Max brand-new cards to build in a single _render_window tick. Caps the work
# per frame so the first visible rows paint fast and the rest fill in smoothly.
NEW_CARDS_PER_TICK = 6

# While the user is actively scrolling we re-render the virtualised window at
# ~60fps (SCROLL_FAST_MS) so the recycled cards keep pace with the native canvas
# scroll instead of lagging a few frames behind it – that lag is what leaves
# stale cards "ghosting" at the edge of the viewport on slow hardware. Once the
# view stops moving we coast for a short while, then fall back to SCROLL_IDLE_MS
# so an idle window costs almost nothing.
SCROLL_FAST_MS = 16
SCROLL_IDLE_MS = 60
SCROLL_COAST_TICKS = 12  # ~200ms of fast polling after the last movement

DEFAULTS = {
    "fps": 30,
    "resolution": "3840x2160",
    "quality": "medium",
    "speed": "medium",
    "encoder": "auto",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _exif_date(path: str) -> datetime | None:
    try:
        img = Image.open(path)
        exif = img._getexif()
        if exif:
            for tag_id, val in exif.items():
                if TAGS.get(tag_id) == "DateTimeOriginal":
                    return datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _file_date(path: str) -> datetime:
    d = _exif_date(path)
    return d or datetime.fromtimestamp(os.path.getmtime(path))


def _file_num(path: str) -> int:
    m = re.search(r"\d+", os.path.basename(path))
    return int(m.group()) if m else 0


# ── thumbnail generation ──────────────────────────────────────────────────────
# Three layers, fastest first: (1) a persistent on-disk cache of finished thumbs,
# (2) the small thumbnail most cameras embed in the file's EXIF, and (3) a
# reduced-scale ("draft") decode of the full image. Together these make the grid
# load near-instantly on repeat visits and an order of magnitude faster on the
# first pass, which matters a lot on slow hardware.

# EXIF orientation → PIL transpose ops (same mapping ImageOps.exif_transpose uses).
_ORIENT_OPS = {
    2: (Image.Transpose.FLIP_LEFT_RIGHT,),
    3: (Image.Transpose.ROTATE_180,),
    4: (Image.Transpose.FLIP_TOP_BOTTOM,),
    5: (Image.Transpose.TRANSPOSE,),
    6: (Image.Transpose.ROTATE_270,),
    7: (Image.Transpose.TRANSVERSE,),
    8: (Image.Transpose.ROTATE_90,),
}

_THUMB_BYTES = THUMB_W * THUMB_H * 3  # raw RGB size of a finished thumbnail
_cache_dir: str | None = None


def _apply_orientation(img: Image.Image, orientation: int) -> Image.Image:
    for op in _ORIENT_OPS.get(orientation, ()):
        img = img.transpose(op)
    return img


def _thumb_cache_path(path: str, st: os.stat_result) -> str:
    """Cache filename keyed by path + mtime + size, so edits invalidate it."""
    global _cache_dir
    if _cache_dir is None:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        _cache_dir = os.path.join(base, "Timelapser", "thumbs")
        try:
            os.makedirs(_cache_dir, exist_ok=True)
        except OSError:
            _cache_dir = tempfile.gettempdir()
    key = f"{os.path.normcase(os.path.abspath(path))}|{st.st_mtime_ns}|{st.st_size}"
    h = hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()
    return os.path.join(_cache_dir, f"{h}.thumb")


def _embedded_thumb(img: Image.Image):
    """Return (thumbnail Image | None, orientation) from the file's EXIF.

    Reading the embedded preview avoids decoding the full-resolution image at
    all – typically a tens-of-megapixels saving per file.
    """
    try:
        exif = img.getexif()
        if not exif:
            return None, 1
        orientation = exif.get(0x0112, 1)
        ifd1 = exif.get_ifd(IFD.IFD1)
        off, length = ifd1.get(0x0201), ifd1.get(0x0202)  # JPEG offset / length
        raw = img.info.get("exif")
        if not off or not length or not raw:
            return None, orientation
        tiff = raw[6:] if raw[:6] == b"Exif\x00\x00" else raw
        thumb = Image.open(io.BytesIO(tiff[off:off + length]))
        thumb.load()
        return thumb, orientation
    except Exception:
        return None, 1


def _letterbox(src: Image.Image) -> Image.Image:
    """Fit src inside the thumb box on a neutral background."""
    if src.mode != "RGB":
        src = src.convert("RGB")
    src.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS, reducing_gap=2.0)
    bg = Image.new("RGB", (THUMB_W, THUMB_H), THUMB_BG)
    bg.paste(src, ((THUMB_W - src.width) // 2, (THUMB_H - src.height) // 2))
    return bg


def _build_thumb(path: str) -> Image.Image | None:
    """Return a finished THUMB_W×THUMB_H RGB thumbnail using the fastest source."""
    try:
        st = os.stat(path)
    except OSError:
        return None

    cpath = _thumb_cache_path(path, st)
    try:
        with open(cpath, "rb") as f:
            data = f.read()
        if len(data) == _THUMB_BYTES:
            return Image.frombytes("RGB", (THUMB_W, THUMB_H), data)
    except OSError:
        pass

    try:
        img = Image.open(path)
    except Exception:
        return None

    src = None
    emb, orientation = _embedded_thumb(img)
    # Use the embedded preview only when it's big enough to fill the box crisply.
    if emb is not None and emb.width >= 120 and emb.height >= 80:
        src = _apply_orientation(emb, orientation)

    if src is None:
        try:
            # Decode at a reduced DCT scale (1/2, 1/4, 1/8) – far less work than
            # a full decode for large JPEGs, then exif_transpose for rotation.
            img.draft("RGB", (THUMB_W * 2, THUMB_H * 2))
            src = ImageOps.exif_transpose(img)
        except Exception:
            return None

    try:
        bg = _letterbox(src)
    except Exception:
        return None

    # Persist as raw RGB: no re-encode/-decode on the next load, just a memcpy.
    try:
        tmp = cpath + ".tmp"
        with open(tmp, "wb") as f:
            f.write(bg.tobytes())
        os.replace(tmp, cpath)
    except OSError:
        pass
    return bg


def _unique_path(path: str) -> str:
    """Return path unchanged if it doesn't exist, otherwise path (1).ext, (2).ext …"""
    if not os.path.exists(path):
        return path
    p = pathlib.Path(path)
    i = 1
    while True:
        candidate = p.parent / f"{p.stem} ({i}){p.suffix}"
        if not candidate.exists():
            return str(candidate)
        i += 1


def _reveal_in_explorer(path: str) -> bool:
    """Open the containing folder and select `path`, scrolling it into view.

    Uses the Windows shell API (SHOpenFolderAndSelectItems) instead of
    `explorer /select,<path>`. The command-line variant strips its own quotes
    and then splits the path at the first space, so a name like
    "timelapse (1).mp4" would only open the folder without selecting anything.
    """
    path = os.path.normpath(path)
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32

    shell32.ILCreateFromPathW.restype = ctypes.c_void_p
    shell32.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell32.ILFree.restype = None
    shell32.ILFree.argtypes = [ctypes.c_void_p]

    ole32.CoInitialize(None)
    try:
        pidl = shell32.ILCreateFromPathW(path)
        if not pidl:
            return False
        try:
            hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        finally:
            shell32.ILFree(pidl)
        return hr == 0
    finally:
        ole32.CoUninitialize()


def _popen_kwargs() -> dict:
    """STARTUPINFO flags that suppress console windows for all ffmpeg calls."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si, "creationflags": subprocess.CREATE_NO_WINDOW}


def _test_encoder(enc: str, kw: dict) -> bool:
    """Returns True only if a real short encode with this encoder succeeds."""
    try:
        r = subprocess.run([
            "ffmpeg", "-loglevel", "warning",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=1",
            "-t", "0.1", "-c:v", enc, "-f", "null", "-",
        ], capture_output=True, timeout=15, **kw)
        err = (r.stderr or b"").lower()
        # Reject if returncode != 0 OR if stderr contains known failure keywords
        if r.returncode != 0:
            return False
        for bad in (b"cannot load", b"error", b"invalid", b"not supported"):
            if bad in err:
                return False
        return True
    except Exception:
        return False


def _detect_encoder(pref: str) -> str:
    kw = _popen_kwargs()

    if pref != "auto":
        # Still validate the explicitly chosen encoder; fall back silently if broken
        return pref if _test_encoder(pref, kw) else "libx264"

    try:
        r = subprocess.run(["ffmpeg", "-encoders"], capture_output=True, text=True,
                           timeout=10, **kw)
        candidates = [hw for hw in ("h264_nvenc", "h264_videotoolbox", "h264_amf", "h264_qsv")
                      if hw in r.stdout]
    except Exception:
        return "libx264"

    for enc in candidates:
        if _test_encoder(enc, kw):
            return enc

    return "libx264"


def _install_ffmpeg() -> bool:
    if shutil.which("ffmpeg"):
        return True
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 4  # SW_SHOWNOACTIVATE: sichtbar aber kein Fokus-Klau
        subprocess.run(
            ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
             "--accept-source-agreements", "--accept-package-agreements"],
            check=True, timeout=300,
            startupinfo=si,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        for candidate in (r"C:\Program Files\FFmpeg\bin", r"C:\ffmpeg\bin"):
            if os.path.isdir(candidate):
                os.environ["PATH"] += os.pathsep + candidate
        if shutil.which("ffmpeg"):
            return True
    except Exception:
        pass
    return False



def _center_over_parent(win, parent, width: int, height: int):
    """Place a Toplevel centered over its parent window (same monitor)."""
    parent.update_idletasks()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    x = px + (pw - width) // 2
    y = py + (ph - height) // 2
    win.geometry(f"{width}x{height}+{x}+{y}")


# ── Error dialog (full ffmpeg output) ────────────────────────────────────────

class ErrorDialog(ctk.CTkToplevel):
    def __init__(self, parent, message: str):
        super().__init__(parent)
        self.title("Fehler beim Rendern")
        self.transient(parent)
        _center_over_parent(self, parent, 640, 420)
        self.grab_set()

        ctk.CTkLabel(self, text="FFmpeg-Fehler",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(18, 8))
        tb = ctk.CTkTextbox(self, wrap="word",
                            font=ctk.CTkFont(family="Consolas", size=11))
        tb.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        tb.insert("1.0", message)
        tb.configure(state="disabled")
        ctk.CTkButton(self, text="Schließen", width=120,
                      command=self.destroy).pack(pady=12)


# ── Settings dialog ───────────────────────────────────────────────────────────

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, settings: dict, on_save):
        super().__init__(parent)
        self.title("Einstellungen")
        self.transient(parent)
        _center_over_parent(self, parent, 420, 360)
        self.resizable(False, False)
        self.grab_set()
        self._on_save = on_save

        ctk.CTkLabel(self, text="Einstellungen",
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(20, 14))

        def row(label, make_widget):
            f = ctk.CTkFrame(self, fg_color="transparent")
            f.pack(fill="x", padx=28, pady=5)
            ctk.CTkLabel(f, text=label, width=140, anchor="w").pack(side="left")
            w = make_widget(f)
            w.pack(side="left")
            return w

        def entry(val, w=130):
            def make(f):
                e = ctk.CTkEntry(f, width=w)
                e.insert(0, val)
                return e
            return make

        def opt(vals, default, w=130):
            def make(f):
                o = ctk.CTkOptionMenu(f, values=vals, width=w)
                o.set(default)
                return o
            return make

        def resolution(val, w=58):
            try:
                cur_w, cur_h = val.lower().split("x")
            except ValueError:
                cur_w, cur_h = "", ""
            def make(f):
                g = ctk.CTkFrame(f, fg_color="transparent")
                ew = ctk.CTkEntry(g, width=w)
                ew.insert(0, cur_w)
                ew.pack(side="left")
                ctk.CTkLabel(g, text="x", width=20).pack(side="left")
                eh = ctk.CTkEntry(g, width=w)
                eh.insert(0, cur_h)
                eh.pack(side="left")
                g.width_entry, g.height_entry = ew, eh
                return g
            return make

        self._fps  = row("FPS:",            entry(str(settings["fps"])))
        self._res  = row("Auflösung:",       resolution(settings["resolution"]))
        self._qual = row("Qualität:",        opt(["high", "medium", "low"],          settings["quality"]))
        self._spd  = row("Geschwindigkeit:", opt(["slow", "medium", "fast"],         settings["speed"]))
        self._enc  = row("Encoder:",         opt(
            ["auto", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"], settings["encoder"], w=160))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(fill="x", padx=28, pady=20)
        ctk.CTkButton(bf, text="Abbrechen", fg_color=("gray70", "gray30"),
                      command=self.destroy).pack(side="left", expand=True, padx=4)
        ctk.CTkButton(bf, text="Speichern", command=self._save).pack(side="left", expand=True, padx=4)

    def _save(self):
        try:
            fps = int(self._fps.get())
            assert fps > 0
        except (ValueError, AssertionError):
            messagebox.showerror("Fehler", "FPS muss eine positive Ganzzahl sein.", parent=self)
            return
        res = f"{self._res.width_entry.get().strip()}x{self._res.height_entry.get().strip()}"
        if not re.match(r"^\d+x\d+$", res.lower()):
            messagebox.showerror("Fehler", "Auflösung: Breite und Höhe als positive Ganzzahlen, z.B. 3840 x 2160", parent=self)
            return
        self._on_save({
            "fps": fps, "resolution": res,
            "quality": self._qual.get(),
            "speed": self._spd.get(),
            "encoder": self._enc.get(),
        })
        self.destroy()


# ── Image card ────────────────────────────────────────────────────────────────

# CTkFont objects are expensive to build and are designed to be shared. Creating
# fresh ones per card (×3) dominated card-construction time on big folders, so we
# build each variant once, lazily (a Tk root must exist first), and reuse it.
_FONT_CACHE: dict = {}


def _shared_font(**kw):
    key = tuple(sorted(kw.items()))
    f = _FONT_CACHE.get(key)
    if f is None:
        f = ctk.CTkFont(**kw)
        _FONT_CACHE[key] = f
    return f


class ImageCard(ctk.CTkFrame):
    NORMAL_FG = ("gray87", "gray22")
    DRAG_FG = ("gray75", "gray34")

    # Cards are expensive to build, so the grid recycles a small pool of them
    # (see App._render_window). A card is created blank once, then re-bound to
    # whatever path currently occupies its on-screen slot via bind_item().

    def __init__(self, parent, on_remove,
                 on_drag_start=None, on_drag_motion=None, on_drag_drop=None):
        super().__init__(parent, corner_radius=8, fg_color=self.NORMAL_FG)
        self.path: str | None = None
        self._on_remove = on_remove
        self._on_drag_start = on_drag_start
        self._on_drag_motion = on_drag_motion
        self._on_drag_drop = on_drag_drop

        self._img_lbl = ctk.CTkLabel(self, text="")
        self._img_lbl.pack(padx=8, pady=(8, 3))
        self._name_lbl = ctk.CTkLabel(self, text="", font=_shared_font(size=11),
                                      text_color=("gray25", "gray75"))
        self._name_lbl.pack()
        self._date_lbl = ctk.CTkLabel(self, text="", font=_shared_font(size=10),
                                      text_color=("gray50", "gray50"))
        self._date_lbl.pack(pady=(0, 6))

        ctk.CTkButton(
            self, text="✕", width=22, height=22,
            font=_shared_font(size=11, weight="bold"),
            fg_color="transparent",
            hover_color=("#cc3333", "#aa2222"),
            text_color=("gray50", "gray55"),
            command=self._remove,
        ).place(relx=1, rely=0, anchor="ne", x=-4, y=4)

        # Drag-to-reorder: bind on the card and its passive children so a drag
        # can start anywhere on the card (the ✕ button keeps its own handler).
        for w in (self, self._img_lbl, self._name_lbl, self._date_lbl):
            w.bind("<Button-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)

    def bind_item(self, path: str, date: datetime, thumb: ctk.CTkImage):
        """Re-point this (possibly recycled) card at a different image."""
        self.path = path
        name = os.path.basename(path)
        self._name_lbl.configure(text=(name[:18] + "…") if len(name) > 19 else name)
        self._date_lbl.configure(text=date.strftime("%d.%m.%Y %H:%M") if date else "")
        self._img_lbl.configure(image=thumb)
        self.set_dragging(False)

    def _remove(self):
        if self.path is not None:
            self._on_remove(self.path)

    def _press(self, e):
        if self._on_drag_start and self.path is not None:
            self._on_drag_start(self.path, e)

    def _motion(self, e):
        if self._on_drag_motion and self.path is not None:
            self._on_drag_motion(self.path, e)

    def _release(self, e):
        if self._on_drag_drop and self.path is not None:
            self._on_drag_drop(self.path, e)

    def set_thumb(self, img: ctk.CTkImage):
        self._img_lbl.configure(image=img)

    def set_date(self, date: datetime):
        self._date_lbl.configure(text=date.strftime("%d.%m.%Y %H:%M"))

    def set_dragging(self, on: bool):
        self.configure(fg_color=self.DRAG_FG if on else self.NORMAL_FG)


# ── Main window ───────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Timelapser")
        self.geometry("1140x760")
        self.minsize(920, 600)

        self._settings: dict = dict(DEFAULTS)
        self._all_paths: list[str] = []
        self._image_paths: list[str] = []
        self._dates: dict[str, datetime] = {}
        self._thumbs: dict[str, ctk.CTkImage] = {}
        self._sort_by = "date"
        self._reversed = False
        self._output_path = ""
        self._rendering = False
        self._cancel_render = False
        self._proc: subprocess.Popen | None = None
        self._ffmpeg_ready = True
        self._ffmpeg_installing = False
        self._session = 0
        # Virtualised grid: only the cards in (or just outside) the viewport
        # exist as widgets. A small pool is recycled as the user scrolls, so the
        # widget count – and the work per frame – stays constant no matter how
        # many thousands of images the folder holds. The full list lives only as
        # cheap data (`_image_paths`, `_dates`, `_thumbs`).
        # `_cards_by_path` – path → card for the slots currently on screen.
        # `_card_pool`     – idle cards waiting to be re-bound to a new slot.
        self._cards_by_path: dict[str, ImageCard] = {}
        self._card_pool: list[ImageCard] = []
        self._scroll_spacer = None   # invisible widget that gives the scroll its height
        self._reserved_rows = 0      # row count the spacer currently reserves height for
        self._win_range = (0, 0)     # slice of _image_paths currently on screen
        self._dragging = False       # freeze recycling while a drag is active

        # Lazy thumbnails: built off-thread only for on-screen cards.
        # `_thumb_q`  – finished previews flowing back to the UI thread.
        # `_thumb_jobs` – paths requested by the viewport, picked up by workers.
        # `_thumb_requested` – paths already queued, so we never request twice.
        self._thumb_q: queue.Queue = queue.Queue()
        self._thumb_jobs: queue.Queue = queue.Queue()
        self._thumb_requested: set[str] = set()
        self._last_yview = None
        # Countdown of remaining fast-poll ticks; refreshed on every movement so
        # the scroll watcher stays at ~60fps through the whole gesture and the
        # brief coast after it, then relaxes to the idle rate.
        self._scroll_coast = 0
        self._row_h = 0  # cached card-row height (px) for viewport sizing
        self._col_w = 0  # cached card-column width (px) for placing cards
        # Background EXIF date refinement feeds accurate capture times back in.
        self._date_q: queue.Queue = queue.Queue()
        self._refine_started = False

        # Drag-to-reorder state
        self._drag_path: str | None = None
        self._drag_active = False
        self._drag_start = (0, 0)
        self._drop_index: int | None = None
        self._drop_indicator: tkinter.Frame | None = None
        self._drag_ghost: tkinter.Toplevel | None = None
        self._drag_ghost_img = None  # keep PhotoImage ref alive

        ph = Image.new("RGB", (THUMB_W, THUMB_H), THUMB_BG)
        self._placeholder = ctk.CTkImage(light_image=ph, dark_image=ph,
                                         size=(THUMB_W, THUMB_H))

        self._build_ui()
        self._set_icon()
        self._measure_row_height()   # cheap now (grid empty); cached thereafter

        # Persistent decode workers: they idle on `_thumb_jobs` until the
        # viewport requests previews, then race to produce them.
        for _ in range(THUMB_WORKERS):
            threading.Thread(target=self._thumb_worker, daemon=True).start()

        self.after(200, self._ffmpeg_check)
        self.after(80, self._drain_thumbs)
        self.after(120, self._drain_dates)
        self.after(150, self._watch_scroll)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = ctk.CTkFrame(self, height=60, corner_radius=0,
                            fg_color=("gray82", "gray15"))
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkLabel(hdr, text="⏱  Timelapser",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=18)
        ctk.CTkButton(hdr, text="📁  Ordner öffnen", height=36, width=158,
                      command=self._pick_folder).pack(side="left", padx=8, pady=12)
        ctk.CTkLabel(hdr, text="ℹ  Wähle ein Bild aus, um den ganzen Ordner zu laden",
                     font=ctk.CTkFont(size=11),
                     text_color=("gray45", "gray55")).pack(side="left", padx=(0, 6))
        self._hdr_lbl = ctk.CTkLabel(hdr, text="",
                                     font=ctk.CTkFont(size=12),
                                     text_color=("gray40", "gray55"))
        self._hdr_lbl.pack(side="left", padx=10)

        # Body
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=10)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=0)
        body.rowconfigure(0, weight=1)

        # ── Left: image grid ──
        left = ctk.CTkFrame(body, corner_radius=10)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        # Sort bar
        sb = ctk.CTkFrame(left, fg_color="transparent", height=46)
        sb.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        sb.pack_propagate(False)

        ctk.CTkLabel(sb, text="Sortierung:").pack(side="left", padx=(4, 8))
        self._seg = ctk.CTkSegmentedButton(
            sb, values=["Nach Datum", "Nach Dateiname"],
            command=self._on_sort_change, width=250,
        )
        self._seg.set("Nach Datum")
        self._seg.pack(side="left")

        self._rev_btn = ctk.CTkButton(
            sb, text="⇅  Umkehren", width=115, height=30,
            fg_color=("gray70", "gray30"),
            command=self._toggle_reverse,
        )
        self._rev_btn.pack(side="left", padx=(10, 0))

        self._count_lbl = ctk.CTkLabel(sb, text="",
                                       font=ctk.CTkFont(size=12),
                                       text_color=("gray40", "gray55"))
        self._count_lbl.pack(side="right", padx=8)

        # Loading spinner (shown while scanning/loading, hidden otherwise)
        self._loading_bar = ctk.CTkProgressBar(left, mode="indeterminate", height=5,
                                               progress_color=("#3b8ed0", "#1f6aa5"))
        # intentionally NOT gridded yet

        # Scrollable grid
        self._grid = ctk.CTkScrollableFrame(left, corner_radius=0,
                                            fg_color="transparent")
        self._grid.grid(row=2, column=0, sticky="nsew", padx=4, pady=(0, 4))
        # Resizing the viewport changes how many rows fit → re-render the window.
        self._grid._parent_canvas.bind(
            "<Configure>", lambda _e: self._render_window(), add="+")
        self._guard_scrollbar()

        # One tall, invisible spacer gives the scroll area its full height. The
        # image cards are then positioned on top of it with place() (O(1) each)
        # rather than gridded into thousands of reserved rows, whose per-scroll
        # relayout was too slow to keep up on weak hardware.
        self._scroll_spacer = ctk.CTkFrame(self._grid, width=1, height=1,
                                            fg_color="transparent")
        self._scroll_spacer.grid(row=0, column=0)

        # ── Right: controls ──
        right = ctk.CTkFrame(body, width=252, corner_radius=10)
        right.grid(row=0, column=1, sticky="ns")
        right.pack_propagate(False)

        ctk.CTkLabel(right, text="Steuerung",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(22, 18))

        ctk.CTkButton(right, text="⚙  Einstellungen", height=40,
                      command=self._open_settings).pack(padx=16, fill="x")

        self._settings_lbl = ctk.CTkLabel(
            right, text=self._fmt_settings(),
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray55"),
            justify="left",
        )
        self._settings_lbl.pack(padx=20, pady=(6, 2), anchor="w")

        self._divider(right)

        ctk.CTkButton(right, text="💾  Ausgabedatei …", height=40,
                      command=self._pick_output).pack(padx=16, fill="x")

        self._output_lbl = ctk.CTkLabel(
            right, text="(kein Pfad gewählt)",
            font=ctk.CTkFont(size=11),
            text_color=("gray40", "gray55"),
            wraplength=216, justify="left",
        )
        self._output_lbl.pack(padx=20, pady=(6, 2), anchor="w")

        self._divider(right)

        self._create_btn = ctk.CTkButton(
            right, text="▶  Timelapse erstellen",
            height=52, font=ctk.CTkFont(size=14, weight="bold"),
            state="disabled", command=self._start_render,
        )
        self._create_btn.pack(padx=16, fill="x")

        self._ffmpeg_hint_lbl = ctk.CTkLabel(
            right, text="",
            font=ctk.CTkFont(size=11),
            text_color=("#b85c00", "#e07820"),
            wraplength=210,
        )
        self._ffmpeg_hint_lbl.pack(padx=16, pady=(5, 0))

        # Progress section – initially hidden, shown only during rendering
        self._pbar_frame = ctk.CTkFrame(right, fg_color="transparent")
        self._pbar = ctk.CTkProgressBar(self._pbar_frame)
        self._pbar.set(0)
        self._pbar.pack(padx=16, pady=(12, 4), fill="x")
        self._pbar_lbl = ctk.CTkLabel(self._pbar_frame, text="",
                                      font=ctk.CTkFont(size=11),
                                      text_color=("gray40", "gray55"))
        self._pbar_lbl.pack(pady=(0, 4))
        self._cancel_btn = ctk.CTkButton(
            self._pbar_frame, text="✕  Abbrechen", height=34,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#cc3333", "#a82828"),
            hover_color=("#b82d2d", "#8f2020"),
            command=self._cancel_render_cb,
        )
        self._cancel_btn.pack(padx=16, pady=(2, 6), fill="x")
        # _pbar_frame intentionally NOT packed here

        self._right_spacer = ctk.CTkFrame(right, fg_color="transparent")
        self._right_spacer.pack(fill="both", expand=True)
        ctk.CTkLabel(right, text="Timelapser v1.0",
                     font=ctk.CTkFont(size=10),
                     text_color=("gray55", "gray42")).pack(pady=10)

    def _guard_scrollbar(self):
        """Break CustomTkinter's scrollbar re-entrancy.

        CTkScrollbar._draw() ends with update_idletasks(), which flushes pending
        events that call _draw() again (via set() or the bar's own <Configure>),
        recursing thousands of levels deep and freezing the UI for many seconds
        – pathologically so with the tall scroll area our virtualised grid uses.
        Wrapping _draw() so a re-entrant call is dropped breaks every path into
        the loop; the outermost draw still paints the bar correctly.
        """
        scrollbar = getattr(self._grid, "_scrollbar", None)
        if scrollbar is None:
            return
        real_draw = scrollbar._draw
        busy = {"v": False}

        def guarded_draw(*args, **kwargs):
            if busy["v"]:
                return
            busy["v"] = True
            try:
                return real_draw(*args, **kwargs)
            finally:
                busy["v"] = False

        scrollbar._draw = guarded_draw

    def _set_icon(self):
        import sys, os
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        ico = os.path.join(base, "icon.ico")
        if os.path.exists(ico):
            self.iconbitmap(ico)

    def _show_spinner(self):
        self._loading_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 2))
        self._loading_bar.start()

    def _hide_spinner(self):
        self._loading_bar.stop()
        self._loading_bar.grid_forget()

    def _divider(self, parent):
        ctk.CTkFrame(parent, height=1,
                     fg_color=("gray70", "gray32")).pack(fill="x", padx=16, pady=14)

    # ── ffmpeg check ──────────────────────────────────────────────────────────

    def _ffmpeg_check(self):
        if shutil.which("ffmpeg"):
            self._ffmpeg_ready = True
            return
        self._ffmpeg_ready = False
        self._ffmpeg_installing = True
        self._update_create_btn()
        self._animate_ffmpeg_status(0)
        def worker():
            ok = _install_ffmpeg()
            self.after(0, lambda: self._on_ffmpeg_done(ok))
        threading.Thread(target=worker, daemon=True).start()

    def _animate_ffmpeg_status(self, tick: int):
        if self._ffmpeg_ready:
            return
        dots = "." * (tick % 3 + 1)
        self._hdr_lbl.configure(text=f"FFmpeg wird installiert{dots}")
        self.after(600, lambda: self._animate_ffmpeg_status(tick + 1))

    def _on_ffmpeg_done(self, ok: bool):
        self._ffmpeg_ready = ok
        self._ffmpeg_installing = False
        if ok:
            self._hdr_lbl.configure(text="FFmpeg bereit.")
            self.after(3000, lambda: self._hdr_lbl.configure(text=""))
        else:
            self._hdr_lbl.configure(text="⚠  FFmpeg: Installation fehlgeschlagen")
            messagebox.showerror(
                "FFmpeg fehlt",
                "FFmpeg konnte nicht automatisch installiert werden.\n"
                "Bitte installieren Sie FFmpeg manuell und starten Sie die App neu.",
                parent=self,
            )
        self._update_create_btn()

    # ── folder loading ────────────────────────────────────────────────────────

    def _pick_folder(self):
        file = filedialog.askopenfilename(
            title="Bild auswählen, um den ganzen Ordner zu laden",
            filetypes=[
                ("Bilder", " ".join(
                    f"*{e} *{e.upper()}" for e in (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp")
                )),
                ("Alle Dateien", "*.*"),
            ],
        )
        if file:
            self._load_folder(os.path.dirname(file))

    def _load_folder(self, folder: str):
        self._session += 1
        self._hdr_lbl.configure(text="Bilder werden gesucht …")
        self._all_paths = []
        self._image_paths = []
        self._dates = {}
        self._thumbs = {}
        self._thumb_requested = set()
        self._last_yview = None
        self._clear_grid()
        self._show_spinner()
        session = self._session

        def worker():
            # One directory read instead of two globs per extension. scandir
            # also hands us each file's mtime for free (no extra stat syscall on
            # Windows), so we get a usable date immediately – no need to open and
            # parse every file's EXIF before the list can appear.
            entries: list[tuple[str, float]] = []
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        if (e.is_file()
                                and os.path.splitext(e.name)[1].lower() in SUPPORTED_EXT):
                            try:
                                mtime = e.stat().st_mtime
                            except OSError:
                                mtime = 0.0
                            entries.append((e.path, mtime))
            except OSError:
                entries = []

            if not entries:
                self.after(0, lambda: (
                    self._hdr_lbl.configure(text="Keine Bilder gefunden."),
                    self._hide_spinner(),
                ))
                return

            self.after(0, lambda: self._begin_stream(session, entries))

        threading.Thread(target=worker, daemon=True).start()

    def _begin_stream(self, session: int, entries: list[tuple[str, float]]):
        if session != self._session:
            return
        self._all_paths = [p for p, _ in entries]
        # Seed every date from the file's mtime so cards show a real date the
        # instant they appear; the accurate EXIF capture time is filled in
        # afterwards in the background and patched into the visible cards live.
        self._dates = {p: datetime.fromtimestamp(mt) if mt else datetime.now()
                       for p, mt in entries}

        # The list is "ready" the moment we have names + dates.
        self._hide_spinner()
        self._count_lbl.configure(text=f"{len(self._all_paths)} Bilder")
        self._hdr_lbl.configure(text=f"{len(self._all_paths)} Bilder")
        self._apply_sort()              # sorts + renders the first viewport
        # EXIF capture times stream in on a background thread; only the handful
        # of on-screen cards are ever touched, so this never blocks the UI.
        if not self._refine_started:
            self._refine_started = True
            self._refine_dates(session)

    def _thumb_worker(self):
        """Daemon: build previews for whatever paths the viewport requests.

        Returns the finished PIL image; the CTkImage (a Tk object) is created on
        the main thread in _drain_thumbs. Creating Tk image objects off-thread
        is what caused stray previews to flash at the top-left of the screen.
        """
        while True:
            session, path = self._thumb_jobs.get()
            if session != self._session:
                continue
            bg = _build_thumb(path)
            if session == self._session:
                self._thumb_q.put((session, path, bg))

    def _watch_scroll(self):
        # Cheap O(1) poll: re-render the window only when the scroll position
        # actually moved, so idling costs nothing. The poll rate adapts – fast
        # while the view is moving (and for a short coast afterwards) so the
        # cards track the native scroll without trailing, idle-slow otherwise.
        if self._dragging:
            self.after(SCROLL_IDLE_MS, self._watch_scroll)
            return
        canvas = getattr(self._grid, "_parent_canvas", None)
        if canvas is not None and self._image_paths:
            try:
                top = canvas.yview()[0]
            except Exception:
                top = None
            if top is not None and top != self._last_yview:
                self._last_yview = top
                self._render_window()
                self._scroll_coast = SCROLL_COAST_TICKS
            elif self._scroll_coast > 0:
                self._scroll_coast -= 1
        delay = SCROLL_FAST_MS if self._scroll_coast > 0 else SCROLL_IDLE_MS
        self.after(delay, self._watch_scroll)

    def _drain_thumbs(self):
        # Per-item error isolation + a guaranteed reschedule: a single failed
        # thumbnail must never kill the loop and freeze every later preview.
        try:
            while True:
                try:
                    sess, p, bg = self._thumb_q.get_nowait()
                except queue.Empty:
                    break
                if sess != self._session or bg is None:
                    continue
                img = ctk.CTkImage(light_image=bg, dark_image=bg,
                                   size=(THUMB_W, THUMB_H))
                self._thumbs[p] = img
                card = self._cards_by_path.get(p)   # only if still on screen
                if card is not None and card.path == p:
                    try:
                        card.set_thumb(img)
                    except Exception:
                        pass
        finally:
            self.after(80, self._drain_thumbs)

    def _refine_dates(self, session: int):
        """Replace the mtime placeholders with real EXIF capture times.

        Runs in the background so it never blocks the UI; each result updates the
        data and, if that image happens to be on screen, its card.
        """
        paths = list(self._all_paths)

        def worker():
            # Gentle concurrency: header-only reads, but on a slow disk too many
            # parallel seeks thrash, and we don't want to starve the decoders.
            with ThreadPoolExecutor(max_workers=2) as ex:
                for p, d in zip(paths, ex.map(_exif_date, paths)):
                    if self._session != session:
                        return
                    if d is not None:
                        self._date_q.put((session, p, d))
            self._date_q.put((session, None, None))  # sentinel: all dates in

        threading.Thread(target=worker, daemon=True).start()

    def _drain_dates(self):
        finalize = False
        try:
            while True:
                try:
                    sess, p, d = self._date_q.get_nowait()
                except queue.Empty:
                    break
                if sess != self._session:
                    continue
                if p is None:
                    finalize = True            # all EXIF dates are now known
                    continue
                self._dates[p] = d
                card = self._cards_by_path.get(p)
                if card is not None and card.path == p:
                    try:
                        card.set_date(d)
                    except Exception:
                        pass
        finally:
            # One final re-sort once accurate dates are in, but only if it
            # actually changes the order (usually mtime already matched).
            if finalize and self._sort_by == "date" and not self._dragging:
                self._apply_sort(reset_scroll=False)
            self.after(150, self._drain_dates)

    # ── virtualised grid ────────────────────────────────────────────────────────

    def _clear_grid(self):
        # Park every live card back in the pool (cheap) rather than destroying
        # widgets we are about to need again for the next folder.
        for card in self._cards_by_path.values():
            card.place_forget()
            self._card_pool.append(card)
        self._cards_by_path = {}
        if self._drop_indicator is not None:
            self._drop_indicator.place_forget()

    def _make_or_take_card(self) -> ImageCard:
        if self._card_pool:
            return self._card_pool.pop()
        return ImageCard(
            self._grid, on_remove=self._remove_image,
            on_drag_start=self._drag_start_cb,
            on_drag_motion=self._drag_motion_cb,
            on_drag_drop=self._drag_drop_cb,
        )

    def _measure_row_height(self):
        """Measure one card's height once, at startup while the grid is empty so
        the update is cheap. Doing it later (with the full reserved scroll area
        in place) would make update_idletasks() flush a giant layout and stall."""
        probe = self._make_or_take_card()
        probe.bind_item("measure.jpg", datetime.now(), self._placeholder)
        probe.place(x=0, y=0)
        probe.update_idletasks()
        h, w = probe.winfo_reqheight(), probe.winfo_reqwidth()
        self._row_h = (h + 2 * CARD_PAD) if h > 1 else 225
        self._col_w = (w + 2 * CARD_PAD) if w > 1 else (THUMB_W + 40)
        probe.place_forget()
        self._card_pool.append(probe)

    def _row_height(self) -> float:
        """Card-row height in px (measured once at startup; cheap lookup)."""
        return float(self._row_h) if self._row_h > 1 else 225.0

    def _col_width(self) -> int:
        """Card-column width in px (measured once at startup; cheap lookup)."""
        return self._col_w if self._col_w > 1 else (THUMB_W + 40)

    def _reserve_rows(self, total_rows: int):
        """Size the invisible spacer so the scroll area spans the whole list
        even though only a window of cards is ever placed on screen. Because the
        cards are positioned with place() (O(1)) rather than gridded into a row
        per image, scrolling never triggers a relayout of the full list – that
        relayout was what couldn't keep up on weak hardware, leaving the edge
        rows a beat behind the scroll."""
        self._reserved_rows = total_rows
        h = int(total_rows * self._row_height()) + CARD_PAD
        if self._scroll_spacer is not None:
            self._scroll_spacer.configure(height=max(1, h))

    def _render_window(self):
        """Show card widgets only for the rows in (or just past) the viewport,
        recycling the rest. O(viewport), independent of folder size."""
        if self._dragging:
            return
        n = len(self._image_paths)
        canvas = getattr(self._grid, "_parent_canvas", None)
        if n == 0 or canvas is None:
            return
        total_rows = (n + CARD_COLS - 1) // CARD_COLS
        row_h = self._row_height()
        try:
            f0 = canvas.yview()[0]
            ch = canvas.winfo_height()
        except Exception:
            return
        vis_rows = max(1, int(ch / row_h) + 1) if ch > 1 else 6
        BUF = 3
        first_row = max(0, int(f0 * total_rows) - BUF)
        last_row = min(total_rows - 1, int(f0 * total_rows) + vis_rows + BUF)
        start, end = first_row * CARD_COLS, min(n, (last_row + 1) * CARD_COLS)
        self._win_range = (start, end)   # the contiguous slice now on screen

        wanted = self._image_paths[start:end]
        wanted_set = set(wanted)
        # Recycle cards that scrolled out of the window.
        for p in list(self._cards_by_path):
            if p not in wanted_set:
                card = self._cards_by_path.pop(p)
                card.place_forget()
                self._card_pool.append(card)
        # Bind/position a card for every slot now in the window. Creating a card
        # from scratch is dear (~30ms), so cap how many we build per tick: the
        # top rows appear at once and the rest fill over the next ticks, keeping
        # the very first paint fast. Re-binding a pooled card is cheap and
        # unlimited, so once the pool is warm scrolling renders in one pass.
        session = self._session
        built = 0
        for offset, p in enumerate(wanted):
            idx = start + offset
            card = self._cards_by_path.get(p)
            if card is None:
                if not self._card_pool and built >= NEW_CARDS_PER_TICK:
                    self.after(1, self._render_window)   # finish the window soon
                    break
                fresh = not self._card_pool
                card = self._make_or_take_card()
                card.bind_item(p, self._dates.get(p, datetime.now()),
                               self._thumbs.get(p, self._placeholder))
                self._cards_by_path[p] = card
                built += fresh
            row, col = idx // CARD_COLS, idx % CARD_COLS
            card.place(x=col * self._col_width() + CARD_PAD,
                       y=row * int(self._row_height()) + CARD_PAD)
            # Build the preview if we don't already have it.
            if p not in self._thumbs and p not in self._thumb_requested:
                self._thumb_requested.add(p)
                self._thumb_jobs.put((session, p))

    def _layout_grid(self):
        # Re-render the window after an in-place change (drag/remove).
        self._render_window()

    # ── drag-to-reorder ─────────────────────────────────────────────────────────

    def _grid_inner(self):
        # The actual frame the cards are placed into (inside the scrollable
        # frame); card.winfo_x()/y() and place() share this coordinate space.
        for card in self._cards_by_path.values():
            return card.nametowidget(card.winfo_parent())
        return self._grid

    def _drag_start_cb(self, path: str, event):
        self._drag_path = path
        self._drag_active = False
        self._drag_start = (event.x_root, event.y_root)
        self._drop_index = None

    def _drag_motion_cb(self, path: str, event):
        if self._drag_path != path:
            return
        if not self._drag_active:
            # Only treat it as a drag once the pointer has moved a little, so a
            # plain click doesn't start dragging.
            if (abs(event.x_root - self._drag_start[0]) < 6
                    and abs(event.y_root - self._drag_start[1]) < 6):
                return
            self._drag_active = True
            self._dragging = True       # freeze window recycling during the drag
            card = self._cards_by_path.get(path)
            if card is not None:
                card.set_dragging(True)
            self._create_ghost(path)
        self._move_ghost(event)
        idx = self._drop_index_at(event)
        if idx != self._drop_index:
            self._drop_index = idx
            self._show_drop_indicator(idx)

    def _drag_drop_cb(self, path: str, event):
        if self._drag_path is None:
            return
        active = self._drag_active
        self._drag_path = None
        self._drag_active = False
        self._dragging = False
        self._hide_drop_indicator()
        self._destroy_ghost()
        card = self._cards_by_path.get(path)
        if card is not None:
            card.set_dragging(False)
        if not active:
            return
        # Reorder only now, on release.
        new = self._drop_index_at(event)
        old = self._image_paths.index(path)
        self._image_paths.pop(old)
        if new > old:
            new -= 1
        new = max(0, min(new, len(self._image_paths)))
        self._image_paths.insert(new, path)
        self._drop_index = None
        # Order changed under the recycled cards – rebind the whole window.
        for c in self._cards_by_path.values():
            c.place_forget()
            self._card_pool.append(c)
        self._cards_by_path = {}
        self._render_window()

    def _rendered_cards(self):
        """(data-index, card) for the cards currently on screen, in order.

        Only the viewport window has widgets, so drag math operates on those.
        """
        start, end = self._win_range
        out = []
        for i in range(start, min(end, len(self._image_paths))):
            card = self._cards_by_path.get(self._image_paths[i])
            if card is not None:
                out.append((i, card))
        return out

    def _drop_index_at(self, event) -> int:
        """Insertion index in _image_paths for the current pointer position."""
        rendered = self._rendered_cards()
        if not rendered:
            return len(self._image_paths)
        inner = self._grid_inner()
        px = event.x_root - inner.winfo_rootx()
        py = event.y_root - inner.winfo_rooty()
        best = rendered[-1][0] + 1
        for k, (i, c) in enumerate(rendered):
            x, y, w, h = c.winfo_x(), c.winfo_y(), c.winfo_width(), c.winfo_height()
            if py < y + h:  # pointer is within this card's row (or above)
                if px < x + w / 2:
                    return i
                best = i + 1
                # If the next card starts a new row, drop at end of this row.
                if k + 1 < len(rendered) and rendered[k + 1][1].winfo_y() > y + h / 2:
                    return i + 1
                if k + 1 >= len(rendered):
                    return i + 1
        return best

    def _show_drop_indicator(self, idx: int):
        rendered = self._rendered_cards()
        if not rendered:
            return
        # Plain tkinter.Frame on purpose: CTk's place() re-applies DPI scaling
        # to x/y, but winfo_x()/y() are already scaled pixels, which would push
        # the bar to the wrong spot. tkinter.Frame.place() uses raw pixels.
        if self._drop_indicator is None:
            self._drop_indicator = tkinter.Frame(self._grid, width=4, bg="#3b8ed0")
        bar = self._drop_indicator
        # Find the rendered card at insertion index idx (or the last one).
        target = None
        for i, c in rendered:
            if i == idx:
                target = c
                break
        if target is None:
            c = rendered[-1][1]
            x = c.winfo_x() + c.winfo_width() + 3
        else:
            c = target
            x = max(0, c.winfo_x() - 7)
        bar.place(x=x, y=c.winfo_y(), width=4, height=c.winfo_height())
        bar.lift()

    def _hide_drop_indicator(self):
        if self._drop_indicator is not None:
            self._drop_indicator.place_forget()

    # ── drag ghost (kleine Vorschau am Cursor) ──────────────────────────────────

    def _create_ghost(self, path: str):
        self._destroy_ghost()
        ctk_img = self._thumbs.get(path)
        pil = ctk_img.cget("dark_image") if ctk_img is not None else None
        if pil is None:
            return
        ghost_img = pil.copy()
        ghost_img.thumbnail((96, 64), Image.LANCZOS)
        win = tkinter.Toplevel(self)
        win.overrideredirect(True)
        try:
            win.attributes("-alpha", 0.8)
            win.attributes("-topmost", True)
        except tkinter.TclError:
            pass
        self._drag_ghost_img = ImageTk.PhotoImage(ghost_img)
        tkinter.Label(win, image=self._drag_ghost_img, borderwidth=2,
                      relief="solid", highlightthickness=0).pack()
        self._drag_ghost = win

    def _move_ghost(self, event):
        if self._drag_ghost is not None:
            self._drag_ghost.geometry(f"+{event.x_root + 12}+{event.y_root + 12}")

    def _destroy_ghost(self):
        if self._drag_ghost is not None:
            self._drag_ghost.destroy()
            self._drag_ghost = None
        self._drag_ghost_img = None

    # ── sort ──────────────────────────────────────────────────────────────────

    def _apply_sort(self, reset_scroll: bool = True):
        key = (
            (lambda p: self._dates.get(p, datetime.min))
            if self._sort_by == "date"
            else (lambda p: (_file_num(p), os.path.basename(p).lower()))
        )
        new_order = sorted(self._all_paths, key=key, reverse=self._reversed)
        # Skip the relayout when the order is unchanged (e.g. the background date
        # refresh finalized an order that already matched the mtime order).
        if new_order == self._image_paths and self._cards_by_path:
            return
        # The visible slots now hold different images: recycle every live card so
        # _render_window re-binds them to the newly-sorted paths.
        for card in self._cards_by_path.values():
            card.place_forget()
            self._card_pool.append(card)
        self._cards_by_path = {}
        self._image_paths = new_order
        self._reserve_rows((len(new_order) + CARD_COLS - 1) // CARD_COLS)
        if reset_scroll:
            canvas = getattr(self._grid, "_parent_canvas", None)
            if canvas is not None:
                canvas.yview_moveto(0.0)
            self._last_yview = 0.0
        self._update_create_btn()
        self._render_window()

    def _on_sort_change(self, val: str):
        self._sort_by = "date" if val == "Nach Datum" else "name"
        self._apply_sort()

    def _toggle_reverse(self):
        self._reversed = not self._reversed
        if self._reversed:
            self._rev_btn.configure(fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"])
        else:
            self._rev_btn.configure(fg_color=("gray70", "gray30"))
        self._apply_sort()

    # ── remove image ──────────────────────────────────────────────────────────

    def _remove_image(self, path: str):
        for lst in (self._image_paths, self._all_paths):
            if path in lst:
                lst.remove(path)
        card = self._cards_by_path.pop(path, None)
        if card is not None:
            card.place_forget()
            self._card_pool.append(card)        # recycle, don't destroy
        self._thumbs.pop(path, None)
        self._dates.pop(path, None)
        self._thumb_requested.discard(path)
        # The list shrank by a row – update the reserved height, then re-render.
        self._reserve_rows((len(self._image_paths) + CARD_COLS - 1) // CARD_COLS)
        self._render_window()
        self._count_lbl.configure(text=f"{len(self._image_paths)} Bilder")
        self._update_create_btn()

    # ── settings ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        SettingsDialog(self, self._settings, self._on_settings_saved)

    def _on_settings_saved(self, s: dict):
        self._settings = s
        self._settings_lbl.configure(text=self._fmt_settings())

    def _fmt_settings(self) -> str:
        s = self._settings
        return (f"FPS: {s["fps"]}  •  {s['resolution']}\n"
                f"Qualität: {s['quality']}  •  Geschw.: {s['speed']}\n"
                f"Encoder: {s['encoder']}")

    # ── output path ───────────────────────────────────────────────────────────

    def _pick_output(self):
        p = filedialog.asksaveasfilename(
            title="Ausgabedatei wählen",
            defaultextension=".mp4",
            filetypes=[("MP4-Video", "*.mp4"), ("Alle Dateien", "*.*")],
            initialfile="timelapse.mp4",
        )
        if p:
            self._output_path = p
            self._output_lbl.configure(text=pathlib.Path(p).name)
            self._update_create_btn()

    # ── render ────────────────────────────────────────────────────────────────

    def _update_create_btn(self):
        ok = (bool(self._image_paths) and bool(self._output_path)
              and not self._rendering and self._ffmpeg_ready)
        self._create_btn.configure(state="normal" if ok else "disabled")

        if ok:
            hint, color = "", ("gray40", "gray55")
        elif self._ffmpeg_installing:
            hint, color = "⚠  FFmpeg wird installiert …", ("#b85c00", "#e07820")
        elif not self._ffmpeg_ready:
            hint, color = "⚠  FFmpeg nicht installiert", ("#b85c00", "#e07820")
        elif self._rendering:
            hint, color = "Rendering läuft …", ("gray40", "gray55")
        elif not self._image_paths:
            hint, color = "Keine Bilder geladen", ("gray40", "gray55")
        else:
            hint, color = "Kein Ausgabepfad gewählt", ("gray40", "gray55")

        self._ffmpeg_hint_lbl.configure(text=hint, text_color=color)

    def _start_render(self):
        resolved = _unique_path(self._output_path)
        if resolved != self._output_path:
            self._output_path = resolved
            self._output_lbl.configure(text=pathlib.Path(resolved).name)
        self._rendering = True
        self._cancel_render = False
        self._update_create_btn()
        self._pbar.set(0)
        self._pbar_lbl.configure(text="Wird vorbereitet …")
        self._cancel_btn.configure(state="normal", text="✕  Abbrechen")
        self._pbar_frame.pack(fill="x", before=self._right_spacer)
        threading.Thread(target=self._render_worker, daemon=True).start()

    def _cancel_render_cb(self):
        if not self._rendering or self._cancel_render:
            return
        self._cancel_render = True
        self._cancel_btn.configure(state="disabled", text="Wird abgebrochen …")
        self._pbar_lbl.configure(text="Wird abgebrochen …")
        # Kill ffmpeg immediately so the writer's pipe breaks right away.
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def _render_worker(self):
        paths = list(self._image_paths)
        s = self._settings

        try:
            w, h = (int(x) for x in s["resolution"].lower().split("x"))
        except ValueError:
            self.after(0, lambda: messagebox.showerror("Fehler", "Ungültige Auflösung.", parent=self))
            self.after(0, lambda: self._finish_render(False))
            return

        out_dir = os.path.dirname(self._output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        encoder = _detect_encoder(s["encoder"])

        cmd = ["ffmpeg", "-y", "-f", "image2pipe",
               "-r", str(s["fps"]), "-i", "-", "-loglevel", "warning"]

        quality_map = {"high": ("18", "20"), "medium": ("23", "26"), "low": ("28", "32")}
        crf, cq = quality_map[s["quality"]]

        if encoder == "libx264":
            cmd += ["-c:v", "libx264", "-preset", s["speed"], "-crf", crf]
        elif encoder == "h264_nvenc":
            cmd += ["-c:v", "h264_nvenc", "-preset", s["speed"], "-rc", "vbr", "-cq", cq]
        elif encoder == "h264_videotoolbox":
            q_map = {"high": "90", "medium": "75", "low": "50"}
            cmd += ["-c:v", "h264_videotoolbox", "-q:v", q_map[s["quality"]]]
        elif encoder == "h264_amf":
            # AMF uses -quality instead of -preset
            amf_speed = {"slow": "quality", "medium": "balanced", "fast": "speed"}
            cmd += ["-c:v", "h264_amf", "-quality", amf_speed[s["speed"]]]
        elif encoder == "h264_qsv":
            cmd += ["-c:v", "h264_qsv", "-preset", s["speed"], "-global_quality", crf]
        else:
            cmd += ["-c:v", encoder]

        sf = f"scale='max({w},iw*{h}/ih)':'max({h},ih*{w}/iw)'"
        cf = f"crop=w={w}:h={h}:x=(iw-ow)/2:y=(ih-oh)/2"
        cmd += ["-vf", f"{sf},{cf},format=yuv420p", self._output_path]

        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            **_popen_kwargs(),
        )
        self._proc = proc

        # Drain stderr on a separate thread. Otherwise ffmpeg can block writing
        # to a full stderr pipe while we block writing to stdin → deadlock/hang.
        stderr_chunks: list[bytes] = []

        def _drain_stderr():
            try:
                for line in iter(proc.stderr.readline, b""):
                    stderr_chunks.append(line)
            except (ValueError, OSError):
                pass

        err_thread = threading.Thread(target=_drain_stderr, daemon=True)
        err_thread.start()

        total = len(paths)
        for i, p in enumerate(paths):
            if self._cancel_render:
                break
            pct = (i + 1) / total
            txt = f"Bild {i + 1} / {total}  ({int(pct * 100)} %)"
            self.after(0, lambda v=pct, t=txt: (
                self._pbar.set(v),
                self._pbar_lbl.configure(text=t),
            ))
            try:
                with Image.open(p) as im:
                    im = ImageOps.exif_transpose(im)  # honor rotation, like thumbnails
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    buf = io.BytesIO()
                    im.save(buf, format="PPM")
                proc.stdin.write(buf.getvalue())
            except (BrokenPipeError, OSError):
                break  # pipe closed (cancel / ffmpeg died) -> stop
            except Exception:
                continue  # unreadable image -> skip, don't abort render

        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait()
        err_thread.join()
        self._proc = None
        stderr = b"".join(stderr_chunks)

        if self._cancel_render:
            # Remove the partial/aborted output file.
            try:
                if os.path.exists(self._output_path):
                    os.remove(self._output_path)
            except OSError:
                pass
            self.after(0, lambda: self._finish_render(False, cancelled=True))
            return

        ok = proc.returncode == 0

        # Catch silent encoder failures (e.g. NVENC exits 0 but writes nothing)
        out_size = os.path.getsize(self._output_path) if os.path.exists(self._output_path) else 0
        if ok and out_size == 0:
            ok = False
            stderr = (stderr or b"") + b"\nFFmpeg hat keine Ausgabedatei erzeugt (leere Datei trotz Returncode 0)."

        if ok:
            folder = str(pathlib.Path(self._output_path).parent.resolve())
            self.after(0, lambda fl=folder: self._render_success(fl))
        else:
            err = stderr.decode("utf-8", errors="ignore")
            self.after(0, lambda e=err: self._render_failure(e))

        self.after(0, lambda: self._finish_render(ok))

    def _render_success(self, folder: str):
        self._pbar.set(1)
        self._pbar_lbl.configure(text="Fertig! ✓")
        try:
            winsound.MessageBeep(winsound.MB_ICONINFORMATION)
        except Exception:
            pass
        messagebox.showinfo("Fertig", "Die Timelapse wurde erfolgreich erstellt!", parent=self)
        abs_path = str(pathlib.Path(self._output_path).resolve())
        if not _reveal_in_explorer(abs_path):
            # Fallback: at least open the containing folder.
            try:
                os.startfile(os.path.dirname(abs_path))
            except OSError:
                pass
        self.after(3000, self._hide_pbar)

    def _render_failure(self, err: str):
        self._pbar_lbl.configure(text="")
        try:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass
        ErrorDialog(self, err if err.strip() else "Kein Fehlertext von FFmpeg erhalten.")

    def _finish_render(self, ok: bool, cancelled: bool = False):
        self._rendering = False
        self._cancel_render = False
        if cancelled:
            self._pbar_lbl.configure(text="Abgebrochen.")
            self.after(2000, self._hide_pbar)
        elif not ok:
            self._hide_pbar()
        self._update_create_btn()

    def _hide_pbar(self):
        self._pbar_frame.pack_forget()
        self._pbar.set(0)
        self._pbar_lbl.configure(text="")


if __name__ == "__main__":
    app = App()
    app.mainloop()
