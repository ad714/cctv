# CCTV

A lightweight desktop client for Hikvision DVRs. Live grid, recorded playback with a
zoomable timeline, motion markers driven by the DVR's own human/vehicle classifier,
and clip export.

Built and tested against a **DS-7108HGHI-M1/T** (8-channel Turbo HD DVR, H.265, 6 analog
cameras). It talks plain ISAPI and RTSP, so other Hikvision DVRs and NVRs are likely to
work, but only this model has actually been verified.

![status](https://img.shields.io/badge/platform-Windows-blue) ![status](https://img.shields.io/badge/status-v1-green)

---

## What it does

**Live tab**
- All cameras in a grid, click any tile to expand it to full resolution
- Per-camera audio (one at a time), snapshot, and record-to-file
- Sub-streams in the grid, main stream when expanded, so six cameras cost very little
- Expanded and playback streams use **D3D11VA hardware decode** (about 40% less CPU),
  falling back to software automatically if the GPU or driver cannot do it

**Playback tab**
- Calendar showing which days have footage
- Timeline of the day: green where footage exists, orange where motion was detected
- Zoom from the full 24 hours down to a 30-second window
- `<< Motion` / `Motion >>` jump straight between motion events and skip the empty hours
- Drag a range on the timeline and export it as an MP4
- Playback audio

**Motion index**
- The DVR classifies motion as `human` or `vehicle` on-device; this app just records it
- Bursts are coalesced into episodes, so one person walking past is one marker, not fifty
- Stored in SQLite, no video decoding and no inference, so it costs almost nothing

---

## Requirements

- Windows 10 or 11
- **FFmpeg** on `PATH` (provides `ffmpeg` and `ffplay`)
  ```
  winget install Gyan.FFmpeg
  ```
- A Hikvision DVR/NVR reachable on the network, with ISAPI and RTSP enabled

For running from source you also need Python 3.10+. The only third-party dependency is
PySide6; everything else is the standard library.

---

## Install (prebuilt)

1. Download the release, or build it yourself (below)
2. Run `CCTV.exe`
3. Enter the DVR address, username and password on first launch

Optional: create a Desktop shortcut and pin it to the taskbar

```
python tools\shortcut.py
```

---

## Run from source

```
git clone https://github.com/ad714/cctv.git
cd cctv
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe app.py
```

## Build the executable

```
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe build.py
```

Output lands in `dist\CCTV\CCTV.exe` (about 69 MB, folder is self-contained apart from
FFmpeg). The build prunes Qt payload the app never uses: the software OpenGL fallback,
Qt's translations, and Qt's own OpenSSL.

---

## Credentials

On first run you are asked for the DVR address, username and password.

Credentials are **not** stored in the repo or next to the executable. They go to:

```
%LOCALAPPDATA%\HikViewer\config.json
```

The password is encrypted with **Windows DPAPI**, tied to your Windows user account, so
another account on the same machine cannot read it. To forget the saved login, delete
that file.

If a legacy `.hikenv` file is present next to the app on first run, it is imported once
and then the encrypted config is used.

---

## Where things are saved

| What | Where |
|---|---|
| Credentials | `%LOCALAPPDATA%\HikViewer\config.json` |
| Motion index | `%LOCALAPPDATA%\HikViewer\events.db` (pruned past 180 days) |
| Logs | `%LOCALAPPDATA%\HikViewer\cctv.log`, `crash.log` |
| Snapshots, recordings, exported clips | `%USERPROFILE%\Videos\CCTV` |

These live outside the app folder on purpose, so rebuilding or replacing the app never
destroys your history or your clips.

---

## Behaviour worth knowing

**Closing the window does not quit.** It hides to the system tray and keeps recording
motion. Quit from the tray icon's menu.

**Only one instance runs.** Launching it again raises the existing window.

**Playback runs at 1x.** The DVR streams recorded footage at real time and offers no
faster transfer, so exporting a 10-minute clip takes 10 minutes. Export runs in the
background and the window stays usable. This is a limitation of the DVR, not the client.

**The motion index only fills while the app is running.** The DVR does not keep a
searchable motion log, and `ContentMgmt/SmartSearch` is unsupported on this model
(it returns `notSupport`), so footage recorded before you first ran the app cannot be
given markers retroactively. Leave the app running and each day gets indexed as it
happens.

---

## Notes on this DVR family

Findings from the DS-7108HGHI-M1/T that shaped the code, kept here because they are easy
to lose and hard to rediscover:

- Streams are **H.265**, main stream `960x1088` with a **4:3** display aspect, so frames
  must be rescaled or they look vertically stretched
- Audio is **G.711 µ-law**, which cannot be muxed into MP4 — exports transcode to AAC
- Playback RTSP **never signals end of stream**, so `ffmpeg` needs an explicit `-t`
  duration or it hangs forever
- `searchID` in `ContentMgmt/search` must be a real UUID; other strings are rejected
- `analyzeduration` dominates stream startup; lowering it takes first frame from 2.2s to
  1.2s, but going too low makes the stream fail to open at all
- Hardware decode helps only the main stream. On the 352x288 sub-streams every hwaccel
  tested dropped 25 fps to 18.8 fps for a trivial CPU saving, so the grid stays software.
  `dxva2` looked best on a single sample but halved the framerate on 2 of 3 repeats;
  `d3d11va` with an explicit `hwdownload` was the only consistent option
- Motion detection already ships with `targetType: human,vehicle` enabled, but the VMD
  trigger has no `center` notification by default, so events never reach the alert
  stream until you add one
- `urllib`'s digest auth handler keeps per-request state and is **not thread safe**.
  Sharing one opener across the parallel camera probe silently dropped cameras from the
  results; each thread needs its own opener

---

## Layout

```
app.py        UI, live grid, playback, tray, entry point
hik.py        ISAPI and RTSP client (device info, search, playback, clips, events)
timeline.py   Zoomable timeline widget
events.py     Motion episode store and DVR alert-stream watcher
login.py      Connection dialog
config.py     Encrypted credential and path handling
build.py      PyInstaller build
detect.py     Optional YOLO pass over an exported clip (not used by the app)
```

`detect.py` is kept separate: the DVR already classifies human vs vehicle, so YOLO is not
needed for normal use. It is there for finer labels on an exported clip if you want them.

---

## Status

v1. Working and in daily use, but young. PTZ is absent because the cameras are fixed
analog. Multi-DVR, remote access and notifications are not implemented.

## Licence

MIT
