import ctypes
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

BOOT = time.monotonic()

from PySide6.QtCore import (QDate, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QThread, QTime, QTimer, Signal)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import (QAction, QColor, QIcon, QImage, QKeySequence, QPainter,
                           QPen, QPixmap, QPolygon, QPolygonF, QShortcut, QTextCharFormat)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCalendarWidget, QCheckBox,
                               QComboBox, QDateEdit, QDialog, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLayout, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QRadioButton,
                               QSizePolicy, QStatusBar, QSystemTrayIcon, QTabWidget,
                               QTimeEdit, QVBoxLayout, QWidget)

import config
import hik
import login
import runtime
from events import EventStore, EventWatcher
from export import ExportJob
from player import Playback, SPEEDS
from timeline import TimelineBar, clock, seconds_of

SPLASH_MIN_SECONDS = 1.1

try:
    import pyi_splash
except ImportError:
    pyi_splash = None


def close_splash():
    if pyi_splash is None:
        return
    linger = SPLASH_MIN_SECONDS - (time.monotonic() - BOOT)
    if linger > 0:
        time.sleep(linger)
    try:
        if pyi_splash.is_alive():
            pyi_splash.close()
    except Exception:
        pass

GRID_COLUMNS = 3
TILE_SIZE = (480, 360)
FOCUS_SIZE = (1024, 768)
GRID_TILE = (480, 360)
PREROLL = 3
STALL_SECONDS = 20
HANG_SECONDS = 30
HARDWARE_DECODE = sys.platform == 'win32'
CAPTURES = config.captures_dir()

BUTTON_STYLE = """
QPushButton { background:#1b1b22; color:#9a9aa8; border:1px solid #2c2c36;
              padding:3px 9px; font-size:11px; }
QPushButton:hover { background:#24242e; color:#d0d0dc; }
QPushButton:checked { background:#2b4a34; color:#7fe0a0; border-color:#3d6b4a; }
"""

COMBO_STYLE = BUTTON_STYLE + """
QComboBox { combobox-popup: 0; }
QComboBox QAbstractItemView {
    background:#17171f; color:#c8c8d4; border:1px solid #2c2c36; outline:0;
    selection-background-color:#2b4a34; selection-color:#eafff0; }
QComboBox QAbstractItemView::item { min-height:22px; padding:2px 10px; }
"""

OVERLAY_STYLE = """
QPushButton { background:rgba(10,10,14,140);
              border:1px solid rgba(255,255,255,45); border-radius:22px; }
QPushButton:hover { background:rgba(32,32,44,210);
                    border-color:rgba(255,255,255,130); }
"""

NAV_PATHS = {
    'back': [[(19, 12), (5, 12)], [(12, 5), (5, 12), (12, 19)]],
    'prev': [[(15, 6), (9, 12), (15, 18)]],
    'next': [[(9, 6), (15, 12), (9, 18)]],
}


def nav_icon(kind, px=48, stroke=2.4, color='#ececf4'):
    scale = px / 24.0
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), stroke * scale)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    for poly in NAV_PATHS[kind]:
        painter.drawPolyline(QPolygonF([QPointF(x * scale, y * scale) for x, y in poly]))
    painter.end()
    return QIcon(pixmap)


class FlowLayout(QLayout):
    def __init__(self, spacing=6):
        super().__init__()
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect, apply):
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


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
        runtime.stop_process(self.proc, wait=False)

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

    def play_local(self, path, offset):
        self.stop()
        self.proc = runtime.spawn(
            ['ffplay', '-autoexit', '-nodisp', '-loglevel', 'error', '-vn',
             '-ss', '%.3f' % offset, '-i', path],
            stdout=subprocess.DEVNULL)
        self.slot = 'local'

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
        self.overlay_text = ''
        self.zoom = 1.0
        self.zoom_cx = 0.5
        self.zoom_cy = 0.5

    def set_frame(self, image):
        self.frame = image
        self.update()

    def reset_zoom(self):
        if self.zoom != 1.0:
            self.zoom = 1.0
            self.zoom_cx = 0.5
            self.zoom_cy = 0.5
            self.update()

    def _source_rect(self):
        w = self.frame.width() / self.zoom
        h = self.frame.height() / self.zoom
        x = min(max(self.zoom_cx * self.frame.width() - w / 2, 0), self.frame.width() - w)
        y = min(max(self.zoom_cy * self.frame.height() - h / 2, 0), self.frame.height() - h)
        return QRectF(x, y, w, h)

    def paintEvent(self, event):
        if self.frame is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#101014'))
        area = self.rect().adjusted(1, 1, -1, -1)
        source = self._source_rect()
        size = source.size().toSize().scaled(area.size(), Qt.KeepAspectRatio)
        painter.setRenderHint(QPainter.SmoothPixmapTransform,
                              size.width() * 1.15 < source.width())
        target = QRectF(area.x() + (area.width() - size.width()) / 2,
                        area.y() + (area.height() - size.height()) / 2,
                        size.width(), size.height())
        painter.drawImage(target, self.frame, source)
        if self.overlay_text:
            badge = QRectF(area.x() + 10, area.y() + 10, 104, 24)
            painter.fillRect(badge, QColor(10, 10, 14, 190))
            painter.setPen(QColor('#e0e0ea'))
            painter.drawText(badge, Qt.AlignCenter, self.overlay_text)

    def mousePressEvent(self, event):
        self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        if self.frame is None:
            return
        if self.zoom > 1.0:
            self.reset_zoom()
            return
        area = self.rect().adjusted(1, 1, -1, -1)
        size = self.frame.size().scaled(area.size(), Qt.KeepAspectRatio)
        if size.width() > 0 and size.height() > 0:
            tx = area.x() + (area.width() - size.width()) / 2
            ty = area.y() + (area.height() - size.height()) / 2
            pos = event.position()
            self.zoom_cx = min(max((pos.x() - tx) / size.width(), 0.0), 1.0)
            self.zoom_cy = min(max((pos.y() - ty) / size.height(), 0.0), 1.0)
        self.zoom = 2.0
        self.update()


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

        self.columns = GRID_COLUMNS
        self.rows = max(1, -(-len(channels) // self.columns))

        self.grid = QGridLayout(self)
        self.grid.setSpacing(6)
        self.grid.setContentsMargins(6, 6, 6, 6)
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
            self.tiles[index] = tile
            self.grid.addWidget(tile, index // self.columns, index % self.columns)

        self.setFocusPolicy(Qt.StrongFocus)
        self.back_btn = self._overlay_button('back', self.exit_focus)
        self.prev_btn = self._overlay_button('prev', lambda: self.focus_step(-1))
        self.next_btn = self._overlay_button('next', lambda: self.focus_step(1))
        for button in (self.back_btn, self.prev_btn, self.next_btn):
            button.hide()

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
            self.focused // self.columns, self.focused % self.columns)
        for index in range(self.columns):
            self.grid.setColumnStretch(index, 1 if column is None or index == column else 0)
        for index in range(self.rows):
            self.grid.setRowStretch(index, 1 if row is None or index == row else 0)

    def enter_focus(self, slot):
        if self.focused is None:
            self.set_focus(slot)

    def set_focus(self, slot):
        self.focused = slot
        self.apply_stretch()
        self.retire_slots({i for i in self.tiles if i != slot})
        for index, tile in self.tiles.items():
            tile.setVisible(index == slot)
            if index != slot:
                tile.has_frame = False
        self.tiles[slot].video.reset_zoom()
        self.start_worker(slot, self.dvr.live_url(self.channels[slot], sub=False),
                          FOCUS_SIZE, hardware=HARDWARE_DECODE)
        self.show_overlays(True)
        self.setFocus()

    def exit_focus(self):
        slot = self.focused
        if slot is None:
            return
        self.focused = None
        self.show_overlays(False)
        self.tiles[slot].video.reset_zoom()
        self.apply_stretch()
        for tile in self.tiles.values():
            tile.setVisible(True)
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
        self.tiles[new].setVisible(True)
        self.tiles[new].has_frame = False
        self.retire_slots({old})
        self.start_worker(new, self.dvr.live_url(self.channels[new], sub=False),
                          FOCUS_SIZE, hardware=HARDWARE_DECODE)
        self.position_overlays()

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
        margin = 14
        middle = (self.height() - size) // 2
        self.back_btn.setGeometry(margin, margin, size, size)
        self.prev_btn.setGeometry(margin, middle, size, size)
        self.next_btn.setGeometry(self.width() - size - margin, middle, size, size)
        for button in (self.back_btn, self.prev_btn, self.next_btn):
            button.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.focused is not None:
            self.position_overlays()

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
                + ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k']
                + runtime.aspect_args()
                + ['-movflags', '+frag_keyframe+empty_moov', path],
                stdout=subprocess.DEVNULL, stdin=subprocess.PIPE)
            self.recorders[slot] = (proc, path)
            self.note = 'recording %s' % os.path.basename(path)
        recording = slot in self.recorders
        self.tiles[slot].record_button.setChecked(recording)
        self.tiles[slot].video.overlay_text = '● REC' if recording else ''
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
            hardware = worker.hardware
            self.slot_token.pop(slot, None)
            self.retire(token)
            self.start_worker(slot, url, size, hardware=hardware)

    def tick(self):
        self.retiring = [w for w in self.retiring if not w.isFinished()]
        self.jobs = [job for job in self.jobs if not job.isFinished()]
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
                self.tiles[slot].video.overlay_text = ''
                self.note = 'recorder for ch%d stopped' % self.channels[slot]
        mode = 'focus' if self.focused is not None else 'grid'
        tail = '   %s' % self.note if self.note else ''
        self.status.emit('%s   %s   rec:%d%s' % (
            mode, '  '.join(parts), len(self.recorders), tail))

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


class ExportDialog(QDialog):
    def __init__(self, parent, dvr, channels, day, begin, finish, current_channel, out_dir,
                 min_date, max_date):
        super().__init__(parent)
        self.setWindowTitle('Export footage')
        self.setStyleSheet(
            'QDialog { background:#121218; } QLabel { color:#c8c8d4; }'
            'QGroupBox { color:#9a9aa8; border:1px solid #2c2c36; border-radius:4px;'
            ' margin-top:8px; padding:8px; }'
            'QGroupBox::title { subcontrol-origin:margin; left:8px; }'
            'QCheckBox, QRadioButton { color:#c8c8d4; spacing:6px; }'
            'QCheckBox::indicator, QRadioButton::indicator { width:15px; height:15px;'
            ' border:1px solid #6a6a7a; background:#1b1b22; }'
            'QRadioButton::indicator { border-radius:8px; }'
            'QCheckBox::indicator { border-radius:3px; }'
            'QCheckBox::indicator:checked, QRadioButton::indicator:checked {'
            ' background:#7fe0a0; border-color:#7fe0a0; }'
            'QCheckBox::indicator:hover, QRadioButton::indicator:hover {'
            ' border-color:#9a9aa8; }')
        self.dvr = dvr
        self.day = day
        self.out_dir = out_dir
        self.job = None
        self.result_path = None

        self.date_edit = QDateEdit(QDate(day.year, day.month, day.day))
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setCalendarWidget(QCalendarWidget())
        self.date_edit.setDisplayFormat('yyyy-MM-dd')
        self.date_edit.setStyleSheet(BUTTON_STYLE)
        if min_date is not None and min_date.isValid():
            self.date_edit.setMinimumDate(min_date)
        self.date_edit.setMaximumDate(
            max_date if max_date is not None and max_date.isValid() else QDate.currentDate())

        self.from_edit = QTimeEdit(QTime(begin.hour, begin.minute, begin.second))
        self.to_edit = QTimeEdit(QTime(finish.hour, finish.minute, finish.second))
        for edit in (self.from_edit, self.to_edit):
            edit.setDisplayFormat('HH:mm:ss')
            edit.setStyleSheet(BUTTON_STYLE)
        date_row = QHBoxLayout()
        date_row.addWidget(QLabel('Date'))
        date_row.addWidget(self.date_edit)
        date_row.addStretch(1)

        times = QHBoxLayout()
        times.addWidget(QLabel('From'))
        times.addWidget(self.from_edit)
        times.addSpacing(12)
        times.addWidget(QLabel('To'))
        times.addWidget(self.to_edit)
        times.addStretch(1)

        self.include = {}
        self.sound = {}
        cams_box = QGroupBox('Cameras')
        grid = QGridLayout(cams_box)
        grid.setHorizontalSpacing(18)
        grid.setColumnStretch(3, 1)
        grid.addWidget(QLabel('include'), 0, 1, Qt.AlignCenter)
        grid.addWidget(QLabel('sound'), 0, 2, Qt.AlignCenter)
        for row, channel in enumerate(channels, start=1):
            name = QLabel('Camera %d' % channel)
            inc = QCheckBox()
            inc.setChecked(channel == current_channel)
            snd = QCheckBox()
            snd.setChecked(channel == current_channel)
            self.include[channel] = inc
            self.sound[channel] = snd
            grid.addWidget(name, row, 0)
            grid.addWidget(inc, row, 1, Qt.AlignCenter)
            grid.addWidget(snd, row, 2, Qt.AlignCenter)

        self.mute = QCheckBox('Mute (no audio in any output)')
        self.mute.toggled.connect(self._mute_toggled)

        self.separate = QRadioButton('Separate clips')
        self.mosaic = QRadioButton('Combined mosaic grid')
        self.both = QRadioButton('Both')
        self.separate.setChecked(True)
        mode_group = QButtonGroup(self)
        for button in (self.separate, self.mosaic, self.both):
            mode_group.addButton(button)
        mode_box = QGroupBox('Output')
        mode_layout = QVBoxLayout(mode_box)
        for button in (self.separate, self.mosaic, self.both):
            mode_layout.addWidget(button)

        self.status = QLabel('')
        self.status.setStyleSheet('color:#8a8a99;')
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()

        self.export_btn = QPushButton('Export')
        self.export_btn.setStyleSheet(BUTTON_STYLE)
        self.export_btn.clicked.connect(self.start_export)
        self.close_btn = QPushButton('Close')
        self.close_btn.setStyleSheet(BUTTON_STYLE)
        self.close_btn.clicked.connect(self.reject)
        self.open_btn = QPushButton('Open folder')
        self.open_btn.setStyleSheet(BUTTON_STYLE)
        self.open_btn.clicked.connect(self._open_folder)
        self.open_btn.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.export_btn)
        buttons.addWidget(self.close_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(date_row)
        layout.addLayout(times)
        layout.addWidget(cams_box)
        layout.addWidget(self.mute)
        layout.addWidget(mode_box)
        layout.addWidget(self.bar)
        layout.addLayout(buttons)

    def _mute_toggled(self, muted):
        for box in self.sound.values():
            box.setEnabled(not muted)

    def _collect(self):
        cameras = [ch for ch, box in self.include.items() if box.isChecked()]
        if self.mute.isChecked():
            audio = set()
        else:
            audio = {ch for ch in cameras if self.sound[ch].isChecked()}
        mode = 'separate' if self.separate.isChecked() else (
            'mosaic' if self.mosaic.isChecked() else 'both')
        day = self.date_edit.date().toPython()
        midnight = datetime.combine(day, datetime.min.time())
        begin = midnight + timedelta(seconds=self._secs(self.from_edit))
        finish = midnight + timedelta(seconds=self._secs(self.to_edit))
        return cameras, begin, finish, mode, audio

    @staticmethod
    def _secs(edit):
        value = edit.time()
        return value.hour() * 3600 + value.minute() * 60 + value.second()

    def start_export(self):
        cameras, begin, finish, mode, audio = self._collect()
        if not cameras:
            self.status.setText('pick at least one camera')
            return
        if (finish - begin).total_seconds() < 1:
            self.status.setText('"To" must be after "From"')
            return
        if finish > datetime.now():
            finish = datetime.now()
        self.job = ExportJob(self.dvr, cameras, begin, finish, mode, audio,
                             self.out_dir, runtime.aspect_args())
        self.job.progress.connect(self.status.setText)
        self.job.done.connect(self._finished)
        self.export_btn.setEnabled(False)
        self.open_btn.hide()
        self.bar.show()
        minutes = (finish - begin).total_seconds() / 60.0
        self.status.setText('exporting %.1f min across %d camera(s)...' % (minutes, len(cameras)))
        self.job.start()

    def _finished(self, ok, detail):
        self.bar.hide()
        self.export_btn.setEnabled(True)
        if ok:
            self.result_path = detail
            self.status.setText('saved to %s' % os.path.dirname(detail))
            self.open_btn.show()
        else:
            self.status.setText('cancelled' if detail == 'cancelled' else 'failed: %s' % detail)

    def _open_folder(self):
        os.startfile(self.out_dir)

    def reject(self):
        if self.job is not None and self.job.isRunning():
            self.job.cancel()
            self.job.wait(5000)
        super().reject()


class EventsDialog(QDialog):
    def __init__(self, parent, view):
        super().__init__(parent)
        self.view = view
        self.setWindowTitle('Motion events')
        self.resize(300, 460)
        self.setStyleSheet('QDialog { background:#121218; } QLabel { color:#9a9aa8; }'
                           'QListWidget { background:#17171f; color:#c8c8d4;'
                           ' border:1px solid #2c2c36; }')
        self.filter = QComboBox()
        self.filter.addItems(['All', 'Person', 'Vehicle'])
        self.filter.setStyleSheet(COMBO_STYLE)
        self.filter.currentIndexChanged.connect(self.refresh)
        self.list = QListWidget()
        self.list.itemActivated.connect(self._jump)
        head = QHBoxLayout()
        head.addWidget(QLabel('Show'))
        head.addWidget(self.filter)
        head.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(QLabel('Double-click an event to jump to it'))
        layout.addWidget(self.list, 1)
        self.refresh()

    def refresh(self):
        self.list.clear()
        want = self.filter.currentText()
        for start, _end, target, hits in self.view.episodes:
            tag = target or ''
            if want == 'Person' and 'human' not in tag:
                continue
            if want == 'Vehicle' and 'vehicle' not in tag:
                continue
            item = QListWidgetItem('%s    %s    %d hit%s' % (
                start.strftime('%H:%M:%S'), target or 'motion', hits,
                '' if hits == 1 else 's'))
            item.setData(Qt.UserRole, seconds_of(start))
            self.list.addItem(item)
        if self.list.count() == 0:
            self.list.addItem('no events for this filter')

    def _jump(self, item):
        seconds = item.data(Qt.UserRole)
        if seconds is not None:
            self.view.seek_to(max(0, seconds - PREROLL), from_jump=True)


class PlaybackView(QWidget):
    status = Signal(str)

    def __init__(self, dvr, channels, store):
        super().__init__()
        self.dvr = dvr
        self.channels = channels
        self.store = store
        self.episodes = []
        self.hit_index = None
        self.segments = []
        self.day_jobs = []
        self.audio = AudioPlayer()
        self.view_note = ''
        self.selection = None
        self.note = ''
        self._autoplay = False
        self._resume_at = None
        self._events_dialog = None
        self.grid_mode = False
        self.grid_engines = {}
        self.grid_labels = {}

        self.engine = Playback(dvr, FOCUS_SIZE)
        self.engine.frame_ready.connect(self._on_engine_frame)
        self.engine.position.connect(self.on_position)
        self.engine.state.connect(self.on_engine_state)
        self.engine.ended.connect(self.on_engine_ended)

        self.camera = QComboBox()
        for channel in channels:
            self.camera.addItem('Camera %d' % channel, channel)
        self.camera.currentIndexChanged.connect(self.change_camera)

        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setCalendarWidget(QCalendarWidget())
        self.date.setDisplayFormat('yyyy-MM-dd')
        self.date.setDate(QDate.currentDate())
        self.date.setMaximumDate(QDate.currentDate())
        self.date.dateChanged.connect(self.reload_day)
        self.date.calendarWidget().currentPageChanged.connect(self.mark_month)

        self.prev_button = QPushButton('<')
        self.prev_button.clicked.connect(lambda: self.step_day(-1))
        self.next_button = QPushButton('>')
        self.next_button.clicked.connect(lambda: self.step_day(1))

        self.play_button = QPushButton('Play')
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button = QPushButton('Stop')
        self.stop_button.clicked.connect(self.stop_playback)
        self.grid_btn = QPushButton('Grid')
        self.grid_btn.setCheckable(True)
        self.grid_btn.clicked.connect(self.toggle_grid)

        self.speed_box = QComboBox()
        for value in SPEEDS:
            self.speed_box.addItem('%gx' % value, value)
        self.speed_box.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_box.currentIndexChanged.connect(self.change_speed)

        self.step_back_btn = QPushButton('|<')
        self.step_back_btn.clicked.connect(self.step_back_all)
        self.step_fwd_btn = QPushButton('>|')
        self.step_fwd_btn.clicked.connect(self.step_all)

        self.audio_button = QPushButton('Audio')
        self.audio_button.setCheckable(True)
        self.audio_button.clicked.connect(self.toggle_audio)
        self.snap_button = QPushButton('Snap')
        self.snap_button.clicked.connect(self.take_snapshot)
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
        self.events_btn = QPushButton('Events')
        self.events_btn.clicked.connect(self.show_events)
        self.export_button = QPushButton('Export')
        self.export_button.clicked.connect(self.export_selection)

        self.readout = QLabel('click the timeline to play')
        self.readout.setStyleSheet('color:#8a8a99; font-size:11px;')

        bar = FlowLayout(spacing=6)
        for widget in (self.camera, self.prev_button, self.date, self.next_button,
                       self.play_button, self.stop_button, self.speed_box,
                       self.step_back_btn, self.back10, self.fwd10, self.step_fwd_btn,
                       self.prev_hit, self.next_hit, self.events_btn, self.grid_btn,
                       self.audio_button, self.snap_button, self.zoom_out, self.zoom_in,
                       self.fit_button, self.export_button, self.readout):
            widget.setStyleSheet(BUTTON_STYLE)
            bar.addWidget(widget)
        self.camera.setStyleSheet(COMBO_STYLE)
        self.speed_box.setStyleSheet(COMBO_STYLE)
        self.readout.setStyleSheet('color:#8a8a99; font-size:11px;')
        toolbar = QWidget()
        toolbar.setLayout(bar)
        policy = toolbar.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Minimum)
        toolbar.setSizePolicy(policy)

        self.video = VideoLabel('Playback')
        self.video.setText('pick a day, then click the timeline')

        self.grid_widget = QWidget()
        grid_layout = QGridLayout(self.grid_widget)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(4)
        columns = max(1, math.ceil(len(channels) ** 0.5))
        for index, channel in enumerate(channels):
            label = VideoLabel('Camera %d' % channel)
            self.grid_labels[channel] = label
            grid_layout.addWidget(label, index // columns, index % columns)
        self.grid_widget.hide()

        for widget, tip in (
                (self.camera, 'Camera'), (self.date, 'Date'),
                (self.prev_button, 'Previous day'), (self.next_button, 'Next day'),
                (self.play_button, 'Play / pause (Space or K)'),
                (self.stop_button, 'Stop playback'),
                (self.grid_btn, 'Show all cameras at once, synced to the same time'),
                (self.speed_box, 'Playback speed'),
                (self.step_back_btn, 'Previous frame (,)'),
                (self.step_fwd_btn, 'Next frame (.)'),
                (self.back10, 'Back 10 seconds (Left or J)'),
                (self.fwd10, 'Forward 10 seconds (Right or L)'),
                (self.prev_hit, 'Jump to previous motion event'),
                (self.next_hit, 'Jump to next motion event'),
                (self.events_btn, 'List all motion events (filter by person / vehicle)'),
                (self.audio_button, 'Toggle audio (1x only)'),
                (self.snap_button, 'Save the current frame as an image'),
                (self.zoom_out, 'Zoom timeline out'), (self.zoom_in, 'Zoom timeline in'),
                (self.fit_button, 'Fit the whole day'),
                (self.export_button, 'Export footage: pick time range, cameras, and mosaic')):
            widget.setToolTip(tip)

        self.timeline = TimelineBar()
        self.timeline.setToolTip('Click or drag to move the playhead. '
                                 'Shift+drag to select a range. Scroll to zoom, double-click to zoom in.')
        self.timeline.seek_requested.connect(self.seek_to)
        self.timeline.selection_changed.connect(self.on_selection)
        self.timeline.view_changed.connect(self.on_view_changed)

        for keys, slot in (('Space', self.toggle_play), ('K', self.toggle_play),
                           ('Left', lambda: self.nudge(-10)), ('J', lambda: self.nudge(-10)),
                           ('Right', lambda: self.nudge(10)), ('L', lambda: self.nudge(10)),
                           (',', self.step_back_all), ('.', self.step_all)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(toolbar)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.grid_widget, 1)
        layout.addWidget(self.timeline)

        self.loaded = False
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.ticker.start(500)

    def ensure_loaded(self):
        if self.loaded:
            return
        self.loaded = True
        self._autoplay = True
        self.apply_date_bounds()
        self.reload_day()

    def apply_date_bounds(self):
        channels = list(self.channels)
        dvr = self.dvr

        def compute():
            best = None
            for channel in channels:
                try:
                    found = dvr.earliest_recording(channel)
                except Exception:
                    found = None
                if found and (best is None or found < best):
                    best = found
            return best

        job = CallWorker(compute)
        job.done.connect(self._date_bounds_ready)
        self.day_jobs.append(job)
        job.start()

    def _date_bounds_ready(self, earliest, error):
        self.date.setMaximumDate(QDate.currentDate())
        if error is None and earliest is not None:
            self.date.setMinimumDate(QDate(earliest.year, earliest.month, earliest.day))

    def on_view_changed(self, start, end):
        self.view_note = 'view %s - %s' % (clock(start), clock(end))

    def _primary(self):
        if self.grid_mode:
            return self.grid_engines.get(self.channels[0])
        return self.engine

    def _active_engines(self):
        if self.grid_mode:
            return list(self.grid_engines.values())
        return [self.engine]

    def nudge(self, delta):
        primary = self._primary()
        if primary is None or primary.current_time() is None:
            return
        for engine in self._active_engines():
            engine.nudge(delta)

    def toggle_play(self):
        primary = self._primary()
        if primary is None:
            return
        if primary.is_playing():
            for engine in self._active_engines():
                engine.toggle_pause()
        elif primary.current_time() is not None:
            for engine in self._active_engines():
                engine.play_at(engine.current_time() or primary.current_time())

    def step_all(self):
        for engine in self._active_engines():
            engine.step()

    def step_back_all(self):
        for engine in self._active_engines():
            engine.step_back()

    def change_speed(self, index):
        speed = self.speed_box.itemData(index)
        for engine in self._active_engines():
            engine.set_speed(speed)
        self.sync_audio()

    def toggle_audio(self):
        self.audio_button.setChecked(not (self.audio.slot is not None))
        self.sync_audio()

    def sync_audio(self):
        primary = self._primary()
        want = (primary is not None and self.audio_button.isChecked()
                and primary.is_playing() and not primary.paused and primary.speed == 1.0)
        if not want:
            self.audio.stop()
            return
        spec = primary.audio_spec()
        if spec is not None:
            self.audio.play_local(spec[0], spec[1])

    def toggle_grid(self, checked):
        if checked:
            self._enter_grid()
        else:
            self._exit_grid()

    def _enter_grid(self):
        target = self.engine.current_time()
        self.engine.stop()
        self.audio.stop()
        self.video.hide()
        self.grid_widget.show()
        self.grid_mode = True
        self.camera.setEnabled(False)
        if not self.grid_engines:
            for channel in self.channels:
                engine = Playback(self.dvr, GRID_TILE)
                engine.frame_ready.connect(self.grid_labels[channel].set_frame)
                self.grid_engines[channel] = engine
            primary = self.grid_engines[self.channels[0]]
            primary.position.connect(self.on_position)
            primary.state.connect(self.on_engine_state)
        self._load_grid(target)

    def _load_grid(self, target):
        day = self.current_day()
        channels = list(self.channels)
        dvr = self.dvr
        self.note = 'loading all cameras...'

        def fetch():
            out = {}
            for channel in channels:
                begin = datetime.combine(day, datetime.min.time())
                try:
                    out[channel] = dvr.playback_segments(
                        channel, begin, begin + timedelta(days=1), max_results=400)
                except Exception:
                    out[channel] = []
            return out

        job = CallWorker(fetch)
        job.done.connect(lambda result, error: self._grid_loaded(day, target, result, error))
        self.day_jobs.append(job)
        job.start()

    def _grid_loaded(self, day, target, result, error):
        if error is not None or result is None or not self.grid_mode or day != self.current_day():
            return
        for channel, engine in self.grid_engines.items():
            engine.configure(channel, day, result.get(channel, []))
        when = target
        if when is None or when.date() != day:
            latest = max((segs[-1][1] for segs in result.values() if segs), default=None)
            if latest is not None:
                when = min(datetime.now() - timedelta(seconds=5), latest - timedelta(seconds=3))
        if when is not None:
            midnight = datetime.combine(day, datetime.min.time())
            self.seek_to((when - midnight).total_seconds())

    def _exit_grid(self):
        self.grid_mode = False
        self.camera.setEnabled(True)
        for engine in self.grid_engines.values():
            engine.stop()
        self.audio.stop()
        self.grid_widget.hide()
        self.video.show()
        self.note = 'single camera'

    def current_channel(self):
        return self.camera.currentData()

    def current_day(self):
        return self.date.date().toPython()

    def step_day(self, delta):
        self.date.setDate(self.date.date().addDays(delta))

    def change_camera(self):
        self._resume_at = self.engine.current_time()
        self.reload_day()

    def reload_day(self):
        self.stop_playback()
        channel = self.current_channel()
        day = self.current_day()
        start = datetime.combine(day, datetime.min.time())
        self.timeline.set_position(None)
        self.timeline.clear_selection()
        self.load_detections()
        self.label_cameras()
        self.timeline.set_segments([], day)
        self.note = 'loading %s ...' % day.isoformat()

        def fetch():
            return (self.dvr.playback_segments(channel, start, start + timedelta(days=1),
                                               max_results=400),
                    self.dvr.record_days(channel, day.year, day.month))

        job = CallWorker(fetch)
        job.done.connect(
            lambda result, error: self._day_loaded(day, channel, result, error))
        self.day_jobs.append(job)
        job.start()

    def _day_loaded(self, day, channel, result, error):
        if day != self.current_day() or channel != self.current_channel():
            return
        if error is not None or result is None:
            self.note = 'could not load %s: %s' % (day.isoformat(), error)
            return
        segments, days = result
        self.segments = segments
        self.engine.configure(channel, day, segments)
        self.timeline.set_segments([(s, e) for s, e, _n in segments], day)
        self.mark_calendar(days)
        total = sum(b - a for a, b in self.timeline.segments)
        self.note = '%s   %d segment(s)   %.1f h recorded   %d motion event(s)' % (
            day.isoformat(), len(self.timeline.segments), total / 3600.0,
            len(self.episodes))
        if self.grid_mode:
            primary = self._primary()
            self._load_grid(primary.current_time() if primary else None)
            return
        midnight = datetime.combine(day, datetime.min.time())
        resume = self._resume_at
        self._resume_at = None
        if resume is not None and resume.date() == day and segments:
            secs = (resume - midnight).total_seconds()
            if secs > 0:
                self.seek_to(secs)
                return
        if self._autoplay and segments:
            self._autoplay = False
            target = min(datetime.now() - timedelta(seconds=5),
                         segments[-1][1] - timedelta(seconds=3))
            secs = (target - midnight).total_seconds()
            if secs > 0:
                self.seek_to(secs)

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
        if self._events_dialog is not None and self._events_dialog.isVisible():
            self._events_dialog.refresh()

    def show_events(self):
        if self._events_dialog is None:
            self._events_dialog = EventsDialog(self, self)
        self._events_dialog.refresh()
        self._events_dialog.show()
        self._events_dialog.raise_()
        self._events_dialog.activateWindow()

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

    def mark_month(self, year, month):
        channel = self.current_channel()

        def fetch():
            return self.dvr.record_days(channel, year, month)

        job = CallWorker(fetch)
        job.done.connect(lambda days, error: self._month_marked(year, month, channel, days, error))
        self.day_jobs.append(job)
        job.start()

    def _month_marked(self, year, month, channel, days, error):
        widget = self.date.calendarWidget()
        if error is not None or widget is None or channel != self.current_channel() or not days:
            return
        available = QTextCharFormat()
        available.setForeground(QColor('#7fe0a0'))
        for number, _kind in days:
            widget.setDateTextFormat(QDate(year, month, number), available)

    def mark_calendar(self, days):
        widget = self.date.calendarWidget()
        if widget is None:
            return
        widget.setDateTextFormat(QDate(), QTextCharFormat())
        if not days:
            return
        day = self.current_day()
        available = QTextCharFormat()
        available.setForeground(QColor('#7fe0a0'))
        for number, _kind in days:
            widget.setDateTextFormat(QDate(day.year, day.month, number), available)

    def seek_to(self, seconds, from_jump=False):
        if not from_jump:
            self.hit_index = None
        self.audio.stop()
        midnight = datetime.combine(self.current_day(), datetime.min.time())
        start = midnight + timedelta(seconds=int(seconds))
        if start >= datetime.now():
            self.note = 'that time is in the future'
            return
        mark = seconds_of(start)
        if not self.timeline.view_start <= mark <= self.timeline.view_end:
            self.timeline.centre_on(mark)
        self.timeline.set_position(mark)
        played = False
        for engine in self._active_engines():
            if engine.play_at(start):
                played = True
        if played:
            self.speed_box.setCurrentIndex(SPEEDS.index(1.0))
            self.note = 'playing from %s' % start.strftime('%H:%M:%S')

    def stop_playback(self):
        self.audio.stop()
        self.engine.stop()
        for engine in self.grid_engines.values():
            engine.stop()
        self.audio_button.setChecked(False)
        self.play_button.setText('Play')

    def take_snapshot(self):
        os.makedirs(CAPTURES, exist_ok=True)
        if self.grid_mode:
            frames = [(ch, lbl.frame) for ch, lbl in self.grid_labels.items()
                      if lbl.frame is not None]
        elif self.video.frame is not None:
            frames = [(self.current_channel(), self.video.frame)]
        else:
            frames = []
        saved = [p for p in (self._save_frame(ch, img) for ch, img in frames) if p]
        if not saved:
            self.note = 'no frame to snapshot yet'
        elif len(saved) == 1:
            self.note = 'saved %s' % os.path.basename(saved[0])
        else:
            self.note = 'saved %d snapshots' % len(saved)

    def _save_frame(self, channel, image):
        path = os.path.join(CAPTURES, 'snap-ch%d-%s.jpg' % (
            channel, datetime.now().strftime('%Y%m%d-%H%M%S')))
        return path if image.save(path, 'JPG') else None

    def _on_engine_frame(self, image):
        self.video.set_frame(image)

    def on_position(self, seconds):
        self.timeline.set_position(seconds)
        self.readout.setText(clock(seconds))

    def on_engine_state(self, state):
        self.play_button.setText('Pause' if state == 'playing' else 'Play')
        self.video.overlay_text = 'Buffering...' if state == 'buffering' else ''
        self.video.update()
        self.sync_audio()
        self.note = state

    def on_engine_ended(self):
        self.note = 'reached the end'

    def on_selection(self, selection):
        self.selection = selection

    def export_selection(self):
        day = self.current_day()
        midnight = datetime.combine(day, datetime.min.time())
        if self.selection:
            begin = midnight + timedelta(seconds=int(self.selection[0]))
            finish = midnight + timedelta(seconds=int(self.selection[1]))
        else:
            primary = self._primary()
            current = (primary.current_time() if primary else None) or datetime.now()
            begin = current
            finish = current + timedelta(minutes=1)
        dialog = ExportDialog(self, self.dvr, self.channels, day, begin, finish,
                              self.current_channel(), CAPTURES,
                              self.date.minimumDate(), self.date.maximumDate())
        dialog.exec()

    def tick(self):
        self.day_jobs = [job for job in self.day_jobs if not job.isFinished()]
        if not self.isVisible():
            return
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
        if self.engine.is_playing():
            self.stop_playback()
            self.note = 'stopped, click the timeline to resume'

    def shutdown(self):
        self.ticker.stop()
        self.stop_playback()
        self.engine.shutdown()
        for engine in self.grid_engines.values():
            engine.shutdown()
        for job in self.day_jobs:
            job.wait(2000)
        self.day_jobs.clear()


IPC_NAME = 'HikViewerShowRequest'
MUTEX_NAME = 'Local\\HikViewerSingleInstance'
ERROR_ALREADY_EXISTS = 183
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
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _instance_mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def ask_running_instance_to_show(attempts=6):
    for _ in range(attempts):
        socket = QLocalSocket()
        socket.connectToServer(IPC_NAME)
        if socket.waitForConnected(400):
            socket.write(b'show')
            socket.waitForBytesWritten(400)
            socket.flush()
            socket.waitForDisconnected(400)
            return True
        QThread.msleep(300)
    return False


class MainWindow(QMainWindow):
    def __init__(self, dvr, channels):
        super().__init__()
        self.setWindowTitle('CCTV')
        self.resize(1480, 880)
        self.setStyleSheet('background:#08080b;')
        self.setWindowIcon(app_icon())
        self.quitting = False
        self.chores = []

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

        self.upkeep = QTimer(self)
        self.upkeep.timeout.connect(self.housekeeping)
        self.upkeep.start(30 * 60 * 1000)
        QTimer.singleShot(5000, self.housekeeping)

        self.hang = QTimer(self)
        self.hang.timeout.connect(self.rearm_hang_dump)
        self.hang.start(5000)
        runtime.arm_hang_dump(HANG_SECONDS)

        self.watcher = EventWatcher(dvr, self.store)
        self.watcher.detected.connect(self.on_detected)
        self.watcher.trouble.connect(self.on_watcher_trouble)
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

        fullscreen = QShortcut(QKeySequence('F11'), self)
        fullscreen.activated.connect(self.toggle_fullscreen)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def on_ipc_request(self):
        connection = self.ipc.nextPendingConnection()
        if connection is not None:
            connection.readyRead.connect(connection.deleteLater)
        self.restore_window()

    def rearm_hang_dump(self):
        runtime.arm_hang_dump(HANG_SECONDS)

    def housekeeping(self):
        runtime.log.info('memory %.0f MB, %d detection(s) stored',
                         runtime.process_rss_mb(), self.store.counts()[0])
        self.chores = [job for job in self.chores if not job.isFinished()]
        job = CallWorker(self.store.prune)
        self.chores.append(job)
        job.start()

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

    def on_watcher_trouble(self, detail):
        self.live.note = 'motion feed dropped, retrying'

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
        self.upkeep.stop()
        self.hang.stop()
        runtime.cancel_hang_dump()
        runtime.remove_pid_file()
        self.tray.hide()
        self.ipc.close()
        QLocalServer.removeServer(IPC_NAME)
        self.watcher.stop()
        self.watcher.wait(2000)
        self.playback.shutdown()
        self.live.shutdown()
        for job in self.chores:
            job.wait(2000)
        self.chores.clear()
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
    qt.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)
    if already_running():
        if ask_running_instance_to_show():
            close_splash()
            return 0
        runtime.log.warning('running instance did not respond, taking over')
        runtime.kill_stale_instance()
    qt.setQuitOnLastWindowClosed(False)
    runtime.write_pid_file()
    missing = [tool for tool in ('ffmpeg', 'ffplay') if not find_tool(tool)]
    if missing:
        close_splash()
        QMessageBox.critical(
            None, 'CCTV',
            'Missing required tool(s): %s\n\n'
            'Install FFmpeg and make sure it is on PATH, for example:\n'
            '    winget install Gyan.FFmpeg' % ', '.join(missing))
        return 1
    here = (os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)
            else os.path.dirname(os.path.abspath(__file__)))
    dvr = login.obtain_dvr(here, close_splash)
    if dvr is None:
        close_splash()
        return 0

    saved = config.load() or {}
    channels = saved.get('channels') or []
    if channels:
        runtime.log.info('using cached channel list %s', channels)
    else:
        channels = dvr.live_channels()
        if not channels:
            close_splash()
            QMessageBox.critical(
                None, 'CCTV',
                'Connected to %s but no cameras are sending video.' % dvr.ip)
            return 1
        config.remember_channels(channels)

    window = MainWindow(dvr, channels)
    close_splash()
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
