import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import uuid
from datetime import datetime, timedelta
from xml.etree import ElementTree

import urllib.error
import urllib.request

NS = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}


class Response:
    """Minimal stand-in for the parts of a requests Response this app uses."""

    def __init__(self, raw):
        self.raw = raw
        self.status = raw.status
        self._body = None

    @property
    def content(self):
        if self._body is None:
            self._body = self.raw.read()
        return self._body

    @property
    def text(self):
        return self.content.decode('utf-8', 'replace')

    def iter_lines(self):
        for line in self.raw:
            yield line.rstrip()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass


class Dvr:
    def __init__(self, ip, user, password, port=80):
        self.ip = ip
        self.port = port
        self.host = ip if port == 80 else '%s:%d' % (ip, port)
        self.user = user
        self.password = password
        self.serial = None
        self._local = threading.local()

    @property
    def opener(self):
        # urllib's digest handler keeps per-request state and is not thread safe,
        # so every thread gets its own opener.
        existing = getattr(self._local, 'opener', None)
        if existing is None:
            manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            manager.add_password(None, 'http://%s' % self.host, self.user, self.password)
            existing = urllib.request.build_opener(
                urllib.request.HTTPDigestAuthHandler(manager))
            self._local.opener = existing
        return existing

    def _open(self, path, data=None, headers=None, timeout=20):
        request = urllib.request.Request('http://%s%s' % (self.host, path),
                                         data=data, headers=headers or {})
        return Response(self.opener.open(request, timeout=timeout))

    def get(self, path, stream=False, timeout=20):
        return self._open(path, timeout=timeout)

    def post_xml(self, path, body, timeout=30):
        resp = self._open(path, data=body.encode('utf-8'),
                          headers={'Content-Type': 'application/xml'}, timeout=timeout)
        return ElementTree.fromstring(resp.content)

    def device_info(self, timeout=20):
        root = ElementTree.fromstring(
            self.get('/ISAPI/System/deviceInfo', timeout=timeout).content)
        return {child.tag.split('}')[-1]: child.text for child in root}

    def snapshot(self, channel, timeout=20):
        return self.get('/ISAPI/Streaming/channels/%d01/picture' % channel,
                        timeout=timeout).content

    def live_channels(self, candidates=range(1, 9), min_bytes=15000, timeout=4):
        def probe(channel):
            try:
                return channel, len(self.snapshot(channel, timeout=timeout)) >= min_bytes
            except Exception:
                return channel, False
        candidates = list(candidates)
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            return [c for c, ok in sorted(pool.map(probe, candidates)) if ok]

    def _creds_in_url(self):
        from urllib.parse import quote
        return quote(self.user, safe=''), quote(self.password, safe='')

    def live_url(self, channel, sub=True):
        user, password = self._creds_in_url()
        return 'rtsp://%s:%s@%s:554/Streaming/Channels/%d0%d' % (
            user, password, self.ip, channel, 2 if sub else 1)

    def playback_url(self, channel, start, end):
        user, password = self._creds_in_url()
        return 'rtsp://%s:%s@%s:554/Streaming/tracks/%d01?starttime=%s&endtime=%s' % (
            user, password, self.ip, channel, _stamp(start), _stamp(end))

    def search(self, channel, start, end, max_results=50):
        body = SEARCH_BODY % (uuid.uuid4(), channel, _iso(start), _iso(end), max_results)
        root = self.post_xml('/ISAPI/ContentMgmt/search', body)
        segments = []
        for item in root.iter('{%s}searchMatchItem' % NS['h']):
            span = item.find('h:timeSpan', NS)
            if span is None:
                continue
            segments.append((_parse(span.find('h:startTime', NS).text),
                             _parse(span.find('h:endTime', NS).text)))
        return segments

    def record_days(self, channel, year, month, sub=False):
        body = DAILY_BODY % (year, month)
        root = self.post_xml('/ISAPI/ContentMgmt/record/tracks/%d0%d/dailyDistribution' % (
            channel, 2 if sub else 1), body)
        days = []
        for day in root.iter('{%s}day' % NS['h']):
            has = day.find('h:record', NS)
            number = day.find('h:dayOfMonth', NS)
            if has is not None and has.text == 'true' and number is not None:
                kind = day.find('h:recordType', NS)
                days.append((int(number.text), kind.text if kind is not None else 'time'))
        return days

    def clip(self, channel, start, end, out_path):
        import runtime
        seconds = int((end - start).total_seconds())
        if seconds <= 0:
            raise ValueError('end must be after start')
        cmd = (['ffmpeg', '-y']
               + runtime.rtsp_input(self.playback_url(channel, start, end),
                                    duration=seconds)
               + ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k']
               + runtime.aspect_args()
               + ['-movflags', '+frag_keyframe+empty_moov', out_path])
        subprocess.run(cmd, check=True, creationflags=runtime.NO_WINDOW)
        return out_path

    def events(self, timeout=None, on_open=None):
        resp = self.get('/ISAPI/Event/notification/alertStream', stream=True, timeout=timeout)
        if on_open is not None:
            on_open(resp)
        chunk = []
        try:
            for line in resp.iter_lines():
                if line is None:
                    continue
                raw = line.decode('utf-8', 'replace') if isinstance(line, bytes) else line
                if raw.startswith('--') and chunk:
                    parsed = _parse_alert('\n'.join(chunk))
                    chunk = []
                    if parsed:
                        yield parsed
                else:
                    chunk.append(raw)
        finally:
            resp.close()


SEARCH_BODY = """<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
  <searchID>%s</searchID>
  <trackIDList><trackID>%d01</trackID></trackIDList>
  <timeSpanList><timeSpan><startTime>%s</startTime><endTime>%s</endTime></timeSpan></timeSpanList>
  <maxResults>%d</maxResults>
  <searchResultPostion>0</searchResultPostion>
  <metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>
</CMSearchDescription>"""


DAILY_BODY = """<?xml version="1.0" encoding="utf-8"?>
<trackDailyParam><year>%d</year><monthOfYear>%d</monthOfYear></trackDailyParam>"""


SADP_GROUP = '239.255.255.250'
SADP_PORT = 37020


def _local_subnets():
    targets = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address.startswith('127.') or address.startswith('169.254.'):
                continue
            broadcast = address.rsplit('.', 1)[0] + '.255'
            if broadcast not in targets:
                targets.append(broadcast)
    except socket.gaierror:
        pass
    return targets


def _parse_sadp(payload):
    try:
        root = ElementTree.fromstring(payload.decode('utf-8', 'replace'))
    except ElementTree.ParseError:
        return None
    field = {child.tag: (child.text or '') for child in root}
    ip = field.get('IPv4Address')
    serial = field.get('DeviceSN')
    if not ip or not serial:
        return None
    try:
        port = int(field.get('HttpPort') or 80)
    except ValueError:
        port = 80
    return {'ip': ip, 'serial': serial, 'port': port,
            'model': field.get('DeviceDescription') or 'Hikvision device',
            'mac': field.get('MAC') or '',
            'dhcp': (field.get('DHCP') or '').lower() == 'true'}


def sadp(timeout=2.0, want=None):
    probe = ('<?xml version="1.0" encoding="utf-8"?>'
             '<Probe><Uuid>%s</Uuid><Types>inquiry</Types></Probe>'
             % str(uuid.uuid4()).upper()).encode('utf-8')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    found = {}
    try:
        sock.bind(('', 0))
        for target in [(SADP_GROUP, SADP_PORT)] + [(net, SADP_PORT)
                                                   for net in _local_subnets()]:
            try:
                sock.sendto(probe, target)
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, _addr = sock.recvfrom(8192)
            except OSError:
                break
            device = _parse_sadp(payload)
            if device is not None:
                found.setdefault(device['serial'], device)
                if want and device['serial'] == want:
                    break
    finally:
        sock.close()
    return list(found.values())


def connect(device, user, password):
    dvr = Dvr(device['ip'], user, password, device['port'])
    info = dvr.device_info(timeout=4)
    if not info.get('serialNumber'):
        raise ValueError('not a Hikvision device')
    dvr.serial = info['serialNumber']
    return dvr


def find_device(user, password, serial=None, near=None):
    devices = sadp(want=serial)
    if serial:
        devices = [item for item in devices if item['serial'] == serial]
    for device in devices:
        try:
            return connect(device, user, password)
        except Exception:
            continue
    if near:
        return discover(user, password, near)
    return None


def port_open(host, port=80, timeout=0.4):
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def discover(user, password, near, port=554, timeout=0.4):
    base = near.rsplit('.', 1)[0]
    hosts = ['%s.%d' % (base, n) for n in range(1, 255)]
    with ThreadPoolExecutor(max_workers=64) as pool:
        flags = pool.map(lambda host: port_open(host, port, timeout), hosts)
    candidates = [host for host, live in zip(hosts, flags) if live]
    for host in candidates:
        try:
            dvr = Dvr(host, user, password)
            serial = dvr.device_info(timeout=4).get('serialNumber')
            if serial:
                dvr.serial = serial
                return dvr
        except Exception:
            continue
    return None


def _stamp(value):
    return value.strftime('%Y%m%dT%H%M%SZ')


def _iso(value):
    return value.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(text):
    return datetime.strptime(text.rstrip('Z'), '%Y-%m-%dT%H:%M:%S')


def _parse_alert(text):
    start = text.find('<EventNotificationAlert')
    if start < 0:
        return None
    try:
        root = ElementTree.fromstring(text[start:])
    except ElementTree.ParseError:
        return None
    field = {child.tag.split('}')[-1]: child.text for child in root}
    if field.get('eventState') == 'inactive':
        return None
    if not field.get('eventType'):
        return None
    channel = field.get('channelID') or field.get('dynChannelID') or '0'
    stamp = field.get('dateTime')
    return {'channel': int(channel), 'type': field.get('eventType'),
            'target': field.get('targetType'),
            'count': int(field.get('activePostCount') or 0),
            'at': _parse_stamp(stamp) if stamp else datetime.now()}


def _parse_stamp(text):
    body = text[:19]
    try:
        return datetime.strptime(body, '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return datetime.now()


if __name__ == '__main__':
    import config

    saved = config.load()
    if saved is None:
        raise SystemExit('no saved DVR credentials, run the app once first')
    dvr = Dvr(saved['ip'], saved['user'], saved['password'], saved['port'])
    try:
        dvr.device_info(timeout=5)
    except Exception:
        dvr = find_device(saved['user'], saved['password'],
                          saved['serial'] or None, saved['ip'])
        if dvr is None:
            raise SystemExit('no DVR answered on this network')
        print('moved to %s' % dvr.ip)
    info = dvr.device_info()
    print('%s  %s  fw %s' % (info.get('model'), info.get('deviceType'), info.get('firmwareVersion')))

    live = dvr.live_channels()
    print('live channels: %s' % live)

    end = datetime.now() - timedelta(minutes=5)
    start = end - timedelta(hours=2)
    for channel in live[:1]:
        found = dvr.search(channel, start, end)
        print('ch%d recordings in last 2h: %s' % (channel, [
            '%s -> %s' % (a.strftime('%H:%M:%S'), b.strftime('%H:%M:%S')) for a, b in found]))

    now = datetime.now()
    days = dvr.record_days(live[0], now.year, now.month)
    print('days with footage this month: %s' % [d for d, _ in days])
