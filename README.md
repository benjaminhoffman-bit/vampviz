# VAMPVIZ by SILVADES

A visualizer for whatever your PC is playing: TIDAL, YouTube Music, a game, anything. It hears the speakers through
Windows' own loopback, so there is nothing to route, and draws on the GPU at your monitor's native resolution and
refresh rate (4K at 240 Hz on an OLED looks the way it should).

Four scenes tuned for rage, trap and slowed-and-reverb: black, blood red, neon crimson and violet. In Dynamic mode
the scenes follow the music: quiet or beatless stretches get the smeared SLOWED fluid, dense kicks get the RAGE
tunnel, and every 32 beats or on a drop each scene rolls a new look, so a track never plays the same way twice.

It lives by the clock like a proper desktop app: close or press Esc and it hides to the tray and keeps running,
ready to throw fullscreen on a second monitor while you work on the first.

| Key | Does |
|-----|------|
| 1 to 4 | Pick a scene: VAMP ring, SLOWED fluid, RAGE tunnel, BARS |
| Space | Dynamic mode on or off (scenes follow the music) |
| F | Fullscreen on or off |
| M | Move to the next monitor |
| Esc | Hide to the tray icon by the clock (left-click it to bring it back) |
| Ctrl+Q | Quit |

Right-click the tray icon for all of the above plus the monitor list and Quit. It remembers your scene, Dynamic,
fullscreen, monitor and window size.

## Install

Press **Win+R**, paste this and press Enter:

```
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/benjaminhoffman-bit/vampviz/main/install.ps1 | iex"
```

Or click **Code**, then **Download ZIP**, on this page, extract it, and double-click `Install.cmd`.

It gets Python if this PC has none (no admin), installs the four libraries it needs, puts VAMPVIZ in
`%LOCALAPPDATA%\VAMPVIZ`, and adds Start menu and desktop shortcuts. Open it once, right-click its taskbar button and
choose Pin to taskbar to keep it there. Run the installer again to update.

Uninstall: the "Uninstall VAMPVIZ" shortcut in the Start menu, or `Install.cmd -Uninstall`.

Needs Windows 10 or 11 and a GPU with OpenGL 3.3 (anything from the last decade).

## Dev

```
python vampviz.pyw --test        analyzer and director self-check
python vampviz.pyw --scene 3     start on a scene
python vampviz.pyw --icon        redraw assets\vampviz.ico
```

Everything is in `vampviz.pyw`: the WASAPI loopback capture, the FFT analyzer, the director that picks scenes,
the tray icon, the GLSL scenes, bloom and post.
