import os
import sqlite3
import threading
from datetime import datetime, timedelta

from PySide6.QtCore import QThread, Signal

import config

DB_PATH = config.data_path('events.db')
EPISODE_GAP = timedelta(seconds=12)

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

    def run(self):
        while self.running:
            try:
                for event in self.dvr.events(timeout=120):
                    if not self.running:
                        return
                    if event['type'] != 'VMD':
                        continue
                    self.store.record(event['channel'], event['at'], event['target'])
                    self.detected.emit(event['channel'], event['target'] or '', event['at'])
            except Exception as exc:
                if not self.running:
                    return
                self.trouble.emit(str(exc))
                self.msleep(4000)

    def stop(self):
        self.running = False
