import math
import os
from datetime import datetime, timedelta

from PySide6.QtCore import QDate, QSize, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCharFormat
from PySide6.QtWidgets import (QButtonGroup, QCalendarWidget, QCheckBox, QComboBox, QDateEdit,
                               QDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QMenu, QProgressBar, QPushButton, QRadioButton, QSizePolicy,
                               QTimeEdit, QVBoxLayout, QWidget)

import config
import runtime
import theme
from export import ExportJob
from player import Playback, SPEEDS
from recent import OVERLAY_TEXT, EventList, camera_name
from timeline import TimelineBar, clock, seconds_of
from widgets import FOCUS_SIZE, TILE_SIZE, AudioPlayer, CallWorker, FlowLayout, VideoLabel

PREROLL = 3
AUDIO_MAX_SPEED = 4.0
CAPTURES = config.captures_dir()


class ExportDialog(QDialog):
    def __init__(self, parent, dvr, channels, day, begin, finish, current_channel, out_dir,
                 min_date, max_date):
        super().__init__(parent)
        self.setWindowTitle('Export footage')
        self.dvr = dvr
        self.day = day
        self.out_dir = out_dir
        self.job = None
        self.result_path = None

        self.date_edit = QDateEdit(QDate(day.year, day.month, day.day))
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setCalendarWidget(QCalendarWidget())
        self.date_edit.setDisplayFormat('yyyy-MM-dd')
        if min_date is not None and min_date.isValid():
            self.date_edit.setMinimumDate(min_date)
        self.date_edit.setMaximumDate(
            max_date if max_date is not None and max_date.isValid() else QDate.currentDate())

        self.from_edit = QTimeEdit(QTime(begin.hour, begin.minute, begin.second))
        self.to_edit = QTimeEdit(QTime(finish.hour, finish.minute, finish.second))
        for edit in (self.from_edit, self.to_edit):
            edit.setDisplayFormat('HH:mm:ss')
        date_row = QHBoxLayout()
        date_row.addWidget(theme.set_role(QLabel('Date'), 'muted'))
        date_row.addWidget(self.date_edit)
        date_row.addSpacing(16)
        date_row.addWidget(theme.set_role(QLabel('From'), 'muted'))
        date_row.addWidget(self.from_edit)
        date_row.addWidget(theme.set_role(QLabel('to'), 'muted'))
        date_row.addWidget(self.to_edit)
        date_row.addStretch(1)

        self.include = {}
        self.sound = {}
        cams_box = QGroupBox('Cameras')
        grid = QGridLayout(cams_box)
        grid.setHorizontalSpacing(18)
        grid.setColumnStretch(3, 1)
        grid.addWidget(theme.set_role(QLabel('Include'), 'muted'), 0, 1, Qt.AlignCenter)
        grid.addWidget(theme.set_role(QLabel('Sound'), 'muted'), 0, 2, Qt.AlignCenter)
        for row, channel in enumerate(channels, start=1):
            name = QLabel(camera_name(channel))
            inc = QCheckBox()
            inc.setChecked(channel == current_channel)
            snd = QCheckBox()
            snd.setChecked(channel == current_channel)
            self.include[channel] = inc
            self.sound[channel] = snd
            grid.addWidget(name, row, 0)
            grid.addWidget(inc, row, 1, Qt.AlignCenter)
            grid.addWidget(snd, row, 2, Qt.AlignCenter)

        self.mute = QCheckBox('No sound in any output')
        self.mute.toggled.connect(self._mute_toggled)

        self.separate = QRadioButton('Separate clips')
        self.mosaic = QRadioButton('One mosaic of all cameras')
        self.both = QRadioButton('Both')
        self.separate.setChecked(True)
        mode_group = QButtonGroup(self)
        for button in (self.separate, self.mosaic, self.both):
            mode_group.addButton(button)
        mode_box = QGroupBox('Output')
        mode_layout = QVBoxLayout(mode_box)
        for button in (self.separate, self.mosaic, self.both):
            mode_layout.addWidget(button)

        self.status = theme.set_role(QLabel(''), 'muted')
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()

        self.export_btn = theme.set_role(QPushButton('Export'), 'primary')
        self.export_btn.clicked.connect(self.start_export)
        self.close_btn = QPushButton('Close')
        self.close_btn.clicked.connect(self.reject)
        self.open_btn = QPushButton('Open folder')
        self.open_btn.clicked.connect(self._open_folder)
        self.open_btn.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.close_btn)
        buttons.addWidget(self.export_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)
        layout.addLayout(date_row)
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
            self.status.setText('Pick at least one camera')
            return
        if (finish - begin).total_seconds() < 1:
            self.status.setText('"To" must be after "From"')
            return
        if finish > datetime.now():
            finish = datetime.now()
        self.job = ExportJob(self.dvr, cameras, begin, finish, mode, audio,
                             self.out_dir, runtime.aspect_args())
        self.job.progress.connect(self.status.setText)
        self.job.fraction.connect(self._progress)
        self.job.done.connect(self._finished)
        self.export_btn.setEnabled(False)
        self.open_btn.hide()
        self.bar.setValue(0)
        self.bar.show()
        minutes = (finish - begin).total_seconds() / 60.0
        self.status.setText('Exporting %.1f min from %d camera(s)' % (minutes, len(cameras)))
        self.job.start()

    def _progress(self, share, remaining):
        self.bar.setValue(int(share * 1000))
        if remaining >= 0:
            minutes, seconds = divmod(int(round(remaining)), 60)
            left = ('%d min %02d s' % (minutes, seconds)) if minutes else ('%d s' % seconds)
            self.status.setText('%d%% done, about %s left' % (int(share * 100), left))

    def _finished(self, ok, detail):
        self.bar.hide()
        self.export_btn.setEnabled(True)
        if ok:
            self.result_path = detail
            self.status.setText('Saved to the Album')
            self.open_btn.show()
        else:
            self.status.setText('Cancelled' if detail == 'cancelled' else 'Failed: %s' % detail)

    def _open_folder(self):
        os.startfile(self.out_dir)

    def reject(self):
        if self.job is not None and self.job.isRunning():
            self.job.cancel()
            self.job.wait(5000)
        super().reject()


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
        self.selection = None
        self.note = ''
        self._autoplay = False
        self._resume_at = None
        self._grid_target = None
        self.grid_mode = False
        self.grid_engines = {}
        self.grid_labels = {}
        self.denoise_on = bool(config.setting('denoise', 0))

        self.engine = Playback(dvr, FOCUS_SIZE)
        self.engine.denoise = runtime.denoise_level(self.denoise_on) if self.denoise_on else 0
        self.engine.frame_ready.connect(self._on_engine_frame)
        self.engine.position.connect(self.on_position)
        self.engine.state.connect(self.on_engine_state)
        self.engine.ended.connect(self.on_engine_ended)

        self.camera = QComboBox()
        for channel in channels:
            self.camera.addItem(camera_name(channel), channel)
        self.camera.currentIndexChanged.connect(self.change_camera)

        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setCalendarWidget(QCalendarWidget())
        self.date.setDisplayFormat('ddd d MMM yyyy')
        self.date.setDate(QDate.currentDate())
        self.date.setMaximumDate(QDate.currentDate())
        self.date.dateChanged.connect(self.reload_day)
        self.date.calendarWidget().currentPageChanged.connect(self.mark_month)

        self.prev_button = theme.set_role(QPushButton('<'), 'quiet')
        self.prev_button.clicked.connect(lambda: self.step_day(-1))
        self.next_button = theme.set_role(QPushButton('>'), 'quiet')
        self.next_button.clicked.connect(lambda: self.step_day(1))

        self.goto = QTimeEdit()
        self.goto.setDisplayFormat('HH:mm:ss')
        self.goto.setTime(QTime.currentTime())
        self.goto.setKeyboardTracking(False)
        self.goto.editingFinished.connect(self.go_to_time)
        self.goto_button = QPushButton('Go')
        self.goto_button.clicked.connect(self.go_to_time)

        self.play_button = theme.set_role(QPushButton('Play'), 'primary')
        self.play_button.setMinimumWidth(72)
        self.play_button.clicked.connect(self.toggle_play)
        self.stop_button = theme.set_role(QPushButton('Stop'), 'quiet')
        self.stop_button.clicked.connect(self.stop_playback)
        self.grid_btn = theme.set_role(QPushButton('All cameras'), 'quiet')
        self.grid_btn.setCheckable(True)
        self.grid_btn.clicked.connect(self.toggle_grid)

        self.speed_box = QComboBox()
        for value in SPEEDS:
            self.speed_box.addItem('%gx' % value, value)
        self.speed_box.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_box.currentIndexChanged.connect(self.change_speed)

        self.step_back_btn = theme.set_role(QPushButton('Frame <'), 'quiet')
        self.step_back_btn.clicked.connect(self.step_back_all)
        self.step_fwd_btn = theme.set_role(QPushButton('Frame >'), 'quiet')
        self.step_fwd_btn.clicked.connect(self.step_all)

        self.audio_button = theme.set_role(QPushButton('Sound'), 'quiet')
        self.audio_button.setCheckable(True)
        self.audio_button.clicked.connect(self.toggle_audio)
        self.denoise_button = theme.set_role(QPushButton('Denoise'), 'quiet')
        self.denoise_button.setCheckable(True)
        self.denoise_button.setChecked(self.denoise_on)
        self.denoise_button.toggled.connect(self.toggle_denoise)
        self.snap_button = theme.set_role(QPushButton('Snapshot'), 'quiet')
        self.snap_button.clicked.connect(self.take_snapshot)
        self.zoom_in = theme.set_role(QPushButton('+'), 'quiet')
        self.zoom_in.clicked.connect(lambda: self.timeline.zoom(0.5))
        self.zoom_out = theme.set_role(QPushButton('-'), 'quiet')
        self.zoom_out.clicked.connect(lambda: self.timeline.zoom(2.0))
        self.lanes_btn = theme.set_role(QPushButton('Lanes'), 'quiet')
        self.lanes_btn.setCheckable(True)
        self.lanes_btn.toggled.connect(lambda _on: self.load_detections())
        self.bookmark_btn = theme.set_role(QPushButton('Bookmark'), 'quiet')
        self.bookmark_btn.clicked.connect(self.add_bookmark)
        self.loop_btn = theme.set_role(QPushButton('Loop'), 'quiet')
        self.loop_btn.setCheckable(True)
        self.loop_btn.toggled.connect(self.toggle_loop)
        self.fit_button = theme.set_role(QPushButton('Whole day'), 'quiet')
        self.fit_button.clicked.connect(lambda: self.timeline.fit())
        self.back10 = theme.set_role(QPushButton('-10 s'), 'quiet')
        self.back10.clicked.connect(lambda: self.nudge(-10))
        self.fwd10 = theme.set_role(QPushButton('+10 s'), 'quiet')
        self.fwd10.clicked.connect(lambda: self.nudge(10))
        self.prev_hit = theme.set_role(QPushButton('< Event'), 'quiet')
        self.prev_hit.clicked.connect(lambda: self.jump_detection(-1))
        self.next_hit = theme.set_role(QPushButton('Event >'), 'quiet')
        self.next_hit.clicked.connect(lambda: self.jump_detection(1))
        self.events_btn = theme.set_role(QPushButton('Events'), 'quiet')
        self.events_btn.setCheckable(True)
        self.events_btn.toggled.connect(self.toggle_events)
        self.export_button = QPushButton('Export')
        self.export_button.clicked.connect(self.export_selection)

        self.readout = theme.set_role(QLabel(''), 'muted')
        self.readout.setFont(theme.app_font(11, tabular=True))
        self.readout.setMinimumWidth(80)

        bar = FlowLayout(spacing=4)
        groups = (
            (self.camera, self.prev_button, self.date, self.next_button),
            (self.play_button, self.readout, self.stop_button, self.speed_box),
            (self.back10, self.fwd10, self.step_back_btn, self.step_fwd_btn),
            (self.prev_hit, self.next_hit, self.events_btn),
            (self.goto, self.goto_button),
            (self.grid_btn, self.audio_button, self.denoise_button, self.snap_button),
            (self.lanes_btn, self.bookmark_btn, self.loop_btn),
            (self.zoom_out, self.zoom_in, self.fit_button, self.export_button),
        )
        for group in groups:
            for widget in group:
                bar.addWidget(widget)
            spacer = QWidget()
            spacer.setFixedWidth(14)
            bar.addWidget(spacer)
        toolbar = QWidget()
        toolbar.setLayout(bar)
        policy = toolbar.sizePolicy()
        policy.setHeightForWidth(True)
        policy.setVerticalPolicy(QSizePolicy.Minimum)
        toolbar.setSizePolicy(policy)

        self.video = VideoLabel('Playback')
        self.video.setText('Pick a day, then click the timeline')

        self.grid_widget = QWidget()
        grid_layout = QGridLayout(self.grid_widget)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(4)
        columns = max(1, math.ceil(len(channels) ** 0.5))
        for index, channel in enumerate(channels):
            label = VideoLabel(camera_name(channel))
            self.grid_labels[channel] = label
            grid_layout.addWidget(label, index // columns, index % columns)
        self.grid_widget.hide()

        self.event_list = EventList(compact=True)
        self.event_list.picked.connect(self._jump_to_event)
        self.event_list.save_requested.connect(self._save_event)
        self.event_list.playback_requested.connect(self._jump_to_event)
        self.events_empty = theme.set_role(QLabel('No events on this day.'), 'faint')
        self.events_panel = QWidget()
        self.events_panel.setFixedWidth(300)
        panel_layout = QVBoxLayout(self.events_panel)
        panel_layout.setContentsMargins(6, 0, 0, 0)
        panel_layout.addWidget(self.event_list, 1)
        panel_layout.addWidget(self.events_empty)
        self.events_panel.hide()

        for widget, tip in (
                (self.camera, 'Camera'), (self.date, 'Date'),
                (self.prev_button, 'Previous day'), (self.next_button, 'Next day'),
                (self.play_button, 'Play or pause (Space or K)'),
                (self.stop_button, 'Stop playback'),
                (self.grid_btn, 'Show all cameras at once, synced to the same time'),
                (self.speed_box, 'Playback speed'),
                (self.step_back_btn, 'Previous frame (,)'),
                (self.step_fwd_btn, 'Next frame (.)'),
                (self.back10, 'Back 10 seconds (Left or J)'),
                (self.fwd10, 'Forward 10 seconds (Right or L)'),
                (self.prev_hit, 'Jump to the previous person or vehicle event'),
                (self.next_hit, 'Jump to the next person or vehicle event'),
                (self.events_btn, 'List this camera\'s events for the day'),
                (self.goto, 'Type a time and press Enter to jump there'),
                (self.audio_button, 'Sound (up to 4x speed)'),
                (self.denoise_button, 'Reduce grain and noise, mostly useful at night'),
                (self.snap_button, 'Save the current frame to the Album'),
                (self.zoom_out, 'Zoom timeline out'), (self.zoom_in, 'Zoom timeline in'),
                (self.fit_button, 'Show the whole day'),
                (self.lanes_btn, 'Show every camera\'s events as rows under the timeline'),
                (self.bookmark_btn, 'Bookmark the current moment (B). Right-click a bookmark '
                                    'to add a note or delete it'),
                (self.loop_btn, 'Repeat the selected range (Shift+drag on the timeline)'),
                (self.export_button, 'Export footage: time range, cameras, mosaic')):
            widget.setToolTip(tip)

        self.timeline = TimelineBar()
        self.timeline.setToolTip('Click or drag to move the playhead. Shift+drag selects a '
                                 'range, drag its edges to adjust. Scroll to zoom.')
        self.timeline.seek_requested.connect(self.seek_to)
        self.timeline.selection_changed.connect(self.on_selection)
        self.timeline.lane_clicked.connect(self.on_lane_clicked)
        self.timeline.bookmark_menu.connect(self.bookmark_menu)

        for keys, slot in (('Space', self.toggle_play), ('K', self.toggle_play),
                           ('Left', lambda: self.nudge(-10)), ('J', lambda: self.nudge(-10)),
                           ('Right', lambda: self.nudge(10)), ('L', lambda: self.nudge(10)),
                           (',', self.step_back_all), ('.', self.step_all),
                           ('B', self.add_bookmark)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

        stage = QHBoxLayout()
        stage.setSpacing(0)
        stage.addWidget(self.video, 1)
        stage.addWidget(self.grid_widget, 1)
        stage.addWidget(self.events_panel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 8)
        layout.setSpacing(8)
        layout.addWidget(toolbar)
        layout.addLayout(stage, 1)
        layout.addWidget(self.timeline)

        self.loaded = False
        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self.tick)
        self.ticker.start(500)

    def minimumSizeHint(self):
        return QSize(860, 440)

    def hasHeightForWidth(self):
        return False

    def ensure_loaded(self):
        if self.loaded:
            return
        self.loaded = True
        self._autoplay = True
        self.apply_date_bounds()
        self.reload_day()

    def open_at(self, channel, moment):
        first = not self.loaded
        self.loaded = True
        if first:
            self.apply_date_bounds()
        if self.grid_mode:
            self.grid_btn.setChecked(False)
            self._exit_grid()
        for widget in (self.camera, self.date):
            widget.blockSignals(True)
        index = self.camera.findData(channel)
        if index >= 0:
            self.camera.setCurrentIndex(index)
        self.date.setDate(QDate(moment.year, moment.month, moment.day))
        for widget in (self.camera, self.date):
            widget.blockSignals(False)
        self._autoplay = False
        self._resume_at = moment - timedelta(seconds=PREROLL)
        self.reload_day()

    def catch_up(self, minutes):
        first = not self.loaded
        self.loaded = True
        if first:
            self.apply_date_bounds()
        target = datetime.now() - timedelta(minutes=minutes)
        self._autoplay = False
        if self.current_day() != target.date():
            self.date.blockSignals(True)
            self.date.setDate(QDate(target.year, target.month, target.day))
            self.date.blockSignals(False)
            self.reload_day()
        self.grid_btn.setChecked(True)
        if self.grid_mode:
            self._load_grid(target)
        else:
            self._enter_grid(target)

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

    def go_to_time(self):
        value = self.goto.time()
        seconds = value.hour() * 3600 + value.minute() * 60 + value.second()
        self.ensure_loaded()
        self.seek_to(seconds)

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

    def toggle_denoise(self, on):
        self.denoise_on = on
        level = runtime.denoise_level(on) if on else 0
        self.engine.set_denoise(level)
        for engine in self.grid_engines.values():
            engine.denoise = level

    def sync_audio(self):
        primary = self._primary()
        want = (primary is not None and self.audio_button.isChecked()
                and primary.is_playing() and not primary.paused
                and primary.speed <= AUDIO_MAX_SPEED)
        if not want:
            self.audio.stop()
            return
        spec = primary.audio_spec()
        if spec is not None:
            self.audio.play_local(spec[0], spec[1], primary.speed)

    def toggle_grid(self, checked):
        if checked:
            self._enter_grid()
        else:
            self._exit_grid()

    def _enter_grid(self, target=None):
        target = target or self.engine.current_time()
        self.engine.stop()
        self.audio.stop()
        self.video.hide()
        self.grid_widget.show()
        self.grid_mode = True
        self.camera.setEnabled(False)
        if not self.grid_engines:
            for channel in self.channels:
                engine = Playback(self.dvr, TILE_SIZE)
                engine.denoise = self.engine.denoise
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
        self.note = '%s   %d segment(s)   %.1f h recorded   %d event(s)' % (
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
            self.camera.setItemText(index, '%s (%d)' % (camera_name(channel), count))

    def load_detections(self):
        self.episodes = self.store.day(self.current_channel(), self.current_day())
        self.timeline.set_markers([(seconds_of(e.start), seconds_of(e.end))
                                   for e in self.episodes],
                                  [(e.target, e.snapshot) for e in self.episodes])
        self.timeline.set_bookmarks(
            [(mark_id, seconds_of(at), note)
             for mark_id, at, note in self.store.bookmarks(self.current_channel(),
                                                           self.current_day())])
        if self.lanes_btn.isChecked():
            day = self.current_day()
            self.timeline.set_lanes(
                [(channel, 'Cam %d' % channel,
                  [(seconds_of(e.start), seconds_of(e.end), e.target)
                   for e in self.store.day(channel, day)])
                 for channel in self.channels], self.current_channel())
        else:
            self.timeline.set_lanes([])
        self.hit_index = None
        if self.events_panel.isVisible():
            self._fill_events()

    def on_lane_clicked(self, channel, seconds):
        midnight = datetime.combine(self.current_day(), datetime.min.time())
        moment = midnight + timedelta(seconds=seconds)
        if channel == self.current_channel() or self.grid_mode:
            self.seek_to(seconds)
        else:
            self.open_at(channel, moment + timedelta(seconds=PREROLL))

    def add_bookmark(self):
        primary = self._primary()
        moment = primary.current_time() if primary is not None else None
        if moment is None:
            self.note = 'play something first, then bookmark it'
            return
        self.store.add_bookmark(self.current_channel(), moment)
        self.note = 'bookmarked %s, right-click it on the timeline to add a note' % (
            moment.strftime('%H:%M:%S'))
        self.load_detections()

    def bookmark_menu(self, mark_id, point):
        menu = QMenu(self)
        menu.addAction('Add or edit note', lambda: self._edit_bookmark(mark_id))
        menu.addAction('Delete bookmark', lambda: (self.store.delete_bookmark(mark_id),
                                                   self.load_detections()))
        menu.exec(point)

    def _edit_bookmark(self, mark_id):
        current = self.store.bookmark(mark_id)
        if current is None:
            return
        note, ok = QInputDialog.getText(self, 'Bookmark note', 'Note', text=current)
        if ok:
            self.store.set_bookmark_note(mark_id, note.strip())
            self.load_detections()

    def toggle_loop(self, on):
        if on and not self.selection:
            self.note = 'select a range first: Shift+drag on the timeline'
            self.loop_btn.setChecked(False)
            return
        if on and self.timeline.position is not None and not (
                self.selection[0] <= self.timeline.position < self.selection[1]):
            self.seek_to(self.selection[0])

    def _fill_events(self):
        events = list(reversed(self.episodes))
        self.event_list.set_events(events, headers=False)
        self.event_list.setVisible(bool(events))
        self.events_empty.setVisible(not events)

    def toggle_events(self, on):
        self.events_panel.setVisible(on)
        if on:
            self._fill_events()

    def _jump_to_event(self, event):
        if event.channel != self.current_channel() or event.start.date() != self.current_day():
            self.open_at(event.channel, event.start)
            return
        self.seek_to(max(0, seconds_of(event.start) - PREROLL), from_jump=True)

    def _save_event(self, event):
        begin = event.start - timedelta(seconds=5)
        finish = event.end + timedelta(seconds=8)
        dialog = ExportDialog(self, self.dvr, self.channels, event.start.date(), begin, finish,
                              event.channel, CAPTURES, self.date.minimumDate(),
                              self.date.maximumDate())
        dialog.start_export()
        dialog.exec()

    def jump_detection(self, direction):
        if not self.episodes:
            self.note = 'no events for this camera and day'
            return
        marks = [seconds_of(e.start) for e in self.episodes]
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
            self.note = 'no %s events on this day' % ('later' if direction > 0 else 'earlier')
            return
        self.hit_index = index
        event = self.episodes[index]
        self.seek_to(max(0, marks[index] - PREROLL), from_jump=True)
        self.note = 'event %d of %d   %s   %s   %d hit(s)' % (
            index + 1, len(marks), event.start.strftime('%H:%M:%S'),
            theme.kind_label(event.target), event.hits)

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
        available.setForeground(QColor(theme.ON_TEXT))
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
        available.setForeground(QColor(theme.ON_TEXT))
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
            self.note = 'saved %s to Album' % os.path.basename(saved[0])
        else:
            self.note = 'saved %d snapshots to Album' % len(saved)

    def _save_frame(self, channel, image):
        path = os.path.join(CAPTURES, 'snap-ch%d-%s.jpg' % (
            channel, datetime.now().strftime('%Y%m%d-%H%M%S')))
        return path if image.save(path, 'JPG') else None

    def _on_engine_frame(self, image):
        self.video.set_frame(image)

    def on_position(self, seconds):
        self.timeline.set_position(seconds)
        self.readout.setText(clock(seconds))
        if self.loop_btn.isChecked() and self.selection and seconds >= self.selection[1]:
            self.seek_to(self.selection[0])

    def on_engine_state(self, state):
        self.play_button.setText('Pause' if state == 'playing' else 'Play')
        self.video.overlay_text = OVERLAY_TEXT.get(state, '')
        self.video.update()
        self.sync_audio()
        self.note = state

    def on_engine_ended(self):
        self.note = 'reached the end'

    def on_selection(self, selection):
        self.selection = selection
        if selection is None and self.loop_btn.isChecked():
            self.loop_btn.setChecked(False)

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
        if self.note:
            parts.append(self.note)
        self.status.emit('   '.join(parts))

    def suspend(self):
        if self.engine.is_playing() or self.grid_mode:
            self.stop_playback()
            self.note = 'stopped, click the timeline to resume'
