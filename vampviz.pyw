"""VAMPVIZ: a desktop-audio visualizer. Hears whatever Windows is playing (WASAPI loopback), renders on the GPU
at the monitor's native mode (4K 240 Hz here) with vsync, so one frame per screen refresh.

    vampviz.pyw                  opens the way you left it (window or fullscreen, last scene)
    python vampviz.pyw --scene 3 start on scene 3
    python vampviz.pyw --icon    write assetsampviz.ico (the installer's shortcut icon)
    python vampviz.pyw --test    self-check of the analyzer, no window

Keys: 1-4 scene, Space dynamic mode (scenes follow the music), F fullscreen, M next monitor, Esc hides it to the tray
icon by the clock, Ctrl+Q quits. The tray icon: left-click shows or hides, right-click for scenes, monitor, fullscreen
and quit. Settings live in vampviz.json next to this file; start-up errors go to vampviz.log (no console under pythonw).
Scenes: 1 VAMP (kaleidoscope spectrum ring), 2 SLOWED (smeared fluid), 3 RAGE (strobing tunnel), 4 BARS (mirrored spectrum).
"""
import ctypes
import json
import math
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SETTINGS, LOG, APP_ID = HERE / "vampviz.json", HERE / "vampviz.log", "SilvaDes.VAMPVIZ"
PNG_MAGIC, PNG_ROW_PREFIX = bytes([0x89]) + b"PNG\r\n" + bytes([0x1A]) + b"\n", bytes([0])  # filter 0 per row


def icon(size: int = 256) -> np.ndarray:
    """The app icon as RGBA: a crimson ring with a violet halo on black, like scene 1. Drawn, so no image file."""
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    uv = (np.stack([x, y], -1) + 0.5) / size - 0.5
    r = np.sqrt((uv ** 2).sum(-1))
    ring = np.exp(-((r - 0.30) / 0.045) ** 2)
    halo = np.exp(-((r - 0.40) / 0.06) ** 2) * 0.7
    core = np.clip(1 - r / 0.22, 0, 1) ** 2
    rgb = (np.array([1.0, 0.10, 0.28]) * (ring + core * 0.9)[..., None]
           + np.array([0.45, 0.0, 1.0]) * halo[..., None] + np.array([0.08, 0.0, 0.03]) * core[..., None])
    alpha = np.clip(r < 0.48, 0, 1) * np.clip((0.48 - r) * size, 0, 1)
    img = np.concatenate([np.clip(rgb, 0, 1), alpha[..., None]], -1)
    return (img * 255 + 0.5).astype(np.uint8)


def write_ico(path: Path):
    """ICO holding PNGs (Vista and later): one chunk helper, zlib, no imaging library."""
    def png(a: np.ndarray) -> bytes:
        def chunk(tag, data): return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
        raw = b"".join(PNG_ROW_PREFIX + row.tobytes() for row in a)
        return (PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", a.shape[1], a.shape[0], 8, 6, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [png(icon(s)) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    offset, entries = 6 + 16 * len(sizes), b""
    for s, data in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(head + entries + b"".join(images))


def load_settings() -> dict:
    s = {"scene": 0, "auto": True, "fullscreen": False, "size": [1920, 1080]}
    try:
        s.update(json.loads(SETTINGS.read_text("utf-8")))
    except (OSError, ValueError):
        pass
    return s

# ─── audio ──────────────────────────────────────────────────────────────────────────────────────────────────────────
N_FFT, N_BANDS, N_WAVE = 4096, 96, 2048


class Loopback:
    """Default speakers' mix as mono float32 in a ring buffer. Thread-safe reads of the latest N samples."""

    def __init__(self):
        import pyaudiowpatch as pa
        self.pa = pa.PyAudio()
        api = self.pa.get_host_api_info_by_type(pa.paWASAPI)
        dev = self.pa.get_device_info_by_index(api["defaultOutputDevice"])
        if not dev["isLoopbackDevice"]:
            dev = next(d for d in self.pa.get_loopback_device_info_generator() if dev["name"] in d["name"])
        self.rate, self.channels = int(dev["defaultSampleRate"]), int(dev["maxInputChannels"])
        self.name = dev["name"]
        self.ring = np.zeros(self.rate, np.float32)  # one second
        self.pos, self.lock, self.last = 0, threading.Lock(), time.perf_counter()
        self.stream = self.pa.open(format=pa.paFloat32, channels=self.channels, rate=self.rate, input=True,
                                   input_device_index=dev["index"], frames_per_buffer=480, stream_callback=self._cb)

    def _cb(self, data, frames, _t, _s):
        mono = np.frombuffer(data, np.float32).reshape(-1, self.channels).mean(1)
        with self.lock:
            idx = (self.pos + np.arange(len(mono))) % len(self.ring)
            self.ring[idx] = mono
            self.pos = (self.pos + len(mono)) % len(self.ring)
            self.last = time.perf_counter()
        return None, 0

    def latest(self, n: int) -> np.ndarray:
        with self.lock:
            if time.perf_counter() - self.last > 0.15:  # loopback goes mute in silence; feed zeros so visuals decay
                self.ring[:] = 0
            idx = (self.pos - n + np.arange(n)) % len(self.ring)
            return self.ring[idx]

    def close(self):
        self.stream.stop_stream(); self.stream.close(); self.pa.terminate()


class Analyzer:
    """FFT → log-spaced bands with attack/release smoothing, AGC, bass/mid/high energy and kick detection."""

    def __init__(self, rate: int):
        self.rate = rate
        self.window = np.hanning(N_FFT).astype(np.float32)
        freqs = np.fft.rfftfreq(N_FFT, 1 / rate)
        edges = np.geomspace(35, min(16000, rate / 2 - 1), N_BANDS + 1)
        idx = np.searchsorted(freqs, edges)
        idx = np.maximum(idx, np.arange(N_BANDS + 1) + idx[0])  # strictly increasing, one bin per band minimum
        self.starts = idx[:-1]
        self.kick_lo, self.kick_hi = np.searchsorted(freqs, 40), np.searchsorted(freqs, 130)
        self.bands = np.zeros(N_BANDS, np.float32)
        self.gain, self.kick_avg, self.kick, self.last_kick, self.beats = 1.0, 1e-3, 0.0, 0.0, 0
        self.bass = self.mid = self.high = self.rms = 0.0

    def step(self, samples: np.ndarray, dt: float, now: float):
        spec = np.abs(np.fft.rfft(samples[-N_FFT:] * self.window)) * (2 / N_FFT)
        raw = np.maximum.reduceat(spec, self.starts)[:N_BANDS]  # peak per band: highs stay alive
        db = 20 * np.log10(raw + 1e-9)
        level = np.clip((db + 65) / 60, 0, 1) ** 1.3  # -65 dBFS floor
        peak = level.max()
        self.gain = max(0.5, min(2.0, self.gain + (0.7 / max(peak, 1e-3) - self.gain) * (1 - math.exp(-dt / 3.0))))
        level = np.clip(level * self.gain, 0, 1)
        up, down = 1 - math.exp(-dt / 0.015), 1 - math.exp(-dt / 0.14)
        self.bands += (level - self.bands) * np.where(level > self.bands, up, down)
        b = self.bands
        self.bass, self.mid, self.high = float(b[:14].mean()), float(b[30:60].mean()), float(b[70:].mean())
        self.rms = float(np.sqrt(np.mean(samples[-2048:] ** 2)))
        # kick: energy flux in 40-130 Hz against its own recent average
        e = float(spec[self.kick_lo:self.kick_hi].mean())
        self.kick_avg += (e - self.kick_avg) * (1 - math.exp(-dt / 0.6))
        if e > self.kick_avg * 1.5 and e > 2e-4 and now - self.last_kick > 0.15:
            self.kick, self.last_kick, self.beats = 1.0, now, self.beats + 1
        self.kick *= math.exp(-dt / 0.16)


# ─── shaders ────────────────────────────────────────────────────────────────────────────────────────────────────────
VERT = """#version 330
out vec2 st;
void main(){ vec2 v = vec2((gl_VertexID<<1)&2, gl_VertexID&2); st = v; gl_Position = vec4(v*2.0-1.0, 0.0, 1.0); }"""

COMMON = """#version 330
uniform vec2 res; uniform float time, bass, mid, high, kick, rms, hue, beats, variant; uniform int scene;
uniform sampler2D spec, wave, prev;
in vec2 st; out vec4 frag;
#define PI 3.14159265
const float N = __N__;
float spectrum(float x){ return texture(spec, vec2(clamp(x,0.0,1.0), 0.5)).r; }
float waveform(float x){ return texture(wave, vec2(fract(x), 0.5)).r; }
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
float noise(vec2 p){ vec2 i=floor(p), f=fract(p); f=f*f*(3.0-2.0*f);
  return mix(mix(hash(i),hash(i+vec2(1,0)),f.x), mix(hash(i+vec2(0,1)),hash(i+vec2(1,1)),f.x), f.y); }
float fbm(vec2 p){ float v=0.0, a=0.5; mat2 m=mat2(1.6,1.2,-1.2,1.6); for(int i=0;i<5;i++){ v+=a*noise(p); p=m*p; a*=0.5; } return v; }
// vamp palette: void → dried blood → neon crimson → violet
vec3 pal(float t){ t=fract(t);
  vec3 a=vec3(0.03,0.0,0.05), b=vec3(0.55,0.0,0.10), c=vec3(1.0,0.10,0.28), d=vec3(0.45,0.0,1.0);
  return t<0.33 ? mix(a,b,t/0.33) : t<0.66 ? mix(b,c,(t-0.33)/0.33) : mix(c,d,(t-0.66)/0.34); }
vec3 hueShift(vec3 c, float h){ const vec3 k=vec3(0.57735); float cs=cos(h), sn=sin(h);
  return c*cs + cross(k,c)*sn + k*dot(k,c)*(1.0-cs); }
mat2 rot(float a){ float c=cos(a), s=sin(a); return mat2(c,-s,s,c); }
vec3 feedback(float zoom, float spin, float decay, vec2 warp){
  vec2 c = (st-0.5)*zoom; c = rot(spin)*c + warp;
  return max(texture(prev, c+0.5).rgb*decay - 0.012, 0.0); }  // floor subtract: trails die, black stays black
"""

SCENE = COMMON.replace("__N__", f"{N_BANDS}.0") + """
vec3 vamp(vec2 uv){ // kaleidoscope spectrum ring
  float r=length(uv), a=atan(uv.y,uv.x);
  float seg=floor(3.0+variant*6.0); float spin=(variant-0.5)*0.5;                   // 3..8 mirrors, either way
  float sa=mod(a+time*spin, 2.0*PI/seg); sa=abs(sa-PI/seg); float fx=sa/(PI/seg);
  float s=spectrum(fx*0.85+0.02);
  float ring=0.14+variant*0.12+bass*0.07+kick*0.02, d=r-ring, bar=s*(0.18+variant*0.16);
  float inside=smoothstep(bar, bar-0.012, d)*step(0.0,d);
  float line=0.0035/(abs(d)+0.0035);
  vec3 col=pal(fx*0.45+s*0.3+hue)*(inside*(0.35+0.6*s)+line*0.4*(0.4+bass));
  col+=vec3(1.0,0.08,0.22)*pow(max(0.0,1.0-r/(ring*0.92)),3.0)*(0.25+kick*1.6);  // beating core
  float wr=0.52+waveform(a/(2.0*PI)+0.5)*0.07*(0.4+rms*4.0);                       // waveform halo
  col+=pal(0.85+hue)*0.0025/(abs(r-wr)+0.0025)*0.9;
  float smoke=fbm(uv*2.5+vec2(time*0.05,-time*0.03))*0.5;                           // void smoke
  col+=vec3(0.10,0.0,0.03)*smoke*(0.2+bass)*smoothstep(ring,ring+0.5,r);
  return col*0.8+feedback(0.986, 0.004*(1.0+bass*2.0), 0.80, vec2(0.0));
}
vec3 slowed(vec2 uv){ // domain-warped fluid, heavy smear
  vec2 p=uv*(1.2+variant*1.4); float t=time*(0.06+variant*0.14);
  float n1=fbm(p+vec2(t,-t*0.7)), n2=fbm(p*1.7+vec2(n1*2.0,t*0.5));
  float n=fbm(p+3.0*vec2(n1,n2)*(0.5+bass*0.8));
  float s=spectrum(fract(n*1.4+bass*0.2));
  vec3 col=pal(n*0.9+hue+0.45)*(0.12+1.1*s);
  col+=vec3(0.55,0.08,1.0)*mid*pow(n2,3.0)*1.2;
  float wy=waveform(st.x*0.5+time*0.02)*0.18*(0.5+rms*3.0)+sin(time*0.3)*0.1;    // drifting waveform
  col+=pal(0.9+hue)*0.004/(abs(uv.y-wy)+0.004)*0.6;
  vec2 warp=vec2(fbm(uv*3.0+time*0.2)-0.5, fbm(uv*3.0-time*0.17)-0.5)*0.004;
  return col*0.22+feedback(0.993, 0.0015, 0.90, warp);
}
vec3 rage(vec2 uv){ // tunnel, strobing on kicks
  uv+=vec2(hash(vec2(time,1.0)),hash(vec2(2.0,time)))*kick*0.01;                    // shake
  float r=length(uv), a=atan(uv.y,uv.x);
  float z=0.25/(r+0.02)+time*(0.6+variant*1.5+rms*8.0)+beats*0.5;
  float ang=a/(2.0*PI)+0.5+time*0.03;
  float s=spectrum(abs(fract(ang*2.0)*2.0-1.0)*0.9);
  float rings=smoothstep(0.08,0.0,abs(fract(z)-0.5)-0.42);
  float walls=floor(12.0+variant*40.0);
  float wall=smoothstep(0.2,0.0,abs(fract(ang*walls)-0.5)-0.45*(0.1+s*0.6));
  vec3 col=pal(fract(z*0.04)+hue)*(wall*s*1.2+rings*0.25*(0.15+s))*clamp(r*3.0,0.0,1.0);
  col+=vec3(1.0,0.03,0.08)*kick*kick*0.7;                                           // strobe
  col+=vec3(0.9,0.0,0.2)*pow(max(0.0,1.0-r*4.0),2.0)*bass;
  return col+feedback(0.97, 0.0, 0.70, vec2(0.0));
}
vec3 bars(vec2 uv){ // mirrored spectrum bars with reflection
  float x=variant<0.5 ? abs(st.x*2.0-1.0) : st.x;                                   // mirrored, or left-to-right
  float bi=floor(x*N)/N; float s=spectrum(bi+0.5/N);
  float inBar=step(fract(x*N),0.78);
  float h=s*0.6+0.01, y=uv.y;
  float bar=inBar*step(abs(y),h*0.5);
  float glow=inBar*0.012/(abs(abs(y)-h*0.5)+0.012)*s;
  vec3 col=pal(bi*0.7+hue)*(bar*(0.5+s*1.2)+glow);
  col*=mix(0.35,1.0,step(0.0,y));                                                   // dim reflection below
  float wy=waveform(st.x)*0.2*(0.5+rms*3.0);
  col+=pal(0.9+hue)*0.003/(abs(y-wy)+0.003);
  col+=vec3(0.3,0.0,0.08)*kick*(1.0-abs(y)*2.0);
  return col+feedback(0.998, 0.0, 0.70, vec2(0.0,-0.002));
}
void main(){
  vec2 uv=(st-0.5)*vec2(res.x/res.y,1.0);
  vec3 col = scene==0 ? vamp(uv) : scene==1 ? slowed(uv) : scene==2 ? rage(uv) : bars(uv);
  frag=vec4(max(col,0.0),1.0);
}"""

BLOOM_DOWN = """#version 330
uniform sampler2D tex; in vec2 st; out vec4 frag;
void main(){ vec3 c=texture(tex,st).rgb; frag=vec4(max(c-0.85,0.0),1.0); }"""

BLUR = """#version 330
uniform sampler2D tex; uniform vec2 dir; in vec2 st; out vec4 frag;
void main(){ vec2 px=dir/vec2(textureSize(tex,0)); vec3 c=vec3(0.0);
  float w[5]=float[](0.227,0.194,0.121,0.054,0.016);
  c+=texture(tex,st).rgb*w[0];
  for(int i=1;i<5;i++){ c+=texture(tex,st+px*float(i)*1.5).rgb*w[i]; c+=texture(tex,st-px*float(i)*1.5).rgb*w[i]; }
  frag=vec4(c,1.0); }"""

COMPOSITE = """#version 330
uniform sampler2D tex, bloom; uniform float time, kick, hue; in vec2 st; out vec4 frag;
float hash(vec2 p){ return fract(sin(dot(p, vec2(127.1,311.7)))*43758.5453); }
vec3 hueShift(vec3 c, float h){ const vec3 k=vec3(0.57735); float cs=cos(h), sn=sin(h);
  return c*cs + cross(k,c)*sn + k*dot(k,c)*(1.0-cs); }
void main(){
  vec2 d=(st-0.5)*(0.0025+kick*0.012);                                   // chromatic aberration, pumps on kicks
  vec3 c=vec3(texture(tex,st+d).r, texture(tex,st).g, texture(tex,st-d).b);
  c+=texture(bloom,st).rgb*0.6;
  c=hueShift(c, hue*0.35);
  c=c/(1.0+c)*1.15;                                                       // tonemap
  c*=1.0-0.55*pow(length(st-0.5),2.2);                                    // vignette
  c+=(hash(st*vec2(1920.0,1080.0)+fract(time))-0.5)*0.02;               // grain
  frag=vec4(pow(max(c,0.0),vec3(1.0/2.2)),1.0);
}"""


# ─── tray ───────────────────────────────────────────────────────────────────────────────────────────────────────────
from ctypes import wintypes as w  # noqa: E402

user32, shell32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("shell32"), ctypes.WinDLL("kernel32")
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
TRAY_CLASS, WM_TRAY, WM_SHOW, WM_CLOSE = "VampVizTray", 0x8001, 0x8002, 0x10
WM_LBUTTONUP, WM_RBUTTONUP = 0x202, 0x205
MF_CHECKED, MF_SEPARATOR, TPM_RETURNCMD = 0x8, 0x800, 0x100
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0, 1, 2, 1, 2, 4, 0x10
ICO = HERE / "assets" / "vampviz.ico"
SCENES = ("VAMP", "SLOWED", "RAGE", "BARS")


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
                      (kernel32.GetModuleHandleW, [w.LPCWSTR], w.HMODULE),
                      (shell32.Shell_NotifyIconW, [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)], w.BOOL)):
    fn.argtypes, fn.restype = args, res


class Tray:
    """The icon by the clock. Left-click shows or hides the window; right-click is the menu. Its messages arrive through
    glfw.poll_events (same thread), so `act(name, arg)` runs inside the render loop."""

    def __init__(self, act, state: dict, monitors: list[str]):
        self.act, self.state, self.monitors = act, state, monitors
        self.proc = WNDPROC(self.handle)  # kept: Windows calls it as long as the window lives
        self.explorer_restarted = user32.RegisterWindowMessageW("TaskbarCreated")
        module = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=module, lpszClassName=TRAY_CLASS)))
        self.hwnd = user32.CreateWindowExW(0, TRAY_CLASS, "", 0, 0, 0, 0, 0, None, None, module, None)  # no title: the main window owns "VAMPVIZ"
        if not ICO.exists():
            write_ico(ICO)
        size = user32.GetSystemMetrics(49)  # SM_CXSMICON
        self.icon = user32.LoadImageW(None, str(ICO), 1, size, size, 0x10)
        self.data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                    uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY,
                                    hIcon=self.icon, szTip="VAMPVIZ")
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.data))

    def handle(self, hwnd, msg, wparam, lparam) -> int:
        if msg == WM_TRAY and lparam == WM_LBUTTONUP:
            self.act("toggle", None)
        elif msg == WM_TRAY and lparam == WM_RBUTTONUP:
            self.menu()
        elif msg == WM_SHOW:  # a second copy started: it hands over to us
            self.act("show", None)
        elif msg == WM_CLOSE:  # the installer, before it updates or removes us
            self.act("quit", None)
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
        user32.DestroyWindow(self.hwnd)
        user32.DestroyIcon(self.icon)


class Director:
    """Dynamic mode: picks the scene from how the music feels and rolls a new look each section.
    Quiet or beatless → SLOWED. Dense kicks and loud → RAGE. Otherwise VAMP and BARS take turns.
    A section is 32 beats, 30 s, or a drop (loudness jumping well above its recent average)."""

    def __init__(self, scene: int):
        self.scene, self.variant, self.section_beat, self.section_t = scene, 0.37, 0, 0.0
        self.loud, self.kicks, self.last_beats, self.hold_until = 0.0, 0.0, 0, 0.0

    def step(self, an, now: float, dt: float) -> None:
        a = 1 - math.exp(-dt / 2.5)
        self.loud += (an.rms - self.loud) * a
        self.kicks += ((an.beats - self.last_beats) / max(dt, 1e-3) - self.kicks) * a  # kicks per second
        self.last_beats = an.beats
        drop = an.rms > self.loud * 2.2 and an.rms > 0.08 and now > self.hold_until
        if drop or an.beats - self.section_beat >= 32 or now - self.section_t > 30:
            self.section_beat, self.section_t, self.hold_until = an.beats, now, now + 8.0
            self.variant = (self.variant * 7.31 + 0.173) % 1.0  # a new, deterministic look
            if self.loud < 0.02 or self.kicks < 0.8:
                want = 1
            elif self.kicks > 2.2 and self.loud > 0.12:
                want = 2
            else:
                want = 0 if self.scene != 0 else 3
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
    state = {"scene": saved["scene"] if scene is None else scene, "auto": saved["auto"], "full": saved["fullscreen"],
             "monitor": min(saved.get("monitor", 0), len(monitors) - 1), "shown": True, "quit": False,
             "tip": saved.get("tip", False)}
    mon = monitors[state["monitor"]]
    mode = glfw.get_video_mode(mon)
    for hint, val in ((glfw.CONTEXT_VERSION_MAJOR, 3), (glfw.CONTEXT_VERSION_MINOR, 3),
                      (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE), (glfw.REFRESH_RATE, mode.refresh_rate),
                      (glfw.AUTO_ICONIFY, False),  # fullscreen on a second monitor stays up while you work on the first
                      (glfw.RED_BITS, mode.bits.red), (glfw.GREEN_BITS, mode.bits.green), (glfw.BLUE_BITS, mode.bits.blue)):
        glfw.window_hint(hint, val)
    size = mode.size if state["full"] else tuple(saved["size"])
    win = glfw.create_window(*size, "VAMPVIZ", mon if state["full"] else None, None)
    ico = icon(64)
    glfw.set_window_icon(win, 1, [(ico.shape[1], ico.shape[0], ico.tolist())])
    glfw.make_context_current(win)
    glfw.swap_interval(1)  # one frame per refresh, when the driver honours it
    ctx = moderngl.create_context()
    period = [1.0 / mode.refresh_rate]
    ctypes.WinDLL("winmm").timeBeginPeriod(1)  # 1 ms sleep granularity for the pacer below

    progs = {name: ctx.program(vertex_shader=VERT, fragment_shader=src)
             for name, src in (("scene", SCENE), ("down", BLOOM_DOWN), ("blur", BLUR), ("comp", COMPOSITE))}
    vao = {k: ctx.vertex_array(p, []) for k, p in progs.items()}
    spec_tex = ctx.texture((N_BANDS, 1), 1, dtype="f4"); spec_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    wave_tex = ctx.texture((N_WAVE, 1), 1, dtype="f4"); wave_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    spec_tex.repeat_x = wave_tex.repeat_x = False
    fbos = {}

    def make_fbos(w, h):
        for f in fbos.values():
            f.color_attachments[0].release(); f.release()  # texture first: release() drops the attachment list
        for name, (fw, fh) in (("a", (w, h)), ("b", (w, h)), ("ba", (w // 4, h // 4)), ("bb", (w // 4, h // 4))):
            t = ctx.texture((fw, fh), 4, dtype="f2"); t.filter = (moderngl.LINEAR, moderngl.LINEAR)
            t.repeat_x = t.repeat_y = False
            fbos[name] = ctx.framebuffer(color_attachments=[t])
            fbos[name].clear()

    state["size"] = glfw.get_framebuffer_size(win)
    make_fbos(*state["size"])
    director = Director(state["scene"])

    def place():
        """Fullscreen on the chosen monitor, or back to a window; the pacer follows that monitor's refresh rate."""
        m = monitors[state["monitor"]]
        vm = glfw.get_video_mode(m)
        if state["full"]:
            glfw.set_window_monitor(win, m, 0, 0, vm.size.width, vm.size.height, vm.refresh_rate)
        else:
            x, y = glfw.get_monitor_pos(m)
            glfw.set_window_monitor(win, None, x + 100, y + 100, *saved["size"], 0)
        period[0] = 1.0 / vm.refresh_rate
        glfw.swap_interval(1)

    def act(name: str, arg):
        if name == "toggle":
            name = "hide" if state["shown"] else "show"
        if name == "show":
            state["shown"] = True; glfw.show_window(win); glfw.focus_window(win)
        elif name == "hide":
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

    def on_key(_w, key, _sc, action, mods):
        if action != glfw.PRESS:
            return
        if key == glfw.KEY_ESCAPE:
            act("hide", None)
        elif key == glfw.KEY_Q and mods & glfw.MOD_CONTROL:
            act("quit", None)
        elif glfw.KEY_1 <= key <= glfw.KEY_4:
            act("scene", key - glfw.KEY_1)
        elif key == glfw.KEY_SPACE:
            act("auto", None)
        elif key == glfw.KEY_F:
            act("fullscreen", None)
        elif key == glfw.KEY_M:  # next monitor
            act("monitor", (state["monitor"] + 1) % len(monitors))
    glfw.set_key_callback(win, on_key)
    glfw.set_window_close_callback(win, lambda _w: (glfw.set_window_should_close(win, False), act("hide", None)))
    tray = Tray(act, state, names)

    audio = Loopback()
    an = Analyzer(audio.rate)
    print(f"hearing: {audio.name} @ {audio.rate} Hz | {ctx.info['GL_RENDERER']} | {names[state['monitor']]}")
    print("1-4 scene  Space dynamic  F fullscreen  M next monitor  Esc hide to tray  Ctrl+Q quit")

    t0 = last = time.perf_counter()
    frames, fps_t = 0, t0
    ping = True
    quit_at = float(a[a.index("--quit-after") + 1]) + t0 if "--quit-after" in (a := sys.argv) else None
    while not state["quit"]:
        now = time.perf_counter()
        if quit_at and now > quit_at:
            state["quit"] = True
        dt, last = min(now - last, 0.05), now
        if not state["shown"]:  # hidden: no GPU work, just keep the tray and audio alive
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
        if state["auto"]:
            state["scene"] = director.scene
        spec_tex.write(an.bands.astype("f4").tobytes())
        wave_tex.write(np.clip(samples[-N_WAVE:] * an.gain * 1.5, -1, 1).astype("f4").tobytes())

        src, dst = (fbos["a"], fbos["b"]) if ping else (fbos["b"], fbos["a"])
        ping = not ping
        p = progs["scene"]
        for k, v in (("res", (float(w), float(h))), ("time", now - t0), ("bass", an.bass), ("mid", an.mid),
                     ("high", an.high), ("kick", an.kick), ("rms", an.rms), ("variant", director.variant),
                     ("hue", (now - t0) * 0.02 + state["scene"] * 0.7 + director.variant * 0.3),
                     ("beats", float(an.beats)), ("scene", state["scene"])):
            if k in p:  # the driver drops uniforms a scene doesn't read
                p[k].value = v
        spec_tex.use(0); wave_tex.use(1); src.color_attachments[0].use(2)
        p["spec"].value, p["wave"].value, p["prev"].value = 0, 1, 2
        dst.use(); vao["scene"].render(moderngl.TRIANGLES, vertices=3)

        fbos["ba"].use(); dst.color_attachments[0].use(0); progs["down"]["tex"].value = 0
        vao["down"].render(moderngl.TRIANGLES, vertices=3)
        for a, b, d in (("ba", "bb", (1.0, 0.0)), ("bb", "ba", (0.0, 1.0)), ("ba", "bb", (1.0, 0.0)), ("bb", "ba", (0.0, 1.0))):
            fbos[b].use(); fbos[a].color_attachments[0].use(0)
            progs["blur"]["tex"].value, progs["blur"]["dir"].value = 0, d
            vao["blur"].render(moderngl.TRIANGLES, vertices=3)

        ctx.screen.use(); ctx.viewport = (0, 0, w, h)
        dst.color_attachments[0].use(0); fbos["ba"].color_attachments[0].use(1)
        c = progs["comp"]
        c["tex"].value, c["bloom"].value, c["time"].value, c["kick"].value = 0, 1, now - t0, an.kick
        c["hue"].value = (now - t0) * 0.02
        vao["comp"].render(moderngl.TRIANGLES, vertices=3)

        # pacer: if the driver ignores vsync, still present exactly one frame per refresh
        due = now + period[0] - 0.0002
        while time.perf_counter() < due - 0.0015:
            time.sleep(0.0005)
        while time.perf_counter() < due:
            pass
        glfw.swap_buffers(win)
        glfw.poll_events()
        frames += 1
        if now - fps_t >= 2.0:
            print(f"\r{frames / (now - fps_t):6.1f} fps  {w}x{h}  scene {state['scene'] + 1}  v {director.variant:.2f}  "
                  f"bass {an.bass:.2f}  beats {an.beats}", end="", flush=True)
            frames, fps_t = 0, now
    if not state["full"] and state["shown"]:
        saved["size"] = list(glfw.get_window_size(win))
    saved.update(scene=state["scene"], auto=state["auto"], fullscreen=state["full"], monitor=state["monitor"], tip=state["tip"])
    SETTINGS.write_text(json.dumps(saved, indent=1), "utf-8")
    tray.close()
    audio.close()
    glfw.terminate()


def test():
    """A 100 Hz tone must light the lowest bands and a kick must register; 8 kHz must light the high bands."""
    rate = 48000
    t = np.arange(N_FFT * 2) / rate
    an = Analyzer(rate)
    for _ in range(40):
        an.step((0.5 * np.sin(2 * np.pi * 100 * t)).astype(np.float32), 1 / 240, 1.0)
    low, rest = an.bands[:12].max(), an.bands[30:].max()
    assert low > 0.5 and low > rest * 3, (low, rest)
    assert an.beats >= 1, "no kick on a sudden 100 Hz tone"
    an2 = Analyzer(rate)
    for _ in range(40):
        an2.step((0.5 * np.sin(2 * np.pi * 8000 * t)).astype(np.float32), 1 / 240, 1.0)
    assert an2.bands[70:].max() > 0.5 > an2.bands[:30].max(), an2.bands
    d = Director(0)
    quiet = type("A", (), {"rms": 0.0, "beats": 0})()
    for i in range(400):
        d.step(quiet, i * 0.1, 0.1)
    assert d.scene == 1, "silence should pick SLOWED"
    loud = type("A", (), {"rms": 0.3, "beats": 0})()
    for i in range(400):
        loud.beats = i // 3  # ~3.3 kicks a second
        d.step(loud, 100 + i * 0.1, 0.1)
    assert d.scene == 2, f"dense kicks should pick RAGE, got {d.scene}"
    print("analyzer ok, director ok")


if __name__ == "__main__":
    a = sys.argv
    if "--test" in a:
        test()
    elif "--icon" in a:
        write_ico(HERE / "assets" / "vampviz.ico")
    else:
        running = user32.FindWindowW(TRAY_CLASS, None)
        if running:  # already open: bring that one back instead of a second copy
            user32.PostMessageW(running, WM_SHOW, 0, 0)
            sys.exit(0)
        try:
            run(load_settings(), int(a[a.index("--scene") + 1]) - 1 if "--scene" in a else None)
        except Exception:  # under pythonw there is no console to see it in
            import traceback
            LOG.write_text(traceback.format_exc(), "utf-8")
            raise
