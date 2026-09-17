import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import uuid
from datetime import datetime, timedelta
from xml.etree import ElementTree

import requests
from requests.auth import HTTPDigestAuth

NS = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
DEFAULT_IP = '192.168.1.6'


def load_env(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), '.hikenv')
    kv = {}
    with open(path, encoding='utf-8-sig') as fh:
        for line in fh:
            if '=' in line and not line.strip().startswith('#'):
                key, _, val = line.partition('=')
                kv[key.strip()] = val.strip().strip('"').strip("'")
    return kv


class Dvr:
    def __init__(self, ip=DEFAULT_IP, user=None, password=None):
        if user is None or password is None:
            kv = load_env()
            user = user or kv.get('HIK_USER', 'admin')
            password = password or kv['HIK_PASS']
        self.ip = ip
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(user, password)

    def get(self, path, stream=False, timeout=20):
        resp = self.session.get('http://%s%s' % (self.ip, path), stream=stream, timeout=timeout)
        resp.raise_for_status()
        return resp

    def post_xml(self, path, body, timeout=30):
        resp = self.session.post('http://%s%s' % (self.ip, path), data=body.encode('utf-8'),
                                 headers={'Content-Type': 'application/xml'}, timeout=timeout)
        resp.raise_for_status()
        return ElementTree.fromstring(resp.content)

    def device_info(self):
        root = ElementTree.fromstring(self.get('/ISAPI/System/deviceInfo').content)
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

    def stream_size(self, channel, sub=True, timeout=25):
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-select_streams', 'v:0',
             '-show_entries', 'stream=coded_width,coded_height', '-of', 'csv=p=0',
             self.live_url(channel, sub=sub)],
            capture_output=True, text=True, timeout=timeout).stdout.strip()
        width, _, height = out.partition(',')
        return int(width), int(height)

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
               + ['-c:v', 'copy', '-c:a', 'aac', '-b:a', '64k', '-aspect', '4:3',
                  '-movflags', '+frag_keyframe+empty_moov', out_path])
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
    dvr = Dvr()
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
