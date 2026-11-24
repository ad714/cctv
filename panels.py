import os
import re
import shutil
import subprocess
from datetime import datetime

from PySide6.QtCore import QFile, QSize, Qt, QThread, QTime, Signal
from PySide6.QtGui import QColor, QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QListView, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QTimeEdit, QVBoxLayout, QWidget)

import config
import runtime
import theme
from widgets import CallWorker

NAME = re.compile(r'^(snap|live|clip|mosaic)-ch([\d-]+)-(\d{8})-(\d{6})')
KIND_TEXT = {'snap': 'Snapshot', 'live': 'Recording', 'clip': 'Clip', 'mosaic': 'Mosaic'}
ICON = QSize(224, 168)
DEFAULT_ALERTS = {'enabled': True, 'kind': None, 'muted': [], 'quiet': False,
                  'quiet_from': '23:00', 'quiet_to': '06:00'}


def alert_settings():
    saved = config.setting('alerts', {}) or {}
    merged = dict(DEFAULT_ALERTS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_ALERTS})
    return merged


def alert_allowed(alerts, channel, target, now):
    if not alerts['enabled'] or channel in alerts['muted']:
        return False
    tag = target or ''
    if alerts['kind'] == 'person' and 'human' not in tag:
        return False
    if alerts['kind'] == 'vehicle' and 'vehicle' not in tag:
        return False
    if alerts['quiet']:
        begin, end = alerts['quiet_from'], alerts['quiet_to']
        inside = (begin <= now < end) if begin <= end else (now >= begin or now < end)
        if inside:
            return False
    return True


def describe(name):
    match = NAME.match(name)
    if not match:
        return None, name
    kind, cams, day, clock = match.groups()
    when = datetime.strptime(day + clock, '%Y%m%d%H%M%S')
    cameras = cams.split('-')
    where = ('Camera %s' % cameras[0]) if len(cameras) == 1 else ('Cameras %s' % ', '.join(cameras))
    label = '%s\n%s, %s' % (KIND_TEXT[kind], where, when.strftime('%d %b %H:%M:%S').lstrip('0'))
    return when, label


class ThumbLoader(QThread):
    ready = Signal(str, object)

    def __init__(self, paths, cache_dir):
        super().__init__()
        self.paths = paths
        self.cache_dir = cache_dir
        self.running = True

    def run(self):
        for path in self.paths:
            if not self.running:
                return
            source = path
            if path.lower().endswith('.mp4'):
                source = self._video_frame(path)
                if source is None:
                    continue
            reader = QImageReader(source)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid():
                reader.setScaledSize(size.scaled(ICON, Qt.KeepAspectRatioByExpanding))
            image = reader.read()
            if not image.isNull():
                self.ready.emit(path, image)

    def _video_frame(self, path):
        stamp = int(os.path.getmtime(path))
        cached = os.path.join(self.cache_dir, '%s-%d.jpg' % (os.path.basename(path), stamp))
        if os.path.exists(cached):
            return cached
        try:
            subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-ss', '1', '-i', path,
                            '-frames:v', '1', '-vf', 'scale=448:-2', cached],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=runtime.NO_WINDOW)
        except Exception:
            return None
        return cached if os.path.exists(cached) else None

    def stop(self):
        self.running = False


class AlbumView(QWidget):
    status = Signal(str)

    def __init__(self, folder):
        super().__init__()
        self.folder = folder
        self.loader = None
        self.items = {}
        self.cache_dir = os.path.join(config.thumbs_dir(), 'album')
        os.makedirs(self.cache_dir, exist_ok=True)

        self.show_kind = QComboBox()
        for label, kinds in (('Everything', None), ('Snapshots', ('snap',)),
                             ('Clips and recordings', ('clip', 'mosaic', 'live'))):
            self.show_kind.addItem(label, kinds)
        self.show_kind.currentIndexChanged.connect(lambda _i: self.refresh())
        self.space = theme.set_role(QLabel(''), 'faint')
        open_folder = QPushButton('Open folder')
        open_folder.clicked.connect(lambda: os.startfile(self.folder))
        head = QHBoxLayout()
        head.addWidget(self.show_kind)
        head.addStretch(1)
        head.addWidget(self.space)
        head.addSpacing(10)
        head.addWidget(open_folder)

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(ICON)
        self.grid.setGridSize(QSize(ICON.width() + 18, ICON.height() + 58))
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setWordWrap(True)
        self.grid.setSpacing(6)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.grid.itemActivated.connect(lambda item: self._open(item.data(Qt.UserRole)))
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.empty = theme.set_role(QLabel('Snapshots, recordings and saved clips land here.'),
                                    'faint')
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(head)
        layout.addWidget(self.grid, 1)
        layout.addWidget(self.empty, 1)

    def refresh(self):
        kinds = self.show_kind.currentData()
        entries = []
        try:
            names = os.listdir(self.folder)
        except OSError:
            names = []
        for name in names:
            path = os.path.join(self.folder, name)
            if not name.lower().endswith(('.jpg', '.png', '.mp4')) or not os.path.isfile(path):
                continue
            match = NAME.match(name)
            if kinds and (match is None or match.group(1) not in kinds):
                continue
            when, label = describe(name)
            entries.append((when or datetime.fromtimestamp(os.path.getmtime(path)), label, path))
        entries.sort(key=lambda entry: entry[0], reverse=True)

        if self.loader is not None:
            self.loader.stop()
            self.loader.wait(2000)
        self.grid.clear()
        self.items = {}
        blank = QPixmap(ICON)
        blank.fill(QColor(theme.RAISED))
        for _when, label, path in entries:
            item = QListWidgetItem(QIcon(blank), label)
            item.setData(Qt.UserRole, path)
            item.setToolTip(os.path.basename(path))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignTop)
            self.grid.addItem(item)
            self.items[path] = item
        self.empty.setVisible(not entries)
        self.grid.setVisible(bool(entries))
        self._space()
        self.loader = ThumbLoader([path for _w, _l, path in entries], self.cache_dir)
        self.loader.ready.connect(self._thumb)
        self.loader.start()

    def _space(self):
        try:
            usage = shutil.disk_usage(self.folder)
            self.space.setText('%.0f GB free' % (usage.free / 1e9))
        except OSError:
            self.space.setText('')

    def _thumb(self, path, image):
        item = self.items.get(path)
        if item is not None:
            pixmap = QPixmap.fromImage(image)
            if pixmap.width() > ICON.width() or pixmap.height() > ICON.height():
                x = max(0, (pixmap.width() - ICON.width()) // 2)
                y = max(0, (pixmap.height() - ICON.height()) // 2)
                pixmap = pixmap.copy(x, y, ICON.width(), ICON.height())
            item.setIcon(QIcon(pixmap))

    def _open(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

    def _selected(self):
        return [item.data(Qt.UserRole) for item in self.grid.selectedItems()]

    def _menu(self, point):
        item = self.grid.itemAt(point)
        if item is None:
            return
        if not item.isSelected():
            self.grid.setCurrentItem(item)
        paths = self._selected()
        menu = QMenu(self)
        menu.addAction('Open', lambda: self._open(paths[0]))
        menu.addAction('Show in folder', lambda: subprocess.Popen(
            ['explorer', '/select,', os.path.normpath(paths[0])]))
        menu.addSeparator()
        menu.addAction('Move to Recycle Bin' if len(paths) == 1
                       else 'Move %d items to Recycle Bin' % len(paths),
                       lambda: self._recycle(paths))
        menu.exec(self.grid.viewport().mapToGlobal(point))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete and self._selected():
            self._recycle(self._selected())
            return
        super().keyPressEvent(event)

    def _recycle(self, paths):
        moved = sum(1 for path in paths if QFile.moveToTrash(path))
        self.status.emit('moved %d item(s) to the Recycle Bin' % moved)
        self.refresh()


def _gb(megabytes):
    return '%.0f GB' % (megabytes / 1024.0)


def _days(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours = rest // 3600
    return ('%d days %d h' % (days, hours)) if days else ('%d h' % hours)


class DeviceView(QWidget):
    settings_changed = Signal()
    storage_problem = Signal(str)
    status = Signal(str)

    def __init__(self, dvr, channels):
        super().__init__()
        self.dvr = dvr
        self.channels = channels
        self.jobs = []
        self.loaded = False

        self.facts = {}
        dvr_form = QFormLayout()
        dvr_form.setHorizontalSpacing(24)
        dvr_form.setVerticalSpacing(8)
        for key, label in (('model', 'Model'), ('serial', 'Serial number'),
                           ('firmware', 'Firmware'), ('address', 'Address'),
                           ('uptime', 'Running for'), ('cameras', 'Cameras')):
            value = QLabel('...')
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.facts[key] = value
            dvr_form.addRow(theme.set_role(QLabel(label), 'muted'), value)
        self.reboot_button = theme.set_role(QPushButton('Restart DVR'), 'danger')
        self.reboot_button.clicked.connect(self.reboot)
        refresh = theme.set_role(QPushButton('Refresh'), 'quiet')
        refresh.clicked.connect(self.load)
        dvr_actions = QHBoxLayout()
        dvr_actions.addWidget(self.reboot_button)
        dvr_actions.addWidget(refresh)
        dvr_actions.addStretch(1)

        self.disks = QVBoxLayout()
        self.disks.setSpacing(6)
        self.oldest = theme.set_role(QLabel(''), 'muted')

        alerts = alert_settings()
        self.alert_on = QCheckBox('Show a desktop alert for each new event')
        self.alert_on.setChecked(alerts['enabled'])
        self.alert_kind = QComboBox()
        for label, kind in (('People and vehicles', None), ('People only', 'person'),
                            ('Vehicles only', 'vehicle')):
            self.alert_kind.addItem(label, kind)
        self.alert_kind.setCurrentIndex(max(0, self.alert_kind.findData(alerts['kind'])))
        self.alert_cams = {}
        cams = QGridLayout()
        cams.setHorizontalSpacing(18)
        for index, channel in enumerate(channels):
            box = QCheckBox('Camera %d' % channel)
            box.setChecked(channel not in alerts['muted'])
            self.alert_cams[channel] = box
            cams.addWidget(box, index // 3, index % 3)
        self.quiet = QCheckBox('Quiet hours')
        self.quiet.setChecked(alerts['quiet'])
        self.quiet_from = QTimeEdit(QTime.fromString(alerts['quiet_from'], 'HH:mm'))
        self.quiet_to = QTimeEdit(QTime.fromString(alerts['quiet_to'], 'HH:mm'))
        quiet_row = QHBoxLayout()
        quiet_row.setSpacing(8)
        quiet_row.addWidget(self.quiet)
        quiet_row.addSpacing(8)
        for edit, text in ((self.quiet_from, 'from'), (self.quiet_to, 'to')):
            edit.setDisplayFormat('HH:mm')
            edit.setFixedWidth(84)
            edit.setEnabled(self.quiet.isChecked())
            quiet_row.addWidget(theme.set_role(QLabel(text), 'muted'))
            quiet_row.addWidget(edit)
        quiet_row.addStretch(1)
        alert_form = QFormLayout()
        alert_form.setHorizontalSpacing(24)
        alert_form.setVerticalSpacing(10)
        alert_form.addRow(self.alert_on)
        alert_form.addRow(theme.set_role(QLabel('Alert for'), 'muted'), self.alert_kind)
        alert_form.addRow(theme.set_role(QLabel('Cameras'), 'muted'), cams)
        alert_form.addRow(quiet_row)
        for widget in [self.alert_on, self.quiet] + list(self.alert_cams.values()):
            widget.toggled.connect(self._save_alerts)
        self.alert_kind.currentIndexChanged.connect(self._save_alerts)
        self.quiet_from.timeChanged.connect(self._save_alerts)
        self.quiet_to.timeChanged.connect(self._save_alerts)

        self.denoise = QComboBox()
        self.denoise.addItem('Off', 0)
        for level in range(1, 6):
            self.denoise.addItem('Level %d' % level, level)
        self.denoise.setCurrentIndex(max(0, self.denoise.findData(config.setting('denoise', 0))))
        self.denoise.currentIndexChanged.connect(self._save_view)
        self.quality = QComboBox()
        self.quality.addItem('HD (main stream)', 'hd')
        self.quality.addItem('SD (sub stream, lighter)', 'sd')
        self.quality.setCurrentIndex(max(0, self.quality.findData(
            config.setting('live_quality', 'hd'))))
        self.quality.currentIndexChanged.connect(self._save_view)
        view_form = QFormLayout()
        view_form.setHorizontalSpacing(24)
        view_form.setVerticalSpacing(10)
        view_form.addRow(theme.set_role(QLabel('Noise reduction'), 'muted'), self.denoise)
        view_form.addRow(theme.set_role(QLabel('Expanded camera quality'), 'muted'),
                         self.quality)

        column = QVBoxLayout()
        column.setSpacing(12)
        column.addWidget(self._section('DVR', dvr_form, dvr_actions))
        storage = QVBoxLayout()
        storage.addLayout(self.disks)
        storage.addWidget(self.oldest)
        column.addWidget(self._section('Storage', storage))
        column.addWidget(self._section('Desktop alerts', alert_form))
        column.addWidget(self._section('Viewing', view_form))
        column.addStretch(1)

        holder = QWidget()
        holder.setMaximumWidth(760)
        holder.setLayout(column)
        outer = QHBoxLayout()
        outer.addWidget(holder)
        outer.addStretch(1)
        page = QWidget()
        page.setObjectName('page')
        page.setLayout(outer)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.addWidget(scroll)

    def _section(self, title, body, footer=None):
        frame = QFrame()
        frame.setProperty('role', 'panel')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(12)
        layout.addWidget(theme.set_role(QLabel(title), 'heading'))
        if isinstance(body, QWidget):
            layout.addWidget(body)
        else:
            layout.addLayout(body)
        if footer is not None:
            layout.addLayout(footer)
        return frame

    def activate(self):
        if not self.loaded:
            self.load()

    def load(self):
        self.loaded = True
        dvr = self.dvr
        channels = list(self.channels)

        def fetch():
            info = dvr.device_info(timeout=8)
            disks, mode = dvr.storage()
            uptime = dvr.uptime()
            earliest = None
            for channel in channels[:1]:
                earliest = dvr.earliest_recording(channel)
            return info, disks, mode, uptime, earliest

        job = CallWorker(fetch)
        job.done.connect(self._loaded)
        self.jobs = [j for j in self.jobs if not j.isFinished()] + [job]
        job.start()

    def check_storage(self):
        dvr = self.dvr

        def fetch():
            return dvr.storage()

        job = CallWorker(fetch)
        job.done.connect(lambda result, error: result and self._warn(result[0]))
        self.jobs = [j for j in self.jobs if not j.isFinished()] + [job]
        job.start()

    def _warn(self, disks):
        bad = [disk for disk in disks if disk['status'].lower() != 'ok']
        if bad:
            self.storage_problem.emit('DVR disk %s reports "%s"' % (bad[0]['id'], bad[0]['status']))
        elif not disks:
            self.storage_problem.emit('The DVR reports no disk')

    def _loaded(self, result, error):
        if error is not None or result is None:
            for value in self.facts.values():
                value.setText('Could not reach the DVR')
            return
        info, disks, mode, uptime, earliest = result
        self.facts['model'].setText(info.get('model') or '?')
        serial = info.get('serialNumber') or ''
        self.facts['serial'].setText(serial[-13:-4] if len(serial) > 13 else serial)
        self.facts['serial'].setToolTip(serial)
        self.facts['firmware'].setText('%s, %s' % (info.get('firmwareVersion') or '?',
                                                   info.get('firmwareReleasedDate') or ''))
        self.facts['address'].setText(self.dvr.ip)
        self.facts['uptime'].setText(_days(uptime) if uptime else '?')
        self.facts['cameras'].setText('%d sending video' % len(self.channels))

        while self.disks.count():
            widget = self.disks.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for disk in disks:
            healthy = disk['status'].lower() == 'ok'
            text = 'Disk %s: %s, %s' % (disk['id'], 'healthy' if healthy else disk['status'],
                                        _gb(disk['capacity_mb']))
            if disk['free_mb'] == 0 and healthy:
                text += ', full, recording over the oldest footage'
            elif disk['capacity_mb']:
                text += ', %s free' % _gb(disk['free_mb'])
            label = QLabel(text)
            if not healthy:
                label.setStyleSheet('color:%s;' % theme.DANGER)
            self.disks.addWidget(label)
        if not disks:
            label = QLabel('No disk reported')
            label.setStyleSheet('color:%s;' % theme.DANGER)
            self.disks.addWidget(label)
        if earliest is not None:
            span = (datetime.now() - earliest).days
            self.oldest.setText('Oldest footage: %s, about %d days kept'
                                % (earliest.strftime('%d %B %Y').lstrip('0'), span))
        self._warn(disks)

    def reboot(self):
        answer = QMessageBox.question(
            self, 'Restart DVR',
            'Restart the DVR now? Recording stops for about two minutes while it restarts.',
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        self.reboot_button.setEnabled(False)
        job = CallWorker(self.dvr.reboot)
        job.done.connect(self._rebooted)
        self.jobs = [j for j in self.jobs if not j.isFinished()] + [job]
        job.start()

    def _rebooted(self, result, error):
        self.reboot_button.setEnabled(True)
        if error is None:
            self.status.emit('DVR is restarting, video returns in about two minutes')
        else:
            self.status.emit('Restart failed: %s' % error)

    def _save_alerts(self, *_args):
        for edit in (self.quiet_from, self.quiet_to):
            edit.setEnabled(self.quiet.isChecked())
        config.set_setting('alerts', {
            'enabled': self.alert_on.isChecked(),
            'kind': self.alert_kind.currentData(),
            'muted': [ch for ch, box in self.alert_cams.items() if not box.isChecked()],
            'quiet': self.quiet.isChecked(),
            'quiet_from': self.quiet_from.time().toString('HH:mm'),
            'quiet_to': self.quiet_to.time().toString('HH:mm')})
        self.settings_changed.emit()

    def _save_view(self, *_args):
        config.set_setting('denoise', self.denoise.currentData())
        config.set_setting('live_quality', self.quality.currentData())
        self.settings_changed.emit()
