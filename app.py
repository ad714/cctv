import ctypes
import glob
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

BOOT = time.monotonic()
MUTEX_NAME = 'Local\\HikViewerSingleInstance'
IPC_NAME = 'HikViewerShowRequest'
PIPE_PATH = r'\\.\pipe\%s' % IPC_NAME
ERROR_ALREADY_EXISTS = 183
ASFW_ANY = -1
_instance_mutex = None


def hand_off_to_running_instance():
    global _instance_mutex
    if sys.platform != 'win32':
        return 'owner'
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _instance_mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return 'owner'
    ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
    for _ in range(12):
        try:
            with open(PIPE_PATH, 'wb', buffering=0) as pipe:
                pipe.write(b'show')
            os._exit(0)
        except OSError:
            time.sleep(0.15)
    return 'unresponsive'


INSTANCE = hand_off_to_running_instance() if __name__ == '__main__' else 'owner'

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtNetwork import QLocalServer
from PySide6.QtGui import QAction, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QMainWindow, QMenu, QMessageBox, QStatusBar,
                               QSystemTrayIcon, QTabWidget)

import config
import login
import panels
import runtime
import theme
from events import EventCache, EventStore, EventWatcher
from live import LiveView
from playback import PlaybackView
from recent import RecentView, camera_name
from widgets import FOCUS_SIZE, CallWorker

HANG_SECONDS = 30
TOAST_WAIT_MS = 2500
CAPTURES = config.captures_dir()


def bring_to_front(hwnd):
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    for name in ('SetForegroundWindow', 'BringWindowToTop'):
        getattr(user32, name).argtypes = [ctypes.c_void_p]
    if user32.SetForegroundWindow(hwnd):
        return
    current = ctypes.windll.kernel32.GetCurrentThreadId()
    other = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    user32.AttachThreadInput(other, current, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        user32.AttachThreadInput(other, current, False)


def clear_stale_temp():
    for pattern in ('cctv-pb-*', 'cctv-exp-*'):
        for path in glob.glob(os.path.join(tempfile.gettempdir(), pattern)):
            shutil.rmtree(path, ignore_errors=True)


class MainWindow(QMainWindow):
    def __init__(self, dvr, channels):
        super().__init__()
        self.setWindowTitle('CCTV')
        area = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1480, int(area.width() * 0.94)), min(900, int(area.height() * 0.92)))
        self.move(area.center() - self.rect().center())
        self.setWindowIcon(theme.app_icon())
        self.quitting = False
        self.chores = []
        self.told_tray = False
        self.toast_queue = []
        self.toast_id = None
        self.dvr = dvr

        self.store = EventStore()
        self.recent = RecentView(dvr, channels, self.store, FOCUS_SIZE, CAPTURES)
        self.live = LiveView(dvr, channels, self.store)
        self.playback = PlaybackView(dvr, channels, self.store)
        self.album = panels.AlbumView(CAPTURES)
        self.device = panels.DeviceView(dvr, channels)
        for part in (self.recent, self.live, self.playback, self.album, self.device):
            part.status.connect(self.show_status)

        self.recent.open_playback.connect(self.open_playback)
        self.recent.open_live.connect(self.open_live)
        self.recent.catch_up.connect(self.catch_up)
        self.recent.seen_changed.connect(self.refresh_unread)
        self.live.event_requested.connect(self.open_event)
        self.live.camera_events_requested.connect(self.open_camera_events)
        self.device.settings_changed.connect(self.on_settings_changed)
        self.device.storage_problem.connect(self.on_storage_problem)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self.recent, 'Recent')
        tabs.addTab(self.live, 'Live')
        tabs.addTab(self.playback, 'Playback')
        tabs.addTab(self.album, 'Album')
        tabs.addTab(self.device, 'Device')
        tabs.currentChanged.connect(self.on_tab_changed)
        self.tabs = tabs
        self.setCentralWidget(tabs)

        self.setStatusBar(QStatusBar())

        self.upkeep = QTimer(self)
        self.upkeep.timeout.connect(self.housekeeping)
        self.upkeep.start(30 * 60 * 1000)
        QTimer.singleShot(5000, self.housekeeping)

        self.storage_timer = QTimer(self)
        self.storage_timer.timeout.connect(self.device.check_storage)
        self.storage_timer.start(6 * 3600 * 1000)
        QTimer.singleShot(8000, self.device.check_storage)

        self.hang = QTimer(self)
        self.hang.timeout.connect(self.rearm_hang_dump)
        self.hang.start(5000)
        runtime.arm_hang_dump(HANG_SECONDS)

        self.toast_timer = QTimer(self)
        self.toast_timer.setSingleShot(True)
        self.toast_timer.timeout.connect(self.flush_toasts)

        self.watcher = EventWatcher(dvr, self.store)
        self.watcher.detected.connect(self.on_detected)
        self.watcher.created.connect(self.on_created)
        self.watcher.snapshot_ready.connect(self.on_snapshot)
        self.watcher.trouble.connect(self.on_watcher_trouble)
        self.watcher.start()
        self.cache = EventCache(dvr, self.store)
        self.cache.snapshot_ready.connect(self.on_snapshot)
        self.cache.start()

        self.tray = QSystemTrayIcon(theme.app_icon(), self)
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
        self.tray.messageClicked.connect(self.on_toast_clicked)
        self.tray.show()

        QLocalServer.removeServer(IPC_NAME)
        self.ipc = QLocalServer(self)
        self.ipc.newConnection.connect(self.on_ipc_request)
        self.ipc.listen(IPC_NAME)

        fullscreen = QShortcut(QKeySequence('F11'), self)
        fullscreen.activated.connect(self.toggle_fullscreen)
        self.refresh_unread()
        self.recent.activate()

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
        job = CallWorker(lambda: (self.store.prune(), self.store.prune_clips()))
        self.chores.append(job)
        job.start()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.restore_window()

    def restore_window(self):
        was_hidden = not self.isVisible() or self.isMinimized()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        if was_hidden:
            self.tabs.setCurrentWidget(self.recent)
            self.recent.refresh(select_newest=True)
        self.raise_()
        self.activateWindow()
        if sys.platform == 'win32':
            bring_to_front(int(self.winId()))

    def quit_app(self):
        self.quitting = True
        self.close()

    def open_playback(self, channel, moment):
        self.tabs.setCurrentWidget(self.playback)
        self.playback.open_at(channel, moment)

    def open_live(self, channel):
        self.tabs.setCurrentWidget(self.live)
        self.live.focus_channel(channel)

    def catch_up(self, minutes):
        self.recent.suspend()
        self.tabs.setCurrentWidget(self.playback)
        self.playback.catch_up(minutes)

    def open_event(self, event_id):
        self.live.deactivate()
        self.tabs.setCurrentWidget(self.recent)
        self.recent.show_by_id(event_id)

    def open_camera_events(self, channel):
        self.tabs.setCurrentWidget(self.recent)
        self.recent.unread.setChecked(True)
        self.recent.focus_camera(channel)

    def refresh_unread(self):
        counts = self.store.unread_counts()
        total = sum(counts.values())
        self.tabs.setTabText(0, 'Recent  %d' % total if total else 'Recent')
        self.live.set_badges(counts)
        self.tray.setToolTip('CCTV - %d new event(s)' % total if total else
                             'CCTV - %s' % self.dvr.ip)

    def on_detected(self, channel, target, moment):
        self.live.note = 'motion ch%d %s %s' % (channel, target or '', moment.strftime('%H:%M:%S'))
        if (self.playback.loaded and channel == self.playback.current_channel()
                and moment.date() == self.playback.current_day()):
            self.playback.load_detections()

    def on_created(self, event_id):
        self.recent.event_added(event_id)
        self.refresh_unread()
        event = self.store.get(event_id)
        if event is not None and self.live.focused is not None \
                and self.live.channels[self.live.focused] == event.channel:
            self.live.refresh_side()
        if event is not None and self.wants_toast(event):
            self.toast_queue.append(event_id)
            if not self.toast_timer.isActive():
                self.toast_timer.start(TOAST_WAIT_MS)

    def on_snapshot(self, event_id):
        self.recent.snapshot_added(event_id)
        event = self.store.get(event_id)
        if event is not None and self.playback.loaded \
                and event.channel == self.playback.current_channel() \
                and event.start.date() == self.playback.current_day():
            self.playback.load_detections()
        if self.toast_queue and self.toast_queue[-1] == event_id:
            self.toast_timer.stop()
            self.flush_toasts()

    def wants_toast(self, event):
        if self.isVisible() and self.isActiveWindow():
            return False
        return panels.alert_allowed(panels.alert_settings(), event.channel, event.target,
                                    datetime.now().strftime('%H:%M'))

    def flush_toasts(self):
        if not self.toast_queue:
            return
        ids, self.toast_queue = self.toast_queue, []
        event = self.store.get(ids[-1])
        if event is None:
            return
        self.toast_id = event.id
        title = '%s at %s' % (theme.kind_label(event.target), camera_name(event.channel))
        body = event.start.strftime('%H:%M:%S')
        if len(ids) > 1:
            body += ', and %d more event(s)' % (len(ids) - 1)
        icon = QImage(event.snapshot) if event.snapshot else QImage()
        if icon.isNull():
            self.tray.showMessage(title, body, theme.kind_icon(event.target, 64), 8000)
        else:
            self.tray.showMessage(title, body, QIcon(QPixmap.fromImage(icon)), 8000)

    def on_toast_clicked(self):
        self.restore_window()
        if self.toast_id is not None:
            self.recent.show_by_id(self.toast_id)

    def on_settings_changed(self):
        level = int(config.setting('denoise', 0) or 0)
        self.live.denoise_on = bool(level)
        self.live.hd = config.setting('live_quality', 'hd') != 'sd'
        self.playback.denoise_button.setChecked(bool(level))

    def on_storage_problem(self, text):
        self.statusBar().showMessage(text, 0)
        self.tray.showMessage('DVR storage problem', text, QSystemTrayIcon.Warning, 10000)

    def on_watcher_trouble(self, detail):
        self.live.note = 'motion feed dropped, retrying'

    def on_tab_changed(self, index):
        widget = self.tabs.widget(index)
        if widget is self.live:
            self.live.activate()
        else:
            self.live.deactivate()
        if widget is self.playback:
            self.playback.ensure_loaded()
            self.playback.load_detections()
        else:
            self.playback.suspend()
        if widget is self.recent:
            self.recent.activate()
        else:
            self.recent.suspend()
        if widget is self.album:
            self.album.refresh()
        if widget is self.device:
            self.device.activate()
        self.statusBar().clearMessage()

    def show_status(self, text):
        self.statusBar().showMessage(text)

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self.quitting:
            self.live.deactivate(immediately=True)
            self.playback.suspend()
            self.recent.suspend()

    def showEvent(self, event):
        super().showEvent(event)
        if self.tabs.currentWidget() is self.live:
            self.live.activate()

    def closeEvent(self, event):
        if not self.quitting:
            event.ignore()
            self.hide()
            if not self.told_tray:
                self.told_tray = True
                self.tray.showMessage('CCTV is still running',
                                      'Events are still being recorded. Quit from the tray icon.',
                                      QSystemTrayIcon.Information, 4000)
            return
        self.hide()
        self.tray.hide()
        QApplication.processEvents()
        started = time.monotonic()
        self.live.stop_recorders()
        runtime.cancel_hang_dump()
        runtime.remove_pid_file()
        runtime.log.info('quitting, cleanup took %.0f ms', (time.monotonic() - started) * 1000)
        runtime.shutdown_logging()
        os._exit(0)


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
    if INSTANCE == 'unresponsive':
        runtime.log.warning('running instance did not respond, taking over')
        runtime.kill_stale_instance()
    qt = QApplication(sys.argv)
    runtime.install_qt_message_handler()
    qt.setApplicationName('CCTV')
    qt.setWindowIcon(theme.app_icon())
    qt.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)
    theme.apply(qt)
    qt.setQuitOnLastWindowClosed(False)
    runtime.write_pid_file()
    missing = [tool for tool in ('ffmpeg', 'ffplay') if not shutil.which(tool)]
    if missing:
        QMessageBox.critical(
            None, 'CCTV',
            'Missing required tool(s): %s\n\n'
            'Install FFmpeg and make sure it is on PATH, for example:\n'
            '    winget install Gyan.FFmpeg' % ', '.join(missing))
        return 1
    dvr = login.obtain_dvr()
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

    clear_stale_temp()
    window = MainWindow(dvr, channels)
    window.show()
    runtime.log.info('window shown %.2fs after launch', time.monotonic() - BOOT)

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
