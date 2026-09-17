import os

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout)

import config
import hik

DIALOG_STYLE = """
QDialog { background:#101014; }
QLabel { color:#b0b0c0; font-size:12px; }
QLineEdit { background:#1b1b22; color:#e0e0ea; border:1px solid #2c2c36;
            padding:5px 7px; font-size:12px; }
QLineEdit:focus { border-color:#4d7fb8; }
QCheckBox { color:#9a9aa8; font-size:11px; }
QPushButton { background:#1b1b22; color:#c8c8d4; border:1px solid #2c2c36;
              padding:6px 16px; font-size:12px; }
QPushButton:hover { background:#24242e; }
QPushButton:default { background:#2b4a34; border-color:#3d6b4a; color:#9fe8bd; }
"""


class ProbeWorker(QThread):
    finished_probe = Signal(bool, str)

    def __init__(self, ip, user, password):
        super().__init__()
        self.ip = ip
        self.user = user
        self.password = password

    def run(self):
        try:
            dvr = hik.Dvr(self.ip, self.user, self.password)
            info = dvr.device_info()
            channels = dvr.live_channels()
            self.finished_probe.emit(True, '%s, %d camera(s) live' % (
                info.get('model', 'device'), len(channels)))
        except Exception as exc:
            message = str(exc)
            if '401' in message:
                message = 'rejected: wrong username or password'
            elif 'timed out' in message.lower() or 'refused' in message.lower():
                message = 'no answer from %s' % self.ip
            self.finished_probe.emit(False, message[:120])


class LoginDialog(QDialog):
    def __init__(self, initial=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Connect to DVR')
        self.setStyleSheet(DIALOG_STYLE)
        self.setMinimumWidth(380)
        self.result_credentials = None
        self.probe = None

        initial = initial or {}
        self.ip = QLineEdit(initial.get('ip', '192.168.1.6'))
        self.user = QLineEdit(initial.get('user', 'admin'))
        self.password = QLineEdit(initial.get('password', ''))
        self.password.setEchoMode(QLineEdit.Password)
        self.remember = QCheckBox('Remember on this computer')
        self.remember.setChecked(True)

        form = QFormLayout()
        form.setSpacing(8)
        form.addRow('DVR address', self.ip)
        form.addRow('Username', self.user)
        form.addRow('Password', self.password)

        self.message = QLabel('Enter the DVR login used by the Hikvision app.')
        self.message.setWordWrap(True)

        self.test_button = QPushButton('Test')
        self.test_button.clicked.connect(self.run_test)
        self.connect_button = QPushButton('Connect')
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self.accept_credentials)
        cancel = QPushButton('Cancel')
        cancel.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addWidget(self.remember)
        buttons.addStretch(1)
        buttons.addWidget(self.test_button)
        buttons.addWidget(cancel)
        buttons.addWidget(self.connect_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addWidget(self.message)
        layout.addLayout(buttons)

    def values(self):
        return self.ip.text().strip(), self.user.text().strip(), self.password.text()

    def set_busy(self, busy, text):
        self.test_button.setEnabled(not busy)
        self.connect_button.setEnabled(not busy)
        self.message.setText(text)

    def run_test(self, then_accept=False):
        ip, user, password = self.values()
        if not ip or not user:
            self.message.setText('Address and username are required.')
            return
        self.set_busy(True, 'Contacting %s ...' % ip)
        self.probe = ProbeWorker(ip, user, password)
        self.probe.finished_probe.connect(
            lambda ok, detail: self.on_probe(ok, detail, then_accept))
        self.probe.start()

    def on_probe(self, ok, detail, then_accept):
        self.set_busy(False, ('Connected: %s' % detail) if ok else ('Failed: %s' % detail))
        if ok and then_accept:
            self.finish()

    def accept_credentials(self):
        self.run_test(then_accept=True)

    def finish(self):
        ip, user, password = self.values()
        self.result_credentials = {'ip': ip, 'user': user, 'password': password}
        if self.remember.isChecked():
            try:
                config.save(ip, user, password)
            except OSError as exc:
                self.message.setText('Connected, but could not save: %s' % exc)
        self.accept()

    def closeEvent(self, event):
        if self.probe is not None:
            self.probe.wait(2000)
        event.accept()


def obtain_dvr(folder):
    saved = config.load()
    if saved is None:
        legacy = config.import_legacy(folder)
        if legacy is not None:
            try:
                hik.Dvr(legacy['ip'], legacy['user'], legacy['password']).device_info()
                config.save(legacy['ip'], legacy['user'], legacy['password'])
                saved = legacy
            except Exception:
                saved = None
    if saved is not None:
        try:
            dvr = hik.Dvr(saved['ip'], saved['user'], saved['password'])
            dvr.device_info()
            return dvr
        except Exception:
            pass
    dialog = LoginDialog(saved)
    if dialog.exec() != QDialog.Accepted or dialog.result_credentials is None:
        return None
    found = dialog.result_credentials
    return hik.Dvr(found['ip'], found['user'], found['password'])
