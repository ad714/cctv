from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QVBoxLayout)

import config
import hik
import theme


class ProbeWorker(QThread):
    finished_probe = Signal(bool, str, str)

    def __init__(self, ip, user, password, port=80):
        super().__init__()
        self.ip = ip
        self.user = user
        self.password = password
        self.port = port

    def run(self):
        try:
            dvr = hik.Dvr(self.ip, self.user, self.password, self.port)
            info = dvr.device_info()
            channels = dvr.live_channels()
            self.finished_probe.emit(True, '%s, %d camera(s) live' % (
                info.get('model', 'device'), len(channels)),
                info.get('serialNumber') or '')
        except Exception as exc:
            message = str(exc)
            if '401' in message:
                message = 'rejected: wrong username or password'
            elif 'timed out' in message.lower() or 'refused' in message.lower():
                message = 'no answer from %s' % self.ip
            self.finished_probe.emit(False, message[:120], '')


class DiscoverWorker(QThread):
    finished_scan = Signal(list)

    def run(self):
        try:
            self.finished_scan.emit(hik.sadp())
        except Exception:
            self.finished_scan.emit([])


class LoginDialog(QDialog):
    def __init__(self, initial=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Connect to DVR')
        self.setMinimumWidth(380)
        self.result_credentials = None
        self.probe = None
        self.devices = {}

        initial = initial or {}
        self.ip = QComboBox()
        self.ip.setEditable(True)
        if initial.get('ip'):
            self.ip.addItem(initial['ip'])
        self.ip.currentTextChanged.connect(self.describe)
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

        self.message = QLabel('Searching the network for your DVR ...')
        self.message.setWordWrap(True)

        self.test_button = QPushButton('Test')
        self.test_button.clicked.connect(self.run_test)
        self.connect_button = theme.set_role(QPushButton('Connect'), 'primary')
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

        self.finder = DiscoverWorker()
        self.finder.finished_scan.connect(self.on_devices)
        self.finder.start()

    def on_devices(self, devices):
        if not devices:
            self.message.setText('No DVR answered. Enter its address manually.')
            return
        saved = self.ip.currentText().strip()
        self.devices = {device['ip']: device for device in devices}
        self.ip.clear()
        self.ip.addItems([device['ip'] for device in devices])
        if saved and saved not in self.devices:
            self.ip.addItem(saved)
        self.ip.setCurrentText(saved if saved in self.devices else devices[0]['ip'])
        self.describe()

    def describe(self):
        if not self.devices:
            return
        device = self.devices.get(self.ip.currentText().strip())
        if device is not None:
            self.message.setText('Found %s at %s' % (device['model'], device['ip']))
        else:
            self.message.setText('Using the address you typed.')

    def port(self):
        device = self.devices.get(self.ip.currentText().strip())
        return device['port'] if device else 80

    def values(self):
        return (self.ip.currentText().strip(), self.user.text().strip(),
                self.password.text())

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
        self.probe = ProbeWorker(ip, user, password, self.port())
        self.probe.finished_probe.connect(
            lambda ok, detail, serial: self.on_probe(ok, detail, serial, then_accept))
        self.probe.start()

    def on_probe(self, ok, detail, serial, then_accept):
        self.set_busy(False, ('Connected: %s' % detail) if ok else ('Failed: %s' % detail))
        if ok and then_accept:
            self.finish(serial)

    def accept_credentials(self):
        self.run_test(then_accept=True)

    def finish(self, serial=''):
        ip, user, password = self.values()
        port = self.port()
        self.result_credentials = {'ip': ip, 'user': user, 'password': password,
                                   'serial': serial, 'port': port}
        if self.remember.isChecked():
            try:
                config.save(ip, user, password, None, serial, port)
            except OSError as exc:
                self.message.setText('Connected, but could not save: %s' % exc)
        self.accept()

    def closeEvent(self, event):
        for worker in (self.probe, self.finder):
            if worker is not None:
                worker.wait(3000)
        event.accept()


def remember(dvr, saved):
    try:
        config.save(dvr.ip, dvr.user, dvr.password,
                    (saved or {}).get('channels'), dvr.serial, dvr.port)
    except OSError:
        pass


def obtain_dvr():
    saved = config.load()
    if saved is not None:
        if hik.port_open(saved['ip'], saved.get('port', 80), 0.8):
            try:
                dvr = hik.Dvr(saved['ip'], saved['user'], saved['password'],
                              saved.get('port', 80))
                dvr.serial = dvr.device_info(timeout=5).get('serialNumber')
                if dvr.serial and dvr.serial != saved.get('serial'):
                    remember(dvr, saved)
                return dvr
            except Exception:
                pass
        found = hik.find_device(saved['user'], saved['password'],
                                saved.get('serial'), saved['ip'])
        if found is not None:
            remember(found, saved)
            return found
    dialog = LoginDialog(saved)
    if dialog.exec() != QDialog.Accepted or dialog.result_credentials is None:
        return None
    picked = dialog.result_credentials
    dvr = hik.Dvr(picked['ip'], picked['user'], picked['password'], picked['port'])
    dvr.serial = picked['serial']
    return dvr
