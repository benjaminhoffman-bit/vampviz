"""VAMPVIZ: a desktop-audio visualizer. Hears whatever Windows is playing (WASAPI loopback), renders on the GPU
at the monitor's native mode (4K 240 Hz here), paced to its refresh rate: one frame per screen refresh.

    vampviz.pyw                  opens the way you left it (window or fullscreen, last scene)
    python vampviz.pyw --scene 3 start on scene 3 (Dynamic off, like pressing 3)
    python vampviz.pyw --icon    write assets/vampviz.ico (the installer's shortcut icon)
    python vampviz.pyw --test    self-check of the analyzer, director, shaders and icon (hidden window, nothing on screen)
    python vampviz.pyw --hidden   start in the tray (for a shell:startup shortcut)

Keys: 1-5 scene, Space dynamic mode (scenes follow the music), F fullscreen, M next monitor, Esc hides it to the tray
icon by the clock, Ctrl+Q quits. The tray icon: left-click shows or hides, right-click for scenes, monitor, fullscreen
and quit. Settings live in vampviz.json next to this file; vampviz.log holds what it opened plus any crash (no console
under pythonw). Scenes: 1 VAMP (kaleidoscope spectrum ring), 2 SLOWED (smeared fluid), 3 RAGE (strobing tunnel),
4 BARS (mirrored spectrum), 5 LATTICE (flight through a beam lattice).
"""
import ctypes
import json
import math
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from appicon import icon, write_ico
from shaders import BLOOM_DOWN, BLOOM_UP, COMPOSITE, SCENE, VERT

HERE = Path(__file__).resolve().parent
SETTINGS, LOG, APP_ID = HERE / "vampviz.json", HERE / "vampviz.log", "SilvaDes.VAMPVIZ"
PROGRAMS = (("scene", SCENE), ("down", BLOOM_DOWN), ("up", BLOOM_UP), ("comp", COMPOSITE))
DEFAULTS = {"scene": 0, "auto": True, "fullscreen": False, "size": [1920, 1080], "monitor": 0, "tip": False}


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text("utf-8-sig")))  # -sig: a hand edit saved with a BOM must not reset everything
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    if not (isinstance(s["size"], list) and len(s["size"]) == 2 and all(isinstance(v, int) and v > 0 for v in s["size"])):
        s["size"] = list(DEFAULTS["size"])  # a quit while minimized used to write [0, 0], which crashed the next launch
    return s

# ─── audio ──────────────────────────────────────────────────────────────────────────────────────────────────────────
N_FFT, N_BANDS, N_WAVE = 4096, 96, 2048  # N_BANDS must match shaders.N_BANDS


class Loopback:
    """Windows' default output as mono float32 in a ring buffer; a watchdog reopens the loopback when the default
    output changes or the stream dies, and starts silent when there is no output device. Thread-safe reads."""

    def __init__(self):
        ole32.CoInitializeEx(None, 2)  # apartment-threaded, as PortAudio wants
        self.rate, self.channels, self.name = 48000, 2, "no output device"
        self.ring, self.pos, self.lock, self.last = np.zeros(48000, np.float32), 0, threading.Lock(), 0.0
        self.pa = self.stream = None; self.endpoint = None; self.stop = threading.Event()
        self.open()
        self.thread = threading.Thread(target=self.watch, daemon=True); self.thread.start()

    def open(self):
        import pyaudiowpatch as pa
        self.endpoint = default_endpoint()
        if self.stream: self.stream.close()
        if self.pa: self.pa.terminate()  # drops PortAudio's frozen device list
        self.pa = self.stream = None
        try:
            self.pa = pa.PyAudio()  # re-enumerates (~120 ms, GIL released)
            dev = self.pa.get_default_wasapi_loopback()
            rate, ch = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])
            with self.lock:
                self.rate, self.channels, self.name, self.ring, self.pos = rate, ch, dev["name"].removesuffix(" [Loopback]"), np.zeros(rate, np.float32), 0
            self.stream = self.pa.open(format=pa.paFloat32, channels=ch, rate=rate, input=True, input_device_index=dev["index"],
                                       frames_per_buffer=480, stream_callback=lambda d, n, t, s, ch=ch: self._cb(d, ch))
            print(f"hearing: {self.name} @ {rate} Hz")
        except (OSError, LookupError, ValueError) as e:
            self.endpoint, self.name = None, "no output device"  # retry next second; the tooltip must not keep naming a device that is gone
            if self.pa: self.pa.terminate(); self.pa = None  # or the stale device list leaks for good
            print(f"no loopback yet: {e}")

    def _cb(self, data, channels):
        mono = np.nan_to_num(np.frombuffer(data, np.float32).reshape(-1, channels).mean(1))  # one NaN would sit in the analyzer's averages for good
        with self.lock:
            idx = (self.pos + np.arange(len(mono))) % len(self.ring)
            self.ring[idx] = mono
            self.pos = (self.pos + len(mono)) % len(self.ring)
            self.last = time.perf_counter()
        return None, 0

    def watch(self):
        ole32.CoInitializeEx(None, 2)
        while not self.stop.wait(1.0):
            try:
                if default_endpoint() != self.endpoint or (self.stream and not self.stream.is_active()):
                    self.open()
            except Exception as e:  # the watchdog never dies silently; worst case is today's behaviour
                print(f"loopback watch: {e}")

    def latest(self, n: int) -> np.ndarray:
        with self.lock:
            if time.perf_counter() - self.last > 0.15:  # loopback goes mute in silence; feed zeros so visuals decay
                self.ring[:] = 0
            idx = (self.pos - n + np.arange(n)) % len(self.ring)
            return self.ring[idx]

    def close(self):
        self.stop.set(); self.thread.join()
        if self.stream: self.stream.stop_stream(); self.stream.close()
        if self.pa: self.pa.terminate()


class Analyzer:
    """FFT → log bands (sub bands interpolated, high bands summed), attack/release, peak hold, dB AGC, bass/mid/high,
    kick on energy flux."""

    def __init__(self, rate: int):
        self.rate = rate
        self.window = np.hanning(N_FFT).astype(np.float32)
        freqs = np.fft.rfftfreq(N_FFT, 1 / rate)
        edges = np.geomspace(30, min(16000, rate / 2 - 1), N_BANDS + 1)
        self.idx = np.searchsorted(freqs, edges)                      # all 97 edges, repeats allowed: band 95 stops at 16 kHz
        self.freqs, self.centers = freqs, np.sqrt(edges[:-1] * edges[1:])
        self.k = int((np.diff(edges) < freqs[1]).sum())               # bands narrower than one FFT bin: a contiguous prefix, read by interpolation
        self.sl = {n: slice(*np.searchsorted(self.centers, hz)) for n, hz in (("bass", (30, 180)), ("mid", (300, 2500)))}
        self.kick_lo, self.kick_hi = np.searchsorted(freqs, 40), np.searchsorted(freqs, 130)
        self.bands = np.zeros(N_BANDS, np.float32); self.peaks = np.zeros(N_BANDS, np.float32)
        self.gain, self.agc_db = 1.0, 0.0
        self.kick_avg, self.kick, self.last_kick, self.beats = 1e-3, 0.0, 0.0, 0
        self.e_prev = self.e_t = 0.0
        self.bass = self.mid = self.high = self.rms = 0.0

    def step(self, samples: np.ndarray, dt: float, now: float):
        spec = np.abs(np.fft.rfft(samples[-N_FFT:] * self.window)) * (2 / N_FFT)
        raw = np.sqrt(np.add.reduceat(spec * spec, self.idx)[:N_BANDS])  # band energy: wide high bands integrate their width, so hats and snares read
        raw[: self.k] = np.interp(self.centers[: self.k], self.freqs, spec)
        db = 20 * np.log10(raw + 1e-9)
        peak_db = float(db.max())
        if peak_db > -55:  # AGC in dB: clamps in 0.4 s when music gets hotter, opens over 4 s, holds through silence
            want = max(-12.0, min(18.0, -15.0 - peak_db))
            self.agc_db += (want - self.agc_db) * (1 - math.exp(-dt / (0.4 if want < self.agc_db else 4.0)))
        self.gain = 10 ** (self.agc_db / 20)  # for the waveform texture
        level = np.clip((db + self.agc_db + 65) / 60, 0, 1) ** 1.3
        up, down = 1 - math.exp(-dt / 0.015), 1 - math.exp(-dt / 0.14)
        self.bands += (level - self.bands) * np.where(level > self.bands, up, down)
        self.peaks = np.maximum(self.peaks - dt * 0.7, self.bands)  # peak caps fall 1.4 s from the top
        b = self.bands
        self.bass, self.mid = (float(np.sort(b[self.sl[n]])[-4:].mean()) for n in ("bass", "mid"))  # mean of the 4 strongest: a lone 808 reads as bass
        self.high = float(b[70:].mean())
        self.rms = float(np.sqrt(np.mean(samples[-2048:] ** 2)))
        # kick: the 40-130 Hz energy rising faster than 6x its 0.6 s average per second (a sustained 808 never does)
        e = float((spec[self.kick_lo:self.kick_hi] ** 2).sum())
        rise = 0.0
        if e != self.e_prev:  # new audio landed (10 ms chunks): rate over the real interval, not the frame
            rise, self.e_prev, self.e_t = (e - self.e_prev) / min(max(now - self.e_t, 1e-3), 0.05), e, now
        self.kick_avg += (e - self.kick_avg) * (1 - math.exp(-dt / 0.6))
        if rise > 6.0 * self.kick_avg and e > 3e-7 and now - self.last_kick > 0.15:
            # + 1e-12: minutes of digital silence decay kick_avg to exactly 0.0, and the first hit after would divide by it
            self.kick, self.last_kick, self.beats = min(1.0, 0.4 + rise / (30.0 * self.kick_avg + 1e-12)), now, self.beats + 1
        self.kick *= math.exp(-dt / 0.16)


# ─── tray ───────────────────────────────────────────────────────────────────────────────────────────────────────────
from ctypes import wintypes as w  # noqa: E402

user32, shell32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("shell32"), ctypes.WinDLL("kernel32", use_last_error=True)
ole32, dwmapi, wtsapi32 = ctypes.WinDLL("ole32"), ctypes.WinDLL("dwmapi"), ctypes.WinDLL("wtsapi32")
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
TRAY_CLASS, WM_TRAY, WM_SHOW, WM_CLOSE = "VampVizTray", 0x8001, 0x8002, 0x10
WM_LBUTTONUP, WM_RBUTTONUP = 0x202, 0x205
WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, WM_WTSSESSION_CHANGE = 0x218, 0x8013, 0x2B1
GUID_CONSOLE_DISPLAY_STATE = uuid.UUID("6fe69556-704a-47a0-8f24-c28d936fda47").bytes_le
MF_CHECKED, MF_SEPARATOR, TPM_RETURNCMD = 0x8, 0x800, 0x100
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0, 1, 2, 1, 2, 4, 0x10
ICO = HERE / "assets" / "vampviz.ico"
SCENES = ("VAMP", "SLOWED", "RAGE", "BARS", "LATTICE")


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
                ("hIcon", w.HICON), ("szTip", w.WCHAR * 128), ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", w.HICON)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


for fn, args, res in ((user32.CreateWindowExW, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
                      (user32.DefWindowProcW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t),
                      (user32.PostMessageW, [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
                      (user32.FindWindowW, [w.LPCWSTR, w.LPCWSTR], w.HWND),
                      (user32.AppendMenuW, [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR], w.BOOL),
                      (user32.TrackPopupMenu, [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID], w.BOOL),
                      (user32.CreatePopupMenu, [], w.HMENU), (user32.DestroyMenu, [w.HMENU], w.BOOL),
                      (user32.SetForegroundWindow, [w.HWND], w.BOOL), (user32.GetCursorPos, [ctypes.POINTER(w.POINT)], w.BOOL),
                      (user32.LoadImageW, [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT], w.HANDLE),
                      (user32.DestroyWindow, [w.HWND], w.BOOL), (user32.DestroyIcon, [w.HICON], w.BOOL),
                      (user32.RegisterWindowMessageW, [w.LPCWSTR], w.UINT), (user32.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], w.ATOM),
                      (user32.RegisterPowerSettingNotification, [w.HANDLE, ctypes.c_char_p, w.DWORD], w.HANDLE),
                      (wtsapi32.WTSRegisterSessionNotification, [w.HWND, w.DWORD], w.BOOL),
                      (wtsapi32.WTSUnRegisterSessionNotification, [w.HWND], w.BOOL),
                      (dwmapi.DwmSetWindowAttribute, [w.HWND, w.DWORD, w.LPCVOID, w.DWORD], ctypes.c_long),
                      (kernel32.GetModuleHandleW, [w.LPCWSTR], w.HMODULE),
                      (kernel32.CreateMutexW, [w.LPVOID, w.BOOL, w.LPCWSTR], w.HANDLE),
                      (ole32.CLSIDFromString, [w.LPCWSTR, ctypes.c_char_p], ctypes.HRESULT),
                      (ole32.CoCreateInstance, [ctypes.c_char_p, ctypes.c_void_p, w.DWORD, ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.HRESULT),
                      (ole32.CoInitializeEx, [ctypes.c_void_p, w.DWORD], ctypes.c_long),
                      (ole32.CoTaskMemFree, [ctypes.c_void_p], None),
                      (shell32.Shell_NotifyIconW, [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], w.BOOL)):
    fn.argtypes, fn.restype = args, res


def guid(s):  # 16-byte GUID buffer from its string
    b = ctypes.create_string_buffer(16); ole32.CLSIDFromString(s, b); return b


CLSID_ENUM, IID_ENUM = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")


def com_method(obj, i, restype, *argtypes):  # slot i of obj's vtable (QueryInterface 0, AddRef 1, Release 2, ...)
    fn = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0] + i * 8, ctypes.POINTER(ctypes.c_void_p))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(fn)


def default_endpoint() -> str:
    """Windows' default output endpoint ID, "" when there is none. ~2 ms, PortAudio not involved."""
    ole32.CoInitializeEx(None, 2)  # S_FALSE when the thread already has COM; without it CoCreateInstance fails
    enum, dev, pid = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_wchar_p()
    try:
        ole32.CoCreateInstance(CLSID_ENUM, None, 1, IID_ENUM, ctypes.byref(enum))
        com_method(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(enum, 0, 1, ctypes.byref(dev))  # GetDefaultAudioEndpoint(eRender, eMultimedia)
        com_method(dev, 5, ctypes.HRESULT, ctypes.POINTER(ctypes.c_wchar_p))(dev, ctypes.byref(pid))  # IMMDevice::GetId
        return pid.value or ""
    except OSError:  # E_NOTFOUND: no render device at all
        return ""
    finally:
        if pid: ole32.CoTaskMemFree(pid)
        for p in (dev, enum):
            if p: com_method(p, 2, ctypes.c_ulong)(p)  # Release


def already_running() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\SilvaDes.VAMPVIZ")  # handle held until the process ends
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


class Tray:
    """The icon by the clock. Left-click shows or hides the window; right-click is the menu. Its messages arrive through
    glfw.poll_events (same thread), so `act(name, arg)` runs inside the render loop. The same hidden window also hears
    display-off and session-lock notices, which it stores in state for the render loop."""

    def __init__(self, act, state: dict, monitors: list[str]):
        self.act, self.state, self.monitors = act, state, monitors
        self.proc = WNDPROC(self.handle)  # kept: Windows calls it as long as the window lives
        self.explorer_restarted = user32.RegisterWindowMessageW("TaskbarCreated")
        module = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=module, lpszClassName=TRAY_CLASS)))
        self.hwnd = user32.CreateWindowExW(0, TRAY_CLASS, "", 0, 0, 0, 0, 0, None, None, module, None)  # no title: the main window owns "VAMPVIZ"
        user32.RegisterPowerSettingNotification(self.hwnd, GUID_CONSOLE_DISPLAY_STATE, 0)  # Windows sends the current state at once
        wtsapi32.WTSRegisterSessionNotification(self.hwnd, 0)
        if not ICO.exists():
            write_ico(ICO)
        size = user32.GetSystemMetrics(49)  # SM_CXSMICON
        self.icon = user32.LoadImageW(None, str(ICO), 1, size, size, 0x10)
        self.data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                    uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
                                    hIcon=self.icon, szTip="VAMPVIZ")
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))

    def handle(self, hwnd, msg, wparam, lparam) -> int:
        try:
            return self._handle(hwnd, msg, wparam, lparam)
        except Exception:  # a ctypes callback swallows exceptions, and under pythonw nobody sees them: log and carry on
            import traceback
            with LOG.open("a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            return 0

    def _handle(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_TRAY and lparam == WM_LBUTTONUP:
            self.act("toggle", None)
        elif msg == WM_TRAY and lparam == WM_RBUTTONUP:
            self.menu()
        elif msg == WM_SHOW:  # a second copy started: it hands over to us
            self.act("show", None)
        elif msg == WM_CLOSE:  # the installer, before it updates or removes us
            self.act("quit", None)
        elif msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE:
            self.state["display"] = ctypes.c_ubyte.from_address(lparam + 20).value != 0  # POWERBROADCAST_SETTING: GUID(16)+DataLength(4)+Data; 0 off, 1 on, 2 dimmed
        elif msg == WM_WTSSESSION_CHANGE and wparam in (1, 2, 7, 8):
            self.state["locked"] = wparam in (2, 7)  # console disconnect / lock
        elif msg == self.explorer_restarted:
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))
        else:
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        return 0

    def menu(self) -> None:
        st, menu = self.state, user32.CreatePopupMenu()
        items = [(1, "Hide VAMPVIZ" if st["shown"] else "Show VAMPVIZ", False), (0, None, False)]
        items += [(2, "Dynamic: scenes follow the music", st["auto"])]
        items += [(10 + i, f"{i + 1}  {n}", st["scene"] == i) for i, n in enumerate(SCENES)]
        items += [(0, None, False), (3, "Fullscreen", st["full"]), (0, None, False)]
        items += [(20 + i, f"On {n}", st["monitor"] == i) for i, n in enumerate(self.monitors)]
        items += [(0, None, False), (4, "Quit VAMPVIZ", False)]
        for cmd, text, checked in items:
            user32.AppendMenuW(menu, MF_SEPARATOR if not cmd else MF_CHECKED if checked else 0, cmd, text)
        at = w.POINT()
        user32.GetCursorPos(ctypes.byref(at))
        user32.SetForegroundWindow(self.hwnd)  # or the menu wouldn't close on a click elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD, at.x, at.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL, as Windows asks after a tray menu
        user32.DestroyMenu(menu)
        if cmd in (1, 2, 3, 4):
            self.act({1: "toggle", 2: "auto", 3: "fullscreen", 4: "quit"}[cmd], None)
        elif 10 <= cmd < 20:
            self.act("scene", cmd - 10)
        elif cmd >= 20:
            self.act("monitor", cmd - 20)

    def notify(self, title: str, text: str) -> None:
        self.data.uFlags, self.data.szInfoTitle, self.data.szInfo, self.data.dwInfoFlags = NIF_INFO, title, text, 1
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.data))
        self.data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP

    def close(self) -> None:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.data))
        wtsapi32.WTSUnRegisterSessionNotification(self.hwnd)
        user32.DestroyWindow(self.hwnd)
        user32.DestroyIcon(self.icon)


class Director:
    """Dynamic mode: picks the scene from how the music feels and rolls a new look each section.
    Quiet or beatless → SLOWED. Dense kicks and loud → RAGE. Otherwise VAMP, BARS and LATTICE take turns; RAGE and
    LATTICE alternate on dense kicks. A section is 32 beats, 30 s, or a drop (loudness jumping well above its recent
    average)."""

    def __init__(self, scene: int):
        self.scene, self.variant, self.section_beat, self.section_t = scene, 0.37, 0, 0.0
        self.loud, self.kicks, self.last_beats, self.hold_until = 0.0, 0.0, 0, 0.0
        self.fast = 0.0

    def step(self, an, now: float, dt: float) -> None:
        a = 1 - math.exp(-dt / 2.5)
        self.loud += (an.rms - self.loud) * a
        self.fast += (an.rms - self.fast) * (1 - math.exp(-dt / 0.25))  # a 0.25 s loudness: one clap is not a drop
        self.kicks += ((an.beats - self.last_beats) / max(dt, 1e-3) - self.kicks) * a  # kicks per second
        self.last_beats = an.beats
        drop = self.fast > self.loud * 1.6 and self.fast > 0.06 and now > self.hold_until
        if drop or an.beats - self.section_beat >= 32 or now - self.section_t > 30:
            self.section_beat, self.section_t, self.hold_until = an.beats, now, now + 8.0
            self.variant = (self.variant * 7.31 + 0.173) % 1.0  # a new, deterministic look
            if self.loud < 0.02 or self.kicks < 0.8:
                want = 1
            elif self.kicks > 2.2 and self.loud > 0.12:
                want = 2 if self.scene != 2 else 4
            else:
                want = {0: 3, 3: 4}.get(self.scene, 0)
            self.scene = want


# ─── app ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def run(saved: dict, scene: int | None = None):
    import glfw
    import moderngl
    shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)  # its own taskbar button, not "Python"
    if not glfw.init():
        sys.exit("glfw init failed")
    monitors = glfw.get_monitors()
    names = []
    for m in monitors:  # "MSI MPG 322URX  3840x2160 240Hz", primary first in GLFW's order
        vm = glfw.get_video_mode(m)
        names.append(f"{glfw.get_monitor_name(m).decode(errors='replace')}  {vm.size.width}x{vm.size.height} {vm.refresh_rate}Hz")
    hidden = "--hidden" in sys.argv
    state = {"scene": (saved["scene"] if scene is None else scene) % len(SCENES), "auto": saved["auto"] and scene is None, "full": saved["fullscreen"],
             "monitor": min(saved["monitor"], len(monitors) - 1), "shown": not hidden, "quit": False,
             "tip": saved["tip"], "display": True, "locked": False}
    mon = monitors[state["monitor"]]
    mode = glfw.get_video_mode(mon)
    for hint, val in ((glfw.CONTEXT_VERSION_MAJOR, 3), (glfw.CONTEXT_VERSION_MINOR, 3),
                      (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE), (glfw.REFRESH_RATE, mode.refresh_rate),
                      (glfw.AUTO_ICONIFY, False),  # fullscreen on a second monitor stays up while you work on the first
                      (glfw.VISIBLE, False),  # shown once everything is up, or not at all with --hidden
                      (glfw.RED_BITS, mode.bits.red), (glfw.GREEN_BITS, mode.bits.green), (glfw.BLUE_BITS, mode.bits.blue)):
        glfw.window_hint(hint, val)
    full_now = state["full"] and not hidden and "--quit-after" not in sys.argv  # a timed dev run stays windowed
    size = mode.size if full_now else tuple(saved["size"])
    win = glfw.create_window(*size, "VAMPVIZ", mon if full_now else None, None)
    if not full_now:  # create_window puts a window on the primary monitor; the saved monitor sets the pacer's period, so put it there
        glfw.set_window_pos(win, *(v + 100 for v in glfw.get_monitor_pos(mon)))
    hwnd = glfw.get_win32_window(win)
    for attr, val in ((20, 1), (35, 0x00050005), (36, 0x00471AFF), (34, 0x00471AFF)):  # dark caption; caption void rgb(5,0,5); title text and border neon crimson rgb(255,26,71); COLORREF is 0x00BBGGRR. Windows 10: 34-36 give E_INVALIDARG, ignored
        dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes.c_uint(val)), 4)
    sizes = sorted({user32.GetSystemMetrics(49), user32.GetSystemMetrics(11)})  # SM_CXSMICON, SM_CXICON: the two GLFW keeps on Win32 (36 and 72 at 225 %)
    glfw.set_window_icon(win, len(sizes), [(s, s, icon(s).tolist()) for s in sizes])
    glfw.make_context_current(win)
    glfw.swap_interval(1)  # one frame per refresh, when the driver honours it
    ctx = moderngl.create_context()
    period = [1.0 / mode.refresh_rate]
    ctypes.WinDLL("winmm").timeBeginPeriod(1)  # 1 ms sleep granularity on Python 3.10; 3.11+ ignores it

    progs = {name: ctx.program(vertex_shader=VERT, fragment_shader=src) for name, src in PROGRAMS}
    vao = {k: ctx.vertex_array(p, []) for k, p in progs.items()}
    spec_tex = ctx.texture((N_BANDS, 1), 2, dtype="f4"); spec_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)  # .r level, .g peak hold
    wave_tex = ctx.texture((N_WAVE, 1), 1, dtype="f4"); wave_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    spec_tex.repeat_x = wave_tex.repeat_x = False
    fbos = {}

    def make_fbos(w, h):
        for f in fbos.values():
            f.color_attachments[0].release(); f.release()  # texture first: release() drops the attachment list
        for name, (fw, fh) in [("a", (w, h)), ("b", (w, h))] + [(f"m{k}", (max(1, w >> (2 + k)), max(1, h >> (2 + k)))) for k in range(5)]:
            t = ctx.texture((fw, fh), 4, dtype="f2"); t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.repeat_x = t.repeat_y = False
            fbos[name] = ctx.framebuffer(color_attachments=[t])
            fbos[name].clear()

    state["size"] = glfw.get_framebuffer_size(win)
    make_fbos(*state["size"])
    director = Director(state["scene"])
    # the first section starts now, not at boot: the saved scene gets its 30 s / 32 beats, and the cold loudness EMAs cannot read as a drop
    director.section_t = time.perf_counter(); director.hold_until = director.section_t + 8.0

    def place():
        """Fullscreen on the chosen monitor, or back to a window; the pacer follows that monitor's refresh rate."""
        m = monitors[state["monitor"]]
        vm = glfw.get_video_mode(m)
        if state["full"] and state["shown"]:  # GLFW's fullscreen SetWindowPos carries SWP_SHOWWINDOW; act("show") places a hidden window
            glfw.set_window_monitor(win, m, 0, 0, vm.size.width, vm.size.height, vm.refresh_rate)
        else:
            x, y = glfw.get_monitor_pos(m)
            glfw.set_window_monitor(win, None, x + 100, y + 100, *saved["size"], 0)
        period[0] = 1.0 / vm.refresh_rate
        glfw.swap_interval(1)

    def save():
        saved.update(scene=state["scene"], auto=state["auto"], fullscreen=state["full"], monitor=state["monitor"], tip=state["tip"])
        try:
            SETTINGS.write_text(json.dumps(saved, indent=1), "utf-8")
        except OSError as e:
            print(f"settings not saved: {e}")

    def act(name: str, arg):
        if name == "toggle":
            name = "hide" if state["shown"] and not glfw.get_window_attrib(win, glfw.ICONIFIED) else "show"  # a tray click on a minimized window brings it back
        if name == "show":
            state["shown"] = True
            if glfw.get_window_attrib(win, glfw.ICONIFIED):  # show_window is SW_SHOWNA: a minimized window would stay minimized
                glfw.restore_window(win)
            glfw.show_window(win); glfw.focus_window(win)
            if state["full"] and not glfw.get_window_monitor(win):
                place()
        elif name == "hide":
            if glfw.get_window_monitor(win):  # GLFW cannot hide a fullscreen window
                x, y = glfw.get_monitor_pos(monitors[state["monitor"]])
                glfw.set_window_monitor(win, None, x + 100, y + 100, *saved["size"], 0)
            state["shown"] = False; glfw.hide_window(win)
            if not state["tip"]:
                state["tip"] = True
                tray.notify("VAMPVIZ is still running", "Click its icon by the clock (under ^ if hidden) to bring it "
                                                       "back; right-click it to quit.")
        elif name == "quit":
            state["quit"] = True
        elif name == "scene":
            state["scene"], state["auto"] = arg, False
        elif name == "auto":
            state["auto"] = not state["auto"]
            director.scene = state["scene"]
        elif name == "fullscreen":
            state["full"] = not state["full"]; place()
        elif name == "monitor":
            state["monitor"] = arg; place()
        save()

    def on_key(_w, key, _sc, action, mods):
        if action != glfw.PRESS:
            return
        if key == glfw.KEY_ESCAPE:
            act("hide", None)
        elif key == glfw.KEY_Q and mods & glfw.MOD_CONTROL:
            act("quit", None)
        elif glfw.KEY_1 <= key <= glfw.KEY_5:
            act("scene", key - glfw.KEY_1)
        elif key == glfw.KEY_SPACE:
            act("auto", None)
        elif key == glfw.KEY_F:
            act("fullscreen", None)
        elif key == glfw.KEY_M:  # next monitor
            act("monitor", (state["monitor"] + 1) % len(monitors))
    glfw.set_key_callback(win, on_key)
    glfw.set_window_close_callback(win, lambda _w: (glfw.set_window_should_close(win, False), act("hide", None)))

    audio = Loopback()
    an = Analyzer(audio.rate)
    tray = Tray(act, state, names)
    try:
        banner = f"{ctx.info['GL_RENDERER']} | {names[state['monitor']]}"
        print(banner)
        print("1-5 scene  Space dynamic  F fullscreen  M next monitor  Esc hide to tray  Ctrl+Q quit")
        LOG.write_text(banner + "\n", "utf-8")

        t0 = last = time.perf_counter()
        due = t0
        frames, fps_t, late = 0, t0, 0
        ping = True
        travel = 0.0
        tipped = None
        quit_at = float(a[a.index("--quit-after") + 1]) + t0 if "--quit-after" in (a := sys.argv) else None
        if not hidden:
            glfw.show_window(win)
        while not state["quit"]:
            now = time.perf_counter()
            if quit_at and now > quit_at:
                state["quit"] = True
            dt, last = min(now - last, 0.05), now
            if an.rate != audio.rate:  # the loopback reopened on a device with another rate
                b = an.beats; an = Analyzer(audio.rate); an.beats = b
            if audio.name != tipped:
                tipped = audio.name; tray.data.szTip = f"VAMPVIZ  hearing {audio.name}"[:127]
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(tray.data))
            if not state["shown"] or glfw.get_window_attrib(win, glfw.ICONIFIED) or not state["display"] or state["locked"]:  # hidden, minimized, screen off or locked: no GPU work, keep tray and audio alive
                glfw.wait_events_timeout(0.1)
                audio.latest(N_FFT)
                continue
            fb = glfw.get_framebuffer_size(win)
            if fb != state["size"] and fb[0] > 0:
                state["size"] = fb; make_fbos(*fb)
            w, h = state["size"]

            samples = audio.latest(N_FFT)
            an.step(samples, dt, now)
            director.step(an, now, dt)
            travel = (travel + (1.2 + director.variant * 2.0 + an.rms * 4.0 + an.kick * 6.0) * dt) % 20.0  # lattice flight: speed integrates, so rms/kick change velocity, never position; 20 = period of every z pattern
            if state["auto"]:
                state["scene"] = director.scene
            spec_tex.write(np.stack([an.bands, an.peaks], 1).astype("f4").tobytes())
            wave_tex.write(np.clip(samples[-N_WAVE:] * an.gain * 1.5, -1, 1).astype("f4").tobytes())

            src, dst = (fbos["a"], fbos["b"]) if ping else (fbos["b"], fbos["a"])
            ping = not ping
            p = progs["scene"]
            for k, v in (("res", (float(w), float(h))), ("time", now - t0), ("bass", an.bass), ("mid", an.mid),
                         ("high", an.high), ("kick", an.kick), ("rms", an.rms), ("variant", director.variant),
                         ("hue", (now - t0) * 0.02 + state["scene"] * 0.7 + director.variant * 0.3),
                         ("beats", float(an.beats)), ("scene", state["scene"]),
                         ("fdt", min(4.0, dt * 240.0)),  # frames at the 240 Hz tuning rate; the cap keeps a 50 ms stall from wiping the trails
                         ("travel", travel)):
                if k in p:  # the driver drops uniforms a scene doesn't read
                    p[k].value = v
            spec_tex.use(0); wave_tex.use(1); src.color_attachments[0].use(2)
            p["spec"].value, p["wave"].value, p["prev"].value = 0, 1, 2
            dst.use(); vao["scene"].render(moderngl.TRIANGLES, vertices=3)

            # bloom: threshold + soft knee into a 5-level mip chain, then tent-upsample additively back to m0
            for k in range(5):
                fbos[f"m{k}"].use(); (dst if k == 0 else fbos[f"m{k-1}"]).color_attachments[0].use(0); progs["down"]["tex"].value = 0
                progs["down"]["thresh"].value, progs["down"]["knee"].value = (1.0, 0.5) if k == 0 else (0.0, 0.0)
                vao["down"].render(moderngl.TRIANGLES, vertices=3)
            ctx.enable(moderngl.BLEND); ctx.blend_func = moderngl.ONE, moderngl.ONE
            for k in (3, 2, 1, 0):
                fbos[f"m{k}"].use(); fbos[f"m{k+1}"].color_attachments[0].use(0); progs["up"]["tex"].value = 0
                vao["up"].render(moderngl.TRIANGLES, vertices=3)
            ctx.disable(moderngl.BLEND)  # before the next scene pass, or the feedback runs away

            ctx.screen.use(); ctx.viewport = (0, 0, w, h)
            dst.color_attachments[0].use(0); fbos["m0"].color_attachments[0].use(1)
            c = progs["comp"]
            c["tex"].value, c["bloom"].value, c["time"].value, c["kick"].value = 0, 1, now - t0, an.kick
            vao["comp"].render(moderngl.TRIANGLES, vertices=3)

            # pacer: the driver ignores swap_interval, so present one frame per refresh ourselves, locked to the period
            due += period[0]
            if now > due + period[0]:  # fell behind (hidden, resize, tray menu, mode switch): resync, don't catch up
                due = now + period[0]
            time.sleep(max(0.0, due - 0.001 - time.perf_counter()))  # one high-res sleep; overshoot p99 ~0.95 ms, so 1 ms lead is the knee
            late += time.perf_counter() > due + 0.0002  # missed its slot: work overran or sleep overshot
            while time.perf_counter() < due:
                pass
            glfw.swap_buffers(win)
            glfw.poll_events()
            frames += 1
            if now - fps_t >= 2.0:
                print(f"\r{frames / (now - fps_t):6.1f} fps  late {late}  {w}x{h}  scene {state['scene'] + 1}  "
                      f"v {director.variant:.2f}  bass {an.bass:.2f}  beats {an.beats}", end="", flush=True)
                frames, fps_t, late = 0, now, 0
        if not state["full"] and state["shown"] and not glfw.get_window_attrib(win, glfw.ICONIFIED):
            saved["size"] = list(glfw.get_window_size(win))
        save()
    finally:
        tray.close()
        audio.close()
        glfw.terminate()


def test():
    """100 Hz must light the low bands, read as bass and register a kick; 8 kHz the high bands; peaks hold and fall;
    the AGC holds through silence; the director picks scenes; every program compiles in a hidden window and reads only
    what run() writes; the ICO loads."""
    rate = 48000
    t = np.arange(N_FFT * 2) / rate
    zeros = np.zeros(N_FFT * 2, np.float32)
    an = Analyzer(rate)
    for _ in range(40):
        an.step((0.5 * np.sin(2 * np.pi * 100 * t)).astype(np.float32), 1 / 240, 1.0)
    low, rest = an.bands[14:24].max(), an.bands[28:].max()
    assert low > 0.5 and low > rest * 3, (low, rest)  # 30-200 Hz vs 200 Hz up
    assert an.beats >= 1, "no kick on a sudden 100 Hz tone"
    assert an.bass > 0.5 > an.high, (an.bass, an.high)
    assert (an.peaks >= an.bands).all()
    p0 = an.peaks.max()
    for _ in range(40):
        an.step(zeros, 1 / 240, 1.0)
    assert an.bands.max() < an.peaks.max() < p0, (an.bands.max(), an.peaks.max(), p0)
    an2 = Analyzer(rate)
    for _ in range(40):
        an2.step((0.5 * np.sin(2 * np.pi * 8000 * t)).astype(np.float32), 1 / 240, 1.0)
    assert an2.bands[70:].max() > 0.5 > an2.bands[:30].max(), an2.bands
    assert an2.bass < 0.05, an2.bass
    an3 = Analyzer(rate)
    for _ in range(720):
        an3.step(zeros, 1 / 240, 1.0)
    assert an3.agc_db == 0.0, "the AGC must hold through silence"
    d = Director(0)
    quiet = type("A", (), {"rms": 0.0, "beats": 0})()
    for i in range(400):
        d.step(quiet, i * 0.1, 0.1)
    assert d.scene == 1, "silence should pick SLOWED"
    loud = type("A", (), {"rms": 0.3, "beats": 0})()
    for i in range(400):
        loud.beats = i // 3  # ~3.3 kicks a second
        d.step(loud, 100 + i * 0.1, 0.1)
    assert d.scene in (2, 4), f"dense kicks should pick RAGE or LATTICE, got {d.scene}"
    import glfw, moderngl, tempfile
    assert glfw.init()
    for hint, val in ((glfw.VISIBLE, False), (glfw.CONTEXT_VERSION_MAJOR, 3), (glfw.CONTEXT_VERSION_MINOR, 3), (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)):
        glfw.window_hint(hint, val)
    win = glfw.create_window(64, 64, "vampviz --test", None, None); glfw.make_context_current(win)
    ctx = moderngl.create_context()
    progs = {n: ctx.program(vertex_shader=VERT, fragment_shader=src) for n, src in PROGRAMS}  # raises with the GLSL log on any error
    active = lambda p: {n for n in p if isinstance(p[n], moderngl.Uniform)}
    SCENE_WRITTEN = {"res", "time", "bass", "mid", "high", "kick", "rms", "hue", "beats", "variant", "fdt", "travel", "scene", "spec", "wave", "prev"}  # what run() writes; a dropped one is fine, a stray one is a typo
    assert active(progs["scene"]) <= SCENE_WRITTEN, active(progs["scene"]) - SCENE_WRITTEN
    assert {"spec", "wave", "prev"} <= active(progs["scene"])
    for n, ok in (("comp", {"tex", "bloom", "time", "kick"}), ("down", {"tex", "thresh", "knee"}), ("up", {"tex"})):
        assert active(progs[n]) <= ok, (n, active(progs[n]) - ok)
    ctx.release(); glfw.destroy_window(win); glfw.terminate()
    with tempfile.TemporaryDirectory() as d:
        ico = Path(d) / "vampviz.ico"; write_ico(ico)
        h = user32.LoadImageW(None, str(ico), 1, 0, 0, 0x10); assert h, "vampviz.ico does not load"; user32.DestroyIcon(h)
    print("analyzer ok, director ok, shaders ok, icon ok")


if __name__ == "__main__":
    a = sys.argv
    if "--test" in a:
        test()
    elif "--icon" in a:
        write_ico(HERE / "assets" / "vampviz.ico")
    else:
        if "--quit-after" not in a and already_running():  # a timed dev run stays its own
            if other := user32.FindWindowW(TRAY_CLASS, None):  # bring the running copy back instead of a second one
                user32.PostMessageW(other, WM_SHOW, 0, 0)
            sys.exit(0)
        try:
            run(load_settings(), int(a[a.index("--scene") + 1]) - 1 if "--scene" in a else None)
        except BaseException:  # under pythonw there is no console to see it in (SystemExit carries the glfw message)
            import traceback
            with LOG.open("a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            raise
