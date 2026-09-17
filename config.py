import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes

APP_NAME = 'HikViewer'
WINDOWS = sys.platform == 'win32'


def config_dir():
    if WINDOWS:
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(config_dir(), 'config.json')


def data_path(name):
    return os.path.join(config_dir(), name)


def captures_dir():
    base = os.path.join(os.path.expanduser('~'), 'Videos', 'CCTV')
    os.makedirs(base, exist_ok=True)
    return base


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]

    def value(self):
        return ctypes.string_at(self.pbData, self.cbData)


def _blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def protect(text):
    if not WINDOWS:
        return 'b64:' + base64.b64encode(text.encode('utf-8')).decode('ascii')
    out = _Blob()
    source = _blob(text.encode('utf-8'))
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), APP_NAME, None, None, None, 0, ctypes.byref(out)):
        raise OSError('CryptProtectData failed')
    try:
        return 'dpapi:' + base64.b64encode(out.value()).decode('ascii')
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(stored):
    if stored.startswith('b64:'):
        return base64.b64decode(stored[4:]).decode('utf-8')
    if not stored.startswith('dpapi:'):
        return stored
    if not WINDOWS:
        raise OSError('DPAPI secret cannot be read on this platform')
    out = _Blob()
    source = _blob(base64.b64decode(stored[6:]))
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError('CryptUnprotectData failed')
    try:
        return out.value().decode('utf-8')
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def load():
    data = _read()
    if data is None:
        return None
    if not data.get('ip') or not data.get('user') or not data.get('secret'):
        return None
    try:
        password = unprotect(data['secret'])
    except OSError:
        return None
    return {'ip': data['ip'], 'user': data['user'], 'password': password,
            'channels': data.get('channels') or [],
            'sizes': data.get('sizes') or None}


def _write(payload):
    path = config_path()
    temp = path + '.tmp'
    with open(temp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2)
    if WINDOWS:
        try:
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x80)
        except Exception:
            pass
    os.replace(temp, path)
    if WINDOWS:
        try:
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)
        except Exception:
            pass
    return path


def _read():
    try:
        with open(config_path(), encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save(ip, user, password, channels=None):
    payload = {'ip': ip, 'user': user, 'secret': protect(password)}
    if channels:
        payload['channels'] = list(channels)
    return _write(payload)


def remember_channels(channels):
    data = _read()
    if data is None or data.get('channels') == list(channels):
        return False
    data['channels'] = list(channels)
    _write(data)
    return True


def remember_sizes(sizes):
    data = _read()
    if data is None or data.get('sizes') == sizes:
        return False
    data['sizes'] = sizes
    _write(data)
    return True


def forget():
    try:
        os.remove(config_path())
        return True
    except OSError:
        return False


def import_legacy(folder):
    path = os.path.join(folder, '.hikenv')
    if not os.path.exists(path):
        return None
    values = {}
    with open(path, encoding='utf-8-sig') as handle:
        for line in handle:
            if '=' in line and not line.strip().startswith('#'):
                key, _, value = line.partition('=')
                values[key.strip()] = value.strip().strip('"').strip("'")
    if not values.get('HIK_PASS'):
        return None
    return {'ip': values.get('HIK_IP', '192.168.1.6'),
            'user': values.get('HIK_USER', 'admin'),
            'password': values['HIK_PASS']}
