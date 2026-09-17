import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QDate, QPoint, QRect, Qt, QThread, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (QAction, QColor, QIcon, QImage, QPainter, QPixmap,
                           QPolygon, QTextCharFormat)
from PySide6.QtWidgets import (QApplication, QComboBox, QDateEdit, QGridLayout,
                               QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QSizePolicy, QStatusBar, QSystemTrayIcon,
                               QTabWidget, QVBoxLayout, QWidget)

import config
import hik
import login
import runtime
from events import EventStore, EventWatcher
from timeline import TimelineBar, clock, seconds_of

TILE_SIZE = (480, 360)
FOCUS_SIZE = (1024, 768)
PREROLL = 3
STALL_SECONDS = 20
HARDWARE_DECODE = sys.platform == 'win32'
CAPTURES = config.captures_dir()

BUTTON_STYLE = """
QPushButton { background:#1b1b22; color:#9a9aa8; border:1px solid #2c2c36;
              padding:3px 9px; font-size:11px; }
QPushButton:hover { background:#24242e; color:#d0d0dc; }
QPushButton:checked { background:#2b4a34; color:#7fe0a0; border-color:#3d6b4a; }
"""


class StreamWorker(QThread):
    frame_ready = Signal(int, int, QImage)
    state_changed = Signal(int, int, str)

    def __init__(self, token, slot, url, size, duration=None, restart=True, hardware=False):
        super().__init__()
        self.token = token
        self.slot = slot
        self.url = url
        self.width, self.height = size
        self.duration = duration
        self.restart = restart
        self.hardware = hardware
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
            if first and self.hardware:
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
        chain = 'scale=%d:%d,setsar=1' % (self.width, self.height)
        if self.hardware:
            chain = 'hwdownload,format=nv12,' + chain
        return (['ffmpeg']
                + runtime.rtsp_input(self.url, hardware=self.hardware,
                                     duration=self.duration)
                + ['-an', '-vf', chain, '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'])

    def stop(self):
        self.running = False
        self._kill()

    def _kill(self):
        runtime.stop_process(self.proc)
        self.proc = None


class CallWorker(QThread):
    done = Signal(object, object)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.done.emit(self.func(*self.args, **self.kwargs), None)
        except Exception as exc:
            runtime.log.warning('%s failed: %s', getattr(self.func, '__name__', '?'), exc)
            self.done.emit(None, exc)


class AudioPlayer:
    def __init__(self):
        self.proc = None
        self.slot = None

    def play(self, slot, url):
        self.stop()
        self.proc = runtime.spawn(
            ['ffplay', '-nodisp', '-loglevel', 'error', '-vn',
             '-rtsp_transport', 'tcp', '-timeout', str(runtime.SOCKET_TIMEOUT_US),
             '-fflags', 'nobuffer', '-i', url],
            stdout=subprocess.DEVNULL)
        self.slot = slot

    def stop(self):
        runtime.stop_process(self.proc)
        self.proc = None
        self.slot = None


class VideoLabel(QLabel):
    clicked = Signal()

    def __init__(self, title):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setStyleSheet('background:#101014; color:#6f6f80; border:1px solid #26262e;')
        self.setText('%s\nconnecting' % title)
        self.frame = None

    def set_frame(self, image):
        self.frame = image
        self.update()

    def paintEvent(self, event):
        if self.frame is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#101014'))
        area = self.rect().adjusted(1, 1, -1, -1)
        size = self.frame.size().scaled(area.size(), Qt.KeepAspectRatio)
        painter.setRenderHint(QPainter.SmoothPixmapTransform,
                              size.width() * 1.15 < self.frame.width())
        painter.drawImage(QRect(area.x() + (area.width() - size.width()) // 2,
                                area.y() + (area.height() - size.height()) // 2,
                                size.width(), size.height()), self.frame)

    def mousePressEvent(self, event):
        self.clicked.emit()


class Tile(QWidget):
    focus_requested = Signal(int)
    audio_requested = Signal(int)
    snap_requested = Signal(int)
    record_requested = Signal(int)

    def __init__(self, slot, channel):
        super().__init__()
        self.slot = slot
        self.channel = channel
        self.title = 'Camera %d' % channel
        self.frames = 0
        self.fps = 0
        self.state = 'connecting'
        self.has_frame = False

        self.video = VideoLabel(self.title)
        self.video.clicked.connect(lambda: self.focus_requested.emit(slot))

        self.name = QLabel(self.title)
        self.name.setStyleSheet('color:#c8c8d4; font-size:11px; padding-left:2px;')

        self.audio_button = QPushButton('Audio')
        self.audio_button.setCheckable(True)
        self.audio_button.clicked.connect(lambda: self.audio_requested.emit(slot))

        self.snap_button = QPushButton('Snap')
        self.snap_button.clicked.connect(lambda: self.snap_requested.emit(slot))

        self.record_button = QPushButton('Rec')
        self.record_button.setCheckable(True)
        self.record_button.clicked.connect(lambda: self.record_requested.emit(slot))

        bar = QHBoxLayout()
        bar.setContentsMargins(2, 2, 2, 0)
        bar.setSpacing(4)
        bar.addWidget(self.name)
        bar.addStretch(1)
        for button in (self.audio_button, self.snap_button, self.record_button):
            button.setStyleSheet(BUTTON_STYLE)
            bar.addWidget(button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.video, 1)
        layout.addLayout(bar)

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

    def __init__(self, dvr, channels):
        super().__init__()
        self.dvr = dvr
        self.channels = channels
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

        self.grid = QGridLayout(self)
        self.grid.setSpacing(6)
        self.grid.setContentsMargins(6, 6, 6, 6)
        for column in range(3):
            self.grid.setColumnStretch(column, 1)
        for row in range(2):
            self.grid.setRowStretch(row, 1)

        self.tiles = {}
        for index, channel in enumerate(channels):
            tile = Tile(index, channel)
            tile.focus_requested.connect(self.toggle_focus)
            tile.audio_requested.connect(self.toggle_audio)
            tile.snap_requested.connect(self.take_snapshot)
            tile.record_requested.connect(self.toggle_record)
            self.tiles[index] = tile
            self.grid.addWidget(tile, index // 3, index % 3)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

        os.makedirs(CAPTURES, exist_ok=True)
        self.start_grid()

    def start_grid(self):
        self.focused = None
        for tile in self.tiles.values():
            tile.setVisible(True)
        for index, channel in enumerate(self.channels):
            self.start_worker(index, self.dvr.live_url(channel, sub=True), TILE_SIZE)

    def start_worker(self, slot, url, size, hardware=False):
        token = self.next_token
        self.next_token += 1
        worker = StreamWorker(token, slot, url, size, hardware=hardware)
        worker.frame_ready.connect(self.on_frame)
        worker.state_changed.connect(self.on_state)
        self.workers[token] = worker
        if slot in self.slot_token:
            self.pending.add(token)
        else:
            self.slot_token[slot] = token
        self.last_frame[slot] = time.monotonic()
        worker.start()

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
            self.focused // 3, self.focused % 3)
        for index in range(3):
            self.grid.setColumnStretch(index, 1 if column is None or index == column else 0)
        for index in range(2):
            self.grid.setRowStretch(index, 1 if row is None or index == row else 0)

    def toggle_focus(self, slot):
        if self.focused == slot:
            self.focused = None
            self.apply_stretch()
            for tile in self.tiles.values():
                tile.setVisible(True)
            for index, channel in enumerate(self.channels):
                if index != slot:
                    self.start_worker(index, self.dvr.live_url(channel, sub=True), TILE_SIZE)
            self.start_worker(slot, self.dvr.live_url(self.channels[slot], sub=True), TILE_SIZE)
            return
        self.focused = slot
        self.apply_stretch()
        self.retire_slots({i for i in self.tiles if i != slot})
        for index, tile in self.tiles.items():
            tile.setVisible(index == slot)
            if index != slot:
                tile.has_frame = False
        self.start_worker(slot, self.dvr.live_url(self.channels[slot], sub=False),
                          FOCUS_SIZE, hardware=HARDWARE_DECODE)

    def toggle_audio(self, slot):
        if self.audio.slot == slot:
            self.audio.stop()
            self.note = 'audio off'
        else:
            self.audio.play(slot, self.dvr.live_url(self.channels[slot], sub=False))
            self.note = 'audio on camera %d' % self.channels[slot]
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
            self.note = 'saved %s' % os.path.basename(result)
        else:
            self.note = 'snapshot failed: %s' % error

    def toggle_record(self, slot):
        channel = self.channels[slot]
        if slot in self.recorders:
            proc, path = self.recorders.pop(slot)
            runtime.stop_process(proc, graceful=True)
            runtime.log.info('stopped recording %s', os.path.basename(path))
            self.note = 'stopped %s' % os.path.basename(path)
        else:
            path = os.path.join(CAPTURES, 'live-ch%d-%s.mp4' % (
                channel, datetime.now().strftime('%Y%m%d-%H%M%S')))
            proc = runtime.spawn(
                ['ffmpeg'] + runtime.rtsp_input(self.dvr.live_url(channel, sub=False))
                + ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k', '-aspect', '4:3',
                   '-movflags', '+frag_keyframe+empty_moov', path],
                stdout=subprocess.DEVNULL, stdin=subprocess.PIPE)
            self.recorders[slot] = (proc, path)
            self.note = 'recording %s' % os.path.basename(path)
        self.tiles[slot].record_button.setChecked(slot in self.recorders)

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
            hardware = worker.hardware
            self.slot_token.pop(slot, None)
            self.retire(token)
            self.start_worker(slot, url, size, hardware=hardware)

    def tick(self):
        self.retiring = [w for w in self.retiring if not w.isFinished()]
        self.jobs = [j for j in self.jobs if j.isRunning()]
        self.check_stalls()
        if not self.isVisible():
            return
        parts = []
        for tile in self.tiles.values():
            if tile.isVisible():
                tile.fps = tile.frames
                parts.append('ch%d %dfps' % (tile.channel, tile.fps))
            tile.frames = 0
        for slot in list(self.recorders):
            proc, path = self.recorders[slot]
            if proc.poll() is not None:
                self.recorders.pop(slot)
                self.tiles[slot].record_button.setChecked(False)
                self.note = 'recorder for ch%d stopped' % self.channels[slot]
        mode = 'focus' if self.focused is not None else 'grid'
        tail = '   %s' % self.note if self.note else ''
        self.status.emit('%s   %s   rec:%d%s' % (
            mode, '  '.join(parts), len(self.recorders), tail))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.focused is not None:
            self.toggle_focus(self.focused)

    def shutdown(self):
        self.timer.stop()
        self.audio.stop()
        for slot in list(self.recorders):
            proc, _ = self.recorders.pop(slot)
            runtime.stop_process(proc, graceful=True)
        for worker in list(self.workers.values()) + list(self.retiring):
            worker.stop()
        for worker in list(self.workers.values()) + list(self.retiring):
            worker.wait(3000)
        for job in self.jobs:
            job.wait(3000)
        self.jobs.clear()
        self.workers.clear()


class ExportWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, dvr, channel, begin, finish, path):
        super().__init__()
        self.dvr = dvr
        self.channel = channel
        self.begin = begin
        self.finish = finish
        self.path = path

    def run(self):
        try:
            self.dvr.clip(self.channel, self.begin, self.finish, self.path)
            self.done.emit(True, self.path)
        except Exception as exc:
            self.done.emit(False, str(exc))


class PlaybackView(QWidget):
    status = Signal(str)

    def __init__(self, dvr, channels, store):
        super().__init__()
        self.dvr = dvr
        self.channels = channels
        self.store = store
        self.episodes = []
        self.hit_index = None
        self.worker = None
        self.exporter = None
        self.day_job = None
        self.audio = AudioPlayer()
        self.view_note = ''
        self.play_from = None
        self.play_started = None
        self.selection = None
        self.note = ''

        self.camera = QComboBox()
        for channel in channels:
            self.camera.addItem('Camera %d' % channel, channel)
        self.camera.currentIndexChanged.connect(self.reload_day)

        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat('yyyy-MM-dd')
        self.date.setDate(QDate.currentDate())
        self.date.dateChanged.connect(self.reload_day)

        self.prev_button = QPushButton('<')
        self.prev_button.clicked.connect(lambda: self.step_day(-1))
        self.next_button = QPushButton('>')
        self.next_button.clicked.connect(lambda: self.step_day(1))
        self.stop_button = QPushButton('Stop')
        self.stop_button.clicked.connect(self.stop_playback)
        self.audio_button = QPushButton('Audio')
        self.audio_button.setCheckable(True)
        self.audio_button.clicked.connect(self.toggle_audio)
        self.zoom_in = QPushButton('+')
        self.zoom_in.clicked.connect(lambda: self.timeline.zoom(0.5))
        self.zoom_out = QPushButton('-')
        self.zoom_out.clicked.connect(lambda: self.timeline.zoom(2.0))
        self.fit_button = QPushButton('Fit day')
        self.fit_button.clicked.connect(lambda: self.timeline.fit())
        self.back10 = QPushButton('-10s')
        self.back10.clicked.connect(lambda: self.nudge(-10))
        self.fwd10 = QPushButton('+10s')
        self.fwd10.clicked.connect(lambda: self.nudge(10))
        self.prev_hit = QPushButton('<< Motion')
        self.prev_hit.clicked.connect(lambda: self.jump_detection(-1))
        self.next_hit = QPushButton('Motion >>')
        self.next_hit.clicked.connect(lambda: self.jump_detection(1))
        self.export_button = QPushButton('Export selection')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_selection)

        self.readout = QLabel('click the timeline to play')
        self.readout.setStyleSheet('color:#8a8a99; font-size:11px;')

        bar = QHBoxLayout()
        bar.setSpacing(6)
        for widget in (self.camera, self.prev_button, self.date, self.next_button,
                       self.stop_button, self.audio_button, self.back10, self.fwd10,
                       self.prev_hit, self.next_hit, self.zoom_out, self.zoom_in,
                       self.fit_button):
            widget.setStyleSheet(BUTTON_STYLE)
            bar.addWidget(widget)
        bar.addWidget(self.readout)
        bar.addStretch(1)
        self.export_button.setStyleSheet(BUTTON_STYLE)
        bar.addWidget(self.export_button)

        self.video = VideoLabel('Playback')
        self.video.setText('pick a day, then click the timeline')

        self.timeline = TimelineBar()
        self.timeline.seek_requested.connect(self.seek_to)
        self.timeline.selection_changed.connect(self.on_selection)
        self.timeline.view_changed.connect(self.on_view_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(bar)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.timeline)

        self.loaded = False
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.ticker.start(500)

    def ensure_loaded(self):
        if self.loaded:
            return
        self.loaded = True
        self.reload_day()

    def on_view_changed(self, start, end):
        self.view_note = 'view %s - %s' % (clock(start), clock(end))

    def nudge(self, delta):
        if self.play_from is None:
            return
        elapsed = (datetime.now() - self.play_started).total_seconds() if self.play_started else 0
        self.seek_to(max(0, seconds_of(self.play_from) + elapsed + delta))

    def toggle_audio(self):
        if self.audio.slot is not None:
            self.audio.stop()
        elif self.play_from is not None:
            self.start_audio()
        self.audio_button.setChecked(self.audio.slot is not None)

    def start_audio(self):
        if self.play_from is None:
            return
        end = datetime.combine(self.current_day(), datetime.min.time()) + timedelta(days=1)
        url = self.dvr.playback_url(self.current_channel(), self.play_from,
                                    min(end, datetime.now()))
        self.audio.play(self.current_channel(), url)

    def current_channel(self):
        return self.camera.currentData()

    def current_day(self):
        return self.date.date().toPython()

    def step_day(self, delta):
        self.date.setDate(self.date.date().addDays(delta))

    def reload_day(self):
        self.stop_playback()
        channel = self.current_channel()
        day = self.current_day()
        start = datetime.combine(day, datetime.min.time())
        self.timeline.set_position(None)
        self.timeline.clear_selection()
        self.load_detections()
        self.label_cameras()
        self.note = 'loading %s ...' % day.isoformat()
        if self.day_job is not None and self.day_job.isRunning():
            return

        def fetch():
            return (self.dvr.search(channel, start, start + timedelta(days=1),
                                    max_results=200),
                    self.dvr.record_days(channel, day.year, day.month))

        self.day_job = CallWorker(fetch)
        self.day_job.done.connect(
            lambda result, error: self._day_loaded(day, result, error))
        self.day_job.start()

    def _day_loaded(self, day, result, error):
        if day != self.current_day():
            return
        if error is not None or result is None:
            self.note = 'could not load %s: %s' % (day.isoformat(), error)
            return
        segments, days = result
        self.timeline.set_segments(segments, day)
        self.mark_calendar(days)
        total = sum(b - a for a, b in self.timeline.segments)
        self.note = '%s   %d segment(s)   %.1f h recorded   %d motion event(s)' % (
            day.isoformat(), len(self.timeline.segments), total / 3600.0,
            len(self.episodes))

    def label_cameras(self):
        day = self.current_day()
        for index in range(self.camera.count()):
            channel = self.camera.itemData(index)
            count = len(self.store.day(channel, day))
            self.camera.setItemText(index, 'Camera %d (%d)' % (channel, count))

    def load_detections(self):
        self.episodes = self.store.day(self.current_channel(), self.current_day())
        self.timeline.set_markers([(seconds_of(start), seconds_of(end))
                                   for start, end, _t, _h in self.episodes])
        self.hit_index = None

    def jump_detection(self, direction):
        if not self.episodes:
            self.note = 'no motion recorded for this camera and day'
            return
        marks = [seconds_of(start) for start, _e, _t, _h in self.episodes]
        if self.hit_index is None:
            here = self.timeline.position
            if here is None:
                index = 0 if direction > 0 else len(marks) - 1
            elif direction > 0:
                index = next((i for i, m in enumerate(marks) if m >= here), None)
            else:
                index = next((i for i in range(len(marks) - 1, -1, -1)
                              if marks[i] <= here), None)
        else:
            index = self.hit_index + direction
        if index is None or not 0 <= index < len(marks):
            self.note = 'no %s motion on this day' % ('later' if direction > 0 else 'earlier')
            return
        self.hit_index = index
        start, _end, label, hits = self.episodes[index]
        self.seek_to(max(0, marks[index] - PREROLL), from_jump=True)
        self.note = 'motion %d of %d   %s   %s   %d hit(s)' % (
            index + 1, len(marks), start.strftime('%H:%M:%S'),
            label or 'unclassified', hits)

    def mark_calendar(self, days):
        widget = self.date.calendarWidget()
        if widget is None or not days:
            return
        day = self.current_day()
        available = QTextCharFormat()
        available.setForeground(QColor('#7fe0a0'))
        for number, _kind in days:
            widget.setDateTextFormat(QDate(day.year, day.month, number), available)

    def seek_to(self, seconds, from_jump=False):
        if not from_jump:
            self.hit_index = None
        self.stop_playback()
        midnight = datetime.combine(self.current_day(), datetime.min.time())
        start = midnight + timedelta(seconds=int(seconds))
        if start >= datetime.now():
            self.note = 'that time is in the future'
            return
        end = min(midnight + timedelta(days=1), datetime.now())
        duration = (end - start).total_seconds()
        if duration <= 0:
            self.note = 'nothing to play there'
            return
        self.play_from = start
        self.play_started = datetime.now()
        mark = seconds_of(start)
        if not self.timeline.view_start <= mark <= self.timeline.view_end:
            self.timeline.centre_on(mark)
        self.timeline.set_position(mark)
        self.worker = StreamWorker(
            0, 0, self.dvr.playback_url(self.current_channel(), start, end),
            FOCUS_SIZE, duration=duration, restart=False, hardware=HARDWARE_DECODE)
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.state_changed.connect(self.on_state)
        self.worker.start()
        if self.audio_button.isChecked():
            self.start_audio()
        self.note = 'playing from %s' % start.strftime('%H:%M:%S')

    def stop_playback(self):
        self.audio.stop()
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
        self.play_from = None
        self.play_started = None

    def on_frame(self, token, slot, image):
        self.video.set_frame(image)

    def on_state(self, token, slot, state):
        if state == 'ended':
            self.note = 'playback reached the end'
            self.play_started = None

    def on_selection(self, selection):
        self.selection = selection
        self.export_button.setEnabled(selection is not None and self.exporter is None)

    def export_selection(self):
        if self.selection is None or self.exporter is not None:
            return
        midnight = datetime.combine(self.current_day(), datetime.min.time())
        start = midnight + timedelta(seconds=int(self.selection[0]))
        end = min(midnight + timedelta(seconds=int(self.selection[1])), datetime.now())
        if (end - start).total_seconds() < 1:
            self.note = 'selection too short'
            return
        channel = self.current_channel()
        path = os.path.join(CAPTURES, 'clip-ch%d-%s-%s.mp4' % (
            channel, start.strftime('%Y%m%d-%H%M%S'), end.strftime('%H%M%S')))
        self.exporter = ExportWorker(self.dvr, channel, start, end, path)
        self.exporter.done.connect(self.on_export_done)
        self.exporter.start()
        self.export_button.setEnabled(False)
        minutes = (end - start).total_seconds() / 60.0
        self.note = 'exporting %.1f min, runs at 1x so expect about %.0f min' % (
            minutes, minutes)

    def on_export_done(self, ok, detail):
        self.exporter = None
        if ok:
            self.note = 'exported %s' % os.path.basename(detail)
        else:
            self.note = 'export failed: %s' % detail
        self.export_button.setEnabled(self.selection is not None)

    def tick(self):
        if not self.isVisible():
            return
        if self.play_from is not None and self.play_started is not None:
            elapsed = (datetime.now() - self.play_started).total_seconds()
            current = self.play_from + timedelta(seconds=elapsed)
            self.timeline.set_position(seconds_of(current))
            self.readout.setText(current.strftime('%H:%M:%S'))
        parts = []
        if self.selection:
            midnight = datetime.combine(self.current_day(), datetime.min.time())
            first = midnight + timedelta(seconds=int(self.selection[0]))
            last = midnight + timedelta(seconds=int(self.selection[1]))
            parts.append('selected %s - %s (%.1f min)' % (
                first.strftime('%H:%M:%S'), last.strftime('%H:%M:%S'),
                (last - first).total_seconds() / 60.0))
        if self.view_note:
            parts.append(self.view_note)
        if self.note:
            parts.append(self.note)
        self.status.emit('   '.join(parts))

    def suspend(self):
        if self.worker is not None:
            self.stop_playback()
            self.note = 'stopped, click the timeline to resume'

    def shutdown(self):
        self.ticker.stop()
        self.stop_playback()
        for job in (self.exporter, self.day_job):
            if job is not None:
                job.wait(2000)


IPC_NAME = 'HikViewerShowRequest'
MUTEX_NAME = 'Local\\HikViewerSingleInstance'
_instance_mutex = None


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor('#161b24'))
    painter.drawRoundedRect(1, 1, 62, 62, 13, 13)
    painter.setBrush(QColor('#2f6f4a'))
    painter.drawRoundedRect(10, 23, 30, 21, 5, 5)
    painter.drawPolygon(QPolygon([QPoint(42, 30), QPoint(55, 23), QPoint(55, 44), QPoint(42, 37)]))
    painter.setBrush(QColor('#ffb347'))
    painter.drawEllipse(QPoint(20, 33), 5, 5)
    painter.end()
    return QIcon(pixmap)


def already_running():
    global _instance_mutex
    if sys.platform != 'win32':
        return False
    kernel32 = ctypes.windll.kernel32
    _instance_mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() == 183


def ask_running_instance_to_show(attempts=10):
    for _ in range(attempts):
        socket = QLocalSocket()
        socket.connectToServer(IPC_NAME)
        if socket.waitForConnected(1000):
            socket.write(b'show')
            socket.waitForBytesWritten(1000)
            socket.flush()
            socket.waitForDisconnected(1000)
            return True
        QThread.msleep(500)
    return False


class MainWindow(QMainWindow):
    def __init__(self, dvr, channels):
        super().__init__()
        self.setWindowTitle('CCTV')
        self.resize(1480, 880)
        self.setStyleSheet('background:#08080b;')
        self.setWindowIcon(app_icon())
        self.quitting = False

        self.store = EventStore()
        self.live = LiveView(dvr, channels)
        self.playback = PlaybackView(dvr, channels, self.store)
        self.live.status.connect(self.show_status)
        self.playback.status.connect(self.show_status)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            'QTabBar::tab { background:#14141a; color:#9a9aa8; padding:5px 16px; }'
            'QTabBar::tab:selected { background:#23232e; color:#e0e0ea; }'
            'QTabWidget::pane { border:0; }')
        tabs.addTab(self.live, 'Live')
        tabs.addTab(self.playback, 'Playback')
        tabs.currentChanged.connect(self.on_tab_changed)
        self.tabs = tabs
        self.setCentralWidget(tabs)

        self.setStatusBar(QStatusBar())
        self.statusBar().setStyleSheet('color:#8a8a99;')

        self.watcher = EventWatcher(dvr, self.store)
        self.watcher.detected.connect(self.on_detected)
        self.watcher.start()

        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip('CCTV - %s' % dvr.ip)
        menu = QMenu()
        show_action = QAction('Show window', self)
        show_action.triggered.connect(self.restore_window)
        quit_action = QAction('Quit', self)
        quit_action.triggered.connect(self.quit_app)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        QLocalServer.removeServer(IPC_NAME)
        self.ipc = QLocalServer(self)
        self.ipc.newConnection.connect(self.on_ipc_request)
        self.ipc.listen(IPC_NAME)

    def on_ipc_request(self):
        connection = self.ipc.nextPendingConnection()
        if connection is not None:
            connection.readyRead.connect(connection.deleteLater)
        self.restore_window()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.restore_window()

    def restore_window(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.update()

    def quit_app(self):
        self.quitting = True
        self.close()

    def on_detected(self, channel, target, moment):
        self.live.note = 'motion ch%d %s %s' % (channel, target or '', moment.strftime('%H:%M:%S'))
        if (self.playback.loaded and channel == self.playback.current_channel()
                and moment.date() == self.playback.current_day()):
            self.playback.load_detections()

    def on_tab_changed(self, index):
        if self.tabs.widget(index) is self.playback:
            self.playback.ensure_loaded()
            self.playback.load_detections()
        else:
            self.playback.suspend()
        self.statusBar().clearMessage()

    def show_status(self, text):
        self.statusBar().showMessage(text)

    def closeEvent(self, event):
        if not self.quitting:
            event.ignore()
            self.hide()
            self.tray.showMessage('CCTV still running',
                                  'Motion is still being recorded. Quit from the tray icon.',
                                  QSystemTrayIcon.Information, 4000)
            return
        self.tray.hide()
        self.ipc.close()
        QLocalServer.removeServer(IPC_NAME)
        self.watcher.stop()
        self.watcher.wait(2000)
        self.playback.shutdown()
        self.live.shutdown()
        event.accept()


def find_tool(name):
    from shutil import which
    return which(name) is not None


class ChannelScan(QThread):
    done = Signal(object)

    def __init__(self, dvr):
        super().__init__()
        self.dvr = dvr

    def run(self):
        try:
            self.done.emit(self.dvr.live_channels())
        except Exception as exc:
            runtime.log.warning('channel rescan failed: %s', exc)


def main():
    runtime.setup_logging()
    runtime.claim_job_object()
    qt = QApplication(sys.argv)
    runtime.install_qt_message_handler()
    qt.setApplicationName('CCTV')
    qt.setWindowIcon(app_icon())
    if already_running():
        ask_running_instance_to_show()
        return 0
    qt.setQuitOnLastWindowClosed(False)
    missing = [tool for tool in ('ffmpeg', 'ffplay') if not find_tool(tool)]
    if missing:
        QMessageBox.critical(
            None, 'CCTV',
            'Missing required tool(s): %s\n\n'
            'Install FFmpeg and make sure it is on PATH, for example:\n'
            '    winget install Gyan.FFmpeg' % ', '.join(missing))
        return 1
    here = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
            else os.path.dirname(os.path.abspath(__file__)))
    dvr = login.obtain_dvr(here)
    if dvr is None:
        return 0

    saved = config.load() or {}
    channels = saved.get('channels') or []
    if channels:
        runtime.log.info('using cached channel list %s', channels)
    else:
        channels = dvr.live_channels()
        if not channels:
            QMessageBox.critical(
                None, 'CCTV',
                'Connected to %s but no cameras are sending video.' % dvr.ip)
            return 1
        config.remember_channels(channels)

    window = MainWindow(dvr, channels)
    window.show()

    def reconcile(found):
        if found and list(found) != list(channels):
            runtime.log.info('camera list changed %s -> %s', channels, found)
            config.remember_channels(found)
            window.statusBar().showMessage(
                'Camera list changed to %s - restart to apply' % found, 15000)
    window.scan = ChannelScan(dvr)
    window.scan.done.connect(reconcile)
    window.scan.start()

    return qt.exec()


if __name__ == '__main__':
    sys.exit(main())
