import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QImage

import runtime

SPEEDS = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
CHUNK_BYTES = 400 * 1024 * 1024
PREBUFFER_BYTES = 96 * 1024
FOLLOW_TIMEOUT_US = 4000000
LIVE_EDGE = timedelta(minutes=3)
LIVE_RETRIES = 20
LIVE_RETRY_MS = 3000
FOLLOW_SPAN = timedelta(hours=2)
DEFAULT_FPS = 15.0
LOCAL = 'local'


def probe_fps(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-probesize', runtime.PROBE_SIZE,
             '-analyzeduration', runtime.ANALYZE_DURATION, '-select_streams', 'v:0',
             '-show_entries', 'stream=avg_frame_rate', '-of', 'default=nk=1:nw=1', path],
            capture_output=True, text=True, timeout=8,
            creationflags=runtime.NO_WINDOW).stdout.strip()
        num, den = out.split('/')
        fps = float(num) / float(den)
        return fps if fps > 1 else DEFAULT_FPS
    except Exception:
        return DEFAULT_FPS


class Recorder(threading.Thread):
    def __init__(self, dvr, channel, begin, finish, path, cap=CHUNK_BYTES):
        super().__init__(daemon=True)
        self.dvr = dvr
        self.channel = channel
        self.begin = begin
        self.finish = finish
        self.path = path
        self.cap = cap
        self.written = 0
        self.finished = False
        self.error = None
        self.proc = None
        self._stop = False

    def run(self):
        seconds = max(1, int((self.finish - self.begin).total_seconds()))
        url = self.dvr.playback_url(self.channel, self.begin, self.finish)
        command = (['ffmpeg', '-y'] + runtime.rtsp_input(url, duration=seconds)
                   + ['-map', '0:v', '-map', '0:a?', '-c:v', 'copy', '-c:a', 'aac',
                      '-b:a', '64k', '-flush_packets', '1', '-f', 'mpegts', self.path])
        try:
            self.proc = runtime.spawn(command, stdout=subprocess.DEVNULL)
            while not self._stop and self.proc.poll() is None:
                if os.path.exists(self.path):
                    self.written = os.path.getsize(self.path)
                    if self.written >= self.cap:
                        break
                time.sleep(0.05)
            runtime.stop_process(self.proc, wait=False)
            if os.path.exists(self.path):
                self.written = os.path.getsize(self.path)
            if self.written == 0 and not self._stop:
                self.error = RuntimeError('no footage at that time')
        except Exception as exc:
            self.error = exc
        finally:
            self.finished = True

    def stop(self):
        self._stop = True
        runtime.stop_process(self.proc, wait=False)


class Decoder(QThread):
    frame_ready = Signal(QImage)
    advanced = Signal(float)
    ended = Signal(float)

    def __init__(self, path, offset, size, fps, denoise=0):
        super().__init__()
        self.denoise = denoise
        self.path = path
        self.offset = offset
        self.width, self.height = size
        self.fps = fps if fps > 1 else DEFAULT_FPS
        self.speed = 1.0
        self.running = True
        self.proc = None
        self._paused = False
        self._steps = 0

    def _command(self):
        return (['ffmpeg', '-hide_banner', '-loglevel', 'error',
                 '-follow', '1', '-rw_timeout', str(FOLLOW_TIMEOUT_US),
                 '-probesize', runtime.PROBE_SIZE, '-analyzeduration', runtime.ANALYZE_DURATION,
                 '-ss', '%.3f' % max(0.0, self.offset), '-i', self.path, '-an',
                 '-vf', '%sscale=%d:%d,setsar=1' % (runtime.denoise_filter(self.denoise),
                                                    self.width, self.height),
                 '-f', 'rawvideo', '-pix_fmt', 'bgr24', '-'])

    def _frames_per_tick(self):
        return max(1, int(round(self.speed)))

    def _tick_ms(self):
        fpt = self._frames_per_tick()
        return max(1, int(1000.0 * fpt / (self.fps * self.speed)))

    def run(self):
        expected = self.width * self.height * 3
        self.proc = runtime.spawn(self._command(), stdout=subprocess.PIPE)
        index = 0
        while self.running:
            stepping = self._steps > 0
            if self._paused and not stepping:
                self.msleep(30)
                continue
            count = 1 if stepping else self._frames_per_tick()
            last = None
            for _ in range(count):
                data = self.proc.stdout.read(expected)
                if len(data) < expected:
                    self._shutdown()
                    self.ended.emit(self.offset + index / self.fps)
                    return
                index += 1
                last = data
            if last is not None:
                image = QImage(last, self.width, self.height,
                               self.width * 3, QImage.Format_BGR888)
                self.frame_ready.emit(image.copy())
                self.advanced.emit(self.offset + index / self.fps)
            if stepping:
                self._steps -= 1
                self._paused = True
            else:
                self.msleep(self._tick_ms())

    def _shutdown(self):
        runtime.stop_process(self.proc, wait=False)
        self.proc = None

    def set_speed(self, speed):
        self.speed = speed

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def request_step(self):
        self._steps += 1

    def stop(self):
        self.running = False
        self._shutdown()


class Playback(QObject):
    frame_ready = Signal(QImage)
    position = Signal(float)
    state = Signal(str)
    ended = Signal()

    def __init__(self, dvr, size):
        super().__init__()
        self.dvr = dvr
        self.size = size
        self.channel = None
        self.day = None
        self.segments = []
        self.speed = 1.0
        self.paused = False
        self.denoise = 0
        self.fetch_limit = None
        self._fps_by_channel = {}
        self._dir = tempfile.mkdtemp(prefix='cctv-pb-')
        self._path = None
        self._file_start = None
        self._segment = None
        self._fps = DEFAULT_FPS
        self._downloader = None
        self._decoder = None
        self._position_dt = None
        self._pending_step = False
        self._retries = 0
        self._prebuffer = QTimer(self)
        self._prebuffer.setInterval(60)
        self._prebuffer.timeout.connect(self._check_prebuffer)

    def configure(self, channel, day, segments):
        self.stop()
        self.channel = channel
        self.day = day
        self.segments = sorted(segments, key=lambda s: s[0])

    def is_playing(self):
        return self._decoder is not None

    def current_time(self):
        return self._position_dt

    def audio_spec(self):
        if self._path and self._file_start and self._position_dt:
            return self._path, max(0.0, (self._position_dt - self._file_start).total_seconds())
        return None

    def _segment_for(self, moment):
        for start, end, name in self.segments:
            if start <= moment <= end and name:
                return (start, end, name)
        last = self.segments[-1] if self.segments else None
        if last is not None and self._is_open(last) and last[0] <= moment < datetime.now():
            return self.segments[-1]
        return None

    @staticmethod
    def _is_open(segment):
        return segment[2] != LOCAL and segment[1] > datetime.now() - LIVE_EDGE

    def play_file(self, path, channel, start, end, moment):
        self.stop()
        self.channel = channel
        self.day = start.date()
        self.segments = [(start, end, LOCAL)]
        self._path = path
        self._file_start = start
        self._segment = self.segments[0]
        self._position_dt = moment
        self._fps = self._probe(path)
        self._start_decoder(max(0.0, (moment - start).total_seconds()))

    def release(self):
        self._teardown_decoder()
        self._teardown_downloader()

    def _probe(self, path):
        if self.channel not in self._fps_by_channel:
            self._fps_by_channel[self.channel] = probe_fps(path)
        return self._fps_by_channel[self.channel]

    def play_at(self, moment, keep_paused=False):
        self._retries = 0
        segment = self._segment_for(moment)
        if segment is None:
            self.state.emit('no footage at that time')
            return False
        cached_until = segment[1] if segment[2] == LOCAL else (
            self._position_dt or self._file_start)
        reuse = (self._path and self._file_start and self._segment
                 and self._segment[2] == segment[2]
                 and self._file_start <= moment <= cached_until)
        self._position_dt = moment
        self.paused = keep_paused
        if reuse:
            self._start_decoder((moment - self._file_start).total_seconds())
        else:
            self._new_download(segment, moment)
        return True

    def _new_download(self, segment, moment):
        self._teardown_decoder()
        self._teardown_downloader()
        self._discard_cache()
        self._path = os.path.join(self._dir, 'win-%d.ts' % int(time.time() * 1000))
        self._file_start = moment
        self._segment = segment
        if self._is_open(segment):
            finish = moment + FOLLOW_SPAN
        else:
            finish = max(segment[1], moment + timedelta(seconds=30))
        if self.fetch_limit is not None:
            finish = max(moment + timedelta(seconds=5), min(finish, self.fetch_limit))
        self._downloader = Recorder(self.dvr, self.channel, moment, finish, self._path)
        self._downloader.start()
        self.state.emit('buffering')
        self._prebuffer.start()

    def _check_prebuffer(self):
        downloader = self._downloader
        if downloader is None:
            self._prebuffer.stop()
            return
        if downloader.error is not None:
            self._prebuffer.stop()
            segment, moment = self._segment, self._file_start
            near_live = (segment is not None and moment is not None and self._is_open(segment)
                         and moment > datetime.now() - LIVE_EDGE)
            if near_live and self._retries < LIVE_RETRIES:
                self._retries += 1
                self.state.emit('waiting')
                QTimer.singleShot(LIVE_RETRY_MS, lambda: self._retry(downloader, segment, moment))
                return
            self.state.emit('could not load footage')
            return
        if downloader.written >= PREBUFFER_BYTES or downloader.finished:
            self._prebuffer.stop()
            if not os.path.exists(self._path) or os.path.getsize(self._path) == 0:
                self.state.emit('no footage at that time')
                return
            self._retries = 0
            self._fps = self._probe(self._path)
            self._start_decoder(0.0)

    def _retry(self, failed, segment, moment):
        if self._downloader is failed:
            self._new_download(segment, moment)

    def _start_decoder(self, offset):
        self._teardown_decoder()
        decoder = Decoder(self._path, offset, self.size, self._fps, self.denoise)
        decoder.set_speed(self.speed)
        if self.paused:
            decoder.pause()
        decoder.frame_ready.connect(self._on_frame)
        decoder.advanced.connect(self._on_advanced)
        decoder.ended.connect(self._on_ended)
        decoder.start()
        if self._pending_step:
            self._pending_step = False
            decoder.request_step()
        self._decoder = decoder
        self.state.emit('paused' if self.paused else 'playing')

    def _on_frame(self, image):
        if self.sender() is self._decoder:
            self.frame_ready.emit(image)

    def _on_advanced(self, decoded_seconds):
        if self.sender() is not self._decoder or self._file_start is None:
            return
        current = self._file_start + timedelta(seconds=decoded_seconds)
        self._position_dt = current
        midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
        self.position.emit((current - midnight).total_seconds())

    def _on_ended(self, final_seconds):
        if self.sender() is not self._decoder or self._file_start is None:
            return
        reached = self._file_start + timedelta(seconds=final_seconds)
        segment = self._segment
        if segment is not None and self._is_open(segment):
            limit = datetime.now() + LIVE_EDGE
        else:
            limit = segment[1] if segment is not None else reached
        if self.fetch_limit is not None:
            limit = min(limit, self.fetch_limit)
        if segment is not None and reached < limit - timedelta(seconds=1) \
                and reached > self._file_start + timedelta(seconds=1):
            self._decoder = None
            self._new_download(segment, reached)
            return
        later = [s for s in self.segments if s[0] > (segment[1] if segment else reached)]
        self._decoder = None
        if later:
            self.play_at(later[0][0])
        else:
            self.state.emit('reached the end')
            self.ended.emit()

    def toggle_pause(self):
        if self._decoder is None:
            return
        self.paused = not self.paused
        if self.paused:
            self._decoder.pause()
            self.state.emit('paused')
        else:
            self._decoder.resume()
            self.state.emit('playing')

    def set_speed(self, speed):
        self.speed = speed
        if self._decoder is not None:
            self._decoder.set_speed(speed)

    def set_denoise(self, level):
        self.denoise = level
        if self._decoder is not None and self._position_dt is not None:
            self.play_at(self._position_dt, keep_paused=self.paused)

    def step(self):
        if self._decoder is None:
            return
        self.paused = True
        self._decoder.request_step()
        self.state.emit('paused')

    def step_back(self):
        if self._position_dt is None:
            return
        self._pending_step = True
        self.play_at(self._position_dt - timedelta(seconds=1.0 / max(self._fps, 1.0)),
                     keep_paused=True)

    def nudge(self, seconds):
        if self._position_dt is None:
            return
        self.play_at(self._position_dt + timedelta(seconds=seconds))

    def _teardown_decoder(self):
        if self._decoder is not None:
            self._decoder.stop()
            self._decoder.wait(2000)
            self._decoder = None

    def _discard_cache(self):
        os.makedirs(self._dir, exist_ok=True)
        for name in os.listdir(self._dir):
            try:
                os.remove(os.path.join(self._dir, name))
            except OSError:
                pass

    def _teardown_downloader(self):
        self._prebuffer.stop()
        if self._downloader is not None:
            self._downloader.stop()
            self._downloader = None

    def stop(self):
        self._teardown_decoder()
        self._teardown_downloader()
        self._file_start = None
        self._segment = None
        self._position_dt = None
        self.paused = False
