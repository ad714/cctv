import os
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QImage

import config
import runtime

DB_PATH = config.data_path('events.db')
EPISODE_GAP = timedelta(seconds=12)
RETENTION_DAYS = 180
THUMB_WIDTH = 480
THUMB_DAYS = 7
CLIP_DAYS = 2
CLIP_BEFORE = timedelta(seconds=4)
CLIP_AFTER = timedelta(seconds=6)
CLIP_SETTLE = timedelta(seconds=45)
CACHE_START_MS = 15000
CACHE_IDLE_MS = 5000
COLUMNS = 'id, channel, started, ended, target, hits, snapshot, seen, clip'

SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY,
    channel INTEGER NOT NULL,
    started TEXT NOT NULL,
    ended TEXT NOT NULL,
    target TEXT,
    hits INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_detections_channel_started
    ON detections (channel, started);
CREATE INDEX IF NOT EXISTS idx_detections_started ON detections (started);
CREATE TABLE IF NOT EXISTS bookmarks (
    id INTEGER PRIMARY KEY,
    channel INTEGER NOT NULL,
    at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_channel_at ON bookmarks (channel, at);
"""


class Event:
    __slots__ = ('id', 'channel', 'start', 'end', 'target', 'hits', 'snapshot', 'seen', 'clip')

    def __init__(self, row):
        (self.id, self.channel, start, end, self.target, self.hits,
         self.snapshot, seen, self.clip) = row
        self.start = datetime.fromisoformat(start)
        self.end = datetime.fromisoformat(end)
        self.seen = bool(seen)


class EventStore:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(detections)')}
            if 'snapshot' not in columns:
                conn.execute('ALTER TABLE detections ADD COLUMN snapshot TEXT')
            if 'seen' not in columns:
                conn.execute('ALTER TABLE detections ADD COLUMN seen INTEGER NOT NULL DEFAULT 0')
                conn.execute('UPDATE detections SET seen = 1')
            if 'clip' not in columns:
                conn.execute('ALTER TABLE detections ADD COLUMN clip TEXT')

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA journal_size_limit=8388608')
        return conn

    def record(self, channel, moment, target):
        with self.lock, self._connect() as conn:
            row = conn.execute(
                'SELECT id, ended, target, hits FROM detections'
                ' WHERE channel = ? ORDER BY started DESC LIMIT 1',
                (channel,)).fetchone()
            if row is not None:
                last_id, ended, last_target, hits = row
                gap = moment - datetime.fromisoformat(ended)
                if timedelta(0) <= gap <= EPISODE_GAP:
                    merged = last_target or target
                    if target and last_target and target not in last_target.split(','):
                        merged = ','.join(sorted({last_target, target}))
                    conn.execute(
                        'UPDATE detections SET ended = ?, hits = ?, target = ? WHERE id = ?',
                        (moment.isoformat(timespec='seconds'), hits + 1, merged, last_id))
                    return last_id, False
            cursor = conn.execute(
                'INSERT INTO detections (channel, started, ended, target, hits)'
                ' VALUES (?, ?, ?, ?, 1)',
                (channel, moment.isoformat(timespec='seconds'),
                 moment.isoformat(timespec='seconds'), target))
            return cursor.lastrowid, True

    def set_snapshot(self, event_id, path):
        with self.lock, self._connect() as conn:
            conn.execute('UPDATE detections SET snapshot = ? WHERE id = ?', (path, event_id))

    def set_clip(self, event_id, path):
        with self.lock, self._connect() as conn:
            conn.execute('UPDATE detections SET clip = ? WHERE id = ?', (path, event_id))

    def prune_clips(self, days=CLIP_DAYS):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
        with self.lock, self._connect() as conn:
            stale = conn.execute('SELECT id, clip FROM detections'
                                 ' WHERE started < ? AND clip IS NOT NULL', (cutoff,)).fetchall()
            conn.executemany('UPDATE detections SET clip = NULL WHERE id = ?',
                             [(event_id,) for event_id, _clip in stale])
        for _event_id, clip in stale:
            try:
                os.remove(clip)
            except OSError:
                pass
        return len(stale)

    def _query(self, where='', params=(), order='started DESC', limit=None):
        sql = 'SELECT %s FROM detections' % COLUMNS
        if where:
            sql += ' WHERE ' + where
        sql += ' ORDER BY ' + order
        if limit:
            sql += ' LIMIT %d' % limit
        with self.lock, self._connect() as conn:
            return [Event(row) for row in conn.execute(sql, params).fetchall()]

    def get(self, event_id):
        found = self._query('id = ?', (event_id,))
        return found[0] if found else None

    def day(self, channel, day):
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        return self._query('channel = ? AND started >= ? AND started < ?',
                           (channel, start.isoformat(timespec='seconds'),
                            end.isoformat(timespec='seconds')), order='started')

    def recent(self, since=None, until=None, channel=None, kind=None, unread=False, limit=500):
        clauses, params = [], []
        if since is not None:
            clauses.append('started >= ?')
            params.append(since.isoformat(timespec='seconds'))
        if until is not None:
            clauses.append('started < ?')
            params.append(until.isoformat(timespec='seconds'))
        if channel is not None:
            clauses.append('channel = ?')
            params.append(channel)
        if kind == 'person':
            clauses.append("target LIKE '%human%'")
        elif kind == 'vehicle':
            clauses.append("target LIKE '%vehicle%'")
        if unread:
            clauses.append('seen = 0')
        return self._query(' AND '.join(clauses), params, limit=limit)

    def mark_seen(self, ids):
        ids = list(ids)
        if not ids:
            return
        with self.lock, self._connect() as conn:
            conn.executemany('UPDATE detections SET seen = 1 WHERE id = ?', [(i,) for i in ids])

    def mark_all_seen(self, channel=None):
        with self.lock, self._connect() as conn:
            if channel is None:
                conn.execute('UPDATE detections SET seen = 1 WHERE seen = 0')
            else:
                conn.execute('UPDATE detections SET seen = 1 WHERE seen = 0 AND channel = ?',
                             (channel,))

    def unread_counts(self):
        with self.lock, self._connect() as conn:
            return dict(conn.execute(
                'SELECT channel, COUNT(*) FROM detections WHERE seen = 0 GROUP BY channel'))

    def add_bookmark(self, channel, moment, note=''):
        with self.lock, self._connect() as conn:
            return conn.execute('INSERT INTO bookmarks (channel, at, note) VALUES (?, ?, ?)',
                                (channel, moment.isoformat(timespec='seconds'), note)).lastrowid

    def bookmarks(self, channel, day):
        start = datetime.combine(day, datetime.min.time())
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT id, at, note FROM bookmarks WHERE channel = ? AND at >= ? AND at < ?'
                ' ORDER BY at', (channel, start.isoformat(timespec='seconds'),
                                 (start + timedelta(days=1)).isoformat(timespec='seconds')))
            return [(mark_id, datetime.fromisoformat(at), note) for mark_id, at, note in rows]

    def bookmark(self, mark_id):
        with self.lock, self._connect() as conn:
            row = conn.execute('SELECT note FROM bookmarks WHERE id = ?', (mark_id,)).fetchone()
        return row[0] if row else None

    def set_bookmark_note(self, mark_id, note):
        with self.lock, self._connect() as conn:
            conn.execute('UPDATE bookmarks SET note = ? WHERE id = ?', (note, mark_id))

    def delete_bookmark(self, mark_id):
        with self.lock, self._connect() as conn:
            conn.execute('DELETE FROM bookmarks WHERE id = ?', (mark_id,))

    def prune(self, days=RETENTION_DAYS):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
        with self.lock, self._connect() as conn:
            stale = [path for row in conn.execute(
                'SELECT snapshot, clip FROM detections WHERE started < ?', (cutoff,))
                for path in row if path]
            removed = conn.execute(
                'DELETE FROM detections WHERE started < ?', (cutoff,)).rowcount
        for path in stale:
            try:
                os.remove(path)
            except OSError:
                pass
        if removed:
            runtime.log.info('pruned %d detection(s) older than %d days', removed, days)
        return removed

    def counts(self):
        with self.lock, self._connect() as conn:
            total = conn.execute('SELECT COUNT(*) FROM detections').fetchone()[0]
            first = conn.execute('SELECT MIN(started) FROM detections').fetchone()[0]
        return total, first


def thumb_path(channel, moment, event_id, folder=None, ext='jpg'):
    return os.path.join(folder or config.thumbs_dir(), 'ch%d-%s-%d.%s' % (
        channel, moment.strftime('%Y%m%d-%H%M%S'), event_id, ext))


def clip_window(event):
    return event.start - CLIP_BEFORE, event.end + CLIP_AFTER


def clips_dir():
    path = config.data_path('clips')
    os.makedirs(path, exist_ok=True)
    return path


def save_thumbnail(data, path, width=THUMB_WIDTH):
    image = data if isinstance(data, QImage) else QImage.fromData(data)
    if image.isNull():
        return False
    if image.width() > width:
        image = image.scaled(width, int(width * 3 / 4), Qt.IgnoreAspectRatio,
                             Qt.SmoothTransformation)
    return image.save(path, 'JPG', 80)


class EventWatcher(QThread):
    detected = Signal(int, str, object)
    created = Signal(int)
    snapshot_ready = Signal(int)
    trouble = Signal(str)

    def __init__(self, dvr, store):
        super().__init__()
        self.dvr = dvr
        self.store = store
        self.running = True
        self.response = None
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='snapshot')

    def run(self):
        attempt = 0
        while self.running:
            try:
                for event in self.dvr.events(timeout=120, on_open=self._opened):
                    attempt = 0
                    if not self.running:
                        return
                    if event['type'] != 'VMD':
                        continue
                    event_id, new = self.store.record(event['channel'], event['at'],
                                                      event['target'])
                    if new:
                        self.pool.submit(self._grab, event_id, event['channel'], event['at'])
                        self.created.emit(event_id)
                    self.detected.emit(event['channel'], event['target'] or '', event['at'])
            except Exception as exc:
                if not self.running:
                    return
                delay = runtime.backoff_delay(attempt, cap=60.0)
                attempt += 1
                runtime.log.warning('alert stream dropped (%s), retry %d in %.1fs',
                                    exc, attempt, delay)
                self.trouble.emit(str(exc))
                self.msleep(int(delay * 1000))

    def _grab(self, event_id, channel, moment):
        path = thumb_path(channel, moment, event_id)
        try:
            if save_thumbnail(self.dvr.snapshot(channel, timeout=6), path):
                self.store.set_snapshot(event_id, path)
        except Exception as exc:
            runtime.log.info('snapshot for event %d failed: %s', event_id, exc)
        if self.running:
            self.snapshot_ready.emit(event_id)

    def _opened(self, response):
        self.response = response

    def stop(self):
        self.running = False
        self.pool.shutdown(wait=False, cancel_futures=True)
        response, self.response = self.response, None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


class EventCache(QThread):
    snapshot_ready = Signal(int)

    def __init__(self, dvr, store):
        super().__init__()
        self.dvr = dvr
        self.store = store
        self.running = True
        self.skip = set()

    def run(self):
        self.msleep(CACHE_START_MS)
        while self.running:
            job = self._next()
            if job is None:
                self.msleep(CACHE_IDLE_MS)
                continue
            kind, event = job
            try:
                done = self._clip(event) if kind == 'clip' else self._thumb(event)
            except Exception as exc:
                runtime.log.info('event cache %s for %d failed: %s', kind, event.id, exc)
                done = False
            if not done:
                self.skip.add((kind, event.id))
            self.msleep(200)

    def _next(self):
        now = datetime.now()
        for event in self.store.recent(since=now - timedelta(days=CLIP_DAYS), limit=2000):
            if not event.clip and event.end < now - CLIP_SETTLE \
                    and ('clip', event.id) not in self.skip:
                return 'clip', event
        for event in self.store.recent(since=now - timedelta(days=THUMB_DAYS), limit=2000):
            if not event.snapshot and ('thumb', event.id) not in self.skip:
                return 'thumb', event
        return None

    def _ffmpeg(self, command, timeout):
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, creationflags=runtime.NO_WINDOW)

    def _clip(self, event):
        begin, finish = clip_window(event)
        seconds = int((finish - begin).total_seconds()) + 1
        path = thumb_path(event.channel, event.start, event.id, clips_dir(), 'ts')
        url = self.dvr.playback_url(event.channel, begin, finish)
        self._ffmpeg(['ffmpeg', '-y'] + runtime.rtsp_input(url, duration=seconds)
                     + ['-map', '0:v', '-map', '0:a?', '-c:v', 'copy', '-c:a', 'aac',
                        '-b:a', '64k', '-f', 'mpegts', path], seconds + 30)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        self.store.set_clip(event.id, path)
        if not event.snapshot:
            snap = thumb_path(event.channel, event.start, event.id)
            self._ffmpeg(['ffmpeg', '-y', '-loglevel', 'error', '-ss',
                          str(int(CLIP_BEFORE.total_seconds()) + 1), '-i', path,
                          '-frames:v', '1', '-q:v', '4', '-vf',
                          'scale=%d:%d' % (THUMB_WIDTH, THUMB_WIDTH * 3 // 4), snap], 20)
            if os.path.exists(snap) and os.path.getsize(snap) > 0:
                self.store.set_snapshot(event.id, snap)
                self.snapshot_ready.emit(event.id)
        return True

    def _thumb(self, event):
        path = thumb_path(event.channel, event.start, event.id)
        at = event.start + timedelta(seconds=1)
        url = self.dvr.playback_url(event.channel, at, at + timedelta(seconds=4))
        self._ffmpeg(['ffmpeg', '-y'] + runtime.rtsp_input(url, duration=4)
                     + ['-frames:v', '1', '-q:v', '4',
                        '-vf', 'scale=%d:%d' % (THUMB_WIDTH, THUMB_WIDTH * 3 // 4), path], 20)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        self.store.set_snapshot(event.id, path)
        self.snapshot_ready.emit(event.id)
        return True

    def stop(self):
        self.running = False
