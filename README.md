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

**Recent tab** (opens first)
- Every person and vehicle event, newest first, with the DVR's snapshot taken the moment
  it happened, the camera, the time and how long ago
- Selecting an event replays it straight away, from a few seconds before to a few after
- Filters: today, yesterday or 7 days, camera, people or vehicles, unread only
- Save the event as a clip, open it in Playback, or jump to that camera live
- "All cameras, last 2 / 5 / 15 min" replays every camera together from that point

**Live tab**
- All cameras in a grid, click any tile to expand it
- Expanded camera: HD or SD stream, noise reduction, and that camera's last 24 hours of
  events beside the video
- Per-camera sound, snapshot and record-to-file; tiles show how many events are unread
- Streams only run while the tab is on screen, so the app costs nothing in the tray

**Playback tab**
- Calendar showing which days have footage, a timeline with a person or vehicle mark at
  each event and the event's snapshot on hover
- Zoom from the full day to 30 seconds, type a time to jump to it, step frames,
  jump between events, and list the day's events beside the video
- Shift+drag selects a range, drag its edges to adjust, then export with a progress bar
  and time left; separate clips or one mosaic of several cameras
- Sound up to 4x speed, noise reduction, all cameras synced in one grid
- Lanes: one row per camera under the timeline showing its events; click one to jump there
- Bookmarks (B): a flag on the timeline, right-click it to add a note or delete it
- Loop: repeats the selected range for close review

**Album tab**
- Snapshots, recordings and saved clips with thumbnails, newest first; open, show in
  folder, or move to the Recycle Bin

**Device tab**
- DVR model, serial, firmware, uptime, restart
- Disk health, capacity, and how many days of footage the disk holds
- Desktop alerts: on or off, people or vehicles, which cameras, quiet hours
- Viewing defaults: noise reduction level, expanded camera quality

**Motion index**
- The DVR classifies motion as `human` or `vehicle` on-device; this app records it, grabs
  one snapshot per event and remembers which events you have seen
- About 45 seconds after each event (once the DVR has saved it) a short clip of it is
  cached locally, so replays start in a fraction of a second and Save clip is quick.
  Clips are kept 2 days; events from the last 7 days without a snapshot get one too
- Bursts are coalesced into episodes, so one person walking past is one event, not fifty
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
3. Pick your DVR from the list it finds and enter the username and password

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

There is no splash screen: the window itself appears in well under a second, with the
Recent tab filled from the local event index before any network call finishes.

`python tools\selfcheck.py` runs the quick logic checks (event store, alert rules).

---

## Finding the DVR

On first run the app asks the network for Hikvision devices (SADP, the same discovery
Hikvision's own tools use) and lists what answers, so you do not need to know the IP
address. Pick the device, enter the username and password, and that is the whole setup.

The serial number is stored alongside the address and is what identifies the DVR from
then on. If DHCP moves the DVR to a different IP, the app finds it again by serial and
updates itself, so a fixed address or a router reservation is never required. If
discovery is blocked on your network, the address field still accepts a typed IP.

---

## Credentials

On first run you are asked for the DVR login.

Credentials are **not** stored in the repo or next to the executable. They go to:

```
%LOCALAPPDATA%\HikViewer\config.json
```

The password is encrypted with **Windows DPAPI**, tied to your Windows user account, so
another account on the same machine cannot read it. To forget the saved login, delete
that file.

---

## Where things are saved

| What | Where |
|---|---|
| Credentials | `%LOCALAPPDATA%\HikViewer\config.json` |
| Motion index | `%LOCALAPPDATA%\HikViewer\events.db` (pruned past 180 days) |
| Event snapshots | `%LOCALAPPDATA%\HikViewer\thumbs` (pruned with their events) |
| Event clips | `%LOCALAPPDATA%\HikViewer\clips` (kept 2 days) |
| Logs | `%LOCALAPPDATA%\HikViewer\cctv.log`, `crash.log` |
| Snapshots, recordings, exported clips | `%USERPROFILE%\Videos\CCTV` |

Recordings and exports are tagged `4:3`, which suits the analog cameras this was built
against. For 16:9 cameras, add `"aspect": "16:9"` to `config.json` (or `"source"` to
leave the stream's own ratio alone).

These live outside the app folder on purpose, so rebuilding or replacing the app never
destroys your history or your clips.

---

## Behaviour worth knowing

**Closing the window does not quit.** It hides to the system tray and keeps recording
events, with live streams stopped. Quit from the tray icon's menu; quitting is instant.

**Only one instance runs.** Launching it again hands over to the running copy before Qt
even loads, and that copy comes to the front on the Recent tab.

**Footage arrives at real time.** The only time-accurate way to read recordings is the
DVR's RTSP playback track, which it sends at 1x. Playback, event replay and export all
use it: a jump lands on the right second within about two seconds, speeds above 1x run
fast through footage already fetched and then follow at real time, and exporting a
10-minute clip takes about 10 minutes (with a progress bar and time left). The last
20-30 seconds before now are not saved by the DVR yet; playback waits for them.

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
- `ContentMgmt/download` with a segment `name` **ignores `starttime`** and always sends
  the segment from its beginning (verified: identical bytes for two start times, open and
  closed segments). It is fast (about 8.6 MB/s) but cannot seek, so it is not used
- Desktop alert snapshots come from `Streaming/channels/<ch>01/picture`, taken the moment
  the alert arrives; the DVR keeps no picture of its own for this event type
- `urllib`'s digest auth handler keeps per-request state and is **not thread safe**.
  Sharing one opener across the parallel camera probe silently dropped cameras from the
  results; each thread needs its own opener

---

## Layout

```
app.py        Main window, tray, single instance, startup
recent.py     Recent tab and the event list used by Live and Playback
live.py       Live tab: camera grid, expanded camera, recording
playback.py   Playback tab and the export dialog
panels.py     Album and Device tabs, desktop alert rules
widgets.py    Video surface, flow layout, background calls, audio player
theme.py      Colour and type tokens, stylesheet, person and vehicle marks
player.py     Time-accurate playback engine (RTSP cache plus frame decoder)
export.py     Clip and mosaic export with progress
hik.py        ISAPI and RTSP client (device info, storage, search, playback, events)
timeline.py   Zoomable timeline widget with event marks and hover snapshots
events.py     Event store, alert-stream watcher, snapshots, event clip cache
login.py      Connection dialog
config.py     Encrypted credential and path handling
build.py      PyInstaller build
```

---

## Status

v1. Working and in daily use, but young. PTZ is absent because the cameras are fixed
analog. Multi-DVR and remote access are not implemented; alerts are desktop notifications only.

## Licence

MIT
