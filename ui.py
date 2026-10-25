from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QEasingCurve, QLineF, QMimeData, QObject, QParallelAnimationGroup, QPointF,
    QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
# Video playback for the HUD. Part of PyQt6, so it costs no new dependency —
# but the multimedia plugins are a separate piece of the Qt install and can be
# absent on a stripped-down system, so a failure here disables one feature
# rather than stopping JARVIS from starting.
try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_VIDEO = True
except Exception as _e:            # noqa: BLE001 - reported, never fatal
    QAudioOutput = QMediaPlayer = QGraphicsVideoItem = None
    HAVE_VIDEO = False
    print(f"[Video] playback unavailable ({_e}) — the HUD will not show video.")

from PyQt6.QtWidgets import (
    QGraphicsScene, QGraphicsView,
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSplitter,
    QStackedWidget, QTabWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

try:
    from core.avatar import HoloAvatar
except Exception:      # pragma: no cover — HUD must never die over cosmetics
    HoloAvatar = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for the release name — the window title, the header
# badge and the readme must never disagree again.
APP_VERSION  = "MARK LV"
APP_PROTOCOL = APP_VERSION.split()[-1]

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#00060a"
    PANEL     = "#010d14"
    PANEL2    = "#010f18"
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#8ffcff"
    TEXT_DIM  = "#3a8a9a"
    TEXT_MED  = "#5ab8cc"
    WHITE     = "#d8f8ff"
    DARK      = "#000d14"
    BAR_BG    = "#011520"


# Keys tied to the accent colour — status colours (ACC, GREEN, RED…) stay fixed
_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    """
    Re-derives the whole teal-family palette from the chosen accent colour
    (hue shift — brightness/saturation ratios are preserved, design stays intact).
    Painted elements (HUD, waveform, metrics) pick up the new colour on the next
    frame; stylesheet-based panels pick it up when they are rebuilt.
    """
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08   # near-grey accent → the whole theme is desaturated

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    """A snapshot of the accent-linked colours currently on class C."""
    return {k: getattr(C, k) for k in _HUE_LINKED}


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    """
    LIVE full theme change. Replaces the old palette colours with the new ones
    in EVERY widget's stylesheet across the app and repaints them. This way the
    colour change applies INSTANTLY across the whole interface — panels, buttons,
    borders included — not just the painted elements. No restart needed.
    """
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


def _status_btn_style(col: str) -> str:
    return f"""
        QPushButton {{
            color: {col};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_A};
            border-radius: 3px;
            padding: 4px;
            font-family: 'Courier New';
            font-size: 7pt;
            font-weight: bold;
            text-align: center;
        }}
        QPushButton:hover {{
            background: rgba(0, 212, 255, 0.15);
            border: 1px solid {col};
            color: #ffffff;
        }}
        QPushButton:pressed {{
            background: rgba(0, 212, 255, 0.3);
            border: 1px solid #ffffff;
        }}
    """


# ── Windows GPU via NVML DLL (no subprocess, no console window) ──────────────
_nvml_lib: object = None   # cached ctypes DLL
_nvml_ok:  object = None   # None=untested, True=works, False=unavailable


def _nvml_gpu_windows() -> float:
    """Return NVIDIA GPU utilisation % using nvml.dll directly — zero subprocess."""
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            import pynvml  # type: ignore
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        # Probe caches — GPU (NVML) and temperature (WMI) are the expensive
        # queries; initialise their handles once and reuse them instead of
        # rebuilding a connection on every poll.
        self._slow_tick = 0            # gpu/temp refreshed every 3rd cycle
        self._pynvml    = None         # cached pynvml module + device handle
        self._pynvml_h  = None
        self._pynvml_ok = None         # None=untested, False=unavailable here
        self._nv_unix   = None         # cached (lib, dev) for Linux/macOS NVML
        self._wmi_conn  = None         # cached WMI connection (creating one is slow)
        self._wmi_ok    = None         # None=untested, False=unavailable here
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(2.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        # GPU and temperature change slowly and are the most expensive probes
        # (NVML / WMI) — refresh them every 3rd cycle (~6 s) instead of every
        # cycle, reusing the previous reading in between.
        self._slow_tick = (self._slow_tick + 1) % 3
        if self._slow_tick == 1:
            gpu = self._get_gpu()
            tmp = self._get_temp()
        else:
            gpu = self.gpu
            tmp = self.tmp

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # pynvml — subprocess-free; initialise once and reuse the handle.
        # Re-initialising NVML on every poll is slow, so cache it and stop
        # retrying pynvml entirely once it proves unavailable here.
        if self._pynvml_ok is not False:
            try:
                if self._pynvml_h is None:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", category=FutureWarning)
                        import pynvml  # type: ignore
                    pynvml.nvmlInit()
                    self._pynvml    = pynvml
                    self._pynvml_h  = pynvml.nvmlDeviceGetHandleByIndex(0)
                    self._pynvml_ok = True
                return float(self._pynvml.nvmlDeviceGetUtilizationRates(self._pynvml_h).gpu)
            except Exception:
                self._pynvml_ok = False

        # Windows: nvml.dll via ctypes (already cached in _nvml_gpu_windows)
        if _OS == "Windows":
            return _nvml_gpu_windows()

        # Linux / macOS: libnvidia-ml shared lib via ctypes — init once, reuse
        try:
            import ctypes

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            if self._nv_unix is None:
                _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"
                nv = ctypes.CDLL(_lib)
                nv.nvmlInit_v2()
                dev = ctypes.c_void_p()
                nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
                self._nv_unix = (nv, dev)

            nv, dev = self._nv_unix
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0   # N/A — zero subprocess on all platforms

    def _get_temp(self) -> float:
        # psutil — works on Linux; occasionally Windows with driver support
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        # Windows: wmi module (pure Python COM, zero subprocess). Reuse a single
        # connection — building a fresh wmi.WMI() on every poll spins up a COM
        # connection each time and is very slow. Give up after one failure.
        if _OS == "Windows" and self._wmi_ok is not False:
            try:
                if self._wmi_conn is None:
                    import wmi  # type: ignore
                    self._wmi_conn = wmi.WMI(namespace="root/wmi")
                tz = self._wmi_conn.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                self._wmi_ok   = False
                self._wmi_conn = None

        return -1.0   # N/A — zero subprocess on all platforms

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        # The holographic head that fills the HUD. If it could not be imported
        # we fall back to the old glowing core so the panel is never empty.
        self._avatar = None
        if HoloAvatar is not None:
            try:
                self._avatar = HoloAvatar()
            except Exception:
                self._avatar = None

        # Which centrepiece to draw. Read once here and changed live by the
        # settings toggle; the avatar object is kept either way so switching
        # back is instant and costs no reload.
        try:
            from memory.config_manager import get_hud_style
            self.hud_style = get_hud_style()
        except Exception:
            self.hud_style = "face"
        self._core_phase = 0.0

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._step_t     = time.time()
        self._blink      = True
        self._blink_tick = 0

        # Rescaled-face cache: the smooth rescale is expensive, so we keep the
        # last result and only rebuild it when the (quantised) size changes.

        # Static grid-dot layer, pre-rendered once per size/theme into a pixmap
        # so paintEvent blits it in one call instead of thousands of drawPoint()s.
        self._grid_cache: QPixmap | None = None
        self._grid_key = None
        # Repaint throttle counter (idle frames drop to ~20 Hz — see _step()).
        self._paint_tick = 0

        # Live audio reactivity: _live_amp is written from the audio threads
        # (0.0–1.0), _amp_disp is the smoothed value the paint code reads.
        self._live_amp  = 0.0
        self._amp_disp  = 0.0
        # (frames, start_time, hop) posted by the playback thread — see
        # push_visemes(). None means "no schedule; use the plain level".
        self._visemes = None
        self._vis_i = None        # first schedule frame not yet handed to the mouth
        self._base_scale = 1.0    # slow "breathing" target; amp is added per-frame
        self._base_halo  = 55.0

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: hand over a schedule of (level, openness, width) frames.

        The playback thread writes up to 200 ms of audio in one go, so a single
        averaged level would only move the mouth five times a second — enough to
        flap, nowhere near enough to articulate. It instead posts the whole
        slice's worth of 20 ms frames here and `_step()` plays them out against
        the wall clock, in step with the audio going to the speakers.

        `at` is the wall-clock time this batch will *begin to sound*, which the
        caller tracks as a playback cursor. It is not the time of the call, and
        the difference is the whole point: `stream.write` returns once the buffer
        accepts the samples, so consecutive batches are handed over far faster
        than they play. Anchoring each one to "now" made every batch start while
        its predecessor was still sounding, so each schedule replaced the last
        after a couple of frames and the mouth only ever played the opening
        instant of every 200 ms — the reason it did not match the words.

        Successive batches are therefore *appended* into one continuous
        timeline, not swapped in. A paragraph is one schedule; the mouth stops
        falling into a gap at every chunk boundary and having to climb back out.
        """
        try:
            if not frames:
                return
            hop = max(1e-3, float(hop))
            at = float(at)
            new = list(frames)
            cur = self._visemes
            if cur is not None:
                old, t0, ohop = cur
                if abs(ohop - hop) < 1e-6:
                    # Where in the existing timeline does this batch land?
                    i = int(round((at - t0) / hop))
                    if 0 <= i <= len(old) + 1:
                        # Continues (or slightly overlaps) what is already
                        # queued: extend rather than restart. Drop whatever has
                        # already been played so the list cannot grow without
                        # bound over a long reply.
                        merged = old[:i] + new
                        played = int((time.time() - t0) / hop) - 2
                        if played > 60:
                            merged = merged[played:]
                            t0 += played * hop
                            if self._vis_i is not None:
                                self._vis_i = max(0, self._vis_i - played)
                        self._visemes = (merged, t0, hop)
                        return
            self._visemes = (new, at, hop)
            self._vis_i = None
        except Exception:
            pass

    def set_audio_level(self, level: float) -> None:
        """Thread-safe entry point for the audio threads. Stores the louder of
        the incoming level and the current value so brief gaps between chunks
        don't make the waveform stutter; _step() decays it back down."""
        try:
            lv = float(level)
        except (TypeError, ValueError):
            return
        if lv < 0.0:
            lv = 0.0
        elif lv > 1.0:
            lv = 1.0
        if lv > self._live_amp:
            self._live_amp = lv

    def _make_grid(self, W: int, H: int) -> QPixmap:
        """Pre-render the static grid-dot background into a transparent pixmap so
        paintEvent can blit it once per frame instead of running a nested
        drawPoint() loop across the whole widget every 16 ms."""
        pm = QPixmap(max(1, W), max(1, H))
        pm.fill(Qt.GlobalColor.transparent)
        gp = QPainter(pm)
        gp.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                gp.drawPoint(x, y)
        gp.end()
        return pm

    def _step(self):
        self._tick += 1
        now = time.time()

        # ── Live audio reactivity ────────────────────────────────────────────
        # A viseme schedule, if one is playing, gives both the level and the
        # mouth shape for this exact instant; otherwise fall back to the peak
        # level the audio threads pushed in.
        v_open = v_wide = v_level = None
        v_seq = None
        sched = self._visemes
        if sched is not None:
            frames, t0, hop = sched
            i = int((now - t0) / hop)
            if 0 <= i < len(frames):
                # Hand over *every* frame since the last tick, not just the one
                # under the cursor. This timer runs at 60 Hz but the paint is
                # throttled and the machine may be busy, so a tick can span two
                # or three 20 ms frames — and a consonant closure is only two
                # frames long. Sampling one and discarding the rest is how the
                # closures between words went missing.
                j = self._vis_i if self._vis_i is not None else i
                v_seq = frames[max(0, j):i + 1]
                self._vis_i = max(j, i + 1)
                v_level, v_open, v_wide = frames[i]
                if v_seq:
                    peak = max(f[0] for f in v_seq)
                    if peak > self._live_amp:
                        self._live_amp = peak
            elif i >= len(frames):
                self._visemes = None        # schedule spent
                self._vis_i = None

        # Audio threads push peaks into _live_amp; decay it toward silence so
        # gaps between chunks fade out instead of freezing, then smooth it.
        self._live_amp *= 0.86
        self._amp_disp += (self._live_amp - self._amp_disp) * 0.45
        amp = self._amp_disp

        # The avatar animates off the very same smoothed level the waveform
        # uses — one audio source, so the mouth can never drift out of sync.
        dt = now - self._step_t
        self._step_t = now
        # Integrated, not derived from absolute time: multiplying wall-clock by
        # a rate that changes with state jumps the rings the instant JARVIS
        # starts talking. Same lesson the head's sway taught.
        self._core_phase += min(0.10, max(0.0, dt))

        if self._avatar is not None and self.hud_style == "face":
            self._avatar.step(dt, amp, speaking=self.speaking,
                              muted=self.muted, state=self.state,
                              v_open=v_open, v_wide=v_wide or 0.0,
                              v_level=v_level, v_seq=v_seq,
                              v_hop=(sched[2] if sched is not None else 0.02))
        else:
            # Fallback core: slow "breathing" base target, lifted by the level.
            if now - self._last_t > (0.12 if self.speaking else 0.5):
                if self.speaking:
                    self._base_scale = 1.03
                    self._base_halo  = 122.0
                elif self.muted:
                    self._base_scale = random.uniform(0.998, 1.002)
                    self._base_halo  = random.uniform(15, 28)
                else:
                    self._base_scale = random.uniform(1.001, 1.008)
                    self._base_halo  = random.uniform(48, 68)
                self._last_t = now

            if self.muted:
                self._tgt_scale, self._tgt_halo = self._base_scale, self._base_halo
            elif self.speaking:
                self._tgt_scale = self._base_scale + amp * 0.13
                self._tgt_halo  = self._base_halo  + amp * 95.0
            else:
                self._tgt_scale = self._base_scale + amp * 0.06
                self._tgt_halo  = self._base_halo  + amp * 75.0

            sp = 0.38 if self.speaking else (0.30 if amp > 0.02 else 0.15)
            self._scale += (self._tgt_scale - self._scale) * sp
            self._halo  += (self._tgt_halo  - self._halo)  * sp

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
            _blinked = True
        else:
            _blinked = False

        # Repaint throttling — advancing the animation state above is cheap at
        # 60 Hz, but the paint is heavy. Active (speaking, audio, thinking) runs
        # at ~30 Hz, which is the frame rate animation has used for talking
        # characters forever and is indistinguishable here; idle drops to ~20 Hz
        # so a sleeping HUD stops pinning a CPU core. The visuals stay smooth
        # either way because the animation state keeps stepping at 60 Hz.
        self._paint_tick = (self._paint_tick + 1) % 6
        active = (self.speaking or amp > 0.02
                  or self.state in ("THINKING", "PROCESSING"))
        if _blinked or (self._paint_tick % 2 == 0 if active
                        else self._paint_tick % 3 == 0):
            # Nothing is on screen when the window is hidden or minimised, so
            # rendering the avatar into it is pure waste — and this app is meant
            # to sit running all day. The animation state above keeps stepping,
            # so it picks up mid-motion instead of snapping when you come back.
            if self._on_screen():
                self.update()

    def _on_screen(self) -> bool:
        """True only when this canvas can actually be seen by the user."""
        try:
            if not self.isVisible():
                return False
            win = self.window()
            return not (win.isMinimized() or win.isHidden())
        except Exception:
            return True      # never let a visibility check stop the HUD drawing

    # ── reactor core ─────────────────────────────────────────────────────────
    # The centrepiece for anyone who did not want a face looking back at them.
    # Built from the same budget as the head — software QPainter, no GPU — and
    # from the same principle: everything on it means something. The rings turn
    # at a rate the state sets, the spectrum ring is the real audio level, and
    # the core brightens with the voice. Nothing here is decoration that moves
    # for its own sake, which is what made the old glowing orb feel dead.

    def _core_colours(self):
        if self.muted:
            return qcol(C.MUTED_C), qcol(C.MUTED_C)
        if self.speaking:
            return qcol(C.PRI), qcol(C.ACC)
        if self.state in ("THINKING", "PROCESSING"):
            return qcol(C.PRI), qcol(C.ACC2)
        if self.state == "LISTENING":
            return qcol(C.PRI), qcol(C.GREEN)
        return qcol(C.PRI), qcol(C.PRI_DIM)

    def _paint_core(self, p: QPainter, cx: float, cy: float, r: float,
                    W: float = 0.0, H: float = 0.0):
        """Draw the reactor at (cx, cy) with outer radius r, using the whole
        canvas (W x H) for the marks that frame it."""
        main, acc = self._core_colours()
        bg = qcol(C.BG)
        amp = self._amp_disp
        t = self._core_phase
        live = (self.speaking or amp > 0.04) and not self.muted

        def blend(col: QColor, a: float) -> QColor:
            """Pre-mix onto the background instead of asking Qt to composite.
            The raster engine's opaque path is several times faster than its
            translucent one, and everything here is a line or an arc."""
            k = max(0.0, min(1.0, a))
            return QColor(int(bg.red()   + (col.red()   - bg.red())   * k),
                          int(bg.green() + (col.green() - bg.green()) * k),
                          int(bg.blue()  + (col.blue()  - bg.blue())  * k))

        p.setBrush(Qt.BrushStyle.NoBrush)

        # 1. The atmosphere. One radial gradient doing what a stack of discs did
        #    badly: a wide, soft body of light that gives the thing presence
        #    before any detail is read. This single element decides whether the
        #    HUD looks vast or looks small, so it is drawn first and drawn big.
        # Concentrated rather than spread: a gradient reaching the outer rim
        # washes the whole disc a flat dim blue and reads as fog. Ending it at
        # two thirds leaves it a body of light with somewhere to fall off to,
        # which is what makes it look lit rather than tinted.
        lift = 1.0 + 0.55 * amp + (0.18 if self.speaking else 0.0)
        p.setPen(Qt.PenStyle.NoPen)
        for gr, a0, a1 in ((r * 0.70, 0.30, 0.0), (r * 0.34, 0.34, 0.0)):
            g = QRadialGradient(cx, cy, gr)
            g.setColorAt(0.00, blend(main, min(0.95, a0 * lift)))
            g.setColorAt(0.45, blend(main, min(0.95, a0 * lift * 0.52)))
            g.setColorAt(0.78, blend(main, min(0.95, a0 * lift * 0.18)))
            g.setColorAt(1.00, blend(main, a1))
            p.setBrush(QBrush(g))
            p.drawEllipse(QRectF(cx - gr, cy - gr, gr * 2, gr * 2))
        p.setBrush(Qt.BrushStyle.NoBrush)

        # 2. Frame marks at the corners of the whole canvas, not of the circle.
        #    They are what set the scale: the eye reads the reactor as filling
        #    the room rather than sitting in the middle of it.
        if W > 40 and H > 40:
            m, arm = min(W, H) * 0.035, min(W, H) * 0.055
            p.setPen(QPen(blend(main, 0.45), 1.4))
            for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
                x = cx + sx * (W / 2 - m)
                y = cy + sy * (H / 2 - m)
                p.drawLine(QLineF(x, y, x - sx * arm, y))
                p.drawLine(QLineF(x, y, x, y - sy * arm))

        # 3. Crosshair across the full canvas, broken around the core so it
        #    frames the reactor rather than crossing it.
        p.setPen(QPen(blend(main, 0.16), 1))
        gap = r * 0.62
        if W > 40:
            p.drawLine(QLineF(cx - W / 2, cy, cx - gap, cy))
            p.drawLine(QLineF(cx + gap, cy, cx + W / 2, cy))
        if H > 40:
            p.drawLine(QLineF(cx, cy - H / 2, cx, cy - gap))
            p.drawLine(QLineF(cx, cy + gap, cx, cy + H / 2))

        # 4. Two thin outer circles. Sparse on purpose — a dense ring reads as a
        #    grey band at this size, and restraint is what made the original
        #    look expensive.
        for rr, a in ((1.00, 0.34), (0.93, 0.16)):
            rad = r * rr
            p.setPen(QPen(blend(main, a), 1))
            p.drawEllipse(QRectF(cx - rad, cy - rad, rad * 2, rad * 2))

        # 5. Long, sparse graduations: 24 majors reaching well in from the rim,
        #    with shorter minors between them.
        major, minor = [], []
        for i in range(72):
            a = math.radians(i * 5.0)
            ca, sa = math.cos(a), math.sin(a)
            if i % 3 == 0:
                major.append(QLineF(cx + ca * r * 0.885, cy + sa * r * 0.885,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
            else:
                minor.append(QLineF(cx + ca * r * 0.945, cy + sa * r * 0.945,
                                    cx + ca * r * 0.985, cy + sa * r * 0.985))
        p.setPen(QPen(blend(main, 0.42), 1.3))
        p.drawLines(major)
        p.setPen(QPen(blend(main, 0.18), 1))
        p.drawLines(minor)

        # 6. Sweeping arcs. Long spans, not dashes — the original's grandeur
        #    came from a few big strokes. Speed is the state: idle drifts,
        #    thinking hurries, speaking runs.
        rate = 1.0 + (1.9 if self.state in ("THINKING", "PROCESSING") else 0.0) \
                   + (1.2 if self.speaking else 0.0)
        for k, (rr, span, count, dirn, col, a, wid) in enumerate((
                (0.955, 118, 2, +1, acc,  0.75, 2.0),
                (0.845, 82,  3, -1, main, 0.38, 1.3),
                (0.760, 150, 1, +1, acc,  0.45, 1.6),
                (0.660, 64,  4, -1, main, 0.26, 1.1),
                (0.545, 128, 2, +1, main, 0.30, 1.2))):
            rad = r * rr
            p.setPen(QPen(blend(col, a), wid))
            box = QRectF(cx - rad, cy - rad, rad * 2, rad * 2)
            base = (t * rate * (9 + k * 6) * dirn) % 360.0
            for sgm in range(count):
                p.drawArc(box, int((base + sgm * (360.0 / count)) * 16),
                          int(span * 16))

        # 7. The voice, as a ring of graduations that grow with it. Kept out at
        #    a wide radius so it never crowds the middle.
        n = 60
        ring = r * 0.415
        spikes = []
        for i in range(n):
            a = math.radians(i * (360.0 / n))
            ca, sa = math.cos(a), math.sin(a)
            wob = 0.5 + 0.5 * math.sin(t * 2.3 + i * 0.42)
            idle = 0.018 + 0.012 * math.sin(t * 1.2 + i * 0.7)
            h = r * (idle + (amp * 0.20 * wob if live else 0.0))
            spikes.append(QLineF(cx + ca * ring, cy + sa * ring,
                                 cx + ca * (ring + h), cy + sa * (ring + h)))
        p.setPen(QPen(blend(acc if live else main, 0.25 + 0.5 * amp), 1.6))
        p.drawLines(spikes)

        # 8. The inner ring the name sits in.
        inner = r * 0.355
        p.setPen(QPen(blend(acc, 0.30 + 0.45 * amp), 1.5))
        p.drawEllipse(QRectF(cx - inner, cy - inner, inner * 2, inner * 2))

        # 9. The name, sized from the string rather than from the radius alone:
        #    "J.A.R.V.I.S" and a name someone renamed to "MAX" are very
        #    different widths, and a fixed fraction of r spills one of them past
        #    the ring it is supposed to sit inside.
        name = self._assistant_name or ""
        if name:
            space = max(1.0, r * 0.018)
            fsz = max(8, int(min(r * 0.105,
                                 (inner * 1.75) / max(1, len(name)) * 1.6 - space)))
            f = QFont("Courier New", fsz, QFont.Weight.Bold)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, space)
            p.setFont(f)
            p.setPen(QPen(blend(qcol(C.WHITE), 0.6 + 0.4 * min(1.0, amp * 2)), 1))
            p.drawText(QRectF(cx - r, cy - fsz, r * 2, fsz * 2),
                       Qt.AlignmentFlag.AlignCenter, name)

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():      # device not ready (e.g. 0-size during layout) — skip cleanly
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots — blitted from a cached layer; rebuilt only when the size
        # or the theme's ghost colour changes (so live re-theming still works).
        _gkey = (W, H, C.PRI_GHO)
        if self._grid_cache is None or self._grid_key != _gkey:
            self._grid_cache = self._make_grid(W, H)
            self._grid_key   = _gkey
        p.drawPixmap(0, 0, self._grid_cache)

        # ── holographic head ────────────────────────────────────────────────
        # Sized to the band between the top of the canvas and the status line,
        # capped by width, so it fills the HUD at any window size — including
        # fullscreen — without ever colliding with the status text below.
        _sy_status = cy + fw * 0.40
        if self._avatar is not None and self.hud_style == "face":
            _band_t = 12.0
            _band_h = max(60.0, _sy_status - 12.0 - _band_t)
            _r_head = min(fw * 0.355, _band_h / (self._avatar.SPAN + 0.08))
            _head_cy = _band_t + (_band_h - self._avatar.SPAN * _r_head) / 2.0 + _r_head

            if self.muted:
                _main = _acc = qcol(C.MUTED_C)
            else:
                _main = qcol(C.PRI)
                if self.speaking:
                    _acc = qcol(C.ACC)
                elif self.state in ("THINKING", "PROCESSING"):
                    _acc = qcol(C.ACC2)
                elif self.state == "LISTENING":
                    _acc = qcol(C.GREEN)
                else:
                    _acc = qcol(C.PRI)
            self._avatar.paint(p, cx, _head_cy, _r_head, _main, _acc, qcol(C.BG))

        # reactor core — the other centrepiece, and the fallback if the head
        # could not be built. There is no third path: the old face.png branch
        # was unreachable (no such file ships) and the bare orb it fell through
        # to is what this replaces.
        else:
            _band_t = 12.0
            _band_h = max(60.0, _sy_status - 12.0 - _band_t)
            _r = min(W * 0.46, _band_h / 2.0)
            self._paint_core(p, cx, _band_t + _band_h / 2.0, _r, W, _band_h)

        # status text
        sy = _sy_status
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform — reacts to the real audio level (mic while listening,
        # JARVIS's own voice while speaking). Falls back to a gentle idle
        # ripple when there's no sound. _amp_disp is the smoothed 0–1 level.
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        amp = self._amp_disp
        mid = (N - 1) / 2.0
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            else:
                env     = (1.0 - abs(i - mid) / mid) ** 0.7      # center-weighted hump
                shimmer = 0.55 + 0.45 * math.sin(self._tick * 0.18 + i * 0.7)
                idle    = 3.0 + 2.0 * math.sin(self._tick * 0.09 + i * 0.6)
                hgt     = int(max(2, min(24, idle + amp * 22.0 * env * shimmer)))
                if amp > 0.05:
                    cl = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
                else:
                    cl = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

        p.end()   # end deterministically so the backing store never flushes an active painter

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        v = max(0.0, min(100.0, pct))
        if v == self._value and text == self._text:
            return          # unchanged — skip the repaint
        self._value = v
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

        p.end()

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        # Cap scrollback so an hours-long session can't grow the document
        # without bound — keeps memory flat and every insert cheap. Oldest
        # lines drop off the top automatically.
        self.document().setMaximumBlockCount(600)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "jarvis"   # updated when assistant name changes
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                # SYS lines are the bulk of the log. Amber fought the cyan HUD
                # and, being a fixed status colour rather than a hue-linked one,
                # stayed amber even after the accent picker retinted everything
                # else. TEXT_MED follows the theme and drops the contrast to a
                # level you can read past.
                "sys":  qcol(C.TEXT_MED),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        # The marching-ants dashed border is only meaningful while the user is
        # hovering or dragging a file over the zone. When idle, skip the repaint
        # entirely instead of redrawing the whole zone 25×/s forever — that idle
        # repaint held the GIL and stole time from the audio/response threads.
        if not (self._hovering or self._drag_over):
            return
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)

    def paste_from_clipboard(self) -> bool:
        """Paste image or file from clipboard directly into the file drop zone."""
        try:
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime.hasImage():
                qimg = clipboard.image()
                if not qimg.isNull():
                    save_dir = Path.home() / "Downloads"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    target = save_dir / f"screenshot_{int(time.time())}.png"
                    qimg.save(str(target), "PNG")
                    self._set_file(str(target))
                    return True
            if mime.hasUrls():
                urls = mime.urls()
                if urls:
                    p = urls[0].toLocalFile()
                    if Path(p).is_file():
                        self._set_file(p)
                        return True
        except Exception as e:
            print(f"[FileDropZone] paste_from_clipboard error: {e}")
        return False

    def keyPressEvent(self, e):
        if (e.key() == Qt.Key.Key_V and e.modifiers() & Qt.KeyboardModifier.ControlModifier) or e.matches(QKeySequence.StandardKey.Paste):
            if self.paste_from_clipboard():
                e.accept()
                return
        super().keyPressEvent(e)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

        p.end()

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    """Floating overlay that briefly shows what the camera captured."""

    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)   # auto-dismiss after 6 s


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class HueWheel(QWidget):
    """
    Circular colour picker. The user drags the handle (small white circle)
    around the wheel to choose from ALL hues. The filled circle in the centre
    is a live preview of the selected colour.
    """

    hue_picked    = pyqtSignal(str)   # while dragging (live)
    hue_committed = pyqtSignal(str)   # when the handle is released

    _RING = 16   # ring thickness (px)

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    # ── API ──────────────────────────────────────────────────────────────────
    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    # ── geometry helpers ─────────────────────────────────────────────────────
    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()          # screen y goes down — flip to math axis
        ang = math.atan2(dy, dx)      # [-π, π], counter-clockwise
        return (ang / (2 * math.pi)) % 1.0

    # ── drawing ──────────────────────────────────────────────────────────────
    def paintEvent(self, _):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        # centre preview circle
        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        # draggable handle
        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)
        p.end()

    # ── fare ─────────────────────────────────────────────────────────────────
    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    """Floating overlay — 4-tab settings panel for JARVIS."""

    saved = pyqtSignal(str, str, str, str)   # assistant_name, user_name, ui_color, voice
    _OW, _OH = 460, 520
    _player = None

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, voice="", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedSize(self._OW, self._OH)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(6)

        hdr = QLabel("\u2699  CUSTOMISE ASSISTANT")
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep0 = QFrame(); sep0.setFrameShape(QFrame.Shape.HLine)
        sep0.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep0)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                background: transparent;
            }}
            QTabBar::tab {{
                background: transparent;
                color: {C.TEXT_MED};
                font-family: 'Courier New';
                font-size: 8pt;
                font-weight: bold;
                padding: 5px 10px;
                border: 1px solid {C.BORDER};
                border-bottom: none;
                border-radius: 3px 3px 0 0;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background: {C.PRI_GHO};
                color: {C.PRI};
                border-color: {C.PRI};
            }}
            QTabBar::tab:hover:!selected {{
                color: {C.TEXT};
                border-color: {C.BORDER_B};
            }}
        """)
        lay.addWidget(self._tabs)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        # ── TAB 1: IDENTITY ─────────────────────────────────────────────────────
        tab1 = QWidget(); t1 = QVBoxLayout(tab1)
        t1.setContentsMargins(12, 10, 12, 10); t1.setSpacing(6)

        t1.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        t1.addWidget(self._name_input)

        t1.addSpacing(4)
        t1.addWidget(_lbl("YOUR NAME  (leave blank for default)", 8,
                          color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        t1.addWidget(self._user_input)

        from memory.config_manager import AVAILABLE_VOICES, DEFAULT_VOICE
        t1.addSpacing(4)
        t1.addWidget(_lbl("ASSISTANT VOICE", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._sel_voice = (voice or DEFAULT_VOICE)
        if self._sel_voice not in AVAILABLE_VOICES:
            self._sel_voice = DEFAULT_VOICE
        self._voice_btns: dict[str, QPushButton] = {}
        voice_row = QHBoxLayout(); voice_row.setSpacing(4)
        for _v in AVAILABLE_VOICES:
            b = QPushButton(_v)
            b.setCheckable(True); b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, name=_v: self._on_voice_pick(name))
            self._voice_btns[_v] = b
            voice_row.addWidget(b)
        t1.addLayout(voice_row)
        self._refresh_voice_btns()

        t1.addSpacing(4)
        t1.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        from memory.config_manager import get_gemini_key
        existing_key = get_gemini_key() or ""
        self._api_key_input = QLineEdit(existing_key)
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_input.setPlaceholderText("AIza...")
        self._api_key_input.setFont(QFont("Courier New", 10))
        self._api_key_input.setFixedHeight(32)
        self._api_key_input.setStyleSheet(_fs)
        t1.addWidget(self._api_key_input)

        t1.addStretch()
        self._tabs.addTab(tab1, "\U0001f916 IDENTITY")

        # ── TAB 2: APPEARANCE ───────────────────────────────────────────────────
        tab2 = QWidget(); t2 = QVBoxLayout(tab2)
        t2.setContentsMargins(12, 10, 12, 10); t2.setSpacing(4)

        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  \u2014  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        t2.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color = self._initial_color
        self.on_preview = None

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        t2.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        t2.addWidget(self._hex_input)

        t2.addSpacing(6)
        t2.addWidget(_lbl("CLOCK FORMAT", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        from memory.config_manager import get_time_format_12h
        clock_row = QHBoxLayout(); clock_row.setSpacing(6)
        self._btn_24h = QPushButton("24H  (13:00)")
        self._btn_12h = QPushButton("12H  (1:00 PM)")
        for b in (self._btn_24h, self._btn_12h):
            b.setCheckable(True); b.setFixedHeight(26)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        is_12h = get_time_format_12h()
        self._btn_12h.setChecked(is_12h); self._btn_24h.setChecked(not is_12h)
        self._btn_24h.clicked.connect(lambda: self._set_clock_format(False))
        self._btn_12h.clicked.connect(lambda: self._set_clock_format(True))
        clock_row.addWidget(self._btn_24h); clock_row.addWidget(self._btn_12h)
        t2.addLayout(clock_row)
        self._refresh_clock_btns()
        t2.addStretch()
        self._tabs.addTab(tab2, "\U0001f3a8 APPEARANCE")

        # ── TAB 3: WAKE & SLEEP ─────────────────────────────────────────────────
        tab3 = QWidget(); t3 = QVBoxLayout(tab3)
        t3.setContentsMargins(12, 10, 12, 10); t3.setSpacing(6)

        from memory.config_manager import get_wake_sensitivity, get_wake_phrases
        sens_hdr = QHBoxLayout()
        sens_hdr.addWidget(_lbl("WAKE WORD SENSITIVITY", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        sens_hdr.addStretch()
        cur_sens = get_wake_sensitivity()
        self._sens_val_lbl = _lbl(f"{cur_sens:.2f}", 8, bold=True, color=C.PRI,
                                   align=Qt.AlignmentFlag.AlignRight)
        sens_hdr.addWidget(self._sens_val_lbl)
        t3.addLayout(sens_hdr)

        self._sens_slider = QSlider(Qt.Orientation.Horizontal)
        self._sens_slider.setRange(30, 80)
        self._sens_slider.setValue(int(cur_sens * 100))
        self._sens_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height: 4px; background: #00121a;
                border: 1px solid {C.BORDER}; border-radius: 2px; }}
            QSlider::sub-page:horizontal {{ background: {C.PRI}; border-radius: 2px; }}
            QSlider::handle:horizontal {{ background: {C.PRI};
                border: 1px solid {C.BORDER_B}; width: 14px;
                margin-top: -5px; margin-bottom: -5px; border-radius: 7px; }}
        """)
        self._sens_slider.valueChanged.connect(
            lambda v: self._sens_val_lbl.setText(f"{v/100.0:.2f}")
        )
        t3.addWidget(self._sens_slider)

        t3.addSpacing(8)
        t3.addWidget(_lbl("WAKE PHRASES (comma-separated, Arabic & English)", 8,
                          color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._phrases_input = QLineEdit(", ".join(get_wake_phrases()))
        self._phrases_input.setFont(QFont("Courier New", 9))
        self._phrases_input.setFixedHeight(28)
        self._phrases_input.setStyleSheet(_fs)
        t3.addSpacing(6)
        t3.addWidget(_lbl("AUTO-SLEEP INACTIVITY TIMEOUT", 8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        from memory.config_manager import get_sleep_timeout
        sleep_row = QHBoxLayout(); sleep_row.setSpacing(4)
        self._sleep_timeout_btns: dict[int, QPushButton] = {}
        cur_sleep = int(get_sleep_timeout())
        for secs, lbl in [(900, "15 MIN"), (1800, "30 MIN"), (3600, "1 HOUR"), (0, "NEVER (معطل)")]:
            b = QPushButton(lbl)
            b.setCheckable(True); b.setFixedHeight(26)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, s=secs: self._set_sleep_timeout(s))
            self._sleep_timeout_btns[secs] = b
            sleep_row.addWidget(b)
        t3.addLayout(sleep_row)
        self._sel_sleep_timeout = cur_sleep if cur_sleep in (900, 1800, 3600, 0) else 900
        self._refresh_sleep_btns()
        t3.addStretch()
        self._tabs.addTab(tab3, "\U0001f514 WAKE & SLEEP")

        # ── TAB 4: TELEGRAM ─────────────────────────────────────────────────────
        tab4 = QWidget(); t4 = QVBoxLayout(tab4)
        t4.setContentsMargins(12, 10, 12, 10); t4.setSpacing(6)

        t4.addWidget(_lbl("TELEGRAM BOT TOKEN", 8, color=C.TEXT_DIM,
                          align=Qt.AlignmentFlag.AlignLeft))
        self._tele_token_input = QLineEdit()
        self._tele_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._tele_token_input.setPlaceholderText("From @BotFather on Telegram")
        self._tele_token_input.setFont(QFont("Courier New", 10))
        self._tele_token_input.setFixedHeight(32)
        self._tele_token_input.setStyleSheet(_fs)
        t4.addWidget(self._tele_token_input)

        self._tele_status_lbl = _lbl("\u25cf Checking...", 8, color=C.TEXT_MED,
                                      align=Qt.AlignmentFlag.AlignLeft)
        t4.addWidget(self._tele_status_lbl)

        t4.addSpacing(4)
        tele_ctrl = QHBoxLayout(); tele_ctrl.setSpacing(6)

        self._tele_enabled_btn = QPushButton("\u25cf ENABLED")
        self._tele_enabled_btn.setCheckable(True)
        self._tele_enabled_btn.setFixedSize(100, 30)
        self._tele_enabled_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._tele_enabled_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tele_enabled_btn.toggled.connect(self._refresh_tele_btn)
        tele_ctrl.addWidget(self._tele_enabled_btn)

        self._tele_start_btn = QPushButton("\u25b6 START BRIDGE")
        self._tele_start_btn.setFixedHeight(30)
        self._tele_start_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._tele_start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tele_start_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        self._tele_start_btn.clicked.connect(self._start_telegram_bridge)
        tele_ctrl.addWidget(self._tele_start_btn)

        self._tele_stop_btn = QPushButton("\u23f9 STOP")
        self._tele_stop_btn.setFixedSize(70, 30)
        self._tele_stop_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._tele_stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tele_stop_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        self._tele_stop_btn.clicked.connect(self._stop_telegram_bridge)
        tele_ctrl.addWidget(self._tele_stop_btn)
        t4.addLayout(tele_ctrl)

        t4.addSpacing(8)
        info_lbl = _lbl(
            "1. \u0627\u0641\u062a\u062d \u062a\u064a\u0644\u064a\u062c\u0631\u0627\u0645 \u2192 @BotFather \u2192 /newbot\n"
            "2. \u0627\u0644\u0635\u0642 \u0627\u0644\u0640 Token \u0641\u0648\u0642 \u2192 SAVE & APPLY\n"
            "3. \u0627\u0636\u063a\u0637 \u25b6 START BRIDGE\n"
            "4. \u0647\u062a\u0648\u0635\u0644\u0643 \u0631\u0633\u0627\u0644\u0629 \u062a\u0623\u0643\u064a\u062f \u0639\u0644\u0649 \u0627\u0644\u0641\u0648\u0631",
            8, color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft
        )
        info_lbl.setWordWrap(True)
        t4.addWidget(info_lbl)
        t4.addStretch()

        # Pre-fill telegram config
        try:
            import json as _json
            from pathlib import Path as _Path
            _tcfg_path = _Path(__file__).parent / "config" / "telegram.json"
            _tcfg = {}
            if _tcfg_path.exists():
                with open(_tcfg_path, "r", encoding="utf-8") as _f:
                    _tcfg = _json.load(_f)
            self._tele_token_input.setText(_tcfg.get("bot_token", ""))
            self._tele_enabled_btn.setChecked(_tcfg.get("enabled", True))
        except Exception:
            self._tele_enabled_btn.setChecked(True)
        self._refresh_tele_btn(self._tele_enabled_btn.isChecked())
        self._update_tele_status()
        self._tabs.addTab(tab4, "\U0001f4f1 TELEGRAM")

        # ── Bottom buttons ─────────────────────────────────────────────────────
        lay.addSpacing(4)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("\u25b8  SAVE & APPLY")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    def set_player(self, player):
        """MainWindow calls this to pass itself so the bridge can write logs."""
        self._player = player

    def _update_tele_status(self):
        try:
            import plugins.telegram_bridge as _tb
            alive = _tb._bridge_running and _tb._bridge_thread and _tb._bridge_thread.is_alive()
            jarvis = getattr(_tb, "_jarvis_instance", None)
            awake = getattr(jarvis, "_awake", False) if jarvis else False
            connected = getattr(jarvis, "session", None) is not None if jarvis else False

            if alive:
                state_str = "🟢 AWAKE" if awake else "😴 SLEEPING"
                conn_str = "🟢 CONNECTED" if connected else "🟡 CONNECTING..."
                self._tele_status_lbl.setText(f"⬤ Bridge RUNNING  |  {state_str}  |  {conn_str}")
                self._tele_status_lbl.setStyleSheet(
                    f"color: {C.PRI}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")
                self._tele_start_btn.setText("\u25b6 RESTART")
            else:
                self._tele_status_lbl.setText("\u25cf Bridge STOPPED")
                self._tele_status_lbl.setStyleSheet(
                    f"color: {C.TEXT_DIM}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")
                self._tele_start_btn.setText("\u25b6 START BRIDGE")
        except Exception:
            self._tele_status_lbl.setText("\u25cf Plugin not loaded yet")
            self._tele_status_lbl.setStyleSheet(
                f"color: {C.TEXT_DIM}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")

    def _start_telegram_bridge(self):
        token = self._tele_token_input.text().strip()
        if not token:
            self._tele_status_lbl.setText("\u26a0 Enter bot token first!")
            self._tele_status_lbl.setStyleSheet(
                f"color: {C.ACC2}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")
            return
        self._save_telegram_config(token, enabled=True)
        try:
            import plugins.telegram_bridge as _tb
            import threading
            self._tele_status_lbl.setText("\u25cf Starting...")
            self._tele_status_lbl.setStyleSheet(
                f"color: {C.ACC2}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")
            _player = self._player
            def _async_start():
                try:
                    if _tb._bridge_running:
                        _tb.run({"action": "stop"})
                        import time; time.sleep(0.3)
                    _tb.run({"action": "start"}, player=_player)
                except Exception as ex:
                    print(f"[TelegramBridge] Error starting from UI: {ex}")

            threading.Thread(target=_async_start, daemon=True, name="TelegramBridgeUI").start()
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(1500, self._update_tele_status)
        except Exception as e:
            self._tele_status_lbl.setText(f"\u26a0 {e}")
            self._tele_status_lbl.setStyleSheet(
                f"color: {C.ACC2}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")

    def _stop_telegram_bridge(self):
        try:
            import plugins.telegram_bridge as _tb
            _tb._bridge_running = False
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(500, self._update_tele_status)
        except Exception as e:
            self._tele_status_lbl.setText(f"\u26a0 {e}")
            self._tele_status_lbl.setStyleSheet(
                f"color: {C.ACC2}; background: transparent; font-family: 'Courier New'; font-size: 8pt;")

    def _save_telegram_config(self, token: str, enabled: bool):
        try:
            import json as _json
            from pathlib import Path as _Path
            cfg_path = _Path(__file__).parent / "config" / "telegram.json"
            cfg = {}
            if cfg_path.exists():
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = _json.load(f)
            if token:
                cfg["bot_token"] = token
            cfg["enabled"] = enabled
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                _json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[UI] Failed to save Telegram config: {e}")

    # ── voice selection ────────────────────────────────────────────────────────
    def _on_voice_pick(self, name: str):
        self._sel_voice = name
        self._refresh_voice_btns()

    def _refresh_voice_btns(self):
        for name, b in self._voice_btns.items():
            on = (name == self._sel_voice)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI}; border-radius: 3px; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_MED};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
                """)

    # ── colour flow ────────────────────────────────────────────────────────────
    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _set_clock_format(self, is_12h: bool):
        self._btn_12h.setChecked(is_12h)
        self._btn_24h.setChecked(not is_12h)
        self._refresh_clock_btns()

    def _refresh_clock_btns(self):
        for b in (self._btn_24h, self._btn_12h):
            if b.isChecked():
                b.setStyleSheet(
                    f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                    f"border: 1px solid {C.PRI}; border-radius: 3px; }}"
                )
            else:
                b.setStyleSheet(
                    f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; }} "
                    f"QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}"
                )

    def _refresh_tele_btn(self, checked: bool):
        if checked:
            self._tele_enabled_btn.setText("\u25cf ENABLED")
            self._tele_enabled_btn.setStyleSheet(
                f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                f"border: 1px solid {C.PRI}; border-radius: 3px; }}"
            )
        else:
            self._tele_enabled_btn.setText("\u25cb DISABLED")
            self._tele_enabled_btn.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                f"border: 1px solid {C.BORDER}; border-radius: 3px; }} "
                f"QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}"
            )

    def _set_sleep_timeout(self, secs: int):
        self._sel_sleep_timeout = secs
        self._refresh_sleep_btns()

    def _refresh_sleep_btns(self):
        for s, b in self._sleep_timeout_btns.items():
            on = (s == self._sel_sleep_timeout)
            b.setChecked(on)
            if on:
                b.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; border: 1px solid {C.PRI}; border-radius: 3px; }}")
            else:
                b.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: 1px solid {C.BORDER}; border-radius: 3px; }} QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}")

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        
        api_key_val = self._api_key_input.text().strip()
        if api_key_val:
            try:
                from memory.config_manager import save_api_keys
                save_api_keys(api_key_val)
            except Exception:
                pass

        try:
            from memory.config_manager import save_time_format_12h, save_wake_sensitivity, save_wake_phrases, save_sleep_timeout
            save_time_format_12h(self._btn_12h.isChecked())
            save_wake_sensitivity(self._sens_slider.value() / 100.0)
            save_sleep_timeout(getattr(self, "_sel_sleep_timeout", 900))
            raw_p = self._phrases_input.text()
            phrases = [p.strip() for p in raw_p.split(",") if p.strip()]
            if phrases:
                save_wake_phrases(phrases)
        except Exception:
            pass
        tele_token = self._tele_token_input.text().strip()
        self._save_telegram_config(tele_token, self._tele_enabled_btn.isChecked())
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR, self._sel_voice)
        self.hide()



class SkillManagerOverlay(QWidget):
    """Floating overlay — list, run, export, import, and view manifests of learned skills."""

    _OW = 460
    _OH = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SkillManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedSize(self._OW, self._OH)

        self._main_lay = QVBoxLayout(self)
        self._main_lay.setContentsMargins(18, 14, 18, 14)
        self._main_lay.setSpacing(8)

        # Header
        hdr_row = QHBoxLayout()
        hdr = QLabel("⚡  SKILL MANAGER & SHARING")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()

        import_btn = QPushButton("📥 IMPORT .SKILL")
        import_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        import_btn.setFixedHeight(22)
        import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        import_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC2}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 6px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.ACC2}; }}
        """)
        import_btn.clicked.connect(self._import_skill_dialog)
        hdr_row.addWidget(import_btn)
        self._main_lay.addLayout(hdr_row)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        self._main_lay.addWidget(sep)

        # Scroll area for skills list
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_lay = QVBoxLayout(self._list_container)
        self._list_lay.setContentsMargins(0, 0, 0, 0)
        self._list_lay.setSpacing(6)
        self._scroll.setWidget(self._list_container)
        self._main_lay.addWidget(self._scroll, stretch=1)

        # Close button
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(28)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        self._main_lay.addWidget(close_btn)

        self.refresh()

    def refresh(self):
        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        try:
            from skills.skill_manager import SkillManager
            skills = SkillManager().list_skills()
        except Exception:
            skills = []

        if not skills:
            empty = QLabel("No learned skills yet.\nSay 'جارفيس اتعلم مهارة' to teach your first skill!")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; padding: 20px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_lay.addWidget(empty)
            self._list_lay.addStretch()
            return

        for s in skills:
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: rgba(0, 15, 25, 180);
                    border: 1px solid {C.BORDER};
                    border-radius: 4px;
                    padding: 4px;
                }}
            """)
            c_lay = QVBoxLayout(card)
            c_lay.setContentsMargins(8, 6, 8, 6)
            c_lay.setSpacing(3)

            top_row = QHBoxLayout()
            title = QLabel(f"⚡ {s.display_name}  ({s.name})")
            title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
            top_row.addWidget(title)
            top_row.addStretch()

            # Manifest button
            m_btn = QPushButton("📋 Manifest")
            m_btn.setFont(QFont("Courier New", 7))
            m_btn.setFixedHeight(20)
            m_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            m_btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 4px; }} QPushButton:hover {{ color: {C.TEXT}; }}")
            m_btn.clicked.connect(lambda _, sk=s: self._show_manifest(sk))
            top_row.addWidget(m_btn)

            # Export button
            exp_btn = QPushButton("📤 Export")
            exp_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            exp_btn.setFixedHeight(20)
            exp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            exp_btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.ACC2}; border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 4px; }} QPushButton:hover {{ border-color: {C.ACC2}; }}")
            exp_btn.clicked.connect(lambda _, sk=s: self._export_skill(sk))
            top_row.addWidget(exp_btn)

            # Delete button
            del_btn = QPushButton("🗑")
            del_btn.setFont(QFont("Courier New", 7))
            del_btn.setFixedSize(22, 20)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(f"QPushButton {{ background: transparent; color: #ff5555; border: 1px solid #552222; border-radius: 2px; }} QPushButton:hover {{ background: #552222; }}")
            del_btn.clicked.connect(lambda _, sk=s: self._delete_skill(sk))
            top_row.addWidget(del_btn)

            c_lay.addLayout(top_row)

            desc = QLabel(s.description or "No description provided.")
            desc.setFont(QFont("Courier New", 7))
            desc.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            desc.setWordWrap(True)
            c_lay.addWidget(desc)

            info = QLabel(f"Author: {s.author}  ·  v{s.version}  ·  {len(s.steps)} steps")
            info.setFont(QFont("Courier New", 6))
            info.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; border: none;")
            c_lay.addWidget(info)

            self._list_lay.addWidget(card)

        self._list_lay.addStretch()

    def _export_skill(self, skill):
        try:
            from skills.skill_manager import SkillManager
            res = SkillManager().export_skill(skill.name)
            QMessageBox.information(self, "Export Skill", res)
        except Exception as e:
            QMessageBox.warning(self, "Export Error", f"Failed to export: {e}")

    def _show_manifest(self, skill):
        manifest_text = skill.generate_manifest()
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Manifest — {skill.display_name}")
        dlg.resize(480, 420)
        dlg.setStyleSheet(f"background: #000c14; color: {C.TEXT};")
        d_lay = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setReadOnly(True)
        te.setFont(QFont("Courier New", 8))
        te.setStyleSheet(f"background: #000810; color: {C.TEXT}; border: 1px solid {C.BORDER};")
        te.setPlainText(manifest_text)
        d_lay.addWidget(te)
        c_btn = QPushButton("CLOSE")
        c_btn.clicked.connect(dlg.accept)
        d_lay.addWidget(c_btn)
        dlg.exec()

    def _delete_skill(self, skill):
        r = QMessageBox.question(
            self, "Delete Skill", f"Delete skill '{skill.display_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if r == QMessageBox.StandardButton.Yes:
            try:
                from skills.skill_manager import SkillManager
                SkillManager().delete_skill(skill.name)
                self.refresh()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _import_skill_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import JARVIS Skill", "", "JARVIS Skill (*.skill *.zip)"
        )
        if path:
            try:
                from skills.skill_manager import SkillManager
                res = SkillManager().import_skill(path)
                QMessageBox.information(self, "Import Result", res)
                self.refresh()
            except Exception as e:
                QMessageBox.warning(self, "Import Error", str(e))


class PluginManagerOverlay(QWidget):
    """Floating overlay — lists discovered plugins with per-plugin ON/OFF toggles."""

    _OW = 420

    def __init__(self, plugins: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginManagerOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🧩  PLUGIN MANAGER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        if not plugins:
            empty = QLabel("No plugins found in /plugins.")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(empty)

        for p in plugins:
            lay.addLayout(self._build_row(p))

        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)
        self.adjustSize()

    def _build_row(self, p: dict) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(6)

        label_text = p["name"] if p["valid"] else f"{p['name']}  (⚠ {p['file']})"
        lbl = QLabel(label_text)
        lbl.setFont(QFont("Courier New", 8))
        lbl.setStyleSheet(f"color: {C.TEXT if p['valid'] else C.TEXT_DIM}; background: transparent;")
        lbl.setToolTip(p["description"] if p["valid"] else p["error"])
        lbl.setWordWrap(False)
        row.addWidget(lbl, stretch=1)

        btn = QPushButton()
        btn.setFixedSize(72, 24)
        btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        if not p["valid"]:
            btn.setText("BROKEN")
            btn.setEnabled(False)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
            """)
        else:
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._style_toggle(btn, p["enabled"])
            btn.clicked.connect(lambda _, name=p["name"], b=btn: self._toggle(name, b))
        row.addWidget(btn)

        if p["name"] == "quick_shortcuts":
            cfg_btn = QPushButton("⚙ EDIT")
            cfg_btn.setFixedSize(54, 24)
            cfg_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            cfg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            cfg_btn.setStyleSheet(f"""
                QPushButton {{ background: #001f2d; color: {C.PRI}; border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            cfg_btn.clicked.connect(self._open_shortcuts_editor)
            row.addWidget(cfg_btn)

        return row

    def _open_shortcuts_editor(self):
        w = self.window()
        if hasattr(w, "_open_shortcuts_manager"):
            w._open_shortcuts_manager()

    def _style_toggle(self, btn: QPushButton, enabled: bool):
        if enabled:
            btn.setText("ON")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            btn.setText("OFF")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)

    def _toggle(self, name: str, btn: QPushButton):
        from memory.config_manager import get_plugin_enabled, save_plugin_enabled
        new_val = not get_plugin_enabled(name)
        save_plugin_enabled(name, new_val)
        self._style_toggle(btn, new_val)


class _FlowNodeWidget(QFrame):
    """Visual Node representing a single step in the Automation Flow pipeline (n8n style)."""
    changed = pyqtSignal()
    move_up_requested = pyqtSignal(object)
    move_down_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)

    def __init__(self, step_idx: int = 1, parent=None):
        super().__init__(parent)
        self.step_idx = step_idx
        self.setStyleSheet(f"""
            _FlowNodeWidget {{
                background: rgba(1, 15, 24, 230);
                border: 1px solid {C.BORDER_A};
                border-radius: 5px;
            }}
            _FlowNodeWidget:hover {{
                border-color: {C.PRI_DIM};
            }}
        """)
        self._build_ui()

    def _build_ui(self):
        vlay = QVBoxLayout(self)
        vlay.setContentsMargins(8, 6, 8, 6)
        vlay.setSpacing(4)

        # Top row: Node badge, Action dropdown, label edit, Up/Down/Delete
        top_row = QHBoxLayout()
        top_row.setSpacing(6)

        self._badge = QLabel(f"NODE #{self.step_idx}")
        self._badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._badge.setStyleSheet(f"""
            color: {C.PRI}; background: #001824;
            border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            padding: 2px 6px;
        """)
        top_row.addWidget(self._badge)

        self._action_combo = QComboBox()
        self._action_combo.setFont(QFont("Segoe UI", 8))
        self._action_combo.setStyleSheet(f"""
            QComboBox {{
                background: #000c14; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                padding: 2px 6px; min-width: 140px;
            }}
            QComboBox QAbstractItemView {{
                background: #000c14; color: {C.TEXT};
                selection-background-color: {C.PRI_GHO}; selection-color: {C.PRI};
                border: 1px solid {C.BORDER_B};
            }}
        """)
        self._action_combo.addItem("🌐 المتصفح (Browser Control)", "browser_control")
        self._action_combo.addItem("🔍 تحقق وشرط (Check Condition)", "check_condition")
        self._action_combo.addItem("🔄 تحديث الصفحة (Refresh Page)", "refresh_page")
        self._action_combo.addItem("👆 نقر على عنصر/زر (Click Element)", "click_element")
        self._action_combo.addItem("👁 الرؤية والكمبيوتر (Vision & PC)", "computer_control")
        self._action_combo.addItem("🚀 فتح تطبيق (Open App)", "open_app")
        self._action_combo.addItem("💬 إرسال رسالة (Send Message)", "send_message")
        self._action_combo.addItem("⏱ انتظار (Wait)", "wait")
        self._action_combo.currentIndexChanged.connect(self._on_action_changed)
        top_row.addWidget(self._action_combo)

        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("تسمية الخطوة / Step Label (اختياري)")
        self._label_edit.setFont(QFont("Segoe UI", 8))
        self._label_edit.setStyleSheet(f"""
            QLineEdit {{
                background: #000c14; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                padding: 2px 6px;
            }}
            QLineEdit:focus {{ border-color: {C.PRI}; }}
        """)
        self._label_edit.textChanged.connect(self.changed.emit)
        top_row.addWidget(self._label_edit, stretch=1)

        # Move Up
        self._btn_up = QPushButton("▲")
        self._btn_up.setFixedSize(20, 20)
        self._btn_up.setFont(QFont("Courier New", 7))
        self._btn_up.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_up.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: 1px solid {C.BORDER}; border-radius: 2px; }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: transparent; }}
        """)
        self._btn_up.clicked.connect(lambda: self.move_up_requested.emit(self))
        top_row.addWidget(self._btn_up)

        # Move Down
        self._btn_down = QPushButton("▼")
        self._btn_down.setFixedSize(20, 20)
        self._btn_down.setFont(QFont("Courier New", 7))
        self._btn_down.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_down.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: 1px solid {C.BORDER}; border-radius: 2px; }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: transparent; }}
        """)
        self._btn_down.clicked.connect(lambda: self.move_down_requested.emit(self))
        top_row.addWidget(self._btn_down)

        # Delete
        self._btn_del = QPushButton("✕")
        self._btn_del.setFixedSize(20, 20)
        self._btn_del.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_del.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: #ff5555; border: 1px solid #552222; border-radius: 2px; }}
            QPushButton:hover {{ background: #552222; color: #ffffff; }}
        """)
        self._btn_del.clicked.connect(lambda: self.delete_requested.emit(self))
        top_row.addWidget(self._btn_del)

        vlay.addLayout(top_row)

        # ── Parameter Containers ──
        _f_style = f"""
            QLineEdit, QComboBox {{
                background: #000a10; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 2px;
                padding: 2px 4px; font-size: 11px;
            }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {C.PRI}; }}
        """
        _toggle_style = f"""
            QPushButton {{
                background: #000a10; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px;
                padding: 2px 6px; font-size: 10px; text-align: left;
            }}
            QPushButton:checked {{
                background: rgba(0, 212, 255, 0.15); color: {C.PRI};
                border-color: {C.PRI}; font-weight: bold;
            }}
        """

        # 1. Browser container
        self._browser_box = QWidget()
        b_lay = QVBoxLayout(self._browser_box)
        b_lay.setContentsMargins(0, 2, 0, 0)
        b_lay.setSpacing(3)
        b_r1 = QHBoxLayout(); b_r1.setSpacing(4)
        self._b_act_combo = QComboBox()
        self._b_act_combo.addItems(["open_with_profile", "navigate", "click", "type"])
        self._b_act_combo.setStyleSheet(_f_style)
        self._b_act_combo.currentIndexChanged.connect(self.changed.emit)
        b_lbl1 = QLabel("إجراء:")
        b_lbl1.setFont(QFont("Segoe UI", 7))
        b_lbl1.setStyleSheet(f"color: {C.TEXT_MED};")
        b_r1.addWidget(b_lbl1)
        b_r1.addWidget(self._b_act_combo)
        self._b_profile = QLineEdit()
        self._b_profile.setPlaceholderText("اسم الحساب (مثال: لويد فورنتيرا)")
        self._b_profile.setStyleSheet(_f_style)
        self._b_profile.textChanged.connect(self.changed.emit)
        b_r1.addWidget(self._b_profile, stretch=1)
        b_lay.addLayout(b_r1)

        b_r2 = QHBoxLayout(); b_r2.setSpacing(4)
        self._b_bookmark = QLineEdit()
        self._b_bookmark.setPlaceholderText("الإشارة المرجعية (مثال: shopify)")
        self._b_bookmark.setStyleSheet(_f_style)
        self._b_bookmark.textChanged.connect(self.changed.emit)
        b_r2.addWidget(self._b_bookmark, stretch=1)
        self._b_url = QLineEdit()
        self._b_url.setPlaceholderText("رابط الصفحة (URL - اختياري)")
        self._b_url.setStyleSheet(_f_style)
        self._b_url.textChanged.connect(self.changed.emit)
        b_r2.addWidget(self._b_url, stretch=1)
        self._b_skip_if_open = QPushButton("✓ تخطي إذا كان مفتوحاً")
        self._b_skip_if_open.setCheckable(True)
        self._b_skip_if_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self._b_skip_if_open.setStyleSheet(_toggle_style)
        self._b_skip_if_open.clicked.connect(self.changed.emit)
        b_r2.addWidget(self._b_skip_if_open)
        b_lay.addLayout(b_r2)
        vlay.addWidget(self._browser_box)

        # 2. Condition Check container
        self._cond_box = QWidget()
        cond_lay = QHBoxLayout(self._cond_box)
        cond_lay.setContentsMargins(0, 2, 0, 0)
        cond_lay.setSpacing(4)
        cond_lbl = QLabel("تحقق من:")
        cond_lbl.setFont(QFont("Segoe UI", 7))
        cond_lbl.setStyleSheet(f"color: {C.TEXT_MED};")
        cond_lay.addWidget(cond_lbl)
        self._cond_target = QLineEdit()
        self._cond_target.setPlaceholderText("الهدف (مثال: shopify, chrome, discord)")
        self._cond_target.setStyleSheet(_f_style)
        self._cond_target.textChanged.connect(self.changed.emit)
        cond_lay.addWidget(self._cond_target, stretch=2)
        self._cond_type = QComboBox()
        self._cond_type.addItem("مفتوح حالياً (Is Open)", "is_open")
        self._cond_type.addItem("ظاهر على الشاشة (Is Visible)", "is_visible")
        self._cond_type.setStyleSheet(_f_style)
        self._cond_type.currentIndexChanged.connect(self.changed.emit)
        cond_lay.addWidget(self._cond_type, stretch=1)
        self._cond_on_met = QComboBox()
        self._cond_on_met.addItem("تخطي الخطوة التالية ⏩", "skip_next")
        self._cond_on_met.addItem("إيقاف البرنامج 🛑", "stop")
        self._cond_on_met.setStyleSheet(_f_style)
        self._cond_on_met.currentIndexChanged.connect(self.changed.emit)
        cond_lay.addWidget(self._cond_on_met, stretch=1)
        vlay.addWidget(self._cond_box)

        # 3. Refresh Page container
        self._refresh_box = QWidget()
        ref_lay = QHBoxLayout(self._refresh_box)
        ref_lay.setContentsMargins(0, 2, 0, 0)
        ref_lay.setSpacing(6)
        ref_icon = QLabel("🔄")
        ref_icon.setFont(QFont("Segoe UI", 9))
        ref_lay.addWidget(ref_icon)
        ref_lbl = QLabel("إعادة تحميل وتحديث الصفحة أو النافذة النشطة تلقائياً (F5 / Browser Reload)")
        ref_lbl.setFont(QFont("Segoe UI", 7))
        ref_lbl.setStyleSheet(f"color: {C.ACC2};")
        ref_lay.addWidget(ref_lbl)
        ref_lay.addStretch()
        vlay.addWidget(self._refresh_box)

        # 4. Click Element container
        self._click_box = QWidget()
        clk_lay = QHBoxLayout(self._click_box)
        clk_lay.setContentsMargins(0, 2, 0, 0)
        clk_lay.setSpacing(4)
        clk_lbl = QLabel("الزر/العنصر:")
        clk_lbl.setFont(QFont("Segoe UI", 7))
        clk_lbl.setStyleSheet(f"color: {C.TEXT_MED};")
        clk_lay.addWidget(clk_lbl)
        self._click_target = QLineEdit()
        self._click_target.setPlaceholderText("اسم أو وصف الزر (مثال: زر الطلبات / Orders / زر الإرسال / submit)")
        self._click_target.setStyleSheet(_f_style)
        self._click_target.textChanged.connect(self.changed.emit)
        clk_lay.addWidget(self._click_target, stretch=2)
        self._click_method = QComboBox()
        self._click_method.addItem("رؤية ذكية (Vision)", "vision")
        self._click_method.addItem("محدد ويب (Selector)", "selector")
        self._click_method.setStyleSheet(_f_style)
        self._click_method.currentIndexChanged.connect(self.changed.emit)
        clk_lay.addWidget(self._click_method, stretch=1)
        vlay.addWidget(self._click_box)

        # 5. Computer container
        self._computer_box = QWidget()
        c_lay = QVBoxLayout(self._computer_box)
        c_lay.setContentsMargins(0, 2, 0, 0)
        c_lay.setSpacing(3)
        c_r1 = QHBoxLayout(); c_r1.setSpacing(4)
        self._c_act_combo = QComboBox()
        self._c_act_combo.addItems(["read_screen", "find_and_click", "type_text", "hotkey"])
        self._c_act_combo.setStyleSheet(_f_style)
        self._c_act_combo.currentIndexChanged.connect(self.changed.emit)
        c_lbl1 = QLabel("إجراء:")
        c_lbl1.setFont(QFont("Segoe UI", 7))
        c_lbl1.setStyleSheet(f"color: {C.TEXT_MED};")
        c_r1.addWidget(c_lbl1)
        c_r1.addWidget(self._c_act_combo)
        self._c_desc = QLineEdit()
        self._c_desc.setPlaceholderText("السؤال أو الأمر (مثال: هل يوجد أي طلبات جديدة باللون الأحمر؟)")
        self._c_desc.setStyleSheet(_f_style)
        self._c_desc.textChanged.connect(self.changed.emit)
        c_r1.addWidget(self._c_desc, stretch=1)
        c_lay.addLayout(c_r1)
        vlay.addWidget(self._computer_box)

        # 6. Open App container
        self._app_box = QWidget()
        a_lay = QHBoxLayout(self._app_box)
        a_lay.setContentsMargins(0, 2, 0, 0)
        a_lay.setSpacing(4)
        self._a_name = QLineEdit()
        self._a_name.setPlaceholderText("اسم التطبيق (مثال: chrome, notepad, calculator)")
        self._a_name.setStyleSheet(_f_style)
        self._a_name.textChanged.connect(self.changed.emit)
        a_lay.addWidget(self._a_name, stretch=1)
        self._a_inst = QLineEdit()
        self._a_inst.setPlaceholderText("تعليمات أو معاملات إضافية (اختياري)")
        self._a_inst.setStyleSheet(_f_style)
        self._a_inst.textChanged.connect(self.changed.emit)
        a_lay.addWidget(self._a_inst, stretch=1)
        self._a_skip_if_open = QPushButton("✓ تخطي إذا كان قيد التشغيل")
        self._a_skip_if_open.setCheckable(True)
        self._a_skip_if_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self._a_skip_if_open.setStyleSheet(_toggle_style)
        self._a_skip_if_open.clicked.connect(self.changed.emit)
        a_lay.addWidget(self._a_skip_if_open)
        vlay.addWidget(self._app_box)

        # 7. Message container
        self._msg_box = QWidget()
        m_lay = QHBoxLayout(self._msg_box)
        m_lay.setContentsMargins(0, 2, 0, 0)
        m_lay.setSpacing(4)
        self._m_target = QLineEdit()
        self._m_target.setPlaceholderText("المستلم / الرقم (Target)")
        self._m_target.setStyleSheet(_f_style)
        self._m_target.textChanged.connect(self.changed.emit)
        m_lay.addWidget(self._m_target, stretch=1)
        self._m_text = QLineEdit()
        self._m_text.setPlaceholderText("نص الرسالة المراد إرسالها")
        self._m_text.setStyleSheet(_f_style)
        self._m_text.textChanged.connect(self.changed.emit)
        m_lay.addWidget(self._m_text, stretch=2)
        vlay.addWidget(self._msg_box)

        # 8. Wait container
        self._wait_box = QWidget()
        w_lay = QHBoxLayout(self._wait_box)
        w_lay.setContentsMargins(0, 2, 0, 0)
        w_lay.setSpacing(4)
        w_lbl = QLabel("مدة الانتظار (ثوانٍ):")
        w_lbl.setFont(QFont("Segoe UI", 7))
        w_lbl.setStyleSheet(f"color: {C.TEXT_MED};")
        w_lay.addWidget(w_lbl)
        self._w_sec = QLineEdit("5")
        self._w_sec.setFixedWidth(80)
        self._w_sec.setStyleSheet(_f_style)
        self._w_sec.textChanged.connect(self.changed.emit)
        w_lay.addWidget(self._w_sec)
        w_lay.addStretch()
        vlay.addWidget(self._wait_box)

        self._on_action_changed()

    def update_index(self, idx: int, is_first: bool, is_last: bool):
        self.step_idx = idx
        self._badge.setText(f"NODE #{idx}")
        self._btn_up.setEnabled(not is_first)
        self._btn_down.setEnabled(not is_last)

    def _on_action_changed(self):
        act = self._action_combo.currentData()
        self._browser_box.setVisible(act == "browser_control")
        self._cond_box.setVisible(act == "check_condition")
        self._refresh_box.setVisible(act in ("refresh_page", "reload"))
        self._click_box.setVisible(act in ("click_element", "click_button", "click"))
        self._computer_box.setVisible(act == "computer_control")
        self._app_box.setVisible(act == "open_app")
        self._msg_box.setVisible(act == "send_message")
        self._wait_box.setVisible(act == "wait")
        self.changed.emit()

    def to_step(self):
        from core.flow_engine import FlowStep
        act = self._action_combo.currentData()
        label = self._label_edit.text().strip()
        params = {}
        condition = ""

        if act == "browser_control":
            params = {
                "action": self._b_act_combo.currentText(),
                "profile_name": self._b_profile.text().strip(),
                "bookmark": self._b_bookmark.text().strip(),
                "url": self._b_url.text().strip(),
                "skip_if_open": self._b_skip_if_open.isChecked(),
            }
            if self._b_skip_if_open.isChecked():
                condition = "skip_if_open"
        elif act == "check_condition":
            params = {
                "target": self._cond_target.text().strip(),
                "check_type": self._cond_type.currentData(),
                "on_met": self._cond_on_met.currentData(),
            }
        elif act in ("refresh_page", "reload"):
            params = {"action": "reload"}
        elif act in ("click_element", "click_button", "click"):
            params = {
                "target": self._click_target.text().strip(),
                "description": self._click_target.text().strip(),
                "method": self._click_method.currentData(),
            }
            if self._click_method.currentData() == "selector":
                params["selector"] = self._click_target.text().strip()
        elif act == "computer_control":
            params = {
                "action": self._c_act_combo.currentText(),
                "question": self._c_desc.text().strip(),
                "description": self._c_desc.text().strip(),
            }
        elif act == "open_app":
            params = {
                "app_name": self._a_name.text().strip(),
                "instruction": self._a_inst.text().strip(),
                "skip_if_open": self._a_skip_if_open.isChecked(),
            }
            if self._a_skip_if_open.isChecked():
                condition = "skip_if_open"
        elif act == "send_message":
            params = {
                "target": self._m_target.text().strip(),
                "message": self._m_text.text().strip(),
            }
        elif act == "wait":
            try:
                s = float(self._w_sec.text().strip() or "1")
            except ValueError:
                s = 1.0
            params = {"seconds": s}
        return FlowStep(action=act, parameters=params, label=label, condition=condition)

    def from_step(self, step):
        idx = self._action_combo.findData(step.action)
        if idx >= 0:
            self._action_combo.setCurrentIndex(idx)
        self._label_edit.setText(step.label or "")
        p = step.parameters or {}
        cond = (step.condition or "").strip().lower()

        if step.action == "browser_control":
            b_act = p.get("action", "open_with_profile")
            b_idx = self._b_act_combo.findText(b_act)
            if b_idx >= 0:
                self._b_act_combo.setCurrentIndex(b_idx)
            self._b_profile.setText(p.get("profile_name", ""))
            self._b_bookmark.setText(p.get("bookmark", ""))
            self._b_url.setText(p.get("url", ""))
            self._b_skip_if_open.setChecked(bool(p.get("skip_if_open") or cond.startswith("skip_if_open")))
        elif step.action == "check_condition":
            self._cond_target.setText(p.get("target", ""))
            c_type_idx = self._cond_type.findData(p.get("check_type", "is_open"))
            if c_type_idx >= 0:
                self._cond_type.setCurrentIndex(c_type_idx)
            c_met_idx = self._cond_on_met.findData(p.get("on_met", "skip_next"))
            if c_met_idx >= 0:
                self._cond_on_met.setCurrentIndex(c_met_idx)
        elif step.action in ("refresh_page", "reload"):
            pass
        elif step.action in ("click_element", "click_button", "click"):
            self._click_target.setText(p.get("target", "") or p.get("description", "") or p.get("selector", ""))
            m_idx = self._click_method.findData(p.get("method", "vision"))
            if m_idx >= 0:
                self._click_method.setCurrentIndex(m_idx)
        elif step.action == "computer_control":
            c_act = p.get("action", "read_screen")
            c_idx = self._c_act_combo.findText(c_act)
            if c_idx >= 0:
                self._c_act_combo.setCurrentIndex(c_idx)
            self._c_desc.setText(p.get("description", "") or p.get("question", "") or p.get("text", ""))
        elif step.action == "open_app":
            self._a_name.setText(p.get("app_name", ""))
            self._a_inst.setText(p.get("instruction", ""))
            self._a_skip_if_open.setChecked(bool(p.get("skip_if_open") or cond.startswith("skip_if_open")))
        elif step.action == "send_message":
            self._m_target.setText(p.get("target", ""))
            self._m_text.setText(p.get("message", ""))
        elif step.action == "wait":
            self._w_sec.setText(str(p.get("seconds", 1)))
        self._on_action_changed()


class AutomationFlowOverlay(QWidget):
    """Floating Sci-Fi overlay for Autonomous Automation Flow Maps (n8n-style engine)."""

    _OW = 660
    _OH = 620

    def __init__(self, parent=None, player=None):
        super().__init__(parent)
        self._player = player
        self._current_flow_id: str | None = None
        self._nodes: list[_FlowNodeWidget] = []

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AutomationFlowOverlay {{
                background: rgba(0, 8, 14, 252);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedSize(self._OW, self._OH)

        self._main_lay = QVBoxLayout(self)
        self._main_lay.setContentsMargins(14, 12, 14, 12)
        self._main_lay.setSpacing(6)

        # ── Top Bar ──
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(6)

        v_titles = QVBoxLayout()
        v_titles.setSpacing(1)
        title_lbl = QLabel("🔀  AUTOMATION FLOW MAP (N8N ENGINE)")
        title_lbl.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        v_titles.addWidget(title_lbl)

        sub_lbl = QLabel("أنظمة التشغيل الآلي وسلاسل المهام المتكررة المستقلة")
        sub_lbl.setFont(QFont("Segoe UI", 7))
        sub_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        v_titles.addWidget(sub_lbl)
        hdr_row.addLayout(v_titles)

        hdr_row.addStretch()

        import_btn = QPushButton("📥 IMPORT .FLOW")
        import_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        import_btn.setFixedHeight(24)
        import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        import_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.ACC2};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 6px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border-color: {C.ACC2}; color: #ffffff;
            }}
        """)
        import_btn.clicked.connect(self._import_flow_dialog)
        hdr_row.addWidget(import_btn)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
        """)
        close_btn.clicked.connect(self.hide)
        hdr_row.addWidget(close_btn)

        self._main_lay.addLayout(hdr_row)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._main_lay.addWidget(sep)

        # ── Mode Switcher Tabs ──
        tab_row = QHBoxLayout()
        tab_row.setSpacing(6)

        self._btn_tab_library = QPushButton("📋 البرامج المسجلة / FLOW LIBRARY")
        self._btn_tab_library.setFixedHeight(28)
        self._btn_tab_library.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._btn_tab_library.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tab_library.clicked.connect(lambda: self._switch_tab(0))
        tab_row.addWidget(self._btn_tab_library, stretch=1)

        self._btn_tab_builder = QPushButton("🛠 مصمم السلاسل / VISUAL BUILDER")
        self._btn_tab_builder.setFixedHeight(28)
        self._btn_tab_builder.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._btn_tab_builder.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tab_builder.clicked.connect(lambda: self._switch_tab(1))
        tab_row.addWidget(self._btn_tab_builder, stretch=1)

        self._btn_tab_skills = QPushButton("⚡ المهارات المسجلة / SKILLS")
        self._btn_tab_skills.setFixedHeight(28)
        self._btn_tab_skills.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._btn_tab_skills.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_tab_skills.clicked.connect(lambda: self._switch_tab(2))
        tab_row.addWidget(self._btn_tab_skills, stretch=1)

        self._main_lay.addLayout(tab_row)

        # ── Stacked Pages ──
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        self._main_lay.addWidget(self._stack, stretch=1)

        # Page 0: Library
        self._page_library = self._build_library_tab()
        self._stack.addWidget(self._page_library)

        # Page 1: Visual Builder
        self._page_builder = self._build_builder_tab()
        self._stack.addWidget(self._page_builder)

        # Page 2: Skills Library
        self._page_skills = self._build_skills_tab()
        self._stack.addWidget(self._page_skills)

        self._switch_tab(0)

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)

    def _switch_tab(self, index: int):
        self._stack.setCurrentIndex(index)
        btn_inactive = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """
        btn_active = f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI}; border-radius: 4px;
            }}
        """
        self._btn_tab_library.setStyleSheet(btn_active if index == 0 else btn_inactive)
        self._btn_tab_builder.setStyleSheet(btn_active if index == 1 else btn_inactive)
        self._btn_tab_skills.setStyleSheet(btn_active if index == 2 else btn_inactive)

        if index == 0:
            self._refresh_library()
        elif index == 1:
            if not self._nodes:
                self._reset_builder()
            else:
                self._update_live_explanation()
        elif index == 2:
            self._refresh_skills()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 3: SKILLS LIBRARY
    # ──────────────────────────────────────────────────────────────────────────

    def _build_skills_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)

        self._skills_scroll = QScrollArea()
        self._skills_scroll.setWidgetResizable(True)
        self._skills_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._skills_container = QWidget()
        self._skills_container.setStyleSheet("background: transparent;")
        self._skills_lay = QVBoxLayout(self._skills_container)
        self._skills_lay.setContentsMargins(0, 0, 4, 0)
        self._skills_lay.setSpacing(8)

        self._skills_scroll.setWidget(self._skills_container)
        lay.addWidget(self._skills_scroll, stretch=1)
        return w

    def _refresh_skills(self):
        while self._skills_lay.count():
            item = self._skills_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        try:
            from skills.skill_manager import SkillManager
            skills = SkillManager().list_skills()
        except Exception as e:
            print(f"[AutomationFlowOverlay] Error reading skills: {e}")
            skills = []

        if not skills:
            empty = QFrame()
            empty.setStyleSheet(f"""
                QFrame {{
                    background: rgba(0, 15, 25, 140);
                    border: 1px dashed {C.BORDER}; border-radius: 6px; padding: 24px;
                }}
            """)
            e_lay = QVBoxLayout(empty)
            e_lbl1 = QLabel("لا توجد مهارات مسجلة حالياً")
            e_lbl1.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            e_lbl1.setStyleSheet(f"color: {C.PRI}; background: transparent;")
            e_lbl1.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e_lay.addWidget(e_lbl1)

            e_lbl2 = QLabel("يمكنك تعليم جارفيس مهارة جديدة في الشات بقول:\n'يا جارفيس اتعلم مهارة كذا' أو 'سجل عندك مهارة...'")
            e_lbl2.setFont(QFont("Segoe UI", 8))
            e_lbl2.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; margin-top: 4px;")
            e_lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e_lay.addWidget(e_lbl2)

            self._skills_lay.addWidget(empty)
            self._skills_lay.addStretch()
            return

        for s in skills:
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: rgba(0, 18, 30, 190);
                    border: 1px solid {C.BORDER_B};
                    border-radius: 6px;
                    padding: 6px;
                }}
            """)
            c_lay = QVBoxLayout(card)
            c_lay.setContentsMargins(10, 8, 10, 8)
            c_lay.setSpacing(4)

            top_row = QHBoxLayout()
            title = QLabel(f"⚡ {s.display_name}  ({s.name})")
            title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
            top_row.addWidget(title)
            top_row.addStretch()

            # Run button
            run_btn = QPushButton("▶ تشغيل")
            run_btn.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            run_btn.setFixedHeight(22)
            run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            run_btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; border: 1px solid {C.PRI}; border-radius: 3px; padding: 0 8px; }} QPushButton:hover {{ background: {C.PRI}; color: #000; }}")
            run_btn.clicked.connect(lambda _, sk=s: self._run_skill_from_overlay(sk))
            top_row.addWidget(run_btn)

            # Export button
            exp_btn = QPushButton("📤 تصدير")
            exp_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
            exp_btn.setFixedHeight(22)
            exp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            exp_btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.ACC2}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 6px; }} QPushButton:hover {{ border-color: {C.ACC2}; }}")
            exp_btn.clicked.connect(lambda _, sk=s: self._export_skill_from_overlay(sk))
            top_row.addWidget(exp_btn)

            # Delete button
            del_btn = QPushButton("🗑")
            del_btn.setFixedSize(24, 22)
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(f"QPushButton {{ background: transparent; color: #ff5555; border: 1px solid #552222; border-radius: 3px; }} QPushButton:hover {{ background: #552222; }}")
            del_btn.clicked.connect(lambda _, sk=s: self._delete_skill_from_overlay(sk))
            top_row.addWidget(del_btn)

            c_lay.addLayout(top_row)

            desc = QLabel(s.description or "لا يوجد وصف لهذه المهارة.")
            desc.setFont(QFont("Segoe UI", 8))
            desc.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
            desc.setWordWrap(True)
            c_lay.addWidget(desc)

            info = QLabel(f"الخطوات: {len(s.steps)}  |  الكاتب: {s.author}  |  الإصدار: v{s.version}")
            info.setFont(QFont("Courier New", 7))
            info.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            c_lay.addWidget(info)

            self._skills_lay.addWidget(card)

        self._skills_lay.addStretch()

    def _run_skill_from_overlay(self, skill):
        try:
            from skills.skill_manager import SkillManager
            res = SkillManager().execute_skill(skill.name)
            QMessageBox.information(self, "تنفيذ المهارة", res)
        except Exception as e:
            QMessageBox.warning(self, "خطأ في التشغيل", f"فشل تشغيل المهارة: {e}")

    def _export_skill_from_overlay(self, skill):
        try:
            from skills.skill_manager import SkillManager
            res = SkillManager().export_skill(skill.name)
            QMessageBox.information(self, "تصدير المهارة", res)
        except Exception as e:
            QMessageBox.warning(self, "خطأ في التصدير", f"فشل تصدير المهارة: {e}")

    def _delete_skill_from_overlay(self, skill):
        reply = QMessageBox.question(
            self, "حذف المهارة",
            f"هل أنت متأكد من حذف المهارة '{skill.display_name}'؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                from skills.skill_manager import SkillManager
                p = SkillManager()._skills_dir / f"{skill.name}.json"
                if p.exists():
                    p.unlink()
                self._refresh_skills()
            except Exception as e:
                QMessageBox.warning(self, "خطأ", f"فشل الحذف: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 1: FLOW LIBRARY
    # ──────────────────────────────────────────────────────────────────────────

    def _build_library_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)

        self._library_scroll = QScrollArea()
        self._library_scroll.setWidgetResizable(True)
        self._library_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._library_container = QWidget()
        self._library_container.setStyleSheet("background: transparent;")
        self._library_lay = QVBoxLayout(self._library_container)
        self._library_lay.setContentsMargins(0, 0, 4, 0)
        self._library_lay.setSpacing(8)

        self._library_scroll.setWidget(self._library_container)
        lay.addWidget(self._library_scroll, stretch=1)
        return w

    def refresh(self):
        self._refresh_library()

    def _refresh_library(self):
        while self._library_lay.count():
            item = self._library_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        try:
            from core.flow_engine import FlowEngine
            flows = FlowEngine.get_instance().list_flows()
        except Exception as e:
            print(f"[AutomationFlowOverlay] Error reading flows: {e}")
            flows = []

        if not flows:
            empty = QFrame()
            empty.setStyleSheet(f"""
                QFrame {{
                    background: rgba(0, 15, 25, 140);
                    border: 1px dashed {C.BORDER}; border-radius: 6px; padding: 24px;
                }}
            """)
            e_lay = QVBoxLayout(empty)
            e_lbl1 = QLabel("لا توجد برامج أوتوميشن مسجلة حالياً")
            e_lbl1.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            e_lbl1.setStyleSheet(f"color: {C.PRI}; background: transparent;")
            e_lbl1.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e_lay.addWidget(e_lbl1)

            e_lbl2 = QLabel("اضغط على تبويب [ مصمم السلاسل / VISUAL BUILDER ] لإنشاء برنامجك وتحديد أمر تشغيله وإيقافه!")
            e_lbl2.setFont(QFont("Segoe UI", 8))
            e_lbl2.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            e_lbl2.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e_lay.addWidget(e_lbl2)

            self._library_lay.addWidget(empty)
            self._library_lay.addStretch()
            return

        for f in flows:
            is_run = (f.status == "RUNNING")
            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: rgba(0, 14, 22, 200);
                    border: 1px solid {C.BORDER_A if not is_run else C.GREEN};
                    border-radius: 6px;
                }}
            """)
            c_lay = QVBoxLayout(card)
            c_lay.setContentsMargins(10, 8, 10, 8)
            c_lay.setSpacing(5)

            # Header row: Name, ID, Status badge
            hdr_row = QHBoxLayout()
            hdr_row.setSpacing(6)

            title = QLabel(f"⚡ {f.name}")
            title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
            hdr_row.addWidget(title)

            id_badge = QLabel(f"({f.id})")
            id_badge.setFont(QFont("Courier New", 7))
            id_badge.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            hdr_row.addWidget(id_badge)

            hdr_row.addStretch()

            status_badge = QLabel("🟢 RUNNING" if is_run else "⚪ IDLE")
            status_badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            if is_run:
                status_badge.setStyleSheet(f"""
                    color: {C.GREEN}; background: #002210;
                    border: 1px solid {C.GREEN}; border-radius: 3px; padding: 2px 6px;
                """)
            else:
                status_badge.setStyleSheet(f"""
                    color: {C.PRI_DIM}; background: #00121d;
                    border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px;
                """)
            hdr_row.addWidget(status_badge)
            c_lay.addLayout(hdr_row)

            # Description
            if f.description:
                desc = QLabel(f.description)
                desc.setFont(QFont("Segoe UI", 8))
                desc.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
                desc.setWordWrap(True)
                c_lay.addWidget(desc)

            # Badges row: start cmd, stop cmd, interval, run count, steps count
            badges_row = QHBoxLayout()
            badges_row.setSpacing(4)

            def _badge_lbl(txt: str, color: str, border_c: str) -> QLabel:
                b = QLabel(txt)
                b.setFont(QFont("Segoe UI", 7))
                b.setStyleSheet(f"""
                    color: {color}; background: #000c14;
                    border: 1px solid {border_c}; border-radius: 3px; padding: 1px 4px;
                """)
                return b

            if f.start_command:
                badges_row.addWidget(_badge_lbl(f"▶ أمر البدء: {f.start_command}", C.TEXT, C.BORDER_B))
            if f.stop_command:
                badges_row.addWidget(_badge_lbl(f"⏹ أمر الإيقاف: {f.stop_command}", C.MUTED_C, "#661122"))
            badges_row.addWidget(_badge_lbl(f"⏱ كل {f.interval_seconds}ث", C.ACC2, C.BORDER))
            badges_row.addWidget(_badge_lbl(f"🔄 {f.run_count} دورة", C.TEXT_DIM, C.BORDER))
            badges_row.addWidget(_badge_lbl(f"⚡ {len(f.steps)} خطوات", C.PRI_DIM, C.BORDER))
            badges_row.addStretch()
            c_lay.addLayout(badges_row)

            # Buttons row
            btn_row = QHBoxLayout()
            btn_row.setSpacing(6)

            # Start / Stop Button
            if is_run:
                st_btn = QPushButton("⏹ STOP / إيقاف")
                st_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
                st_btn.setFixedHeight(22)
                st_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                st_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #2b0008; color: {C.RED};
                        border: 1px solid {C.RED}; border-radius: 3px; padding: 0 8px;
                    }}
                    QPushButton:hover {{ background: #550012; color: #ffffff; }}
                """)
                st_btn.clicked.connect(lambda _, fid=f.id: self._stop_flow(fid))
            else:
                st_btn = QPushButton("▶ START / تشغيل")
                st_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
                st_btn.setFixedHeight(22)
                st_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                st_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #002210; color: {C.GREEN};
                        border: 1px solid {C.GREEN}; border-radius: 3px; padding: 0 8px;
                    }}
                    QPushButton:hover {{ background: #004420; color: #ffffff; }}
                """)
                st_btn.clicked.connect(lambda _, fid=f.id: self._start_flow(fid))
            btn_row.addWidget(st_btn)

            # Explain
            exp_btn = QPushButton("📋 شرح الخطوات")
            exp_btn.setFont(QFont("Segoe UI", 7))
            exp_btn.setFixedHeight(22)
            exp_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            exp_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_MED};
                    border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 6px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
            """)
            exp_btn.clicked.connect(lambda _, flow=f: self._show_explanation_dialog(flow))
            btn_row.addWidget(exp_btn)

            # Edit
            edit_btn = QPushButton("✏ تعديل")
            edit_btn.setFont(QFont("Segoe UI", 7))
            edit_btn.setFixedHeight(22)
            edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            edit_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.PRI};
                    border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 6px;
                }}
                QPushButton:hover {{ border-color: {C.PRI}; background: {C.PRI_GHO}; }}
            """)
            edit_btn.clicked.connect(lambda _, flow=f: self._load_flow_into_builder(flow))
            btn_row.addWidget(edit_btn)

            # Export
            export_btn = QPushButton("📤 تصدير")
            export_btn.setFont(QFont("Segoe UI", 7))
            export_btn.setFixedHeight(22)
            export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            export_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.ACC2};
                    border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 6px;
                }}
                QPushButton:hover {{ border-color: {C.ACC2}; color: #ffffff; }}
            """)
            export_btn.clicked.connect(lambda _, fid=f.id: self._export_flow(fid))
            btn_row.addWidget(export_btn)

            btn_row.addStretch()

            # Delete
            del_btn = QPushButton("🗑")
            del_btn.setFixedSize(22, 22)
            del_btn.setFont(QFont("Courier New", 7))
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: #ff5555;
                    border: 1px solid #552222; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #552222; color: #ffffff; }}
            """)
            del_btn.clicked.connect(lambda _, fid=f.id: self._delete_flow(fid))
            btn_row.addWidget(del_btn)

            c_lay.addLayout(btn_row)
            self._library_lay.addWidget(card)

        self._library_lay.addStretch()

    def _start_flow(self, flow_id: str):
        try:
            from core.flow_engine import FlowEngine
            engine = FlowEngine.get_instance()
            res = engine.start_flow(flow_id, player=self._player)
            QMessageBox.information(self, "تشغيل الأوتوميشن", res)
            self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "خطأ في التشغيل", str(e))

    def _stop_flow(self, flow_id: str):
        try:
            from core.flow_engine import FlowEngine
            engine = FlowEngine.get_instance()
            res = engine.stop_flow(flow_id, player=self._player)
            QMessageBox.information(self, "إيقاف الأوتوميشن", res)
            self.refresh()
        except Exception as e:
            QMessageBox.warning(self, "خطأ في الإيقاف", str(e))

    def _delete_flow(self, flow_id: str):
        from core.flow_engine import FlowEngine
        engine = FlowEngine.get_instance()
        flow = engine.get_flow(flow_id)
        name = flow.name if flow else flow_id
        r = QMessageBox.question(
            self, "حذف برنامج الأوتوميشن",
            f"هل أنت متأكد من حذف البرنامج:\n[{name}]؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if r == QMessageBox.StandardButton.Yes:
            engine.delete_flow(flow_id)
            self.refresh()

    def _export_flow(self, flow_id: str):
        try:
            from core.flow_engine import FlowEngine
            out_file = FlowEngine.get_instance().export_flow(flow_id)
            QMessageBox.information(
                self, "تم التصدير بنجاح",
                f"✅ تم تصدير حزمة الأوتوميشن بنجاح إلى سطح المكتب:\n\n"
                f"📁 {out_file.name}\n"
                f"📄 {out_file.stem}_EXPLANATION.md\n\n"
                f"المسار: {out_file.parent}"
            )
        except Exception as e:
            QMessageBox.warning(self, "خطأ في التصدير", f"فشل تصدير البرنامج:\n{e}")

    def _import_flow_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import JARVIS Flow", "", "Flow Packages (*.flow *.json);;All Files (*.*)"
        )
        if path:
            try:
                from core.flow_engine import FlowEngine
                ok, msg = FlowEngine.get_instance().import_flow(path)
                if ok:
                    QMessageBox.information(self, "تم الاستيراد بنجاح", msg)
                    self.refresh()
                else:
                    QMessageBox.warning(self, "فشل الاستيراد", msg)
            except Exception as e:
                QMessageBox.critical(self, "خطأ غير متوقع", f"تعذر استيراد الملف:\n{e}")

    def _show_explanation_dialog(self, flow):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"شرح البرنامج — {flow.name}")
        dlg.resize(520, 420)
        dlg.setStyleSheet(f"background: #000c14; color: {C.TEXT};")
        d_lay = QVBoxLayout(dlg)
        d_lay.setContentsMargins(14, 12, 14, 12)
        d_lay.setSpacing(8)

        title_lbl = QLabel(f"📋 شرح خطوات برنامج: {flow.name}")
        title_lbl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        d_lay.addWidget(title_lbl)

        te = QTextEdit()
        te.setReadOnly(True)
        te.setFont(QFont("Segoe UI", 9))
        te.setStyleSheet(f"""
            QTextEdit {{
                background: #000810; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 4px;
                padding: 8px; line-height: 1.4;
            }}
        """)
        from core.flow_engine import FlowEngine
        expl_text = flow.explanation or FlowEngine.get_instance().generate_explanation(flow.to_dict())
        te.setPlainText(expl_text)
        d_lay.addWidget(te, stretch=1)

        c_btn = QPushButton("إغلاق / CLOSE")
        c_btn.setFixedHeight(28)
        c_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        c_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        c_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        c_btn.clicked.connect(dlg.accept)
        d_lay.addWidget(c_btn)
        dlg.exec()

    # ──────────────────────────────────────────────────────────────────────────
    # TAB 2: VISUAL BUILDER (N8N STYLE)
    # ──────────────────────────────────────────────────────────────────────────

    def _build_builder_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(6)

        _f_style = f"""
            QLineEdit {{
                background: #000c14; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                padding: 3px 6px; font-size: 11px;
            }}
            QLineEdit:focus {{ border-color: {C.PRI}; }}
        """

        # Scroll Area for Metadata + Nodes Pipeline + Live Preview
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(container)
        c_lay.setContentsMargins(0, 0, 4, 0)
        c_lay.setSpacing(8)

        # 1. Flow Metadata Card
        meta_box = QFrame()
        meta_box.setStyleSheet(f"""
            QFrame {{
                background: rgba(0, 14, 22, 190);
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
        """)
        m_lay = QVBoxLayout(meta_box)
        m_lay.setContentsMargins(10, 8, 10, 8)
        m_lay.setSpacing(5)

        m_hdr = QLabel("▸ بيانات البرنامج والمشغلات (FLOW TRIGGER & METADATA)")
        m_hdr.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        m_hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        m_lay.addWidget(m_hdr)

        r1 = QHBoxLayout(); r1.setSpacing(6)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("اسم البرنامج (مثال: مراقبة متجر شوبيفاي)")
        self._name_edit.setStyleSheet(_f_style)
        self._name_edit.textChanged.connect(self._update_live_explanation)
        r1.addWidget(self._name_edit, stretch=2)

        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText("الوصف المختصر للوظيفة والغرض")
        self._desc_edit.setStyleSheet(_f_style)
        r1.addWidget(self._desc_edit, stretch=3)
        m_lay.addLayout(r1)

        r2 = QHBoxLayout(); r2.setSpacing(6)
        self._start_cmd_edit = QLineEdit()
        self._start_cmd_edit.setPlaceholderText("أمر البدء من الشات (مثال: ابدأ مراقبة شوبيفاي)")
        self._start_cmd_edit.setStyleSheet(_f_style)
        self._start_cmd_edit.textChanged.connect(self._update_live_explanation)
        r2.addWidget(self._start_cmd_edit, stretch=1)

        self._stop_cmd_edit = QLineEdit()
        self._stop_cmd_edit.setPlaceholderText("أمر الإيقاف من الشات (مثال: وقف مراقبة شوبيفاي)")
        self._stop_cmd_edit.setStyleSheet(_f_style)
        self._stop_cmd_edit.textChanged.connect(self._update_live_explanation)
        r2.addWidget(self._stop_cmd_edit, stretch=1)
        m_lay.addLayout(r2)

        r3 = QHBoxLayout(); r3.setSpacing(6)
        r3_lbl = QLabel("فاصل التكرار (ثوانٍ):")
        r3_lbl.setFont(QFont("Segoe UI", 7))
        r3_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        r3.addWidget(r3_lbl)
        self._interval_edit = QLineEdit("60")
        self._interval_edit.setFixedWidth(80)
        self._interval_edit.setStyleSheet(_f_style)
        self._interval_edit.textChanged.connect(self._update_live_explanation)
        r3.addWidget(self._interval_edit)

        r3_hint = QLabel("• يتم تكرار مسار الخطوات كاملاً كل فاصل زمني حتى يتلقى أمر الإيقاف من الشات.")
        r3_hint.setFont(QFont("Segoe UI", 7))
        r3_hint.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        r3.addWidget(r3_hint, stretch=1)
        m_lay.addLayout(r3)

        c_lay.addWidget(meta_box)

        # 2. Nodes Chain Section
        pipe_box = QFrame()
        pipe_box.setStyleSheet(f"""
            QFrame {{
                background: rgba(0, 10, 18, 160);
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
        """)
        p_lay = QVBoxLayout(pipe_box)
        p_lay.setContentsMargins(10, 8, 10, 8)
        p_lay.setSpacing(6)

        p_hdr = QHBoxLayout()
        p_title = QLabel("▸ مسار خطوات التنفيذ (N8N EXECUTION PIPELINE)")
        p_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p_title.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        p_hdr.addWidget(p_title)
        p_hdr.addStretch()

        add_node_btn = QPushButton("➕ إضافة خطوة (ADD NODE)")
        add_node_btn.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        add_node_btn.setFixedHeight(22)
        add_node_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_node_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; padding: 0 6px;
            }}
            QPushButton:hover {{
                background: rgba(0, 212, 255, 0.25); border-color: {C.PRI}; color: #ffffff;
            }}
        """)
        add_node_btn.clicked.connect(lambda: self._add_node())
        p_hdr.addWidget(add_node_btn)
        p_lay.addLayout(p_hdr)

        self._nodes_container = QWidget()
        self._nodes_container.setStyleSheet("background: transparent; border: none;")
        self._nodes_lay = QVBoxLayout(self._nodes_container)
        self._nodes_lay.setContentsMargins(0, 4, 0, 4)
        self._nodes_lay.setSpacing(4)
        p_lay.addWidget(self._nodes_container)

        c_lay.addWidget(pipe_box)

        # 3. Live Explanation Preview Box
        expl_box = QFrame()
        expl_box.setStyleSheet(f"""
            QFrame {{
                background: rgba(0, 10, 18, 160);
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
        """)
        x_lay = QVBoxLayout(expl_box)
        x_lay.setContentsMargins(10, 8, 10, 8)
        x_lay.setSpacing(4)

        x_hdr = QLabel("▸ معاينة شرح البرنامج باللغة الطبيعية (LIVE EXPLANATION PREVIEW)")
        x_hdr.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        x_hdr.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        x_lay.addWidget(x_hdr)

        self._explanation_preview = QTextEdit()
        self._explanation_preview.setReadOnly(True)
        self._explanation_preview.setFixedHeight(85)
        self._explanation_preview.setFont(QFont("Segoe UI", 8))
        self._explanation_preview.setStyleSheet(f"""
            QTextEdit {{
                background: #000810; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                padding: 4px;
            }}
        """)
        x_lay.addWidget(self._explanation_preview)
        c_lay.addWidget(expl_box)

        scroll.setWidget(container)
        lay.addWidget(scroll, stretch=1)

        # 4. Pinned Bottom Actions Bar
        bot_bar = QHBoxLayout()
        bot_bar.setSpacing(8)

        save_btn = QPushButton("💾 SAVE FLOW / حفظ البرنامج")
        save_btn.setFixedHeight(28)
        save_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI}; border-radius: 3px; padding: 0 10px;
            }}
            QPushButton:hover {{
                background: rgba(0, 212, 255, 0.25); color: #ffffff;
            }}
        """)
        save_btn.clicked.connect(self._save_flow)
        bot_bar.addWidget(save_btn, stretch=2)

        new_btn = QPushButton("🔄 NEW / مسح وإنشاء جديد")
        new_btn.setFixedHeight(28)
        new_btn.setFont(QFont("Courier New", 8))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        new_btn.clicked.connect(self._reset_builder)
        bot_bar.addWidget(new_btn, stretch=1)

        export_now_btn = QPushButton("📤 EXPORT NOW / حفظ وتصدير")
        export_now_btn.setFixedHeight(28)
        export_now_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        export_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        export_now_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.ACC2};
                border: 1px solid {C.BORDER_B}; border-radius: 3px; padding: 0 8px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border-color: {C.ACC2}; color: #ffffff;
            }}
        """)
        export_now_btn.clicked.connect(self._save_and_export)
        bot_bar.addWidget(export_now_btn, stretch=1)

        lay.addLayout(bot_bar)
        return w

    def _add_node(self, step=None):
        node = _FlowNodeWidget(step_idx=len(self._nodes) + 1)
        if step:
            node.from_step(step)
        node.changed.connect(self._update_live_explanation)
        node.move_up_requested.connect(self._move_node_up)
        node.move_down_requested.connect(self._move_node_down)
        node.delete_requested.connect(self._remove_node)
        self._nodes.append(node)
        self._rebuild_nodes_layout()

    def _remove_node(self, node_widget):
        if len(self._nodes) <= 1:
            QMessageBox.information(self, "تنبيه", "يجب أن يحتوي مسار الأوتوميشن على خطوة واحدة على الأقل.")
            return
        if node_widget in self._nodes:
            self._nodes.remove(node_widget)
            node_widget.deleteLater()
            self._rebuild_nodes_layout()

    def _move_node_up(self, node_widget):
        if node_widget in self._nodes:
            idx = self._nodes.index(node_widget)
            if idx > 0:
                self._nodes[idx], self._nodes[idx - 1] = self._nodes[idx - 1], self._nodes[idx]
                self._rebuild_nodes_layout()

    def _move_node_down(self, node_widget):
        if node_widget in self._nodes:
            idx = self._nodes.index(node_widget)
            if idx < len(self._nodes) - 1:
                self._nodes[idx], self._nodes[idx + 1] = self._nodes[idx + 1], self._nodes[idx]
                self._rebuild_nodes_layout()

    def _rebuild_nodes_layout(self):
        while self._nodes_lay.count():
            item = self._nodes_lay.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        n_total = len(self._nodes)
        for i, node in enumerate(self._nodes):
            node.update_index(i + 1, is_first=(i == 0), is_last=(i == n_total - 1))
            self._nodes_lay.addWidget(node)
            if i < n_total - 1:
                arrow = QLabel("▼")
                arrow.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                arrow.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; border: none;")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._nodes_lay.addWidget(arrow)

        self._update_live_explanation()

    def _update_live_explanation(self):
        name = self._name_edit.text().strip() or "برنامج الأوتوميشن"
        start_cmd = self._start_cmd_edit.text().strip() or "(لم يحدد)"
        stop_cmd = self._stop_cmd_edit.text().strip() or "(لم يحدد)"
        try:
            interval = int(self._interval_edit.text().strip() or "60")
        except ValueError:
            interval = 60

        steps_dicts = [node.to_step().to_dict() for node in self._nodes]
        flow_data = {
            "name": name,
            "start_command": start_cmd,
            "stop_command": stop_cmd,
            "interval_seconds": interval,
            "steps": steps_dicts,
        }
        try:
            from core.flow_engine import FlowEngine
            expl = FlowEngine.get_instance().generate_explanation(flow_data)
            self._explanation_preview.setPlainText(expl)
        except Exception:
            pass

    def _load_flow_into_builder(self, flow):
        self._current_flow_id = flow.id
        self._name_edit.setText(flow.name)
        self._desc_edit.setText(flow.description)
        self._start_cmd_edit.setText(flow.start_command)
        self._stop_cmd_edit.setText(flow.stop_command)
        self._interval_edit.setText(str(flow.interval_seconds))

        for n in self._nodes:
            n.deleteLater()
        self._nodes.clear()

        for s in flow.steps:
            self._add_node(step=s)

        if not self._nodes:
            self._add_node()

        self._switch_tab(1)

    def _reset_builder(self):
        self._current_flow_id = None
        self._name_edit.clear()
        self._desc_edit.clear()
        self._start_cmd_edit.clear()
        self._stop_cmd_edit.clear()
        self._interval_edit.setText("60")

        for n in self._nodes:
            n.deleteLater()
        self._nodes.clear()
        self._add_node()
        self._update_live_explanation()

    def _save_flow(self) -> bool:
        name = self._name_edit.text().strip()
        desc = self._desc_edit.text().strip()
        start_cmd = self._start_cmd_edit.text().strip()
        stop_cmd = self._stop_cmd_edit.text().strip()
        try:
            interval = int(self._interval_edit.text().strip() or "60")
        except ValueError:
            interval = 60

        if not name:
            QMessageBox.warning(self, "تنبيه", "يرجى إدخال اسم البرنامج (Flow Name).")
            return False
        if not start_cmd:
            QMessageBox.warning(self, "تنبيه", "يرجى كتابة أمر البدء من الشات (Start Command).")
            return False
        if not stop_cmd:
            QMessageBox.warning(self, "تنبيه", "يرجى كتابة أمر الإيقاف من الشات (Stop Command).")
            return False
        if not self._nodes:
            QMessageBox.warning(self, "تنبيه", "يجب إضافة خطوة واحدة على الأقل في مسار التنفيذ.")
            return False

        from core.flow_engine import FlowEngine, AutomationFlow
        engine = FlowEngine.get_instance()

        if self._current_flow_id:
            fid = self._current_flow_id
            existing = engine.get_flow(fid)
            run_count = existing.run_count if existing else 0
            created_at = existing.created_at if existing else time.time()
            status = existing.status if existing else "IDLE"
        else:
            slug = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name.lower())
            fid = f"{slug}_{int(time.time())}" if slug else f"flow_{int(time.time())}"
            self._current_flow_id = fid
            run_count = 0
            created_at = time.time()
            status = "IDLE"

        steps = [n.to_step() for n in self._nodes]
        flow = AutomationFlow(
            id=fid,
            name=name,
            description=desc,
            start_command=start_cmd,
            stop_command=stop_cmd,
            interval_seconds=interval,
            steps=steps,
            run_count=run_count,
            created_at=created_at,
            status=status,
        )
        flow.explanation = engine.generate_explanation(flow.to_dict())

        ok = engine.save_flow(flow)
        if ok:
            QMessageBox.information(
                self, "تم الحفظ بنجاح",
                f"✅ تم حفظ برنامج الأوتوميشن [{flow.name}] بنجاح!\n\n"
                f"• أمر البدء: `{flow.start_command}`\n"
                f"• أمر الإيقاف: `{flow.stop_command}`\n"
                f"• الفاصل الزمني: كل {flow.interval_seconds} ثانية\n\n"
                f"يمكنك تشغيل البرنامج الآن من الشات بكتابة أمر البدء أو بالضغط على زر START في قائمة البرامج!"
            )
            self._refresh_library()
            self._switch_tab(0)
            return True
        else:
            QMessageBox.critical(self, "خطأ", "تعذر حفظ ملف البرنامج.")
            return False

    def _save_and_export(self):
        if self._save_flow():
            if self._current_flow_id:
                self._export_flow(self._current_flow_id)


class _HudOverlay(QWidget):
    """Base for the floating panels placed by hand over the HUD.

    They are children of the central widget but sit in no layout, so Qt never
    invalidates the region they occupy when they hide or shrink: the HUD keeps
    painting around them and their last frame stays on screen as a ghost. Any
    overlay positioned with _centre_overlay needs this."""

    def hideEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            # Repaint exactly what we were covering, before we stop covering it.
            p.update(self.geometry())
        super().hideEvent(e)

    def closeEvent(self, e):
        p = self.parentWidget()
        if p is not None:
            p.update(self.geometry())
        super().closeEvent(e)


class ConfirmBanner(_HudOverlay):
    """The gate in front of an action that cannot be taken back.

    The old confirmation was a tool parameter the model filled in itself, which
    means it confirmed its own shutdown requests. This is the interface asking,
    and the answer travels from a human finger to core/confirm.py without the
    model in the loop. Nothing blocks while it is up: the assistant keeps
    talking, so this costs no latency — unlike the old gate, which spent two
    tool round trips on every power command."""

    answered = pyqtSignal(bool)
    _OW = 430

    def __init__(self, title: str, detail: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ConfirmBanner {{
                background: rgba(14, 3, 0, 250);
                border: 1px solid {C.ACC};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        hdr = QLabel("⚠  CONFIRM")
        hdr.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(hdr)

        ttl = QLabel(title)
        ttl.setWordWrap(True)
        ttl.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        ttl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(ttl)

        if detail:
            dtl = QLabel(detail)
            dtl.setWordWrap(True)
            dtl.setFont(QFont("Courier New", 8))
            dtl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            lay.addWidget(dtl)

        row = QHBoxLayout(); row.setSpacing(8)

        yes = QPushButton("▸  CONFIRM")
        yes.setFixedHeight(32)
        yes.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        yes.setCursor(Qt.CursorShape.PointingHandCursor)
        yes.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC};
                border: 1px solid {C.ACC}; border-radius: 3px; }}
            QPushButton:hover {{ background: rgba(255,107,0,40); }}
        """)
        yes.clicked.connect(lambda: self.answered.emit(True))
        row.addWidget(yes)

        no = QPushButton("CANCEL")
        no.setFixedHeight(32)
        no.setFont(QFont("Courier New", 9))
        no.setCursor(Qt.CursorShape.PointingHandCursor)
        no.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        no.clicked.connect(lambda: self.answered.emit(False))
        row.addWidget(no)
        lay.addLayout(row)

        # Default focus on CANCEL: if someone hits Enter without reading, the
        # safe answer wins.
        no.setDefault(True)
        no.setFocus()


class AudioDeviceOverlay(_HudOverlay):
    """Choose which microphone JARVIS listens to and which speakers it uses.

    Both audio streams used to open with no `device=` at all, so they always
    took the OS default — which on Windows moves by itself the moment a headset
    is plugged in. 'JARVIS can't hear me' is usually 'JARVIS is listening to the
    webcam'."""

    picked = pyqtSignal()      # emitted after Apply, when something changed
    _OW = 460

    def __init__(self, parent=None):
        super().__init__(parent)
        from core.audio_devices import list_devices, DEFAULT_LABEL
        from memory.config_manager import get_input_device, get_output_device

        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            AudioDeviceOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(6)

        hdr = QLabel("🎧  AUDIO DEVICES")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        _combo_css = (
            f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
            f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
            f"QComboBox:hover {{ border-color: {C.BORDER_B}; }}"
            f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
            f"selection-background-color: {C.PRI_GHO}; border: 1px solid {C.BORDER}; }}"
        )

        def _row(label: str, kind: str, current: str) -> QComboBox:
            cap = QLabel(label)
            cap.setFont(QFont("Courier New", 8))
            cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            lay.addWidget(cap)

            box = QComboBox()
            box.setFont(QFont("Courier New", 9))
            box.setFixedHeight(30)
            box.setStyleSheet(_combo_css)
            # The list is served from a cache warmed on a background thread at
            # startup, so opening this panel never blocks the Qt thread on the
            # host audio API.
            box.addItem(DEFAULT_LABEL, "")
            for name in list_devices(kind):
                box.addItem(name, name)
            idx = box.findData(current) if current else 0
            box.setCurrentIndex(idx if idx >= 0 else 0)
            if current and idx < 0:
                # Saved device is not plugged in right now. Show it rather than
                # silently resetting the user's choice to default.
                box.addItem(f"{current}  (not connected)", current)
                box.setCurrentIndex(box.count() - 1)
            lay.addWidget(box)
            return box

        self._in_box  = _row("MICROPHONE — what JARVIS hears you with",
                             "input", get_input_device())
        lay.addSpacing(4)
        self._out_box = _row("SPEAKERS — what JARVIS talks through",
                             "output", get_output_device())

        note = QLabel("Applying reconnects the session. Your conversation is kept.")
        note.setWordWrap(True)
        note.setFont(QFont("Courier New", 7))
        note.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addSpacing(6)
        lay.addWidget(note)

        row = QHBoxLayout(); row.setSpacing(8)
        ok = QPushButton("▸  APPLY")
        ok.setFixedHeight(32)
        ok.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        ok.clicked.connect(self._apply)
        row.addWidget(ok)

        cancel = QPushButton("CLOSE")
        cancel.setFixedHeight(32)
        cancel.setFont(QFont("Courier New", 9))
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel.clicked.connect(self.hide)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _apply(self):
        from memory.config_manager import (
            get_input_device, get_output_device,
            save_input_device, save_output_device,
        )
        new_in  = self._in_box.currentData()  or ""
        new_out = self._out_box.currentData() or ""
        changed = (new_in != get_input_device()) or (new_out != get_output_device())
        save_input_device(new_in)
        save_output_device(new_out)
        self.hide()
        # Only rebuild the session if something actually moved — a no-op Apply
        # should not cost a reconnect.
        if changed:
            self.picked.emit()


class MemoryOverlay(_HudOverlay):
    """Everything JARVIS has stored about you, and when it learned it.

    Memory used to be a 2200-character store that deleted its oldest entries
    when full and mentioned it only on stdout. The cap is gone; this panel is
    the other half of that change — a memory you cannot inspect is a memory you
    cannot trust, and 'delete' has to be something the person can do."""

    _OW = 520

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            MemoryOverlay {{
                background: rgba(0, 6, 10, 246);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(20, 16, 20, 16)
        self._lay.setSpacing(5)
        self._rebuild()

    def _clear_layout(self):
        """Take every item out of the layout and detach it from the widget tree
        in this call.

        deleteLater() on its own is not enough: it queues destruction for the
        next event-loop pass, and until then the old rows are still children of
        this widget and still paint — which is what drew half of the previous
        panel over the new one. setParent(None) removes them from the tree now;
        deleteLater() then frees them safely."""
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # hide() stops it painting in this frame; deleteLater() frees it
                # safely afterwards. setParent(None) would also stop the paint,
                # but it turns the widget into a top-level window for the moment
                # between the two calls, which is not something to leave lying
                # around inside a click handler.
                w.hide()
                w.deleteLater()
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    si = sub.takeAt(0)
                    sw = si.widget()
                    if sw is not None:
                        sw.hide()
                        sw.deleteLater()
                sub.deleteLater()

    def _settle(self, before):
        """Size the panel to its content, re-centre it, and repaint what the old
        size covered.

        The re-size has to happen here rather than at the end of _rebuild
        because Qt has not polished the freshly-created children at that point,
        so the size hint it would read is the empty-layout one. Measured: a
        first adjustSize() returned 32 px for a panel whose content needed 155,
        and a second call — after the same widgets had been through the event
        loop — returned 155. So this runs twice: once now, once on the next
        turn, from _rebuild.

        The re-centre and the repaint are needed because the overlay is placed
        by hand and is in no layout: shrinking it leaves it off-centre and
        leaves its former pixels on screen, since nothing tells the parent that
        region changed. The repaint has to cover the union of the old and new
        rectangles."""
        self._lay.invalidate()
        self._lay.activate()
        self.updateGeometry()
        self.adjustSize()

        p = self.parentWidget()
        if p is None:
            self.update()
            return
        self.move(max(0, (p.width()  - self.width())  // 2),
                  max(0, (p.height() - self.height()) // 2))
        p.update(before.united(self.geometry()))
        self.update()

    def _rebuild(self):
        before = self.geometry()
        self._clear_layout()

        from memory.memory_manager import all_entries_for_ui

        hdr = QLabel("🧠  WHAT JARVIS REMEMBERS")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._lay.addWidget(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        self._lay.addWidget(sep)

        rows = all_entries_for_ui()

        cap = QLabel(f"{len(rows)} stored facts — newest first. "
                     f"Nothing here is sent anywhere; it lives in "
                     f"memory/long_term.json on this machine.")
        cap.setWordWrap(True)
        cap.setFont(QFont("Courier New", 7))
        cap.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._lay.addWidget(cap)

        if not rows:
            empty = QLabel("Nothing stored yet.")
            empty.setFont(QFont("Courier New", 9))
            empty.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            self._lay.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFixedHeight(min(420, 34 * len(rows) + 10))
            scroll.setStyleSheet(
                f"QScrollArea {{ border: 1px solid {C.BORDER}; border-radius: 3px; "
                f"background: transparent; }}"
            )
            inner = QWidget()
            ilay  = QVBoxLayout(inner)
            ilay.setContentsMargins(6, 6, 6, 6)
            ilay.setSpacing(3)

            for r in rows:
                line = QHBoxLayout(); line.setSpacing(6)
                txt = QLabel(f"<b>{r['key'].replace('_', ' ')}</b> "
                             f"<span style='color:{C.TEXT_MED}'>— {r['value']}</span>")
                txt.setWordWrap(True)
                txt.setFont(QFont("Courier New", 8))
                txt.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
                line.addWidget(txt, 1)

                meta = QLabel(f"{r['category'][:4]} · {r['updated'] or '—'}")
                meta.setFont(QFont("Courier New", 7))
                meta.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
                line.addWidget(meta)

                rm = QPushButton("✕")
                rm.setFixedSize(20, 20)
                rm.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                rm.setCursor(Qt.CursorShape.PointingHandCursor)
                rm.setToolTip("Forget this")
                rm.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px; }}
                    QPushButton:hover {{ color: {C.RED}; border-color: {C.RED}; }}
                """)
                rm.clicked.connect(
                    lambda _=False, c=r["category"], k=r["key"]: self._forget(c, k))
                line.addWidget(rm)

                holder = QWidget()
                holder.setLayout(line)
                ilay.addWidget(holder)

            ilay.addStretch()
            scroll.setWidget(inner)
            self._lay.addWidget(scroll)

        close = QPushButton("CLOSE")
        close.setFixedHeight(30)
        close.setFont(QFont("Courier New", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close.clicked.connect(self.hide)
        self._lay.addWidget(close)

        self._settle(before)
        # …and again once Qt has polished the new children, because the size
        # hint is not final until then. Harmless when the first pass already
        # got it right: _settle is idempotent.
        QTimer.singleShot(0, lambda g=before: self._settle(g))

    def _forget(self, category: str, key: str):
        from memory.memory_manager import forget
        forget(key, category)
        # Rebuild on the NEXT event-loop turn, not inside this click handler.
        # The rebuild destroys the very ✕ button that emitted this signal, and
        # Qt is entitled to touch the sender after a slot returns; tearing it
        # down mid-emission is how a widget ends up half-alive on screen.
        QTimer.singleShot(0, self._rebuild)


class ProtocolInfoOverlay(_HudOverlay):
    """Sci-Fi HUD Diagnostics, Architecture & Shortcuts Overlay."""

    audio_requested     = pyqtSignal()
    memory_requested    = pyqtSignal()
    customize_requested = pyqtSignal()

    _OW = 540

    def __init__(self, protocol: str = "", assistant_name: str = "JARVIS",
                 user_name: str = "AMOX", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ProtocolInfoOverlay {{
                background: rgba(0, 6, 10, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(8)

        # ── Header ──
        hdr_row = QHBoxLayout()
        hdr_title = QLabel("⚡  PROTOCOL DIAGNOSTICS & SYSTEM INFO")
        hdr_title.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        hdr_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr_row.addWidget(hdr_title)

        hdr_row.addStretch()

        ver_badge = QLabel("LIV · BUILD 54")
        ver_badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        ver_badge.setStyleSheet(f"""
            color: {C.GREEN}; background: {C.PANEL2};
            border: 1px solid {C.BORDER_A}; border-radius: 3px;
            padding: 2px 6px;
        """)
        hdr_row.addWidget(ver_badge)
        lay.addLayout(hdr_row)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        # ── System Identity Block ──
        sec_title = QLabel("▸ SYSTEM IDENTITY")
        sec_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        sec_title.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(sec_title)

        ident_box = QFrame()
        ident_box.setStyleSheet(f"""
            QFrame {{
                background: {C.PANEL2};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
            }}
        """)
        ident_lay = QVBoxLayout(ident_box)
        ident_lay.setContentsMargins(10, 6, 10, 6)
        ident_lay.setSpacing(3)

        def _info_row(k: str, v: str, col: str = C.TEXT):
            r = QHBoxLayout()
            r.setSpacing(6)
            k_lbl = QLabel(k)
            k_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            k_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; min-width: 100px;")
            r.addWidget(k_lbl)

            v_lbl = QLabel(v)
            v_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            v_lbl.setStyleSheet(f"color: {col}; background: transparent;")
            r.addWidget(v_lbl)
            r.addStretch()
            ident_lay.addLayout(r)

        _info_row("PROTOCOL:", protocol or f"Protocol Mark {APP_PROTOCOL} (Build 54)", C.PRI)
        _info_row("ASSISTANT:", assistant_name, C.GREEN)
        _info_row("OPERATOR:", user_name, C.WHITE)
        _info_row("SYS ENGINE:", "Antigravity Multi-Agent Dev Team (11 Autonomous Agents)", C.ACC2)
        lay.addWidget(ident_box)

        # ── Multi-Agent Dev Team Roster ──
        team_title = QLabel("▸ DEV TEAM ARCHITECTURE")
        team_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        team_title.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(team_title)

        team_box = QFrame()
        team_box.setStyleSheet(f"""
            QFrame {{
                background: {C.PANEL};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
            }}
        """)
        team_lay = QVBoxLayout(team_box)
        team_lay.setContentsMargins(10, 6, 10, 6)
        team_lay.setSpacing(2)

        t1 = QLabel("🧭 Nour (Tech Lead) · 🎨 Rem (UI/UX) · ⚡ Dr.Light (Core) · 🤖 Kamski (Native)")
        t1.setFont(QFont("Courier New", 7))
        t1.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        team_lay.addWidget(t1)

        t2 = QLabel("♟️ Shikamaru (Python) · 🏗️ X (Domain) · 🗄️ Miku (DB) · 💎 Topaz (FinTech)")
        t2.setFont(QFont("Courier New", 7))
        t2.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        team_lay.addWidget(t2)

        t3 = QLabel("🧪 Senku (R&D) · 🐺 Silver Wolf (AppSec) · 🎯 Conor (QA Lead)")
        t3.setFont(QFont("Courier New", 7))
        t3.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        team_lay.addWidget(t3)
        lay.addWidget(team_box)

        # ── Shortcuts Reference ──
        sc_title = QLabel("▸ KEYBOARD SHORTCUTS")
        sc_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        sc_title.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(sc_title)

        sc_box = QFrame()
        sc_box.setStyleSheet(f"""
            QFrame {{
                background: {C.PANEL2};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
            }}
        """)
        sc_lay = QVBoxLayout(sc_box)
        sc_lay.setContentsMargins(10, 6, 10, 6)
        sc_lay.setSpacing(3)

        def _sc_row(key: str, desc: str):
            r = QHBoxLayout()
            r.setSpacing(8)
            k_badge = QLabel(f" {key} ")
            k_badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            k_badge.setStyleSheet(f"""
                color: {C.PRI}; background: #001824;
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                padding: 1px 4px; min-width: 60px;
            """)
            k_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            r.addWidget(k_badge)

            d_lbl = QLabel(desc)
            d_lbl.setFont(QFont("Courier New", 7))
            d_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
            r.addWidget(d_lbl)
            r.addStretch()
            sc_lay.addLayout(r)

        _sc_row("F4", "Mute / Unmute Microphone")
        _sc_row("F11", "Toggle Fullscreen Sci-Fi HUD")
        _sc_row("Ctrl + K", "Discord Quick Switcher")
        _sc_row("Space", "Push to Talk (Active when enabled)")
        _sc_row("Esc", "Interrupt Assistant Speech")
        lay.addWidget(sc_box)

        # ── Quick Action Buttons ──
        act_title = QLabel("▸ QUICK ACTION PANELS")
        act_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        act_title.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(act_title)

        act_row = QHBoxLayout()
        act_row.setSpacing(6)

        def _action_btn(label: str, tip: str, primary: bool = False) -> QPushButton:
            b = QPushButton(label)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setFixedHeight(26)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            if primary:
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: {C.PRI_GHO}; color: {C.PRI};
                        border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                        padding: 0 6px;
                    }}
                    QPushButton:hover {{
                        background: rgba(0, 212, 255, 0.25);
                        border-color: {C.PRI}; color: #ffffff;
                    }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent; color: {C.TEXT};
                        border: 1px solid {C.BORDER_A}; border-radius: 3px;
                        padding: 0 6px;
                    }}
                    QPushButton:hover {{
                        background: {C.PANEL2};
                        border-color: {C.BORDER_B}; color: #ffffff;
                    }}
                """)
            return b

        audio_btn = _action_btn("🎧 AUDIO", "Manage Microphones & Speakers", primary=True)
        audio_btn.clicked.connect(self._on_audio)
        act_row.addWidget(audio_btn)

        mem_btn = _action_btn("🧠 MEMORY", "Inspect Learned Facts & Profile")
        mem_btn.clicked.connect(self._on_memory)
        act_row.addWidget(mem_btn)

        cust_btn = _action_btn("⚙ CUSTOMIZE", "Colors, Names & Voice Settings")
        cust_btn.clicked.connect(self._on_customize)
        act_row.addWidget(cust_btn)

        close_btn = QPushButton("✕ CLOSE")
        close_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        close_btn.setFixedHeight(26)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("Close Diagnostics Overlay")
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                padding: 0 6px;
            }}
            QPushButton:hover {{
                color: {C.RED}; border-color: {C.RED};
            }}
        """)
        close_btn.clicked.connect(self.hide)
        act_row.addWidget(close_btn)

        lay.addLayout(act_row)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.hide()
            e.accept()
            return
        super().keyPressEvent(e)

    def _on_audio(self):
        self.hide()
        self.audio_requested.emit()

    def _on_memory(self):
        self.hide()
        self.memory_requested.emit()

    def _on_customize(self):
        self.hide()
        self.customize_requested.emit()


class ClipboardPanel(QWidget):
    """Floating panel shown when text is copied — offers quick Jarvis actions."""

    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class PluginSettingsOverlay(QWidget):
    """Floating overlay — renders per-plugin settings forms.

    Fully generic: it iterates the settings schemas a plugin declared via its
    PLUGIN_SETTINGS constant (delivered by PluginRegistry.settings_schemas) and
    builds a form for each. It knows NOTHING about any specific plugin, so the
    core stays clean and plugins remain pure drop-in — install a plugin that
    declares fields (e.g. the 3D-printer suite) and its section appears here;
    install none and this panel simply says there's nothing to configure.
    """

    _test_done = pyqtSignal(str, bool, str)   # namespace, ok, message
    _OW = 460

    def __init__(self, sections: list[dict], parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            PluginSettingsOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self._sections = sections or []
        self._widgets: dict[tuple, object] = {}    # (namespace, key) -> input widget
        self._types:   dict[tuple, str]    = {}     # (namespace, key) -> field type
        self._status_labels: dict[str, QLabel] = {} # namespace -> status QLabel
        self._test_done.connect(self._on_test_done)

        self._fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
                    f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 16)
        root.setSpacing(8)

        root.addWidget(self._lbl("⚙  PLUGIN SETTINGS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        root.addWidget(sep)

        if not self._sections:
            root.addWidget(self._lbl(
                "No configurable plugins are installed.\nDrop a plugin that needs "
                "settings (like the 3D-printer suite) into the plugins folder and "
                "it will show up here.", 9, color=C.TEXT_DIM))
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            inner = QWidget()
            inner.setStyleSheet("background: transparent;")
            form = QVBoxLayout(inner)
            form.setContentsMargins(0, 0, 6, 0)
            form.setSpacing(6)
            for sec in self._sections:
                self._build_section(form, sec)
            form.addStretch(1)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        # ── bottom buttons ───────────────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        if self._sections:
            save_btn = QPushButton("▸  SAVE")
            save_btn.setFixedHeight(34)
            save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            save_btn.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
            """)
            save_btn.clicked.connect(self._save_all)
            btn_row.addWidget(save_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px; }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _lbl(self, txt, fs=9, bold=False, color=C.PRI,
             align=Qt.AlignmentFlag.AlignLeft):
        w = QLabel(txt); w.setAlignment(align); w.setWordWrap(True)
        w.setFont(QFont("Courier New", fs,
                        QFont.Weight.Bold if bold else QFont.Weight.Normal))
        w.setStyleSheet(f"color: {color}; background: transparent;")
        return w

    def _build_section(self, form: QVBoxLayout, sec: dict):
        ns     = sec.get("namespace") or sec.get("plugin") or "plugin"
        title  = sec.get("title") or ns
        fields = sec.get("fields") or []
        values = sec.get("values") or {}

        form.addSpacing(4)
        form.addWidget(self._lbl(title, 10, True, C.PRI))

        for field in fields:
            if not isinstance(field, dict) or not field.get("key"):
                continue
            key   = field["key"]
            ftype = (field.get("type") or "text").lower()
            label = field.get("label") or key
            default = field.get("default")
            stored  = values.get(key, default)

            form.addWidget(self._lbl(label.upper(), 8, color=C.TEXT_DIM))

            if ftype == "choice":
                w = QComboBox()
                w.addItems([str(o) for o in field.get("options", [])])
                w.setFont(QFont("Courier New", 9))
                w.setFixedHeight(30)
                w.setStyleSheet(
                    f"QComboBox {{ background: #000d12; color: {C.TEXT}; "
                    f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 8px; }}"
                    f"QComboBox QAbstractItemView {{ background: #000d12; color: {C.TEXT}; "
                    f"selection-background-color: {C.PRI_GHO}; }}")
                if stored is not None:
                    w.setCurrentText(str(stored))
            elif ftype == "toggle":
                w = QPushButton()
                w.setCheckable(True)
                w.setChecked(bool(stored))
                w.setFixedHeight(28)
                w.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
                w.setCursor(Qt.CursorShape.PointingHandCursor)
                self._style_toggle(w)
                w.toggled.connect(lambda _=False, b=w: self._style_toggle(b))
            else:  # text / password
                w = QLineEdit("" if stored is None else str(stored))
                w.setFont(QFont("Courier New", 10))
                w.setFixedHeight(30)
                w.setStyleSheet(self._fs)
                if field.get("placeholder"):
                    w.setPlaceholderText(str(field["placeholder"]))
                if ftype == "password":
                    w.setEchoMode(QLineEdit.EchoMode.Password)

            self._widgets[(ns, key)] = w
            self._types[(ns, key)]   = ftype
            form.addWidget(w)

        # optional test/connect action button + status line
        action = sec.get("action")
        if isinstance(action, dict) and callable(action.get("run")):
            form.addSpacing(2)
            ab = QPushButton(str(action.get("label") or "TEST"))
            ab.setFixedHeight(30)
            ab.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            ab.setCursor(Qt.CursorShape.PointingHandCursor)
            ab.setStyleSheet(f"""
                QPushButton {{ background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)
            ab.clicked.connect(lambda _=False, n=ns: self._run_action(n))
            form.addWidget(ab)

        if ns == "quick_shortcuts":
            form.addSpacing(2)
            m_btn = QPushButton("⚡ OPEN SHORTCUTS MANAGER (تعديل وإضافة الاختصارات)")
            m_btn.setFixedHeight(30)
            m_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            m_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            m_btn.setStyleSheet(f"""
                QPushButton {{ background: #001f2d; color: {C.PRI}; border: 1px solid {C.PRI}; border-radius: 3px; }}
                QPushButton:hover {{ background: {C.PRI_GHO}; }}
            """)
            m_btn.clicked.connect(self._open_shortcuts_from_settings)
            form.addWidget(m_btn)

        status = self._lbl("", 8, color=C.TEXT_DIM)
        self._status_labels[ns] = status
        form.addWidget(status)

        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        form.addWidget(line)

    def _open_shortcuts_from_settings(self):
        w = self.window()
        if hasattr(w, "_open_shortcuts_manager"):
            w._open_shortcuts_manager()

    def _style_toggle(self, btn: QPushButton):
        on = btn.isChecked()
        btn.setText("ON" if on else "OFF")
        if on:
            btn.setStyleSheet(f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI}; "
                              f"border: 1px solid {C.PRI}; border-radius: 3px; }}")
        else:
            btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; "
                              f"border: 1px solid {C.BORDER}; border-radius: 3px; }}")

    # ── data ──────────────────────────────────────────────────────────────────
    def _gather(self, ns: str) -> dict:
        out = {}
        for (n, key), w in self._widgets.items():
            if n != ns:
                continue
            t = self._types.get((n, key), "text")
            if t == "choice":
                out[key] = w.currentText()
            elif t == "toggle":
                out[key] = w.isChecked()
            else:
                out[key] = w.text().strip()
        return out

    def _save_ns(self, ns: str):
        from memory.config_manager import save_plugin_config
        save_plugin_config(ns, self._gather(ns))

    def _save_all(self):
        for sec in self._sections:
            ns = sec.get("namespace") or sec.get("plugin")
            if ns:
                self._save_ns(ns)
                lbl = self._status_labels.get(ns)
                if lbl:
                    lbl.setText("Saved ✓")
                    lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")

    def _run_action(self, ns: str):
        sec = next((s for s in self._sections
                    if (s.get("namespace") or s.get("plugin")) == ns), None)
        if not sec:
            return
        run_fn = (sec.get("action") or {}).get("run")
        if not callable(run_fn):
            return
        self._save_ns(ns)                 # persist what the user typed before testing
        values = self._gather(ns)
        lbl = self._status_labels.get(ns)
        if lbl:
            lbl.setText("Testing…")
            lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")

        def worker():
            try:
                res = run_fn(values)
                if isinstance(res, tuple) and len(res) == 2:
                    ok, msg = bool(res[0]), str(res[1])
                else:
                    ok, msg = bool(res), str(res)
            except Exception as e:
                ok, msg = False, str(e)
            self._test_done.emit(ns, ok, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_done(self, ns: str, ok: bool, msg: str):
        lbl = self._status_labels.get(ns)
        if not lbl:
            return
        lbl.setText(msg)
        color = C.PRI if ok else "#ff6b6b"
        lbl.setStyleSheet(f"color: {color}; background: transparent;")


class ShortcutsManagerOverlay(QWidget):
    """Floating overlay — manage custom shortcuts (keywords, apps, URLs, fullscreen/minimize mode)."""

    closed = pyqtSignal()
    _OW = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ShortcutsManagerOverlay {{
                background: rgba(0, 6, 10, 252);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._OW)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(8)

        # Header
        hdr = QLabel("⚡  CUSTOM SHORTCUTS & LAUNCHER")
        hdr.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(hdr)

        sub = QLabel("Configure custom keywords for apps & URLs with fullscreen or minimize mode.")
        sub.setFont(QFont("Courier New", 8))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        sub.setWordWrap(True)
        lay.addWidget(sub)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        # List of existing shortcuts inside a scroll area
        lbl_list = QLabel("📋 REGISTERED SHORTCUTS")
        lbl_list.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        lbl_list.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(lbl_list)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self._scroll.setFixedHeight(180)
        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_lay = QVBoxLayout(self._list_container)
        self._list_lay.setContentsMargins(0, 0, 4, 0)
        self._list_lay.setSpacing(5)
        self._scroll.setWidget(self._list_container)
        lay.addWidget(self._scroll)

        # Separator
        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        # Form: Add / Edit Shortcut
        form_lbl = QLabel("➕ ADD / UPDATE SHORTCUT")
        form_lbl.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        form_lbl.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        lay.addWidget(form_lbl)

        # Keyword row
        kw_lay = QHBoxLayout(); kw_lay.setSpacing(6)
        kw_title = QLabel("KEYWORD:")
        kw_title.setFixedWidth(75)
        kw_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        kw_title.setStyleSheet(f"color: {C.TEXT_MED};")
        kw_lay.addWidget(kw_title)
        self._kw_input = QLineEdit()
        self._kw_input.setPlaceholderText("e.g. فيسبوك, discord, antigravity, blender")
        self._kw_input.setFont(QFont("Courier New", 9))
        self._kw_input.setFixedHeight(28)
        self._kw_input.setStyleSheet(f"QLineEdit {{ background: #000d12; color: {C.TEXT}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px; }} QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")
        kw_lay.addWidget(self._kw_input)
        lay.addLayout(kw_lay)

        # Type & Mode row
        tm_lay = QHBoxLayout(); tm_lay.setSpacing(6)
        type_title = QLabel("TYPE:")
        type_title.setFixedWidth(75)
        type_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        type_title.setStyleSheet(f"color: {C.TEXT_MED};")
        tm_lay.addWidget(type_title)

        self._type_combo = QComboBox()
        self._type_combo.addItems(["🌐 Website (موقع)", "💻 Application (برنامج)"])
        self._type_combo.setFont(QFont("Courier New", 8))
        self._type_combo.setFixedHeight(28)
        self._type_combo.setStyleSheet(f"QComboBox {{ background: #000d12; color: {C.TEXT}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px; }}")
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        tm_lay.addWidget(self._type_combo)

        mode_title = QLabel("MODE:")
        mode_title.setFixedWidth(45)
        mode_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        mode_title.setStyleSheet(f"color: {C.TEXT_MED};")
        tm_lay.addWidget(mode_title)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["⛶ Fullscreen (شاشة كاملة)", "_ Minimized (تصغير)"])
        self._mode_combo.setFont(QFont("Courier New", 8))
        self._mode_combo.setFixedHeight(28)
        self._mode_combo.setStyleSheet(f"QComboBox {{ background: #000d12; color: {C.TEXT}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px; }}")
        tm_lay.addWidget(self._mode_combo)
        lay.addLayout(tm_lay)

        # Target row (URL or Path)
        tgt_lay = QHBoxLayout(); tgt_lay.setSpacing(6)
        tgt_title = QLabel("TARGET:")
        tgt_title.setFixedWidth(75)
        tgt_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        tgt_title.setStyleSheet(f"color: {C.TEXT_MED};")
        tgt_lay.addWidget(tgt_title)
        self._tgt_input = QLineEdit()
        self._tgt_input.setPlaceholderText("https://... or website domain")
        self._tgt_input.setFont(QFont("Courier New", 8))
        self._tgt_input.setFixedHeight(28)
        self._tgt_input.setStyleSheet(f"QLineEdit {{ background: #000d12; color: {C.TEXT}; border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px; }} QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")
        tgt_lay.addWidget(self._tgt_input)

        self._browse_btn = QPushButton("📁 Browse")
        self._browse_btn.setFixedSize(75, 28)
        self._browse_btn.setFont(QFont("Courier New", 8))
        self._browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_btn.setStyleSheet(f"QPushButton {{ background: #001f2d; color: {C.PRI}; border: 1px solid {C.PRI_DIM}; border-radius: 3px; }} QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}")
        self._browse_btn.clicked.connect(self._browse_file)
        self._browse_btn.hide()
        tgt_lay.addWidget(self._browse_btn)
        lay.addLayout(tgt_lay)

        # Save button & Status line
        save_lay = QHBoxLayout(); save_lay.setSpacing(8)
        self._status_lbl = QLabel("")
        self._status_lbl.setFont(QFont("Courier New", 8))
        self._status_lbl.setStyleSheet(f"color: {C.GREEN};")
        save_lay.addWidget(self._status_lbl, stretch=1)

        save_btn = QPushButton("💾 SAVE SHORTCUT")
        save_btn.setFixedHeight(30)
        save_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"QPushButton {{ background: #002b1b; color: {C.GREEN}; border: 1px solid {C.GREEN}; border-radius: 3px; padding: 0 14px; }} QPushButton:hover {{ background: #004028; }}")
        save_btn.clicked.connect(self._save_shortcut)
        save_lay.addWidget(save_btn)
        lay.addLayout(save_lay)

        # Bottom close
        lay.addSpacing(4)
        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 9))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.TEXT_MED}; border: 1px solid {C.BORDER}; border-radius: 3px; }} QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}")
        close_btn.clicked.connect(self._do_close)
        lay.addWidget(close_btn)

        self._refresh_list()

    def _on_type_changed(self, idx: int):
        if idx == 1:  # Application
            self._browse_btn.show()
            self._tgt_input.setPlaceholderText("Path to .exe / shortcut, or app name (e.g. Discord, Blender)")
        else:  # Website
            self._browse_btn.hide()
            self._tgt_input.setPlaceholderText("https://... or website domain")

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Application Executable or Shortcut",
            "",
            "Applications (*.exe *.bat *.cmd *.lnk);;All Files (*.*)"
        )
        if path:
            self._tgt_input.setText(path)
            if not self._kw_input.text().strip():
                base = Path(path).stem
                self._kw_input.setText(base)

    def _save_shortcut(self):
        kw = self._kw_input.text().strip()
        tgt = self._tgt_input.text().strip()
        if not kw or not tgt:
            self._status_lbl.setText("⚠ Keyword and target are required.")
            self._status_lbl.setStyleSheet("color: #ff6b6b;")
            return

        sc_type = "app" if self._type_combo.currentIndex() == 1 else "url"
        sc_mode = "minimize" if self._mode_combo.currentIndex() == 1 else "fullscreen"

        try:
            from plugins.quick_shortcuts import run as run_shortcut
            res = run_shortcut({
                "action": "add",
                "keyword": kw,
                "target": tgt,
                "type": sc_type,
                "mode": sc_mode
            })
            self._status_lbl.setText(f"✓ Saved '{kw}' ({sc_mode})")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN};")
            self._kw_input.clear()
            self._tgt_input.clear()
            self._refresh_list()
        except Exception as e:
            self._status_lbl.setText(f"Error: {e}")
            self._status_lbl.setStyleSheet("color: #ff6b6b;")

    def _refresh_list(self):
        while self._list_lay.count():
            item = self._list_lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        try:
            from plugins.quick_shortcuts import load_shortcuts
            data = load_shortcuts()
        except Exception:
            data = {}

        if not data:
            empty = QLabel("No shortcuts registered yet. Add one below!")
            empty.setFont(QFont("Courier New", 8))
            empty.setStyleSheet(f"color: {C.TEXT_DIM};")
            self._list_lay.addWidget(empty)
            return

        for key, sc in data.items():
            row = QFrame()
            row.setStyleSheet(f"QFrame {{ background: rgba(0, 16, 25, 180); border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px; }}")
            rlay = QHBoxLayout(row)
            rlay.setContentsMargins(6, 4, 6, 4)
            rlay.setSpacing(6)

            kw_lbl = QLabel(f"<b>{sc.get('keyword', key)}</b>")
            kw_lbl.setFont(QFont("Courier New", 8))
            kw_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
            rlay.addWidget(kw_lbl)

            sc_t = str(sc.get("type", "url")).upper()
            t_col = C.GREEN if sc_t == "URL" else C.PRI
            t_badge = QLabel(sc_t)
            t_badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            t_badge.setStyleSheet(f"color: {t_col}; background: rgba(0,0,0,100); border: 1px solid {t_col}; border-radius: 2px; padding: 1px 3px;")
            rlay.addWidget(t_badge)

            sc_m = str(sc.get("mode", "fullscreen")).upper()
            m_badge = QLabel("⛶ FULL" if "FULL" in sc_m else "_ MIN")
            m_badge.setFont(QFont("Courier New", 7))
            m_badge.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: 1px solid {C.ACC2}; border-radius: 2px; padding: 1px 3px;")
            rlay.addWidget(m_badge)

            tgt_text = sc.get("target", "")
            tgt_short = (tgt_text[:24] + "…") if len(tgt_text) > 26 else tgt_text
            tgt_lbl = QLabel(tgt_short)
            tgt_lbl.setFont(QFont("Courier New", 7))
            tgt_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
            tgt_lbl.setToolTip(tgt_text)
            rlay.addWidget(tgt_lbl, stretch=1)

            test_b = QPushButton("▷")
            test_b.setFixedSize(22, 20)
            test_b.setFont(QFont("Courier New", 7))
            test_b.setToolTip("Test Launch")
            test_b.setCursor(Qt.CursorShape.PointingHandCursor)
            test_b.setStyleSheet(f"QPushButton {{ background: transparent; color: {C.GREEN}; border: 1px solid {C.GREEN_D}; }} QPushButton:hover {{ background: #002b1b; }}")
            test_b.clicked.connect(lambda _, k=key: self._test_shortcut(k))
            rlay.addWidget(test_b)

            del_b = QPushButton("✕")
            del_b.setFixedSize(22, 20)
            del_b.setFont(QFont("Courier New", 7))
            del_b.setToolTip("Delete Shortcut")
            del_b.setCursor(Qt.CursorShape.PointingHandCursor)
            del_b.setStyleSheet(f"QPushButton {{ background: transparent; color: #ff6b6b; border: 1px solid #772222; }} QPushButton:hover {{ background: #3a1111; }}")
            del_b.clicked.connect(lambda _, k=key: self._delete_shortcut(k))
            rlay.addWidget(del_b)

            self._list_lay.addWidget(row)

        self._list_lay.addStretch(1)

    def _test_shortcut(self, key: str):
        try:
            from plugins.quick_shortcuts import run as run_shortcut
            run_shortcut({"action": "open", "keyword": key})
            self._status_lbl.setText(f"✓ Launched '{key}'")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN};")
        except Exception as e:
            self._status_lbl.setText(f"Launch error: {e}")
            self._status_lbl.setStyleSheet("color: #ff6b6b;")

    def _delete_shortcut(self, key: str):
        try:
            from plugins.quick_shortcuts import run as run_shortcut
            run_shortcut({"action": "delete", "keyword": key})
            self._status_lbl.setText(f"✓ Deleted '{key}'")
            self._status_lbl.setStyleSheet(f"color: {C.TEXT_DIM};")
            self._refresh_list()
        except Exception as e:
            self._status_lbl.setText(f"Delete error: {e}")
            self._status_lbl.setStyleSheet("color: #ff6b6b;")

    def _do_close(self):
        self.hide()
        self.closed.emit()


class RemoteKeyOverlay(QWidget):
    """Floating overlay — QR code for instant phone pairing + manual key fallback."""

    closed = pyqtSignal()

    _OW, _OH = 420, 525

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", tailscale_url: str = "",
                 tailscale_auto: str = "", tailscale_manual: str = "",
                 expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.96);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry            = time.time() + expiry_secs
        self._on_new_key        = None
        self._url               = url
        self._key               = key
        self._auto_login_url    = auto_login_url
        self._manual_url        = manual_url or url
        self._tailscale_url     = tailscale_url
        self._tailscale_auto    = tailscale_auto
        self._tailscale_manual  = tailscale_manual or tailscale_url
        self._active_mode       = "tailscale" if tailscale_auto else "lan"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE DASHBOARD ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        # ── Network Mode Selector (Tailscale Anywhere vs Local WiFi) ──────────
        mode_box = QHBoxLayout()
        mode_box.setSpacing(8)
        self._ts_btn = QPushButton("🌐 TAILSCALE (ANYWHERE)")
        self._lan_btn = QPushButton("🏠 LOCAL (WI-FI)")
        for b in (self._ts_btn, self._lan_btn):
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ts_btn.clicked.connect(lambda: self._set_mode("tailscale"))
        self._lan_btn.clicked.connect(lambda: self._set_mode("lan"))
        mode_box.addWidget(self._ts_btn)
        mode_box.addWidget(self._lan_btn)
        lay.addLayout(mode_box)

        self._status_badge = QLabel()
        self._status_badge.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._status_badge)

        # ── QR code ───────────────────────────────────────────────────────────
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(160, 160)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._hint_lbl = _lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM)
        lay.addWidget(self._hint_lbl)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter address manually in phone browser:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 26, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 4px 4px;
            letter-spacing: 8px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(30)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(30)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._set_mode(self._active_mode)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _set_mode(self, mode: str):
        self._active_mode = mode
        if mode == "tailscale" and self._tailscale_auto:
            self._update_qr(self._tailscale_auto)
            self._url_lbl.setText(self._tailscale_manual or self._tailscale_url)
            self._status_badge.setText("🟢 TAILSCALE ACTIVE — ACCESS FROM ANY NETWORK / MOBILE DATA")
            self._status_badge.setStyleSheet(f"color: {C.GREEN}; background: rgba(34,197,94,0.12); border-radius: 4px; padding: 2px 6px;")
            self._ts_btn.setStyleSheet(f"background: {C.PRI_GHO}; color: {C.PRI}; border: 1px solid {C.PRI}; border-radius: 4px;")
            self._lan_btn.setStyleSheet(f"background: transparent; color: {C.TEXT_DIM}; border: 1px solid {C.BORDER}; border-radius: 4px;")
            self._hint_lbl.setText("Scan with phone connected to Tailscale (any network or cellular)")
        else:
            self._active_mode = "lan"
            self._update_qr(self._auto_login_url)
            self._url_lbl.setText(self._manual_url)
            if self._tailscale_auto:
                self._status_badge.setText("🔵 LOCAL WI-FI SUBNET (SAME NETWORK ONLY)")
                self._status_badge.setStyleSheet(f"color: {C.PRI}; background: rgba(0,212,255,0.1); border-radius: 4px; padding: 2px 6px;")
            else:
                self._status_badge.setText("⚠️ TAILSCALE NOT DETECTED — USING LOCAL WI-FI")
                self._status_badge.setStyleSheet(f"color: #f59e0b; background: rgba(245,158,11,0.1); border-radius: 4px; padding: 2px 6px;")
            self._lan_btn.setStyleSheet(f"background: {C.PRI_GHO}; color: {C.PRI}; border: 1px solid {C.PRI}; border-radius: 4px;")
            self._ts_btn.setStyleSheet(f"background: transparent; color: {C.TEXT_DIM}; border: 1px solid {C.BORDER}; border-radius: 4px;")
            self._hint_lbl.setText("Scan with phone on the SAME local Wi-Fi router")

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=4, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(154, 154,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        """Call from any thread when a phone successfully connects."""
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url         = result[0]
                key         = result[1]
                auto        = result[2] if len(result) >= 3 else ""
                manual      = result[3] if len(result) >= 4 else url
                ts_url      = result[4] if len(result) >= 5 else ""
                ts_auto     = result[5] if len(result) >= 6 else ""
                ts_manual   = result[6] if len(result) >= 7 else ""
                self._url               = url
                self._key               = key
                self._manual_url        = manual or url
                self._auto_login_url    = auto
                self._tailscale_url     = ts_url
                self._tailscale_auto    = ts_auto
                self._tailscale_manual  = ts_manual or ts_url
                self._key_lbl.setText(key)
                self._expiry = time.time() + 600
                self._set_mode(self._active_mode)
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 4px 4px;
                    letter-spacing: 8px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class _CommandInput(QLineEdit):
    def __init__(self, main_win, parent=None):
        super().__init__(parent)
        self._win = main_win

    def keyPressEvent(self, e):
        if (e.key() == Qt.Key.Key_V and e.modifiers() & Qt.KeyboardModifier.ControlModifier) or e.matches(QKeySequence.StandardKey.Paste):
            try:
                clipboard = QApplication.clipboard()
                mime = clipboard.mimeData()
                if mime.hasImage() and not mime.hasText():
                    if hasattr(self._win, "_paste_from_clipboard") and self._win._paste_from_clipboard():
                        e.accept()
                        return
                elif mime.hasImage() and mime.hasText():
                    if hasattr(self._win, "_paste_from_clipboard"):
                        self._win._paste_from_clipboard()
            except Exception:
                pass
        super().keyPressEvent(e)


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)   # (title, text) — thread-safe content display
    _reconfig_sig   = pyqtSignal()           # trigger setup overlay from any thread
    _camera_sig     = pyqtSignal(bytes)      # show camera frame preview (small overlay)
    _cam_stream_sig = pyqtSignal(bool)       # True=start live stream, False=stop
    _cam_frame_sig  = pyqtSignal(bytes)      # live camera frame → HUD area
    _clipboard_sig  = pyqtSignal(str)        # clipboard text changed (thread-safe)
    _confirm_sig    = pyqtSignal(str, str)   # (title, detail) — irreversible-action gate
    _confirm_hide_sig = pyqtSignal()
    _wake_dl_sig    = pyqtSignal(bool, str)  # wake-word install finished (ok, message)
    _quiz_sig       = pyqtSignal(str, object, object)  # (topic, questions, grader)
    _quiz_hide_sig  = pyqtSignal()
    _review_sig     = pyqtSignal(str, str, object, object)  # document review payload
    _flow_status_sig = pyqtSignal(str, str)                 # (flow_id, status) — automation flow loop state updates
    _video_open_sig  = pyqtSignal(str, str, bool, str)  # video, title, muted, audio
    _video_close_sig = pyqtSignal()
    _video_mute_sig  = pyqtSignal(bool)

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        # Load customization from config
        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        # Apply the saved UI colour BEFORE panels/stylesheets are built
        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — {APP_VERSION}")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)
        self.setAcceptDrops(True)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None   # callable: () -> (url, key) | None
        self.on_interrupt      = None   # callable: () -> None — stop JARVIS mid-speech
        self.on_voice_change   = None   # callable: () -> None — rebuild session with new voice
        self.on_audio_device_change = None  # callable: () -> None — reopen audio streams
        self._confirm_overlay  = None   # live ConfirmBanner, if one is on screen
        self.get_plugins       = None   # callable: () -> list[dict], set by JarvisLive
        self.get_plugin_settings = None # callable: () -> list[dict] settings schemas, set by JarvisLive
        self.on_wake_toggle    = None   # callable: (enable: bool) -> str, set by JarvisLive
        self.on_wake_manual    = None   # callable: () -> None — manual sleep/wake
        self.on_push_to_talk   = None   # callable: (enable: bool) -> str scope
        self.ptt_hold          = None   # callable: (held: bool) -> None — windowed chord
        self.wake_get_state    = None   # callable: () -> dict {enabled, awake, ready}
        self._muted            = False
        self._current_file: str | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None
        self._protocol_overlay: ProtocolInfoOverlay | None = None
        self._flow_overlay: AutomationFlowOverlay | None = None

        sys._jarvis_ui_instance = self
        try:
            import plugins.telegram_bridge as _tb
            _tb.set_player(self)
        except Exception:
            pass

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        # Center column: HUD + resizable content panel via QSplitter
        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._quiz_panel = self._build_quiz_panel()

        # Live camera container — replaces HUD when camera stream is active
        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        self._video_split = False        # is the sound a separate stream?
        self._video_auto_muted = False   # did JARVIS close the mic, or the user?
        self._video_on = False
        self._video_cont = QWidget()
        self._video_cont.setStyleSheet(f"background: {C.BG};")
        _vid_v = QVBoxLayout(self._video_cont)
        _vid_v.setContentsMargins(8, 6, 8, 8)
        _vid_v.setSpacing(4)

        _vid_hdr = QHBoxLayout()
        self._video_title = QLabel("▶  VIDEO")
        self._video_title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._video_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _vid_hdr.addWidget(self._video_title)
        _vid_hdr.addStretch()

        def _vid_btn(text: str) -> QPushButton:
            b = QPushButton(text)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    color: {C.TEXT_DIM}; background: transparent;
                    border: none; padding: 2px 6px;
                }}
                QPushButton:hover {{ color: {C.PRI}; }}
            """)
            return b

        self._video_mute_btn = _vid_btn("🔇  SOUND OFF")
        self._video_mute_btn.clicked.connect(self._toggle_video_mute)
        _vid_hdr.addWidget(self._video_mute_btn)

        _vid_x = _vid_btn("✕  CLOSE")
        _vid_x.clicked.connect(self.stop_video)
        _vid_hdr.addWidget(_vid_x)
        _vid_v.addLayout(_vid_hdr)

        if HAVE_VIDEO:
            self._video_scene = QGraphicsScene(self)
            self._video_item = QGraphicsVideoItem()
            self._video_scene.addItem(self._video_item)
            self._video_widget = QGraphicsView(self._video_scene)
            self._video_widget.setStyleSheet("background: #000; border: none;")
            self._video_widget.setFrameShape(QGraphicsView.Shape.NoFrame)
            self._video_widget.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._video_widget.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
            )
            _vid_v.addWidget(self._video_widget, stretch=1)

            self._video_audio = QAudioOutput()
            self._video_audio.setMuted(True)
            self._video_player = QMediaPlayer()
            self._video_player.setVideoOutput(self._video_item)
            self._video_item.nativeSizeChanged.connect(self._fit_video)
            self._video_player.setAudioOutput(self._video_audio)
            self._video_player.errorOccurred.connect(self._on_video_error)

            self._video_sound = QMediaPlayer()
            self._video_sound_out = QAudioOutput()
            self._video_sound_out.setMuted(True)
            self._video_sound.setAudioOutput(self._video_sound_out)

            self._video_sync = QTimer(self)
            self._video_sync.setInterval(1000)
            self._video_sync.timeout.connect(self._sync_video_sound)
        else:
            self._video_scene = None
            self._video_item = None
            self._video_widget = None
            self._video_player = None
            self._video_audio = None
            self._video_sound = None
            self._video_sound_out = None
            self._video_sync = None
            _miss = QLabel("Video playback is not available in this Qt install.")
            _miss.setAlignment(Qt.AlignmentFlag.AlignCenter)
            _miss.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            _vid_v.addWidget(_miss, stretch=1)

        # Stack: 0 = animated HUD, 1 = live camera, 2 = video
        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)
        self._hud_cam_stack.addWidget(self._video_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.addWidget(self._quiz_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        # Quick-access drawer (floating overlay, built after central widget layout is done)
        self._quick_drawer = self._build_quick_drawer()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        # Metric update timer
        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._confirm_sig.connect(self._show_confirm_banner)
        self._confirm_hide_sig.connect(self._hide_confirm_banner)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._wake_dl_sig.connect(self._on_wake_install_done)
        self._quiz_sig.connect(self._show_quiz)
        self._quiz_hide_sig.connect(self._hide_quiz)
        self._review_sig.connect(self._show_review)
        self._flow_status_sig.connect(self._on_flow_status_changed)
        self._video_open_sig.connect(self._on_video_open)
        self._video_close_sig.connect(self._on_video_close)
        self._video_mute_sig.connect(self._on_video_mute)
        try:
            from core.flow_engine import FlowEngine
            FlowEngine.get_instance().add_status_listener(
                lambda fid, st: self._flow_status_sig.emit(fid, st)
            )
        except Exception as _fe_err:
            print(f"[MainWindow] FlowEngine status listener setup error: {_fe_err}")
        self._cam_stop = threading.Event()

        # Camera preview overlay (child of central widget, positioned in resizeEvent)
        self._cam_preview = _CameraPreview(self.centralWidget())

        # Clipboard panel (child of central widget, bottom-center)
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)
        sc_paste = QShortcut(QKeySequence.StandardKey.Paste, self)
        sc_paste.activated.connect(self._paste_from_clipboard)

    def _paste_from_clipboard(self) -> bool:
        """Pasting screenshot or image file directly from clipboard."""
        try:
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime.hasImage():
                qimg = clipboard.image()
                if not qimg.isNull():
                    save_dir = Path.home() / "Downloads"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    target = save_dir / f"screenshot_{int(time.time())}.png"
                    qimg.save(str(target), "PNG")
                    if hasattr(self, "_drop_zone") and self._drop_zone:
                        self._drop_zone._set_file(str(target))
                    self.write_log(f"SYS: 📸 Screenshot pasted from clipboard ({target.name})")
                    return True
            if mime.hasUrls():
                urls = mime.urls()
                if urls:
                    p = urls[0].toLocalFile()
                    if Path(p).is_file():
                        if hasattr(self, "_drop_zone") and self._drop_zone:
                            self._drop_zone._set_file(p)
                        self.write_log(f"SYS: 📁 File pasted from clipboard ({Path(p).name})")
                        return True
        except Exception as e:
            print(f"[MainWindow] _paste_from_clipboard error: {e}")
        return False

    def _show_camera_frame(self, img_bytes: bytes):
        """Slot — display camera preview overlay (main thread)."""
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    # --- Live camera stream in HUD area ------------------------------------
    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._hud_cam_stack.setCurrentIndex(1)
        else:
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            # Reuse camera index detected by screen_processor (cached in api_keys.json)
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            # warm-up frames
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    # ------------------------------------------------------------------
    # Icon generation — arc-reactor style, rendered with Pillow
    # ------------------------------------------------------------------
    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        """
        Render a JARVIS arc-reactor icon at 4× resolution and downsample
        for crisp results at all sizes. Saves a multi-res .ico to out_path.
        Returns True on success.
        """
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4                     # draw at 4× then downscale
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            # ── filled background circle ──────────────────────────────────
            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))

            # ── outer border ring ─────────────────────────────────────────
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)

            # ── mid decorative ring ───────────────────────────────────────
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            # ── 6 radial spokes (hex bolt) ────────────────────────────────
            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            # ── 6 tick marks on outer ring ────────────────────────────────
            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            # ── inner glowing ring ────────────────────────────────────────
            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            # ── bright glow soft blur applied before core ─────────────────
            # (draw a slightly larger cyan circle on a separate layer)
            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            # ── core dot ──────────────────────────────────────────────────
            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))

            # ── downscale to target size ──────────────────────────────────
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        """
        Create a Windows .lnk shortcut WITHOUT launching PowerShell or cmd.
        Tries win32com (pywin32) first; falls back to wscript.exe + VBScript.
        wscript.exe is a GUI-mode host — it never opens a console window.
        """
        # ── Option 1: pywin32 (pure Python COM, zero subprocess) ──────────
        try:
            from win32com.client import Dispatch   # type: ignore
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        # ── Option 2: wscript.exe + VBScript (always available on Windows,
        #    GUI-mode executable — never opens a console window) ────────────
        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        """
        Resolve the user's REAL desktop directory instead of assuming
        ~/Desktop, which breaks when:
          • OneDrive "Known Folder Move" relocates the desktop
            (C:/Users/x/OneDrive/Desktop) — very common on Win 10/11;
          • the XDG desktop is localized on Linux (~/Masaüstü,
            ~/Schreibtisch, ~/Bureau, …).
        Falls back to ~/Desktop only as a last resort.
        """
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            # ── 1) SHGetKnownFolderPath(FOLDERID_Desktop) — the canonical
            #       answer; follows OneDrive redirection. No dependencies. ──
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            # ── 2) Registry: User Shell Folders (may contain %VARS%) ──────
            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            # ── xdg-user-dir honours localized names (~/Masaüstü, …) ──────
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        # macOS: ~/Desktop is always the real path (localization is
        # display-only). Everything else lands here as a last resort.
        return home / "Desktop"

    def _create_desktop_shortcut(self):
        """
        Create a desktop shortcut on Windows / macOS / Linux.
        Never opens a terminal, console, or PowerShell window on any platform.
        """
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        # Arc-reactor icon (.ico — also exported as .png for Linux/macOS)
        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()

            # ── Windows ───────────────────────────────────────────────────────
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            # ── macOS — proper .app bundle (no Terminal window) ───────────────
            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                # Launcher executable (bash — runs as background process,
                # macOS does NOT open Terminal for executables inside .app bundles)
                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                # Minimal Info.plist (required for .app recognition)
                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                # Optional: copy icon as .icns (skip silently if Pillow is missing)
                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    # Inject icon reference into plist
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass  # icon is optional

            # ── Linux — .desktop file (Terminal=false, no console) ────────────
            else:
                # Export .ico → .png for better desktop integration
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path  # fallback to .ico

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            self._overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._remote_overlay and self._remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            self._remote_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if self._customize_overlay and self._customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            self._customize_overlay.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
        if getattr(self, "_protocol_overlay", None) and self._protocol_overlay.isVisible():
            self._centre_overlay(self._protocol_overlay)
        # Camera preview — bottom-right corner of the center/HUD area
        pw = _CameraPreview._W
        ph = self._cam_preview.height() or _CameraPreview._H
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )
        # Clipboard panel — bottom-center
        if hasattr(self, '_clipboard_panel') and self._clipboard_panel.isVisible():
            self._position_clipboard_panel()
        # Quick drawer — reposition if open
        if hasattr(self, '_quick_drawer') and self._quick_drawer.isVisible():
            self._position_quick_drawer()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        # CPU
        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        # MEM
        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        # NET
        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)  # 10 MB/s = %100
        self._bar_net.set_value(net_pct, net_str)

        # GPU
        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        # TMP
        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Courier New", 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_badge(APP_VERSION, C.PRI_DIM))
        lay.addSpacing(8)
        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(26, 26)
        self._drawer_btn.setFont(QFont("Courier New", 11))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont("Courier New", 17, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = ("A Friendly Assistant"
                     if _disp in ("JARVIS", "J.A.R.V.I.S")
                     else "Personal AI Assistant")
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        
        clk_row = QHBoxLayout(); clk_row.setSpacing(6)
        self._sleep_countdown_lbl = QLabel("")
        self._sleep_countdown_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._sleep_countdown_lbl.setStyleSheet("color: #ffaa00; background: rgba(255, 170, 0, 25); border: 1px solid #ffaa00; border-radius: 3px; padding: 1px 5px;")
        self._sleep_countdown_lbl.hide()
        clk_row.addWidget(self._sleep_countdown_lbl)

        self._clock_lbl = QPushButton("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
        self._clock_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clock_lbl.setToolTip("Click to toggle 12-hour / 24-hour clock format")
        self._clock_lbl.setStyleSheet(f"QPushButton {{ color: {C.PRI}; background: transparent; border: none; text-align: right; }} QPushButton:hover {{ color: {C.ACC2}; }}")
        self._clock_lbl.clicked.connect(self._toggle_clock_format)
        clk_row.addWidget(self._clock_lbl)
        right_col.addLayout(clk_row)

        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)

        # ── Header Mic Lock button (always visible) ──────────────────────────
        self._header_mic_btn = QPushButton("🎙")
        self._header_mic_btn.setFixedSize(28, 22)
        self._header_mic_btn.setFont(QFont("Courier New", 9))
        self._header_mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header_mic_btn.setToolTip("Mute / Unmute microphone")
        self._header_mic_btn.setCheckable(True)
        self._header_mic_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{
                background: #200000; color: #ff4466;
                border: 1px solid #ff4466;
            }}
        """)
        self._header_mic_btn.clicked.connect(self._on_header_mic_clicked)
        right_col.addWidget(self._header_mic_btn, 0, Qt.AlignmentFlag.AlignRight)

        lay.addLayout(right_col)
        return w


    def _toggle_clock_format(self):
        try:
            from memory.config_manager import get_time_format_12h, save_time_format_12h
            cur = get_time_format_12h()
            save_time_format_12h(not cur)
            self._tick_clock()
        except Exception:
            pass

    def update_sleep_countdown(self, seconds_remaining: int | None):
        """Update or hide sleep countdown badge in top bar."""
        if not hasattr(self, "_sleep_countdown_lbl"):
            return
        if seconds_remaining is not None and seconds_remaining > 0:
            m = seconds_remaining // 60
            s = seconds_remaining % 60
            self._sleep_countdown_lbl.setText(f"😴 in {m}:{s:02d}")
            self._sleep_countdown_lbl.show()
        else:
            self._sleep_countdown_lbl.hide()

    def _tick_clock(self):
        fmt = "%H:%M:%S"
        try:
            from memory.config_manager import get_time_format_12h
            if get_time_format_12h():
                fmt = "%I:%M:%S %p"
        except Exception:
            pass
        self._clock_lbl.setText(time.strftime(fmt))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))
        if int(time.time()) % 5 == 0 and hasattr(self, "_update_stats_display"):
            self._update_stats_display()

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff6688")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 5, 6, 5)
        ip_lay.setSpacing(3)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Courier New", 8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Courier New", 8))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(4)

        lay.addStretch()

        def _btn_style(col: str) -> str:
            return _status_btn_style(col)

        self._ai_core_btn = QPushButton("AI CORE\nACTIVE")
        self._ai_core_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._ai_core_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ai_core_btn.setStyleSheet(_btn_style(C.GREEN))
        self._ai_core_btn.setToolTip("AI Core Status — Click to Toggle Sleep/Wake or Check State")
        self._ai_core_btn.clicked.connect(self._on_ai_core_clicked)
        lay.addWidget(self._ai_core_btn)

        self._sec_btn = QPushButton("SEC\nCLEARED")
        self._sec_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._sec_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sec_btn.setStyleSheet(_btn_style(C.PRI))
        self._sec_btn.setToolTip("Security Protocols — Click to Manage Plugins & Permissions")
        self._sec_btn.clicked.connect(self._on_sec_clicked)
        lay.addWidget(self._sec_btn)

        self._protocol_btn = QPushButton("PROTOCOL\n" + APP_PROTOCOL)
        self._protocol_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._protocol_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._protocol_btn.setStyleSheet(_btn_style(C.TEXT_DIM))
        self._protocol_btn.setToolTip(f"Protocol {APP_PROTOCOL} — Click for System Diagnostics & Shortcuts")
        self._protocol_btn.clicked.connect(self._on_protocol_clicked)
        lay.addWidget(self._protocol_btn)

        return w
    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("✋  INTERRUPT  [ESC]")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: #200010; border: 1px solid #ff6688;
            }}
            QPushButton:pressed {{
                background: #300018;
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        # ── Always-On Mic toggle button ──────────────────────────────────────
        self._always_on_btn = QPushButton("🎤  MIC: ALWAYS ON")
        self._always_on_btn.setFixedHeight(28)
        self._always_on_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._always_on_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._always_on_btn.setCheckable(True)
        self._always_on_btn.setToolTip(
            "Always-On: mic stays open continuously — no Push-to-Talk, no Wake Word needed.\n"
            "Click to toggle between Always-On and normal mode."
        )
        self._always_on_btn.clicked.connect(self._toggle_always_on_mic)
        self._style_always_on_btn(False)
        lay.addWidget(self._always_on_btn)

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)


        return w

    def _build_quick_drawer(self) -> QWidget:
        """Floating overlay panel shown when the ⚙ header button is toggled."""
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(_BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(QFont("Courier New", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(QFont("Courier New", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(QFont("Courier New", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        self._brief_btn = QPushButton()
        self._brief_btn.setFixedHeight(26)
        self._brief_btn.setFont(QFont("Courier New", 7))
        self._brief_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        # ── Wake word ──────────────────────────────────────────────────────────
        self._wake_btn = QPushButton()
        self._wake_btn.setFixedHeight(26)
        self._wake_btn.setFont(QFont("Courier New", 7))
        self._wake_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wake_btn.clicked.connect(self._toggle_wake_word)
        lay.addWidget(self._wake_btn)

        self._wake_sleep_btn = QPushButton()
        self._wake_sleep_btn.setFixedHeight(26)
        self._wake_sleep_btn.setFont(QFont("Courier New", 7))
        self._wake_sleep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._wake_sleep_btn.clicked.connect(self._tap_wake_manual)
        lay.addWidget(self._wake_sleep_btn)
        # Neutral placeholder now; the real state (which may load the model to
        # check readiness) is resolved lazily the first time the drawer opens.
        self._wake_btn.setText("🎙  WAKE WORD")
        self._wake_btn.setStyleSheet(_BTN_STYLE_DIM)
        self._wake_sleep_btn.hide()

        self._ptt_btn = QPushButton()
        self._ptt_btn.setFixedHeight(26)
        self._ptt_btn.setFont(QFont("Courier New", 7))
        self._ptt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ptt_btn.clicked.connect(self._toggle_ptt)
        lay.addWidget(self._ptt_btn)

        self._refresh_talk_btns()

        self._hud_btn = QPushButton()
        self._hud_btn.setFixedHeight(26)
        self._hud_btn.setFont(QFont("Courier New", 7))
        self._hud_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hud_btn.clicked.connect(self._toggle_hud_style)
        lay.addWidget(self._hud_btn)
        self._refresh_hud_btn()

        audio_btn = QPushButton("🎧  AUDIO DEVICES")
        audio_btn.setFixedHeight(26)
        audio_btn.setFont(QFont("Courier New", 7))
        audio_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        audio_btn.setStyleSheet(_BTN_STYLE_DIM)
        audio_btn.clicked.connect(self._open_audio_devices)
        lay.addWidget(audio_btn)

        mem_btn = QPushButton("🧠  MEMORY")
        mem_btn.setFixedHeight(26)
        mem_btn.setFont(QFont("Courier New", 7))
        mem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        mem_btn.setStyleSheet(_BTN_STYLE_DIM)
        mem_btn.clicked.connect(self._open_memory_panel)
        lay.addWidget(mem_btn)

        plugin_btn = QPushButton("🧩  PLUGINS")
        plugin_btn.setFixedHeight(26)
        plugin_btn.setFont(QFont("Courier New", 7))
        plugin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        plugin_btn.setStyleSheet(_BTN_STYLE_DIM)
        plugin_btn.clicked.connect(self._open_plugin_manager)
        lay.addWidget(plugin_btn)

        settings_btn = QPushButton("⚙  PLUGIN SETTINGS")
        settings_btn.setFixedHeight(26)
        settings_btn.setFont(QFont("Courier New", 7))
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.setStyleSheet(_BTN_STYLE_DIM)
        settings_btn.clicked.connect(self._open_plugin_settings)
        lay.addWidget(settings_btn)

        shortcuts_btn = QPushButton("⚡  SHORTCUTS")
        shortcuts_btn.setFixedHeight(26)
        shortcuts_btn.setFont(QFont("Courier New", 7))
        shortcuts_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        shortcuts_btn.setStyleSheet(_BTN_STYLE_DIM)
        shortcuts_btn.clicked.connect(self._open_shortcuts_manager)
        lay.addWidget(shortcuts_btn)

        w.adjustSize()
        return w

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._refresh_wake_btns()   # resolve wake state on open (lazy)
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        _W = 220
        self._quick_drawer.setFixedWidth(_W)
        self._quick_drawer.adjustSize()
        self._quick_drawer.setGeometry(12, 54, _W, self._quick_drawer.sizeHint().height())

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = _CommandInput(self)
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        """
        Collapsible panel below the HUD — shows search results, news, briefings.
        Hidden by default; appears when show_content() is called.
        """
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        # ── header row ───────────────────────────────────────────────────────
        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        # ── separator ─────────────────────────────────────────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        # ── text display ──────────────────────────────────────────────────────
        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        """Slot — runs on Qt main thread. Updates and shows the content panel."""
        import time as _time
        # The panel opens below the head, so the head looks down at it. It is a
        # tiny thing that answers "did that land?" before you read a word.
        self.hud.glance(0.0, -0.85, hold=1.3)
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    # ── document review ──────────────────────────────────────────────────────
    # Rendered as rich text into the content panel that already exists, rather
    # than into a panel of its own. A review is read, not clicked, so QTextEdit
    # gives scrolling, selection and copy for nothing, and the HUD gains no
    # widget it has to lay out. Severity decides colour and order here because
    # that is presentation; the plugin supplies no styling and knows no palette,
    # which is also what lets a re-theme repaint a review correctly.

    # Severity is marked by a symbol and a colour, not by a word. The findings
    # themselves are in the user's language, and "[SERIOUS]" sitting inside a
    # Turkish sentence is the kind of seam this project tries not to have —
    # while translating the tag would mean a table per language, which is worse.
    # A shape carries it in every language, and shape plus colour still reads
    # for someone who cannot separate red from amber. What the marks mean
    # arrives the way everything else does: JARVIS says it out loud.
    _REVIEW_MARKS = {"serious": ("RED", "▲"), "caution": ("ACC2", "●"), "note": ("PRI_DIM", "·")}

    @staticmethod
    def _esc(s) -> str:
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\n", "<br>"))

    def _show_review(self, title: str, summary: str, findings, unclear):
        """Slot — Qt main thread. Lays a document review into the content panel."""
        e = self._esc
        parts = [f'<div style="color:{C.TEXT}; font-family:Courier New;">']

        if summary:
            parts.append(
                f'<div style="color:{C.WHITE}; border-left:2px solid {C.PRI};'
                f' padding-left:8px; margin-bottom:10px;">{e(summary)}</div>')

        for f in (findings or []):
            key, mark = self._REVIEW_MARKS.get(f.get("severity"), ("PRI_DIM", "·"))
            colour = getattr(C, key)
            parts.append(f'<div style="margin-bottom:11px;">')
            parts.append(
                f'<span style="color:{colour}; font-weight:bold;">{mark}</span> '
                f'<span style="color:{C.WHITE}; font-weight:bold;">'
                f'{e(f.get("heading"))}</span>')
            if f.get("detail"):
                parts.append(f'<div style="margin-left:12px;">{e(f["detail"])}</div>')
            if f.get("quote"):
                # The document's own wording, visually separated from the
                # explanation so the two are never mistaken for each other.
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_DIM};'
                    f' border-left:1px solid {C.BORDER}; padding-left:7px;">'
                    f'&ldquo;{e(f["quote"])}&rdquo;</div>')
            if f.get("suggestion"):
                parts.append(
                    f'<div style="margin-left:12px; color:{C.PRI};">'
                    f'&rarr; {e(f["suggestion"])}</div>')
            parts.append('</div>')

        if unclear:
            parts.append(
                f'<div style="margin-top:6px; border-top:1px solid {C.BORDER};'
                f' padding-top:7px; color:{C.TEXT_MED};">'
                'The document does not settle:</div>')
            for u in unclear:
                parts.append(
                    f'<div style="margin-left:12px; color:{C.TEXT_MED};">'
                    f'&middot; {e(u)}</div>')
        parts.append('</div>')

        import time as _time
        self.hud.glance(0.0, -0.85, hold=1.3)
        # Left as written, not upper-cased. The other content-panel titles are
        # the app's own English labels, but this one is the document's name in
        # the user's language, and str.upper() applies English casing rules to
        # it: Turkish "Sözleşmesi" comes back "SÖZLEŞMESI", having lost the
        # dotted capital İ. Python has no locale-aware upper to reach for, and
        # imposing one language's rules on all of them is the bug, not the fix.
        self._content_title_lbl.setText((title or "Document")[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setHtml("".join(parts))
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start)
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 260, 120), 260, 0])

    # ── quiz panel ───────────────────────────────────────────────────────────
    # An interactive twin of the content panel. The plugin only ever hands over
    # questions; everything about asking, marking and reporting happens here,
    # and the finished result is pushed back into the conversation the same way
    # a dropped file is — as a message JARVIS reads and responds to. That keeps
    # the tool call short (it returns the moment the board is up) and leaves the
    # talking to the assistant, in the user's own language.

    def _quiz_btn(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        b.setFont(QFont("Courier New", 8))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumHeight(24)
        edge = C.BORDER_B if primary else C.BORDER
        col = C.PRI if primary else C.TEXT_MED
        b.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {col};
                border: 1px solid {edge}; border-radius: 2px;
                padding: 3px 9px; text-align: left;
            }}
            QPushButton:hover {{ color: {C.WHITE}; border-color: {C.PRI_DIM}; }}
            QPushButton:disabled {{ color: {C.TEXT_DIM}; border-color: {C.BORDER}; }}
        """)
        return b

    def _build_quiz_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("QuizPanel")
        w.setStyleSheet(f"""
            QWidget#QuizPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(6)
        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._quiz_title_lbl = QLabel("QUIZ")
        self._quiz_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._quiz_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;")
        hdr.addWidget(self._quiz_title_lbl)
        hdr.addStretch()

        self._quiz_count_lbl = QLabel("")
        self._quiz_count_lbl.setFont(QFont("Courier New", 7))
        self._quiz_count_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._quiz_count_lbl)

        quit_btn = QPushButton("DISMISS  ✕")
        quit_btn.setFont(QFont("Courier New", 7))
        quit_btn.setFixedHeight(18)
        quit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        quit_btn.clicked.connect(self._hide_quiz)
        hdr.addWidget(quit_btn)
        lay.addLayout(hdr)

        rule = QFrame(); rule.setFixedHeight(1)
        rule.setStyleSheet(f"background: {C.BORDER};")
        lay.addWidget(rule)

        self._quiz_q_lbl = QLabel("")
        self._quiz_q_lbl.setWordWrap(True)
        self._quiz_q_lbl.setFont(QFont("Courier New", 9))
        self._quiz_q_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        lay.addWidget(self._quiz_q_lbl)

        self._quiz_answers = QWidget()
        self._quiz_answers.setStyleSheet("background: transparent;")
        self._quiz_answers_lay = QVBoxLayout(self._quiz_answers)
        self._quiz_answers_lay.setContentsMargins(0, 2, 0, 0)
        self._quiz_answers_lay.setSpacing(4)
        lay.addWidget(self._quiz_answers)

        self._quiz_note_lbl = QLabel("")
        self._quiz_note_lbl.setWordWrap(True)
        self._quiz_note_lbl.setFont(QFont("Courier New", 8))
        self._quiz_note_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._quiz_note_lbl.hide()
        lay.addWidget(self._quiz_note_lbl)

        foot = QHBoxLayout()
        foot.addStretch()
        self._quiz_next_btn = self._quiz_btn("NEXT  →", primary=True)
        self._quiz_next_btn.setFixedWidth(110)
        self._quiz_next_btn.clicked.connect(self._quiz_next)
        self._quiz_next_btn.hide()
        foot.addWidget(self._quiz_next_btn)
        lay.addLayout(foot)

        self._quiz = None
        return w

    def _show_quiz(self, topic: str, questions, grader=None):
        """Slot — Qt main thread. Puts a fresh quiz on the board."""
        if not questions:
            return
        self._quiz = {
            "topic": topic or "",
            "questions": list(questions),
            "grader": grader,
            "i": 0,
            "results": [],
            "answered": False,
        }
        self._quiz_title_lbl.setText((topic or "quiz").upper()[:48])
        self.hud.glance(0.0, -0.85, hold=1.3)
        first_show = not self._quiz_panel.isVisible()
        self._quiz_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 250, 120), 0, 250])
        self._quiz_render()

    def _hide_quiz(self):
        self._quiz = None
        self._quiz_panel.hide()

    def _quiz_clear_answers(self):
        while self._quiz_answers_lay.count():
            item = self._quiz_answers_lay.takeAt(0)
            child = item.widget()
            if child is not None:
                child.setParent(None)
                child.deleteLater()

    def _quiz_render(self):
        q = self._quiz["questions"][self._quiz["i"]]
        n, total = self._quiz["i"] + 1, len(self._quiz["questions"])
        self._quiz_count_lbl.setText(f"{n} / {total}")
        self._quiz_q_lbl.setText(q.get("question", ""))
        self._quiz_note_lbl.hide()
        self._quiz_next_btn.hide()
        self._quiz["answered"] = False
        self._quiz_clear_answers()

        opts = q.get("options") or []
        if opts:
            for text in opts:
                b = self._quiz_btn("   " + text)
                b.clicked.connect(lambda _=False, t=text: self._quiz_submit(t))
                self._quiz_answers_lay.addWidget(b)
        else:
            row = QWidget(); row.setStyleSheet("background: transparent;")
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            field = QLineEdit()
            field.setFont(QFont("Courier New", 9))
            field.setPlaceholderText("your answer")
            field.setStyleSheet(f"""
                QLineEdit {{
                    background: {C.PANEL2}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 2px; padding: 4px 7px;
                }}
                QLineEdit:focus {{ border-color: {C.PRI_DIM}; }}
            """)
            send = self._quiz_btn("ANSWER", primary=True)
            send.setFixedWidth(90)
            field.returnPressed.connect(lambda: self._quiz_submit(field.text()))
            send.clicked.connect(lambda: self._quiz_submit(field.text()))
            h.addWidget(field, stretch=1)
            h.addWidget(send)
            self._quiz_answers_lay.addWidget(row)
            field.setFocus()

    def _quiz_submit(self, given: str):
        if self._quiz is None or self._quiz["answered"]:
            return
        self._quiz["answered"] = True
        q = self._quiz["questions"][self._quiz["i"]]
        grader = self._quiz.get("grader")
        verdict = None
        if callable(grader):
            try:
                verdict = grader(q, given)
            except Exception:
                verdict = None
        self._quiz["results"].append({
            "question": q.get("question", ""),
            "type": q.get("type", ""),
            "given": str(given or "").strip(),
            "answer": q.get("answer", ""),
            "correct": verdict,
        })

        for i in range(self._quiz_answers_lay.count()):
            wdg = self._quiz_answers_lay.itemAt(i).widget()
            if wdg is not None:
                wdg.setEnabled(False)

        if verdict is True:
            mark, colour = "✓  correct", C.GREEN
        elif verdict is False:
            mark, colour = "✕  " + str(q.get("answer", "")), C.RED
        else:
            # Open answers and near-miss gap-fills are JARVIS's to judge. Saying
            # so is honest; marking it wrong here would be a guess.
            mark, colour = "…  noted — I'll go over this one with you", C.ACC2
        note = q.get("note") or ""
        self._quiz_note_lbl.setText(mark + (("\n" + note) if note else ""))
        self._quiz_note_lbl.setStyleSheet(f"color: {colour}; background: transparent;")
        self._quiz_note_lbl.show()

        last = self._quiz["i"] >= len(self._quiz["questions"]) - 1
        self._quiz_next_btn.setText("FINISH  →" if last else "NEXT  →")
        self._quiz_next_btn.show()
        self._quiz_next_btn.setFocus()

    def _quiz_next(self):
        if self._quiz is None:
            return
        if self._quiz["i"] >= len(self._quiz["questions"]) - 1:
            self._quiz_finish()
        else:
            self._quiz["i"] += 1
            self._quiz_render()

    def _quiz_finish(self):
        if self._quiz is None:
            return
        topic = self._quiz["topic"]
        results = self._quiz["results"]
        right = sum(1 for r in results if r["correct"] is True)
        unsure = sum(1 for r in results if r["correct"] is None)
        total = len(results)
        self._quiz_panel.hide()
        self._quiz = None

        self._log.append_log(f"QUIZ: {topic or 'quiz'} — {right}/{total} correct")

        # Hand it back to JARVIS as a message, not as a tool return: the tool
        # call ended minutes ago. This is the same channel a dropped file uses.
        lines = [f"[QUIZ_DONE] topic={topic or 'general'} | "
                 f"auto-marked {right}/{total} correct"
                 + (f", {unsure} still need your marking" if unsure else "")]
        for i, r in enumerate(results, 1):
            state = ("correct" if r["correct"] is True
                     else "wrong" if r["correct"] is False else "NEEDS MARKING")
            lines.append(
                f"{i}. [{r['type']}] {r['question']} | they answered: "
                f"{r['given'] or '(blank)'} | expected: {r['answer']} | {state}")
        lines.append(
            "Mark every question flagged NEEDS MARKING yourself — accept an answer "
            "that means the same thing. Then tell them how they did in their own "
            "language: the score, what they got wrong and why, in a couple of "
            "sentences. Offer another round only if it fits. "
            "Remember something only if it would still matter next week — that they "
            "are working through a subject, or keep missing the same thing. A score "
            "from one session is not worth a memory, and a memory per quiz would "
            "bury the things that are.")
        msg = "\n".join(lines)
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addStretch()

        # Stats label
        self._stats_lbl = QLabel("")
        self._stats_lbl.setFont(QFont("Courier New", 7))
        self._stats_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        lay.addWidget(self._stats_lbl)
        self._update_stats_display()

        lay.addSpacing(8)

        # Skills button
        skills_btn = QPushButton("⚡ SKILLS")
        skills_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        skills_btn.setFixedHeight(18)
        skills_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        skills_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.PRI}; border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """)
        skills_btn.clicked.connect(self._open_skills_overlay)
        lay.addWidget(skills_btn)

        lay.addSpacing(6)

        # Automation Flow Map button
        self._flow_btn = QPushButton("🔀 FLOW MAP")
        self._flow_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._flow_btn.setFixedHeight(18)
        self._flow_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._flow_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {C.ACC2}; border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px; }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.ACC2}; }}
        """)
        self._flow_btn.clicked.connect(self._open_flow_overlay)
        lay.addWidget(self._flow_btn)

        lay.addSpacing(10)
        lay.addWidget(_fl("JARVIS MK", C.PRI_DIM))
        return w

    def _update_stats_display(self):
        try:
            from memory.usage_stats import get_stats_summary
            if hasattr(self, "_stats_lbl"):
                self._stats_lbl.setText(get_stats_summary())
        except Exception:
            pass

    def _open_skills_overlay(self):
        cw = self.centralWidget()
        if hasattr(self, "_skill_overlay") and self._skill_overlay:
            if self._skill_overlay.isVisible():
                self._skill_overlay.hide()
                return
            self._skill_overlay.refresh()
            self._skill_overlay.show()
            self._skill_overlay.raise_()
            return
        self._skill_overlay = SkillManagerOverlay(parent=cw)
        ow, oh = SkillManagerOverlay._OW, SkillManagerOverlay._OH
        oh = min(oh, cw.height() - 20)
        self._skill_overlay.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh
        )
        self._skill_overlay.show()
        self._skill_overlay.raise_()

    def _open_flow_overlay(self):
        cw = self.centralWidget()
        if hasattr(self, "_flow_overlay") and self._flow_overlay:
            if self._flow_overlay.isVisible():
                self._flow_overlay.hide()
                return
            self._flow_overlay.refresh()
            self._flow_overlay.show()
            self._flow_overlay.raise_()
            return
        self._flow_overlay = AutomationFlowOverlay(parent=cw, player=self)
        ow, oh = AutomationFlowOverlay._OW, AutomationFlowOverlay._OH
        oh = min(oh, cw.height() - 20)
        self._flow_overlay.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh
        )
        self._flow_overlay.show()
        self._flow_overlay.raise_()

    def _on_flow_status_changed(self, flow_id: str, status: str):
        try:
            from core.flow_engine import FlowEngine
            engine = FlowEngine.get_instance()
            running = [f for f in engine.list_flows() if f.status == "RUNNING" or engine.is_running(f.id)]
            if status == "RUNNING" and not any(f.id == flow_id for f in running):
                fl = engine.get_flow(flow_id)
                if fl:
                    running.append(fl)
            elif status in ("STOPPED", "IDLE"):
                running = [f for f in running if f.id != flow_id]

            if hasattr(self, "_flow_btn"):
                if running:
                    self._flow_btn.setText(f"🔀 FLOW MAP ({len(running)} ACTIVE)")
                    self._flow_btn.setStyleSheet(f"""
                        QPushButton {{ background: #002b18; color: {C.GREEN}; border: 1px solid {C.GREEN}; border-radius: 2px; padding: 0 5px; }}
                        QPushButton:hover {{ background: #004024; border-color: {C.GREEN}; }}
                    """)
                else:
                    self._flow_btn.setText("🔀 FLOW MAP")
                    self._flow_btn.setStyleSheet(f"""
                        QPushButton {{ background: transparent; color: {C.ACC2}; border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px; }}
                        QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.ACC2}; }}
                    """)
            if hasattr(self, "_flow_overlay") and self._flow_overlay and self._flow_overlay.isVisible():
                self._flow_overlay.refresh()
        except Exception as e:
            print(f"[MainWindow] Flow status update error: {e}")

    def write_log(self, text: str):
        """Thread-safe logging bridge for actions, background workers, and FlowEngine."""
        self._log_sig.emit(str(text))

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e: QDropEvent):
        urls = e.mimeData().urls()
        if urls:
            local_path = urls[0].toLocalFile()
            if local_path and os.path.exists(local_path):
                sys._jarvis_last_uploaded_file = local_path
                self._on_file_selected(local_path)
                e.acceptProposedAction()

    def _on_file_selected(self, path: str):
        self._current_file = path
        sys._jarvis_last_uploaded_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")
        if self.on_text_command:
            msg = (
                f"[FILE_UPLOADED] path={path} | name={p.name} | "
                f"type={p.suffix.lstrip('.')} | size={size} | "
                f"Briefly tell the user you can see the file '{p.name}' "
                f"({size}) has been uploaded and ask what they'd like to do with it."
            )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url       = result[0]
        key       = result[1]
        auto      = result[2] if len(result) >= 3 else ""
        manual    = result[3] if len(result) >= 4 else url
        ts_url    = result[4] if len(result) >= 5 else ""
        ts_auto   = result[5] if len(result) >= 6 else ""
        ts_manual = result[6] if len(result) >= 7 else ""
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(
            url, key, auto_login_url=auto, manual_url=manual,
            tailscale_url=ts_url, tailscale_auto=ts_auto, tailscale_manual=ts_manual,
            expiry_secs=600, parent=cw
        )
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    # ── Auto-start ──────────────────────────────────────────────────────────────

    def _check_autostart(self) -> bool:
        """Returns True if auto-start is currently registered on this OS."""
        try:
            if _OS == "Windows":
                # Check Startup folder shortcut
                startup_folder = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                if (startup_folder / "JARVIS_AI.lnk").exists():
                    return True
                # Also check Registry Run key
                import winreg
                try:
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                    try:
                        winreg.QueryValueEx(key, "JARVIS_AI")
                        return True
                    except FileNotFoundError:
                        return False
                    finally:
                        winreg.CloseKey(key)
                except Exception:
                    return False
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            root_dir = str(Path(__file__).resolve().parent)
            if _OS == "Windows":
                startup_folder = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
                lnk_file = startup_folder / "JARVIS_AI.lnk"
                pythonw = Path(sys.executable).parent / "pythonw.exe"
                exe = str(pythonw if pythonw.exists() else sys.executable)

                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    try:
                        winreg.DeleteValue(reg, "JARVIS_AI")
                    except Exception:
                        pass
                    if lnk_file.exists():
                        try:
                            lnk_file.unlink()
                        except Exception:
                            pass
                else:
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ, f'"{exe}" "{script}"')
                    # Create Windows Startup folder shortcut with working directory
                    try:
                        ps_cmd = (
                            f'$ws = New-Object -ComObject WScript.Shell; '
                            f'$s = $ws.CreateShortcut("{lnk_file}"); '
                            f'$s.TargetPath = "{exe}"; '
                            f'$s.Arguments = "`"{script}`""; '
                            f'$s.WorkingDirectory = "{root_dir}"; '
                            f'$s.Description = "JARVIS Mark LIV Voice Assistant"; '
                            f'$s.Save()'
                        )
                        subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                                       capture_output=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    except Exception as e:
                        print(f"[Autostart] Lnk creation error: {e}")
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    # ── Wake word settings ───────────────────────────────────────────────────

    def _wake_state(self) -> dict:
        """Combined state for the two wake-word buttons. Readiness is a cheap,
        deterministic on-disk check now (see core.wake_word.is_ready), so there
        is nothing to cache — the button never flickers to a stale value."""
        if self.wake_get_state:
            try:
                s = self.wake_get_state()
                return {"ready": bool(s.get("ready")),
                        "enabled": bool(s.get("enabled")),
                        "awake": bool(s.get("awake"))}
            except Exception:
                pass
        # Before JarvisLive has wired its callback (drawer built at startup).
        ready, enabled = False, False
        try:
            from core.wake_word import is_ready
            from memory.config_manager import get_wake_word_enabled
            ready, enabled = is_ready(), get_wake_word_enabled()
        except Exception:
            pass
        return {"ready": ready, "enabled": enabled, "awake": True}

    def _refresh_wake_btns(self):
        if not hasattr(self, '_wake_btn'):
            return
        st = self._wake_state()
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""
        self._wake_btn.setEnabled(True)
        if not st["ready"]:
            self._wake_btn.setText("⬇  WAKE WORD: DOWNLOAD")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        elif st["enabled"]:
            self._wake_btn.setText("🎙  WAKE WORD: ON")
            self._wake_btn.setStyleSheet(_on)
            self._wake_sleep_btn.show()
            self._wake_sleep_btn.setText("😴  SLEEP NOW" if st["awake"] else "👂  WAKE NOW")
            self._wake_sleep_btn.setStyleSheet(_off)
        else:
            self._wake_btn.setText("🎙  WAKE WORD: OFF")
            self._wake_btn.setStyleSheet(_off)
            self._wake_sleep_btn.hide()
        self._update_ai_core_btn(st.get("awake", True))

    def _refresh_talk_btns(self):
        """Repaint the push-to-talk row from the saved setting."""
        if not hasattr(self, "_ptt_btn"):
            return
        from core.hotkey import chord_label
        from memory.config_manager import get_push_to_talk_enabled
        _on = f"""
            QPushButton {{ background: #001a08; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ background: #002010; }}"""
        _off = f"""
            QPushButton {{ background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}"""

        ptt = get_push_to_talk_enabled()
        self._ptt_btn.setText(f"🎚  PUSH-TO-TALK: {chord_label()}" if ptt
                              else "🎚  PUSH-TO-TALK: OFF")
        self._ptt_btn.setStyleSheet(_on if ptt else _off)
        self._ptt_btn.setToolTip(
            "Microphone stays closed until you hold the key — nothing is sent "
            "while you are not holding it." if ptt
            else "Hold a key to talk instead of streaming the mic continuously.")


    def _refresh_hud_btn(self):
        from memory.config_manager import get_hud_style
        face = get_hud_style() == "face"
        # Neither state is "off", so both read as active — this is a choice
        # between two things, not a switch with a disabled side.
        style = f"""
            QPushButton {{ background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.BORDER_A}; border-radius: 3px;
                text-align: left; padding: 0 8px; }}
            QPushButton:hover {{ color: {C.WHITE}; border: 1px solid {C.BORDER_B}; }}"""
        self._hud_btn.setText("🧑  HUD: ANIMATED FACE" if face
                              else "◉  HUD: REACTOR CORE")
        self._hud_btn.setStyleSheet(style)
        self._hud_btn.setToolTip(
            "An animated head that speaks your words and shows what JARVIS is "
            "doing. Tap to switch to the reactor core."
            if face else
            "A reactor core that turns with the state and moves with your voice. "
            "Tap to switch to the animated head.")

    def _toggle_hud_style(self):
        """Swap the centrepiece. Both objects stay in memory, so the change is
        instant and switching back costs nothing."""
        from memory.config_manager import get_hud_style, save_hud_style
        want = "core" if get_hud_style() == "face" else "face"
        save_hud_style(want)
        try:
            self.hud.hud_style = want
            self.hud.update()
        except Exception:
            pass
        self._refresh_hud_btn()
        self._log.append_log(
            "SYS: HUD switched to the animated face." if want == "face"
            else "SYS: HUD switched to the reactor core.")

    def _toggle_ptt(self):
        from memory.config_manager import (get_push_to_talk_enabled,
                                           save_push_to_talk_enabled)
        want = not get_push_to_talk_enabled()
        save_push_to_talk_enabled(want)
        scope = None
        if self.on_push_to_talk:
            try:
                scope = self.on_push_to_talk(want)
            except Exception as e:
                self._log.append_log(f"ERR: Push-to-talk failed — {e}")
                save_push_to_talk_enabled(False)
                want = False
        self._apply_ptt_shortcut(want and scope != "global")
        self._refresh_talk_btns()

    def _apply_ptt_shortcut(self, needed: bool):
        """Bind the chord inside the window when no global hook is available.

        On macOS and Linux there is no dependency-free way to read global key
        state, so the chord is at least live whenever this window has focus.
        Qt gives no key-release for a QShortcut, so a press latches the mic open
        and a short timer closes it; held down, auto-repeat keeps pushing that
        timer out, which behaves like holding a key.
        """
        from PyQt6.QtGui import QKeySequence, QShortcut
        from core.hotkey import qt_sequence

        if not needed:
            sc = getattr(self, "_ptt_sc", None)
            if sc is not None:
                sc.setEnabled(False)
                self._ptt_sc = None
            self._ptt_hold(False)
            return
        if getattr(self, "_ptt_sc", None) is not None:
            return

        self._ptt_release = QTimer(self)
        self._ptt_release.setSingleShot(True)
        self._ptt_release.setInterval(420)
        self._ptt_release.timeout.connect(lambda: self._ptt_hold(False))

        def _press():
            self._ptt_hold(True)
            self._ptt_release.start()

        self._ptt_sc = QShortcut(QKeySequence(qt_sequence()), self)
        self._ptt_sc.setAutoRepeat(True)
        self._ptt_sc.activated.connect(_press)

    def _ptt_hold(self, held: bool):
        """Report a windowed press/release to whoever owns the microphone."""
        cb = getattr(self, "ptt_hold", None)
        if cb:
            try:
                cb(bool(held))
            except Exception:
                pass

    def _toggle_wake_word(self):
        st = self._wake_state()
        if not st["ready"]:
            # First time: download openwakeword + model in a worker thread.
            self._wake_btn.setText("⬇  DOWNLOADING… (one-time)")
            self._wake_btn.setEnabled(False)
            def _work():
                try:
                    from core.wake_word import install_and_download
                    ok, msg = install_and_download(
                        logger=lambda m: self._log_sig.emit(f"SYS: {m}"))
                except Exception as e:
                    ok, msg = False, str(e)
                if ok and self.on_wake_toggle:
                    try:
                        self.on_wake_toggle(True)   # auto-enable after a successful download
                    except Exception:
                        pass
                self._wake_dl_sig.emit(ok, msg)
            threading.Thread(target=_work, daemon=True).start()
            return
        # Already downloaded → just flip enabled/disabled through JarvisLive.
        if self.on_wake_toggle:
            try:
                self.on_wake_toggle(not st["enabled"])
            except Exception:
                pass
        self._refresh_wake_btns()

    def _on_wake_install_done(self, ok: bool, msg: str):
        self._log_sig.emit(f"SYS: {'Wake word ready.' if ok else 'Wake word setup failed: ' + msg}")
        self._refresh_wake_btns()

    def _tap_wake_manual(self):
        if self.on_wake_manual:
            try:
                self.on_wake_manual()
            except Exception:
                pass
        self._refresh_wake_btns()

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    # ── Customization ────────────────────────────────────────────────────────────

    def _open_customize(self):
        try:
            cfg = _read_full_config()
            if self._customize_overlay:
                try:
                    self._customize_overlay.hide()
                    self._customize_overlay.deleteLater()
                except Exception:
                    pass
                self._customize_overlay = None
            cw = self.centralWidget()
            ov = CustomizeOverlay(
                cfg.get("assistant_name", "JARVIS") or "JARVIS",
                cfg.get("user_name", ""),
                cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
                cfg.get("voice_name", ""),
                parent=cw,
            )
            ov.set_player(self)
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            oh = min(oh, cw.height() - 16)
            ov.setGeometry(
                (cw.width()  - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )
            ov.on_preview = self._preview_ui_color
            ov.saved.connect(self._apply_name_update)
            ov.show()
            ov.raise_()
            self._customize_overlay = ov
        except Exception as e:
            print(f"[UI] Error opening customize overlay: {e}")
            import traceback
            traceback.print_exc()

    def _preview_ui_color(self, hex_color: str):
        """Live preview — paints the whole interface the new colour (does NOT write to config)."""
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = "",
                           voice: str = ""):
        """Update all name/theme-dependent UI elements and persist to config."""
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — {APP_VERSION}")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                # Live-paint the whole interface (panels, buttons, borders, HUD)
                retheme_all_widgets(old, current_palette())
                self._retheme_status_buttons()
                color_changed = old["PRI"] != C.PRI

        # Voice change → persist and, if it actually changed, rebuild the Live
        # session so the new voice takes effect (it's fixed at connect time).
        voice_changed = False
        if voice:
            from memory.config_manager import get_voice, save_voice
            if voice != get_voice():
                save_voice(voice)
                voice_changed = True

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
            if voice_changed:
                self._log.append_log(f"SYS: Voice set — {voice}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

        self._tick_clock()

        if voice_changed and self.on_voice_change:
            self.on_voice_change()

    def _centre_overlay(self, ov) -> None:
        """Place a floating overlay in the middle of the HUD and show it."""
        cw = self.centralWidget()
        ov.adjustSize()
        ov.setGeometry(
            max(0, (cw.width()  - ov.width())  // 2),
            max(0, (cw.height() - ov.height()) // 2),
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()

    # ── Audio devices ────────────────────────────────────────────────────────

    def _open_audio_devices(self):
        ov = AudioDeviceOverlay(parent=self.centralWidget())
        ov.picked.connect(self._on_audio_devices_applied)
        self._centre_overlay(ov)
        self._audio_overlay = ov            # keep a reference so it isn't GC'd

    def _on_audio_devices_applied(self):
        self._log.append_log("SYS: Audio devices updated.")
        if self.on_audio_device_change:
            self.on_audio_device_change()

    # ── Memory panel ─────────────────────────────────────────────────────────

    def _open_memory_panel(self):
        ov = MemoryOverlay(parent=self.centralWidget())
        self._centre_overlay(ov)
        self._memory_overlay = ov

    # ── Irreversible-action confirmation ─────────────────────────────────────

    def _show_confirm_banner(self, title: str, detail: str):
        self._hide_confirm_banner()
        ov = ConfirmBanner(title, detail, parent=self.centralWidget())
        ov.answered.connect(self._on_confirm_answered)
        self._centre_overlay(ov)
        self._confirm_overlay = ov

    def _hide_confirm_banner(self):
        ov = getattr(self, "_confirm_overlay", None)
        if ov is not None:
            ov.hide()
            ov.deleteLater()
            self._confirm_overlay = None

    def _on_confirm_answered(self, accepted: bool):
        # Tear the banner down first: core.confirm.resolve() may be about to
        # shut the machine down, and a live widget mid-callback is not where you
        # want to be when that happens.
        self._hide_confirm_banner()
        try:
            from core.confirm import resolve
            resolve(bool(accepted))
        except Exception as e:
            self._log.append_log(f"ERR: Confirmation failed — {e}")

    def _open_plugin_manager(self):
        plugins = self.get_plugins() if self.get_plugins else []
        cw = self.centralWidget()
        ov = PluginManagerOverlay(plugins, parent=cw)
        ov.adjustSize()
        ov.setGeometry(
            (cw.width()  - ov.width())  // 2,
            (cw.height() - ov.height()) // 2,
            ov.width(), ov.height(),
        )
        ov.show()
        ov.raise_()
        self._plugin_manager_overlay = ov   # keep a reference so it isn't GC'd

    def _open_plugin_settings(self):
        sections = self.get_plugin_settings() if self.get_plugin_settings else []
        cw = self.centralWidget()
        ov = PluginSettingsOverlay(sections, parent=cw)
        ow = PluginSettingsOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._plugin_settings_overlay = ov   # keep a reference so it isn't GC'd

    def _open_shortcuts_manager(self):
        if getattr(self, "_shortcuts_overlay", None) is not None:
            try:
                self._shortcuts_overlay.hide()
                self._shortcuts_overlay.deleteLater()
            except Exception:
                pass
            self._shortcuts_overlay = None

        cw = self.centralWidget()
        ov = ShortcutsManagerOverlay(parent=cw)
        ow = ShortcutsManagerOverlay._OW
        oh = min(560, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        ov.raise_()
        self._shortcuts_overlay = ov

    # ── Status badges (AI Core, Sec, Protocol) ───────────────────────────────────

    def _update_ai_core_btn(self, awake: bool | None = None):
        if not hasattr(self, "_ai_core_btn"):
            return
        if awake is None:
            st = self._wake_state()
            awake = st.get("awake", True) and (getattr(self.hud, "state", "") != "SLEEPING")
        if awake:
            self._ai_core_btn.setText("AI CORE\nACTIVE")
            self._ai_core_btn.setStyleSheet(_status_btn_style(C.GREEN))
        else:
            self._ai_core_btn.setText("AI CORE\nSTANDBY")
            self._ai_core_btn.setStyleSheet(_status_btn_style(C.ACC2))

    def _retheme_status_buttons(self):
        if hasattr(self, "_ai_core_btn"):
            st = self._wake_state()
            awake = st.get("awake", True) and (getattr(self.hud, "state", "") != "SLEEPING")
            self._update_ai_core_btn(awake)
        if hasattr(self, "_sec_btn"):
            self._sec_btn.setStyleSheet(_status_btn_style(C.PRI))
        if hasattr(self, "_protocol_btn"):
            self._protocol_btn.setStyleSheet(_status_btn_style(C.TEXT_DIM))

    def _on_ai_core_clicked(self):
        st = self._wake_state()
        is_sleeping = (not st.get("awake", True)) or (getattr(self.hud, "state", "") == "SLEEPING")
        if is_sleeping:
            self._tap_wake_manual()
            self._update_ai_core_btn(awake=True)
            self._log.append_log("SYS: AI Core activated from standby.")
        else:
            if self.on_wake_manual and st.get("enabled", False):
                self._tap_wake_manual()
                new_st = self._wake_state()
                self._update_ai_core_btn(new_st.get("awake", True))
            else:
                self._log.append_log("SYS: AI Core status nominal — active & listening.")

    def _on_sec_clicked(self):
        self._log.append_log("SYS: Security protocol cleared — all tools & plugins verified.")
        self._open_plugin_manager()

    def _on_protocol_clicked(self):
        self._open_protocol_info()

    def _open_protocol_info(self):
        if getattr(self, "_protocol_overlay", None) is not None:
            try:
                self._protocol_overlay.hide()
                self._protocol_overlay.deleteLater()
            except Exception:
                pass
            self._protocol_overlay = None

        cfg = _read_full_config()
        asst = self._assistant_name or cfg.get("assistant_name", "JARVIS")
        user = cfg.get("user_name", "AMOX") or "AMOX"
        ov = ProtocolInfoOverlay(
            protocol=f"PROTOCOL MARK {APP_PROTOCOL} (BUILD 54)",
            assistant_name=asst,
            user_name=user,
            parent=self.centralWidget(),
        )
        ov.audio_requested.connect(self._open_audio_devices)
        ov.memory_requested.connect(self._open_memory_panel)
        ov.customize_requested.connect(self._open_customize)
        self._centre_overlay(ov)
        self._protocol_overlay = ov

    # ── Clipboard intelligence ───────────────────────────────────────────────────

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    # ────────────────────────────────────────────────────────────────────────────

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    # ------------------------------------------------------------------
    # Video playback inside the HUD
    # ------------------------------------------------------------------
    def _set_muted(self, muted: bool, note: str = ""):
        muted = bool(muted)
        if muted == self._muted:
            return
        self._muted = muted
        self.hud.muted = muted
        self._style_mute_btn()
        if muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted." + (f" {note}" if note else ""))
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active." + (f" {note}" if note else ""))

    def _on_video_open(self, source: str, title: str, muted: bool,
                       audio_source: str = "") -> None:
        if not HAVE_VIDEO or not self._video_player:
            self.write_log("SYS: Video playback is not available in this Qt install.")
            return
        self._cam_stop.set()

        self._video_title.setText(f"▶  {(title or 'VIDEO')[:44].upper()}")
        self._video_split = bool(audio_source)
        self._set_video_muted(bool(muted))

        url = (QUrl.fromLocalFile(source) if Path(source).exists()
               else QUrl(source))
        self._video_player.setSource(url)
        if self._video_split:
            self._video_sound.setSource(QUrl(audio_source))
        self._hud_cam_stack.setCurrentIndex(2)
        self._video_on = True
        self._sync_mic_for_video()
        self._video_player.play()
        if self._video_split:
            self._video_sound.play()
            self._video_sync.start()

    def _fit_video(self, *_a) -> None:
        if not (self._video_item and self._video_widget):
            return
        try:
            native = self._video_item.nativeSize()
            if native.isEmpty():
                return
            view = self._video_widget.viewport().size()
            scale = min(view.width() / native.width(),
                        view.height() / native.height())
            w, h = native.width() * scale, native.height() * scale
            self._video_item.setSize(QSizeF(w, h))
            self._video_scene.setSceneRect(0, 0, w, h)
            self._video_widget.centerOn(self._video_item)
        except Exception:
            pass

    def _on_video_close(self) -> None:
        if self._video_sync:
            self._video_sync.stop()
        for p in (self._video_player, self._video_sound):
            if p:
                p.stop()
                p.setSource(QUrl())
        self._video_split = False
        self._video_on = False
        self._hud_cam_stack.setCurrentIndex(0)
        self._sync_mic_for_video()

    def _set_video_muted(self, muted: bool) -> None:
        muted = bool(muted)
        if self._video_audio:
            self._video_audio.setMuted(muted)
        if self._video_sound_out:
            self._video_sound_out.setMuted(muted)
        self._sync_video_mute_btn()
        self._sync_mic_for_video()

    def _on_video_mute(self, muted: bool) -> None:
        self._set_video_muted(muted)

    def _sync_mic_for_video(self) -> None:
        sound_on = bool(self._video_on and self._video_sound_out
                        and not self._video_sound_out.isMuted())
        if sound_on and not self._muted:
            self._video_auto_muted = True
            self._set_muted(True, "The video's sound is on — silence it, close it, or press F4 to talk. Typing still works.")
        elif not sound_on and self._video_auto_muted:
            self._video_auto_muted = False
            self._set_muted(False, "The video is quiet again.")

    def _sync_video_sound(self) -> None:
        if not (self._video_split and self._video_sound and self._video_player):
            return
        try:
            if self._video_player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                return
            drift = self._video_sound.position() - self._video_player.position()
            if abs(drift) > 300:
                self._video_sound.setPosition(self._video_player.position())
        except Exception:
            pass

    def _sync_video_mute_btn(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        muted = bool(out and out.isMuted())
        self._video_mute_btn.setText("🔇  SOUND OFF" if muted else "🔊  SOUND ON")

    def _toggle_video_mute(self) -> None:
        out = self._video_sound_out if self._video_split else self._video_audio
        if out:
            self._set_video_muted(not out.isMuted())

    def _on_video_error(self, *_a) -> None:
        err = ""
        try:
            err = self._video_player.errorString()
        except Exception:
            pass
        self.write_log(f"SYS: The video could not be played{(' — ' + err) if err else ''}.")
        self._on_video_close()

    def stop_video(self) -> None:
        self._video_close_sig.emit()

    def video_is_playing(self) -> bool:
        return bool(self._video_on)

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #140006; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 3px;
                }}
            """)
        else:
            self._mute_btn.setText("🎙  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #001f10; }}
            """)
        # Sync the header mic button too
        try:
            self._header_mic_btn.setChecked(self._muted)
            self._header_mic_btn.setText("🔇" if self._muted else "🎙")
        except Exception:
            pass

    def _on_header_mic_clicked(self):
        """Header mic lock button — mirrors the mute toggle in the right panel."""
        self._toggle_mute()

    def _style_always_on_btn(self, active: bool):
        """Style the Always-On Mic toggle button."""
        try:
            if active:
                self._always_on_btn.setChecked(True)
                self._always_on_btn.setText("🔴  MIC: ALWAYS ON")
                self._always_on_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #001a06; color: {C.GREEN};
                        border: 1px solid {C.GREEN}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ background: #00260a; }}
                    QPushButton:checked {{ background: #001a06; color: {C.GREEN}; }}
                """)
            else:
                self._always_on_btn.setChecked(False)
                self._always_on_btn.setText("🎤  MIC: ALWAYS ON")
                self._always_on_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
                """)
        except Exception:
            pass

    def _toggle_always_on_mic(self):
        """Toggle Always-On Mic mode — connects to JarvisLive.set_always_on_mic()."""
        try:
            import sys
            jarvis = getattr(sys, "_jarvis_live_instance", None)
            if jarvis is None:
                self._log.append_log("SYS: JARVIS session not ready yet.")
                return
            current = getattr(jarvis, "_always_on_mic", False)
            new_state = not current
            jarvis.set_always_on_mic(new_state)
            self._style_always_on_btn(new_state)
        except Exception as e:
            self._log.append_log(f"SYS: Always-On Mic error — {e}")


    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        if hasattr(self, "_ai_core_btn"):
            if state == "SLEEPING":
                self._update_ai_core_btn(awake=False)
            else:
                self._update_ai_core_btn(awake=True)

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. {self._assistant_name} online.")


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = MainWindow(face_path)
        self.root = _RootShim(self._app)
        self._win.show()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    @property
    def on_voice_change(self):
        return self._win.on_voice_change

    @on_voice_change.setter
    def on_voice_change(self, cb):
        self._win.on_voice_change = cb

    @property
    def on_audio_device_change(self):
        return self._win.on_audio_device_change

    @on_audio_device_change.setter
    def on_audio_device_change(self, cb):
        self._win.on_audio_device_change = cb

    def show_confirm(self, title: str, detail: str) -> None:
        """Thread-safe: raise the irreversible-action gate. Called from action
        handlers running in executor threads, so it goes through a signal."""
        self._win._confirm_sig.emit(str(title)[:120], str(detail)[:300])

    def hide_confirm(self) -> None:
        """Thread-safe: take the gate down."""
        self._win._confirm_hide_sig.emit()

    @property
    def get_plugins(self):
        return self._win.get_plugins

    @get_plugins.setter
    def get_plugins(self, cb):
        self._win.get_plugins = cb

    @property
    def get_plugin_settings(self):
        return self._win.get_plugin_settings

    @get_plugin_settings.setter
    def get_plugin_settings(self, cb):
        self._win.get_plugin_settings = cb

    @property
    def on_wake_toggle(self):
        return self._win.on_wake_toggle

    @on_wake_toggle.setter
    def on_wake_toggle(self, cb):
        self._win.on_wake_toggle = cb

    @property
    def on_wake_manual(self):
        return self._win.on_wake_manual

    @on_wake_manual.setter
    def on_wake_manual(self, cb):
        self._win.on_wake_manual = cb

    @property
    def wake_get_state(self):
        return self._win.wake_get_state

    @wake_get_state.setter
    def wake_get_state(self, cb):
        self._win.wake_get_state = cb

    def set_audio_level(self, level: float) -> None:
        """Thread-safe: feed a 0.0–1.0 live audio level to the HUD waveform.
        Called from the audio threads; a plain float store is atomic under the
        GIL, so no signal/lock is needed for this cosmetic value."""
        try:
            self._win.hud.set_audio_level(level)
        except Exception:
            pass

    def glance(self, dx: float, dy: float, hold: float = 1.1) -> None:
        """Ask the avatar to look somewhere for a moment (see HoloAvatar.glance)."""
        try:
            if self._avatar is not None:
                self._avatar.glance(dx, dy, hold)
        except Exception:
            pass

    @property
    def ptt_hold(self):
        return self._win.ptt_hold

    @ptt_hold.setter
    def ptt_hold(self, cb):
        self._win.ptt_hold = cb

    @property
    def on_push_to_talk(self):
        return self._win.on_push_to_talk

    @on_push_to_talk.setter
    def on_push_to_talk(self, cb):
        self._win.on_push_to_talk = cb

    def push_visemes(self, frames, hop: float, at: float) -> None:
        """Thread-safe: post a schedule of (level, openness, width) mouth frames
        for JARVIS's own speech. `at` is the wall-clock time the batch begins to
        sound, not the time of the call. See HudCanvas.push_visemes()."""
        try:
            self._win.hud.push_visemes(frames, hop, at)
        except Exception:
            pass

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the panel below the HUD."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def show_quiz(self, topic: str, questions, grade=None) -> None:
        """Thread-safe: put an interactive quiz on the board.

        `grade(question, given)` decides each answer — the plugin supplies it so
        the marking rules live with the questions rather than being duplicated
        here. Returning None from it means "JARVIS should judge this one", which
        is how open answers and near-miss gap-fills are handled.

        Returns immediately: the user answers at their own pace and the finished
        result is delivered back through on_text_command.
        """
        self._win._quiz_sig.emit(str(topic or ""), list(questions or []), grade)

    def hide_quiz(self) -> None:
        """Thread-safe: clear any quiz currently on the board."""
        self._win._quiz_hide_sig.emit()

    def show_review(self, title: str, summary: str, findings, unclear=None) -> None:
        """Thread-safe: lay a document review into the panel below the HUD.

        `findings` is a list of {heading, detail, severity, quote, suggestion};
        severity is one of 'serious' / 'caution' / 'note' and decides colour and
        order here, so the caller supplies no styling of its own.
        """
        self._win._review_sig.emit(str(title or ""), str(summary or ""),
                                   list(findings or []), list(unclear or []))

    def prompt_reconfig(self):
        """Thread-safe: show the API key setup overlay (e.g. after an auth error)."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_video(self, source: str, title: str = "", muted: bool = True,
                   audio_source: str = "") -> None:
        """Thread-safe: play a video where the avatar normally is."""
        self._win._video_open_sig.emit(str(source or ""), str(title or ""),
                                       bool(muted), str(audio_source or ""))

    def stop_video(self) -> None:
        """Thread-safe: close the video and give the HUD back to the avatar."""
        self._win._video_close_sig.emit()

    def set_video_muted(self, muted: bool) -> None:
        """Thread-safe: silence or unsilence whatever is playing."""
        self._win._video_mute_sig.emit(bool(muted))

    def video_is_playing(self) -> bool:
        return bool(self._win.video_is_playing())

    def show_camera_frame(self, img_bytes: bytes):
        """Thread-safe: show a webcam frame in the small overlay (screen captures)."""
        self._win._camera_sig.emit(img_bytes)

    def start_camera_stream(self) -> None:
        """Thread-safe: start live camera feed in the full HUD area."""
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        """Thread-safe: stop the live camera feed."""
        self._win.stop_camera_stream()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")