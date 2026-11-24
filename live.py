import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import config
import runtime
import theme
from recent import EventList, camera_name
from widgets import FOCUS_SIZE, TILE_SIZE, AudioPlayer, CallWorker, VideoLabel, nav_icon

GRID_COLUMNS = 3
STALL_SECONDS = 20
SLOW_FPS = 8
LIVE_GRACE_MS = 15000
HARDWARE_DECODE = sys.platform == 'win32'
CAPTURES = config.captures_dir()

OVERLAY_STYLE = """
QPushButton { background:rgba(12,12,11,150);
              border:1px solid rgba(235,232,225,50); border-radius:22px; }
QPushButton:hover { background:rgba(36,35,32,220);
                    border-color:rgba(235,232,225,140); }
"""



class StreamWorker(QThread):
    frame_ready = Signal(int, int, QImage)
    state_changed = Signal(int, int, str)

    def __init__(self, token, slot, url, size, duration=None, restart=True, hardware=False,
                 denoise=0):
        super().__init__()
        self.token = token
        self.slot = slot
        self.url = url
        self.width, self.height = size
        self.duration = duration
        self.restart = restart
        self.hardware = hardware
        self.denoise = denoise
        self.running = True
        self.proc = None

    def run(self):
        stride = self.width * 3
        expected = stride * self.height
        attempt = 0
        while self.running:
            self.state_changed.emit(self.token, self.slot, 'connecting')
            self.proc = runtime.spawn(self._command(), stdout=subprocess.PIPE)
            first = True
            while self.running:
                data = self.proc.stdout.read(expected)
                if len(data) < expected:
                    break
                if first:
                    self.state_changed.emit(self.token, self.slot, 'live')
                    first = False
                    attempt = 0
                image = QImage(data, self.width, self.height, stride, QImage.Format_BGR888)
                self.frame_ready.emit(self.token, self.slot, image.copy())
            self._kill()
            if first and self.hardware and self.running:
                runtime.log.warning('ch-slot %d: hardware decode failed, using software',
                                    self.slot)
                self.hardware = False
                continue
            if not self.restart:
                if self.running:
                    self.state_changed.emit(self.token, self.slot, 'ended')
                return
            if self.running:
                delay = runtime.backoff_delay(attempt)
                attempt += 1
                runtime.log.info('slot %d stream ended, retry %d in %.1fs',
                                 self.slot, attempt, delay)
                self.state_changed.emit(self.token, self.slot, 'reconnecting')
                self.msleep(int(delay * 1000))

    def _command(self):
        chain = runtime.denoise_filter(self.denoise) + 'scale=%d:%d,setsar=1' % (
            self.width, self.height)
        if self.hardware:
            chain = 'hwdownload,format=nv12,' + chain
        return (['ffmpeg']
                + runtime.rtsp_input(self.url, hardware=self.hardware,
                                     duration=self.duration)
                + ['-an', '-vf', chain, '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'])

    def stop(self):
        self.running = False
        runtime.stop_process(self.proc, wait=False)

    def _kill(self):
        runtime.stop_process(self.proc)
        self.proc = None


class Tile(QWidget):
    focus_requested = Signal(int)
    audio_requested = Signal(int)
    snap_requested = Signal(int)
    record_requested = Signal(int)
    events_requested = Signal(int)
    quality_toggled = Signal(int, bool)
    denoise_toggled = Signal(int, bool)

    def __init__(self, slot, channel):
        super().__init__()
        self.slot = slot
        self.channel = channel
        self.title = camera_name(channel)
        self.frames = 0
        self.fps = 0
        self.state = 'connecting'
        self.has_frame = False

        self.video = VideoLabel(self.title)
        self.video.clicked.connect(lambda: self.focus_requested.emit(slot))

        self.name = QLabel(self.title)
        self.badge = theme.set_role(QPushButton(''), 'quiet')
        self.badge.setStyleSheet('color:%s; font-weight:600; padding:2px 6px;' % theme.PERSON)
        self.badge.setToolTip('Show this camera\'s new events')
        self.badge.clicked.connect(lambda: self.events_requested.emit(channel))
        self.badge.hide()

        self.hd_button = theme.set_role(QPushButton('HD'), 'quiet')
        self.hd_button.setCheckable(True)
        self.hd_button.setToolTip('HD main stream, or SD sub stream when off')
        self.hd_button.toggled.connect(lambda on: self.quality_toggled.emit(slot, on))
        self.denoise_button = theme.set_role(QPushButton('Denoise'), 'quiet')
        self.denoise_button.setCheckable(True)
        self.denoise_button.setToolTip('Reduce grain and noise, mostly useful at night')
        self.denoise_button.toggled.connect(lambda on: self.denoise_toggled.emit(slot, on))
        self.audio_button = theme.set_role(QPushButton('Sound'), 'quiet')
        self.audio_button.setCheckable(True)
        self.audio_button.clicked.connect(lambda: self.audio_requested.emit(slot))
        self.snap_button = theme.set_role(QPushButton('Snapshot'), 'quiet')
        self.snap_button.clicked.connect(lambda: self.snap_requested.emit(slot))
        self.record_button = theme.set_role(QPushButton('Record'), 'quiet')
        self.record_button.setCheckable(True)
        self.record_button.clicked.connect(lambda: self.record_requested.emit(slot))
        self.focus_only = (self.hd_button, self.denoise_button)
        for button in self.focus_only:
            button.hide()

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 0, 0)
        bar.setSpacing(2)
        bar.addWidget(self.name)
        bar.addWidget(self.badge)
        bar.addStretch(1)
        for button in (self.hd_button, self.denoise_button, self.audio_button,
                       self.snap_button, self.record_button):
            bar.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self.video, 1)
        layout.addLayout(bar)

    def set_focus_mode(self, focused):
        for button in self.focus_only:
            button.setVisible(focused)

    def set_unread(self, count):
        self.badge.setText('%d new' % count if count else '')
        self.badge.setVisible(bool(count))

    def show_frame(self, image):
        self.frames += 1
        self.has_frame = True
        self.video.set_frame(image)

    def show_state(self, state):
        self.state = state
        if state != 'live' and not self.has_frame:
            self.video.setText('%s\n%s' % (self.title, state))


class LiveView(QWidget):
    status = Signal(str)
    event_requested = Signal(int)
    camera_events_requested = Signal(int)

    def __init__(self, dvr, channels, store):
        super().__init__()
        self.dvr = dvr
        self.channels = channels
        self.store = store
        self.workers = {}
        self.slot_token = {}
        self.pending = set()
        self.retiring = []
        self.next_token = 0
        self.recorders = {}
        self.audio = AudioPlayer()
        self.focused = None
        self.note = ''
        self.last_frame = {}
        self.jobs = []
        self.streaming = False
        self.hd = config.setting('live_quality', 'hd') != 'sd'
        self.denoise_on = bool(config.setting('denoise', 0))

        self.columns = GRID_COLUMNS
        self.rows = max(1, -(-len(channels) // self.columns))

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.grid.setContentsMargins(8, 8, 8, 8)
        for column in range(self.columns):
            self.grid.setColumnStretch(column, 1)
        for row in range(self.rows):
            self.grid.setRowStretch(row, 1)

        self.tiles = {}
        for index, channel in enumerate(channels):
            tile = Tile(index, channel)
            tile.focus_requested.connect(self.enter_focus)
            tile.audio_requested.connect(self.toggle_audio)
            tile.snap_requested.connect(self.take_snapshot)
            tile.record_requested.connect(self.toggle_record)
            tile.events_requested.connect(self.camera_events_requested)
            tile.quality_toggled.connect(self.set_quality)
            tile.denoise_toggled.connect(self.set_denoise)
            self.tiles[index] = tile
            self.grid.addWidget(tile, index // self.columns, index % self.columns)

        self.side_title = theme.set_role(QLabel(''), 'heading')
        self.side_list = EventList(compact=True)
        self.side_list.picked.connect(lambda event: self.event_requested.emit(event.id))
        self.side_empty = theme.set_role(QLabel('No events in the last 24 hours.'), 'faint')
        self.side_empty.setWordWrap(True)
        self.side = QWidget()
        self.side.setFixedWidth(300)
        side_layout = QVBoxLayout(self.side)
        side_layout.setContentsMargins(4, 12, 8, 8)
        side_layout.setSpacing(8)
        side_layout.addWidget(self.side_title)
        side_layout.addWidget(self.side_list, 1)
        side_layout.addWidget(self.side_empty)
        side_layout.addStretch(0)
        self.side.hide()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.grid_host, 1)
        row.addWidget(self.side)

        self.setFocusPolicy(Qt.StrongFocus)
        self.back_btn = self._overlay_button('back', self.exit_focus)
        self.prev_btn = self._overlay_button('prev', lambda: self.focus_step(-1))
        self.next_btn = self._overlay_button('next', lambda: self.focus_step(1))
        for button in (self.back_btn, self.prev_btn, self.next_btn):
            button.hide()

        self.grace = QTimer(self)
        self.grace.setSingleShot(True)
        self.grace.timeout.connect(self.stop_streams)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

        os.makedirs(CAPTURES, exist_ok=True)

    def activate(self):
        self.grace.stop()
        if self.streaming:
            return
        self.streaming = True
        if self.focused is None:
            self.start_grid()
        else:
            for index, tile in self.tiles.items():
                tile.setVisible(index == self.focused)
            self._start_focus_worker(self.focused)

    def deactivate(self, immediately=False):
        if immediately:
            self.grace.stop()
            self.stop_streams()
        elif self.streaming:
            self.grace.start(LIVE_GRACE_MS)

    def stop_streams(self):
        if not self.streaming:
            return
        self.streaming = False
        self.audio.stop()
        for tile in self.tiles.values():
            tile.audio_button.setChecked(False)
        for token in list(self.workers):
            self.retire(token)
        self.slot_token.clear()
        self.pending.clear()

    def start_grid(self):
        self.focused = None
        for tile in self.tiles.values():
            tile.setVisible(True)
            tile.set_focus_mode(False)
        for index, channel in enumerate(self.channels):
            self.start_worker(index, self.dvr.live_url(channel, sub=True), TILE_SIZE)

    def start_worker(self, slot, url, size, hardware=False, denoise=0):
        token = self.next_token
        self.next_token += 1
        worker = StreamWorker(token, slot, url, size, hardware=hardware, denoise=denoise)
        worker.frame_ready.connect(self.on_frame)
        worker.state_changed.connect(self.on_state)
        self.workers[token] = worker
        if slot in self.slot_token:
            self.pending.add(token)
        else:
            self.slot_token[slot] = token
        self.last_frame[slot] = time.monotonic()
        worker.start()

    def _start_focus_worker(self, slot):
        self.start_worker(slot, self.dvr.live_url(self.channels[slot], sub=not self.hd),
                          FOCUS_SIZE, hardware=HARDWARE_DECODE and self.hd,
                          denoise=runtime.denoise_level(self.denoise_on))

    def retire(self, token):
        worker = self.workers.pop(token, None)
        self.pending.discard(token)
        if worker is None:
            return
        worker.stop()
        self.retiring.append(worker)

    def retire_slots(self, slots):
        for token, worker in list(self.workers.items()):
            if worker.slot in slots:
                if self.slot_token.get(worker.slot) == token:
                    self.slot_token.pop(worker.slot, None)
                self.retire(token)

    def apply_stretch(self):
        row, column = (None, None) if self.focused is None else (
            self.focused // self.columns, self.focused % self.columns)
        for index in range(self.columns):
            self.grid.setColumnStretch(index, 1 if column is None or index == column else 0)
        for index in range(self.rows):
            self.grid.setRowStretch(index, 1 if row is None or index == row else 0)

    def enter_focus(self, slot):
        if self.focused is None:
            self.set_focus(slot)

    def focus_channel(self, channel):
        if channel not in self.channels:
            return
        slot = self.channels.index(channel)
        if self.focused is None:
            self.set_focus(slot)
        elif self.focused != slot:
            self.focus_step(slot - self.focused)

    def set_focus(self, slot):
        self.focused = slot
        self.apply_stretch()
        self.retire_slots({i for i in self.tiles if i != slot})
        for index, tile in self.tiles.items():
            tile.setVisible(index == slot)
            tile.set_focus_mode(index == slot)
            if index != slot:
                tile.has_frame = False
        tile = self.tiles[slot]
        tile.video.reset_zoom()
        self._sync_focus_buttons(tile)
        if self.streaming:
            self._start_focus_worker(slot)
        self.show_overlays(True)
        self.refresh_side()
        self.setFocus()

    def _sync_focus_buttons(self, tile):
        for button, value in ((tile.hd_button, self.hd), (tile.denoise_button, self.denoise_on)):
            button.blockSignals(True)
            button.setChecked(value)
            button.blockSignals(False)

    def exit_focus(self):
        slot = self.focused
        if slot is None:
            return
        self.focused = None
        self.show_overlays(False)
        self.side.hide()
        self.tiles[slot].video.reset_zoom()
        self.apply_stretch()
        for tile in self.tiles.values():
            tile.setVisible(True)
            tile.set_focus_mode(False)
        if not self.streaming:
            return
        for index, channel in enumerate(self.channels):
            if index != slot:
                self.start_worker(index, self.dvr.live_url(channel, sub=True), TILE_SIZE)
        self.start_worker(slot, self.dvr.live_url(self.channels[slot], sub=True), TILE_SIZE)

    def focus_step(self, delta):
        if self.focused is None or len(self.channels) < 2:
            return
        new = (self.focused + delta) % len(self.channels)
        if new == self.focused:
            return
        old = self.focused
        self.focused = new
        self.apply_stretch()
        self.tiles[old].video.reset_zoom()
        self.tiles[new].video.reset_zoom()
        self.tiles[old].setVisible(False)
        self.tiles[old].set_focus_mode(False)
        self.tiles[new].setVisible(True)
        self.tiles[new].set_focus_mode(True)
        self.tiles[new].has_frame = False
        self._sync_focus_buttons(self.tiles[new])
        self.retire_slots({old})
        if self.streaming:
            self._start_focus_worker(new)
        self.refresh_side()
        self.position_overlays()

    def set_quality(self, slot, hd):
        self.hd = hd
        self._restart_focus(slot)

    def set_denoise(self, slot, on):
        self.denoise_on = on
        self._restart_focus(slot)

    def _restart_focus(self, slot):
        if self.focused != slot or not self.streaming:
            return
        self.tiles[slot].has_frame = True
        self.retire_slots({slot})
        self._start_focus_worker(slot)

    def refresh_side(self):
        if self.focused is None:
            return
        channel = self.channels[self.focused]
        events = self.store.recent(since=datetime.now() - timedelta(hours=24),
                                   channel=channel, limit=60)
        self.side_title.setText('%s, last 24 hours' % camera_name(channel))
        self.side_list.set_events(events)
        self.side_list.setVisible(bool(events))
        self.side_empty.setVisible(not events)
        self.side.show()
        self.position_overlays()

    def set_badges(self, counts):
        for tile in self.tiles.values():
            tile.set_unread(counts.get(tile.channel, 0))

    def _overlay_button(self, kind, handler):
        button = QPushButton('', self)
        button.setIcon(nav_icon(kind))
        button.setIconSize(QSize(26, 26))
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.setStyleSheet(OVERLAY_STYLE)
        button.clicked.connect(handler)
        return button

    def show_overlays(self, visible):
        multi = len(self.channels) > 1
        self.back_btn.setVisible(visible)
        self.prev_btn.setVisible(visible and multi)
        self.next_btn.setVisible(visible and multi)
        if visible:
            self.position_overlays()

    def position_overlays(self):
        size = 44
        margin = 18
        area = self.grid_host.geometry()
        middle = area.top() + (area.height() - size) // 2
        self.back_btn.setGeometry(area.left() + margin, area.top() + margin, size, size)
        self.prev_btn.setGeometry(area.left() + margin, middle, size, size)
        self.next_btn.setGeometry(area.right() - size - margin, middle, size, size)
        for button in (self.back_btn, self.prev_btn, self.next_btn):
            button.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.focused is not None:
            QTimer.singleShot(0, self.position_overlays)

    def toggle_audio(self, slot):
        if self.audio.slot == slot:
            self.audio.stop()
            self.note = 'sound off'
        else:
            self.audio.play(slot, self.dvr.live_url(self.channels[slot], sub=False))
            self.note = 'sound on camera %d' % self.channels[slot]
        for index, tile in self.tiles.items():
            tile.audio_button.setChecked(self.audio.slot == index)

    def take_snapshot(self, slot):
        channel = self.channels[slot]
        path = os.path.join(CAPTURES, 'snap-ch%d-%s.jpg' % (
            channel, datetime.now().strftime('%Y%m%d-%H%M%S')))

        def grab():
            data = self.dvr.snapshot(channel, timeout=8)
            with open(path, 'wb') as fh:
                fh.write(data)
            return path

        self.note = 'saving snapshot...'
        job = CallWorker(grab)
        job.done.connect(lambda result, error: self._snapshot_done(result, error))
        self.jobs.append(job)
        job.start()

    def _snapshot_done(self, result, error):
        if error is None and result:
            self.note = 'saved %s to Album' % os.path.basename(result)
        else:
            self.note = 'snapshot failed: %s' % error

    def toggle_record(self, slot):
        channel = self.channels[slot]
        if slot in self.recorders:
            proc, path = self.recorders.pop(slot)
            runtime.stop_process(proc, graceful=True)
            runtime.log.info('stopped recording %s', os.path.basename(path))
            self.note = 'saved %s to Album' % os.path.basename(path)
        else:
            path = os.path.join(CAPTURES, 'live-ch%d-%s.mp4' % (
                channel, datetime.now().strftime('%Y%m%d-%H%M%S')))
            proc = runtime.spawn(
                ['ffmpeg'] + runtime.rtsp_input(self.dvr.live_url(channel, sub=False))
                + ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k']
                + runtime.aspect_args()
                + ['-movflags', '+frag_keyframe+empty_moov', path],
                stdout=subprocess.DEVNULL, stdin=subprocess.PIPE)
            self.recorders[slot] = (proc, path)
            self.note = 'recording %s' % os.path.basename(path)
        recording = slot in self.recorders
        self.tiles[slot].record_button.setChecked(recording)
        self.tiles[slot].video.overlay_text = 'Recording' if recording else ''
        self.tiles[slot].video.update()

    def on_frame(self, token, slot, image):
        if token in self.pending:
            self.pending.discard(token)
            previous = self.slot_token.get(slot)
            if previous is not None and previous != token:
                self.retire(previous)
            self.slot_token[slot] = token
        if self.slot_token.get(slot) != token:
            return
        self.last_frame[slot] = time.monotonic()
        tile = self.tiles.get(slot)
        if tile is not None and tile.isVisible():
            tile.show_frame(image)

    def on_state(self, token, slot, state):
        if self.slot_token.get(slot) != token:
            return
        tile = self.tiles.get(slot)
        if tile is not None:
            tile.show_state(state)

    def check_stalls(self):
        now = time.monotonic()
        for slot, token in list(self.slot_token.items()):
            worker = self.workers.get(token)
            if worker is None or not worker.running:
                continue
            since = now - self.last_frame.get(slot, now)
            if since < STALL_SECONDS:
                continue
            runtime.log.warning('slot %d stalled %.0fs with no frame, restarting',
                                slot, since)
            self.note = 'camera %d stalled, restarting' % self.channels[slot]
            url, size = worker.url, (worker.width, worker.height)
            hardware, denoise = worker.hardware, worker.denoise
            self.slot_token.pop(slot, None)
            self.retire(token)
            self.start_worker(slot, url, size, hardware=hardware, denoise=denoise)

    def tick(self):
        self.retiring = [w for w in self.retiring if not w.isFinished()]
        self.jobs = [job for job in self.jobs if not job.isFinished()]
        self.check_stalls()
        for slot in list(self.recorders):
            proc, path = self.recorders[slot]
            if proc.poll() is not None:
                self.recorders.pop(slot)
                self.tiles[slot].record_button.setChecked(False)
                self.tiles[slot].video.overlay_text = ''
                self.note = 'recorder for ch%d stopped' % self.channels[slot]
        if not self.isVisible():
            return
        parts = []
        for tile in self.tiles.values():
            if tile.isVisible():
                tile.fps = tile.frames
                if self.streaming and tile.has_frame and tile.fps < SLOW_FPS:
                    parts.append('%s is slow (%d fps)' % (tile.title, tile.fps))
            tile.frames = 0
        if self.recorders:
            parts.append('recording %d camera(s)' % len(self.recorders))
        if self.note:
            parts.append(self.note)
        self.status.emit('   '.join(parts))

    def keyPressEvent(self, event):
        if self.focused is None:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key_Escape:
            self.exit_focus()
        elif key == Qt.Key_Left:
            self.focus_step(-1)
        elif key == Qt.Key_Right:
            self.focus_step(1)
        else:
            super().keyPressEvent(event)

    def stop_recorders(self):
        for slot in list(self.recorders):
            proc, _ = self.recorders.pop(slot)
            runtime.stop_process(proc, graceful=True, timeout=3)
