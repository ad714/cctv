import os
import sqlite3
import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QThread, Signal

import config
import runtime

DB_PATH = config.data_path('events.db')
EPISODE_GAP = timedelta(seconds=12)
RETENTION_DAYS = 180

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
"""


class EventStore:
    def __init__(self, path=DB_PATH):
        self.path = path
        self.lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)

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
                    return False
            conn.execute(
                'INSERT INTO detections (channel, started, ended, target, hits)'
                ' VALUES (?, ?, ?, ?, 1)',
                (channel, moment.isoformat(timespec='seconds'),
                 moment.isoformat(timespec='seconds'), target))
            return True

    def day(self, channel, day):
        start = datetime.combine(day, datetime.min.time())
        end = start + timedelta(days=1)
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                'SELECT started, ended, target, hits FROM detections'
                ' WHERE channel = ? AND started >= ? AND started < ? ORDER BY started',
                (channel, start.isoformat(timespec='seconds'),
                 end.isoformat(timespec='seconds'))).fetchall()
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b), c, d)
                for a, b, c, d in rows]

    def prune(self, days=RETENTION_DAYS):
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
        with self.lock, self._connect() as conn:
            removed = conn.execute(
                'DELETE FROM detections WHERE started < ?', (cutoff,)).rowcount
        if removed:
            runtime.log.info('pruned %d detection(s) older than %d days', removed, days)
        return removed

    def counts(self):
        with self.lock, self._connect() as conn:
            total = conn.execute('SELECT COUNT(*) FROM detections').fetchone()[0]
            first = conn.execute('SELECT MIN(started) FROM detections').fetchone()[0]
        return total, first


class EventWatcher(QThread):
    detected = Signal(int, str, object)
    trouble = Signal(str)

    def __init__(self, dvr, store):
        super().__init__()
        self.dvr = dvr
        self.store = store
        self.running = True
        self.response = None

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
                    self.store.record(event['channel'], event['at'], event['target'])
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

    def _opened(self, response):
        self.response = response

    def stop(self):
        self.running = False
        response, self.response = self.response, None
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
