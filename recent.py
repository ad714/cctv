import os
import subprocess
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QListWidget, QListWidgetItem, QMenu, QProgressBar,
                               QPushButton, QSplitter, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

import config
import runtime
import theme
from export import ExportJob
from events import clip_window, save_thumbnail, thumb_path
from player import Playback
from widgets import AudioPlayer, CallWorker, VideoLabel

FETCH_SLACK = timedelta(seconds=2)
ROW_HEIGHT = 96
HEADER_HEIGHT = 36
THUMB = QSize(120, 90)
EVENT_ROLE = Qt.UserRole
HEADER_ROLE = Qt.UserRole + 1
OVERLAY_TEXT = {'buffering': 'Buffering',
                'waiting': 'Waiting for the DVR to save the latest seconds'}
RANGES = (('Today', 0), ('Yesterday', 1), ('7 days', 7))
KINDS = (('People and vehicles', None), ('People', 'person'), ('Vehicles', 'vehicle'))


def ago(moment, now=None):
    seconds = int(((now or datetime.now()) - moment).total_seconds())
    if seconds < 45:
        return 'just now'
    if seconds < 3600:
        return '%d min ago' % max(1, round(seconds / 60))
    if seconds < 6 * 3600:
        hours, minutes = divmod(seconds // 60, 60)
        return '%d h %d min ago' % (hours, minutes) if minutes else '%d h ago' % hours
    return ''


def day_title(day, today=None):
    today = today or datetime.now().date()
    if day == today:
        return 'Today'
    if day == today - timedelta(days=1):
        return 'Yesterday'
    return day.strftime('%A %d %B').replace(' 0', ' ')


def length_text(event):
    seconds = max(1, int((event.end - event.start).total_seconds()) + 1)
    return '%d s' % seconds if seconds < 60 else '%d min %d s' % divmod(seconds, 60)


def camera_name(channel):
    return 'Camera %d' % channel


class EventDelegate(QStyledItemDelegate):
    def __init__(self, parent, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.cache = {}
        self.title_font = theme.app_font(10.5, QFont.DemiBold)
        self.body_font = theme.app_font(9.5, tabular=True)
        self.header_font = theme.app_font(9.5, QFont.DemiBold)

    def sizeHint(self, option, index):
        if index.data(HEADER_ROLE):
            return QSize(option.rect.width(), HEADER_HEIGHT)
        return QSize(option.rect.width(), 72 if self.compact else ROW_HEIGHT)

    def thumb(self, path, size):
        key = (path, size.width())
        pixmap = self.cache.get(key)
        if pixmap is None:
            pixmap = QPixmap(path) if path else QPixmap()
            if not pixmap.isNull():
                pixmap = pixmap.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if len(self.cache) > 400:
                self.cache.clear()
            self.cache[key] = pixmap
        return pixmap

    def forget(self, path):
        for key in [k for k in self.cache if k[0] == path]:
            self.cache.pop(key, None)

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect
        header = index.data(HEADER_ROLE)
        if header:
            painter.setFont(self.header_font)
            painter.setPen(QColor(theme.TEXT_2))
            painter.drawText(rect.adjusted(14, 0, -14, -6), Qt.AlignLeft | Qt.AlignBottom, header)
            painter.restore()
            return
        event = index.data(EVENT_ROLE)
        selected = option.state & QStyle.State_Selected
        hovered = option.state & QStyle.State_MouseOver
        if selected or hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.RAISED if selected else '#191816'))
            painter.drawRoundedRect(QRectF(rect.adjusted(6, 2, -6, -2)), 7, 7)

        size = QSize(80, 60) if self.compact else THUMB
        box = QRectF(rect.left() + 14, rect.center().y() - size.height() / 2 + 1,
                     size.width(), size.height())
        clip = QPainterPath()
        clip.addRoundedRect(box, 5, 5)
        painter.setClipPath(clip)
        pixmap = self.thumb(event.snapshot, size)
        if pixmap.isNull():
            painter.fillRect(box, QColor(theme.RAISED))
            theme.draw_kind(painter, box.adjusted(box.width() * 0.36, box.height() * 0.3,
                                                  -box.width() * 0.36, -box.height() * 0.3),
                            event.target)
        else:
            painter.drawPixmap(box.toRect(), pixmap)
        painter.setClipping(False)

        left = box.right() + 14
        width = rect.right() - left - 28
        line = rect.center().y()
        theme.draw_kind(painter, QRectF(left, line - 21, 15, 15), event.target)
        painter.setFont(self.title_font)
        painter.setPen(QColor(theme.TEXT))
        title = theme.kind_label(event.target) + ('' if self.compact else '  ')
        title = painter.fontMetrics().elidedText(title, Qt.ElideRight, int(width - 21))
        painter.drawText(QRectF(left + 21, line - 25, width - 21, 22),
                         Qt.AlignLeft | Qt.AlignVCenter, title)
        if not self.compact:
            advance = painter.fontMetrics().horizontalAdvance(title)
            painter.setFont(self.body_font)
            painter.setPen(QColor(theme.TEXT_2))
            painter.drawText(QRectF(left + 21 + advance, line - 25, width - 21 - advance, 22),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             painter.fontMetrics().elidedText(camera_name(event.channel),
                                                              Qt.ElideRight,
                                                              int(width - 21 - advance)))
        painter.setFont(self.body_font)
        painter.setPen(QColor(theme.TEXT_2))
        stamp = event.start.strftime('%H:%M:%S')
        painter.drawText(QRectF(left, line + 1, width, 22), Qt.AlignLeft | Qt.AlignVCenter, stamp)
        advance = painter.fontMetrics().horizontalAdvance(stamp + '   ')
        relative = ago(event.start) or length_text(event)
        painter.setPen(QColor(theme.TEXT_3))
        painter.drawText(QRectF(left + advance, line + 1, width - advance, 22),
                         Qt.AlignLeft | Qt.AlignVCenter, relative)
        if not event.seen:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(theme.PERSON))
            painter.drawEllipse(QRectF(rect.right() - 22, line - 4, 8, 8))
        painter.restore()


class EventList(QListWidget):
    picked = Signal(object)
    activated_event = Signal(object)
    save_requested = Signal(object)
    playback_requested = Signal(object)

    def __init__(self, compact=False):
        super().__init__()
        self.delegate = EventDelegate(self, compact)
        self.setItemDelegate(self.delegate)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setUniformItemSizes(False)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.currentItemChanged.connect(self._changed)
        self.itemDoubleClicked.connect(self._double)
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.viewport().update)
        self.ticker.start(30000)

    def set_events(self, events, keep=None, headers=True):
        self.blockSignals(True)
        self.clear()
        current_day = None
        chosen = None
        today = datetime.now().date()
        for event in events:
            if headers and event.start.date() != current_day:
                current_day = event.start.date()
                item = QListWidgetItem()
                item.setData(HEADER_ROLE, day_title(current_day, today))
                item.setFlags(Qt.NoItemFlags)
                self.addItem(item)
            item = QListWidgetItem()
            item.setData(EVENT_ROLE, event)
            item.setToolTip('%s at %s, %s' % (theme.kind_label(event.target),
                                                camera_name(event.channel),
                                                event.start.strftime('%H:%M:%S')))
            self.addItem(item)
            if keep is not None and event.id == keep:
                chosen = item
        if chosen is not None:
            self.setCurrentItem(chosen)
        self.blockSignals(False)
        return chosen is not None

    def events(self):
        return [self.item(i).data(EVENT_ROLE) for i in range(self.count())
                if self.item(i).data(EVENT_ROLE) is not None]

    def first_event_item(self):
        for i in range(self.count()):
            if self.item(i).data(EVENT_ROLE) is not None:
                return self.item(i)
        return None

    def replace(self, event):
        for i in range(self.count()):
            item = self.item(i)
            old = item.data(EVENT_ROLE)
            if old is not None and old.id == event.id:
                self.delegate.forget(old.snapshot)
                item.setData(EVENT_ROLE, event)
                return True
        return False

    def mark_seen_local(self, ids):
        ids = set(ids)
        for event in self.events():
            if event.id in ids:
                event.seen = True
        self.viewport().update()

    def _changed(self, current, _previous):
        if current is not None and current.data(EVENT_ROLE) is not None:
            self.picked.emit(current.data(EVENT_ROLE))

    def _double(self, item):
        if item.data(EVENT_ROLE) is not None:
            self.activated_event.emit(item.data(EVENT_ROLE))

    def _menu(self, point):
        item = self.itemAt(point)
        event = item.data(EVENT_ROLE) if item is not None else None
        if event is None:
            return
        menu = QMenu(self)
        menu.addAction('Save clip', lambda: self.save_requested.emit(event))
        menu.addAction('Open in Playback', lambda: self.playback_requested.emit(event))
        menu.exec(self.viewport().mapToGlobal(point))


class ClipSaver(QWidget):
    finished = Signal(bool, str)

    def __init__(self, dvr):
        super().__init__()
        self.dvr = dvr
        self.job = None
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedWidth(120)
        self.label = theme.set_role(QLabel(''), 'muted')
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.bar)
        row.addWidget(self.label)
        self.bar.hide()

    def busy(self):
        return self.job is not None and self.job.isRunning()

    def save(self, event, out_dir):
        if self.busy():
            return
        begin, finish = clip_window(event)
        finish = min(datetime.now(), finish)
        if event.clip and os.path.exists(event.clip):
            target = os.path.join(out_dir, 'clip-ch%d-%s-%s.mp4' % (
                event.channel, begin.strftime('%Y%m%d-%H%M%S'), finish.strftime('%H%M%S')))
            command = (['ffmpeg', '-y', '-loglevel', 'error', '-i', event.clip,
                        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21',
                        '-c:a', 'aac', '-b:a', '64k'] + runtime.aspect_args()
                       + ['-movflags', '+faststart', target])

            def encode():
                subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=runtime.NO_WINDOW)
                return target

            self.job = CallWorker(encode)
            self.job.done.connect(lambda result, error: self._done(
                error is None, result if error is None else str(error)))
            self.bar.setRange(0, 0)
            self.bar.show()
            self.label.setText('Saving clip')
            self.job.start()
            return
        self.bar.setRange(0, 1000)
        self.job = ExportJob(self.dvr, [event.channel], begin, finish, 'separate',
                             {event.channel}, out_dir, runtime.aspect_args())
        self.job.fraction.connect(self._progress)
        self.job.done.connect(self._done)
        self.bar.setValue(0)
        self.bar.show()
        self.label.setText('Saving clip')
        self.job.start()

    def _progress(self, share, remaining):
        self.bar.setValue(int(share * 1000))
        if remaining >= 0:
            self.label.setText('Saving clip, %d s left' % max(1, round(remaining)))

    def _done(self, ok, detail):
        self.bar.hide()
        self.label.setText('Clip saved to Album' if ok else (
            'Cancelled' if detail == 'cancelled' else 'Could not save: %s' % detail))
        self.finished.emit(ok, detail)


class RecentView(QWidget):
    open_playback = Signal(int, object)
    open_live = Signal(int)
    catch_up = Signal(int)
    seen_changed = Signal()
    status = Signal(str)

    def __init__(self, dvr, channels, store, size, out_dir):
        super().__init__()
        self.dvr = dvr
        self.channels = channels
        self.store = store
        self.out_dir = out_dir
        self.current = None
        self.moment = None
        self.opened_at = None
        self.finished = False
        self.jobs = []
        self.dirty = True
        self.audio = AudioPlayer()
        self.engine = Playback(dvr, size)
        self.engine.frame_ready.connect(self._frame)
        self.engine.position.connect(self._position)
        self.engine.state.connect(self._state)
        self.engine.set_denoise(config.setting('denoise', 0))

        self.range_buttons = QButtonGroup(self)
        range_row = QHBoxLayout()
        range_row.setSpacing(4)
        for index, (label, _days) in enumerate(RANGES):
            button = theme.set_role(QPushButton(label), 'quiet')
            button.setCheckable(True)
            button.setChecked(index == 0)
            self.range_buttons.addButton(button, index)
            range_row.addWidget(button)
        range_row.addStretch(1)
        read_row = QHBoxLayout()
        read_row.setSpacing(4)
        self.unread = theme.set_role(QPushButton('Unread only'), 'quiet')
        self.unread.setCheckable(True)
        self.mark_all = theme.set_role(QPushButton('Mark all read'), 'quiet')
        read_row.addWidget(self.unread)
        read_row.addStretch(1)
        read_row.addWidget(self.mark_all)
        self.range_buttons.idClicked.connect(lambda _id: self.refresh())
        self.unread.toggled.connect(lambda _on: self.refresh())
        self.mark_all.clicked.connect(self.mark_all_read)

        self.camera = QComboBox()
        self.camera.addItem('All cameras', None)
        for channel in channels:
            self.camera.addItem(camera_name(channel), channel)
        self.kind = QComboBox()
        for label, kind in KINDS:
            self.kind.addItem(label, kind)
        self.camera.currentIndexChanged.connect(lambda _i: self.refresh())
        self.kind.currentIndexChanged.connect(lambda _i: self.refresh())
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(self.camera, 1)
        filter_row.addWidget(self.kind, 1)

        self.list = EventList()
        self.list.picked.connect(lambda event: self.show_event(event))
        self.list.save_requested.connect(lambda event: self.saver.save(event, self.out_dir))
        self.list.playback_requested.connect(
            lambda event: self.open_playback.emit(event.channel, event.start))
        self.empty = theme.set_role(QLabel('Nothing recorded in this range.'), 'faint')
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()

        side = QFrame()
        side.setProperty('role', 'panel')
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(10, 12, 6, 6)
        side_layout.setSpacing(10)
        side_layout.addLayout(range_row)
        side_layout.addLayout(read_row)
        side_layout.addLayout(filter_row)
        side_layout.addWidget(self.list, 1)
        side_layout.addWidget(self.empty, 1)
        side.setMinimumWidth(330)

        self.title = theme.set_role(QLabel('No events yet'), 'title')
        self.subtitle = theme.set_role(QLabel(''), 'muted')
        for label in (self.title, self.subtitle):
            label.setMinimumWidth(10)
        catch = QHBoxLayout()
        catch.setSpacing(2)
        catch.addWidget(theme.set_role(QLabel('All cameras, last'), 'faint'))
        for minutes in (2, 5, 15):
            button = theme.set_role(QPushButton('%d min' % minutes), 'quiet')
            button.setToolTip('Replay every camera together from %d minutes ago' % minutes)
            button.clicked.connect(lambda _c=False, m=minutes: self.catch_up.emit(m))
            catch.addWidget(button)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)

        self.video = VideoLabel('Event')
        self.video.setText('')

        self.play_button = QPushButton('Pause')
        self.play_button.clicked.connect(self.toggle_play)
        self.replay_button = QPushButton('Replay')
        self.replay_button.clicked.connect(self.replay)
        self.sound = theme.set_role(QPushButton('Sound'), 'quiet')
        self.sound.setCheckable(True)
        self.sound.toggled.connect(lambda _on: self._sync_audio())
        self.clock = theme.set_role(QLabel(''), 'muted')
        self.clock.setFont(theme.app_font(10, tabular=True))
        self.playback_button = QPushButton('Open in Playback')
        self.playback_button.clicked.connect(self._to_playback)
        self.live_button = QPushButton('Live view')
        self.live_button.clicked.connect(self._to_live)
        self.save_button = QPushButton('Save clip')
        self.save_button.clicked.connect(lambda: self.current and self.saver.save(
            self.current, self.out_dir))
        self.saver = ClipSaver(dvr)
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addLayout(titles, 1)
        for widget in (self.save_button, self.playback_button, self.live_button):
            head.addWidget(widget, 0, Qt.AlignTop)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        for widget in (self.play_button, self.replay_button, self.sound):
            controls.addWidget(widget)
        controls.addSpacing(8)
        controls.addWidget(self.clock)
        controls.addWidget(self.saver)
        controls.addStretch(1)
        controls.addLayout(catch)

        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(12, 10, 4, 4)
        stage_layout.setSpacing(10)
        stage_layout.addLayout(head)
        stage_layout.addWidget(self.video, 1)
        stage_layout.addLayout(controls)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(side)
        split.addWidget(stage)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([430, 1000])
        split.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(split)
        self._set_actions(False)
        for keys, handler in (('Space', self.toggle_play), ('R', self.replay)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)

    def _set_actions(self, enabled):
        for widget in (self.play_button, self.replay_button, self.sound, self.playback_button,
                       self.live_button, self.save_button):
            widget.setEnabled(enabled)

    def filters(self):
        days = RANGES[max(0, self.range_buttons.checkedId())][1]
        today = datetime.combine(datetime.now().date(), datetime.min.time())
        if days == 0:
            since, until = today, None
        elif days == 1:
            since, until = today - timedelta(days=1), today
        else:
            since, until = today - timedelta(days=days - 1), None
        return dict(since=since, until=until, channel=self.camera.currentData(),
                    kind=self.kind.currentData(), unread=self.unread.isChecked())

    def refresh(self, select_newest=False):
        self.dirty = False
        events = self.store.recent(**self.filters())
        keep = None if select_newest or self.current is None else self.current.id
        kept = self.list.set_events(events, keep=keep)
        self.empty.setVisible(not events)
        self.list.setVisible(bool(events))
        if select_newest or (not kept and self.current is None):
            first = self.list.first_event_item()
            if first is not None:
                self.list.setCurrentItem(first)

    def focus_camera(self, channel):
        index = self.camera.findData(channel)
        if index >= 0:
            self.camera.setCurrentIndex(index)

    def show_by_id(self, event_id):
        event = self.store.get(event_id)
        if event is None:
            return
        if event.start.date() == datetime.now().date():
            self.range_buttons.button(0).setChecked(True)
        self.camera.setCurrentIndex(0)
        self.kind.setCurrentIndex(0)
        self.unread.setChecked(False)
        self.current = event
        self.refresh()
        self.show_event(event, force=True)

    def event_added(self, event_id):
        if not self.isVisible():
            self.dirty = True
            return
        newest_was_current = (self.current is None
                              or self.list.first_event_item() is None
                              or self.list.first_event_item().data(EVENT_ROLE).id
                              == self.current.id)
        self.refresh()
        if newest_was_current and not self.engine.is_playing():
            self.refresh(select_newest=True)

    def snapshot_added(self, event_id):
        event = self.store.get(event_id)
        if event is None:
            return
        if self.list.replace(event):
            self.list.viewport().update()
        if self.current is not None and self.current.id == event_id:
            self.current.snapshot = event.snapshot
            if self.video.frame is None:
                self._poster(event)

    def activate(self):
        if self.dirty:
            self.refresh(select_newest=not self.engine.is_playing())
        elif self.current is None:
            self.refresh(select_newest=True)

    def show_event(self, event, force=False):
        if not force and self.current is not None and self.current.id == event.id \
                and self.engine.is_playing():
            return
        self.current = event
        self.moment = None
        self.audio.stop()
        self.engine.stop()
        self.title.setText('%s at %s' % (theme.kind_label(event.target),
                                         camera_name(event.channel)))
        when = '%s %s' % (day_title(event.start.date()), event.start.strftime('%H:%M:%S'))
        relative = ago(event.start)
        self.subtitle.setText('%s,  %s%s' % (when, length_text(event),
                                             ('  (%s)' % relative) if relative else ''))
        self.video.frame = None
        self._poster(event)
        self._set_actions(True)
        self.finished = False
        self.play_button.setText('Pause')
        if not event.seen:
            self.store.mark_seen([event.id])
            event.seen = True
            self.list.mark_seen_local([event.id])
            self.seen_changed.emit()
        self.opened_at = time.monotonic()
        begin, finish = clip_window(event)
        if event.clip and os.path.exists(event.clip):
            self.engine.fetch_limit = None
            self.engine.play_file(event.clip, event.channel, begin, finish, begin)
            return
        self._load(event)

    def _poster(self, event):
        image = QImage(event.snapshot) if event.snapshot else QImage()
        if image.isNull():
            self.video.frame = None
            self.video.setText('Loading footage')
        else:
            self.video.set_frame(image)

    def _load(self, event):
        dvr = self.dvr
        channel = event.channel
        start = event.start - timedelta(hours=3)
        end = event.end + timedelta(minutes=10)

        def fetch():
            return dvr.playback_segments(channel, start, end, max_results=100)

        job = CallWorker(fetch)
        job.done.connect(lambda segments, error: self._loaded(event, segments, error))
        self.jobs = [j for j in self.jobs if not j.isFinished()] + [job]
        job.start()

    def _loaded(self, event, segments, error):
        if self.current is None or self.current.id != event.id:
            return
        if error is not None or not segments:
            self.clock.setText('No footage for this event' if error is None
                               else 'Could not reach the DVR')
            return
        begin, finish = clip_window(event)
        self.engine.fetch_limit = finish + FETCH_SLACK
        self.engine.configure(event.channel, event.start.date(), segments)
        self.engine.play_at(begin)

    def replay(self):
        if self.current is None:
            return
        self.finished = False
        if self.engine.segments:
            self.engine.play_at(clip_window(self.current)[0])
        else:
            self.show_event(self.current, force=True)

    def toggle_play(self):
        if self.current is None:
            return
        if self.finished:
            self.replay()
        elif not self.engine.is_playing():
            moment = self.engine.current_time()
            if moment is None or not self.engine.play_at(moment):
                self.replay()
        else:
            self.engine.toggle_pause()

    def _frame(self, image):
        self.video.set_frame(image)
        if self.opened_at is not None and self.current is not None:
            runtime.log.info('event %d replay: first frame after %.2fs', self.current.id,
                             time.monotonic() - self.opened_at)
            self.opened_at = None
        event = self.current
        reached = self.moment is not None and event is not None and self.moment >= event.start
        if reached and not event.snapshot:
            path = thumb_path(event.channel, event.start, event.id)
            if save_thumbnail(image, path):
                event.snapshot = path
                self.store.set_snapshot(event.id, path)
                self.list.replace(event)
                self.list.viewport().update()

    def _position(self, seconds):
        if self.current is None:
            return
        midnight = datetime.combine(self.current.start.date(), datetime.min.time())
        moment = midnight + timedelta(seconds=seconds)
        self.moment = moment
        offset = (moment - self.current.start).total_seconds()
        self.clock.setText('%s   %+.0f s' % (moment.strftime('%H:%M:%S'), offset))
        if moment >= clip_window(self.current)[1] and not self.finished:
            self.finished = True
            self._release()

    def _state(self, state):
        self.play_button.setText('Play' if state in ('paused', 'reached the end') else 'Pause')
        self.video.overlay_text = OVERLAY_TEXT.get(state, '')
        self.video.update()
        if state in ('no footage at that time', 'could not load footage'):
            self.clock.setText(state.capitalize())
        self._sync_audio()

    def _sync_audio(self):
        want = (self.sound.isChecked() and self.engine.is_playing() and not self.engine.paused)
        if not want:
            self.audio.stop()
            return
        spec = self.engine.audio_spec()
        if spec is not None:
            self.audio.play_local(spec[0], spec[1])

    def _to_playback(self):
        if self.current is not None:
            self.suspend()
            self.open_playback.emit(self.current.channel, self.current.start)

    def _to_live(self):
        if self.current is not None:
            self.suspend()
            self.open_live.emit(self.current.channel)

    def mark_all_read(self):
        self.store.mark_all_seen(self.camera.currentData())
        self.list.mark_seen_local([event.id for event in self.list.events()])
        self.seen_changed.emit()
        if self.unread.isChecked():
            self.refresh()

    def _release(self):
        self.audio.stop()
        self.engine.release()
        self.play_button.setText('Play')

    def suspend(self):
        self._release()
