import os
import subprocess
import tempfile
import threading
import time
from datetime import timedelta

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtGui import QImage

import runtime

SPEEDS = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
CHUNK_BYTES = 96 * 1024 * 1024
PREBUFFER_BYTES = 1024 * 1024
DEFAULT_FPS = 15.0


def probe_fps(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=avg_frame_rate', '-of', 'default=nk=1:nw=1', path],
            capture_output=True, text=True, timeout=8,
            creationflags=runtime.NO_WINDOW).stdout.strip()
        num, den = out.split('/')
        fps = float(num) / float(den)
        return fps if fps > 1 else DEFAULT_FPS
    except Exception:
        return DEFAULT_FPS


class Downloader(threading.Thread):
    def __init__(self, dvr, channel, name, begin, finish, path, cap=CHUNK_BYTES):
        super().__init__(daemon=True)
        self.dvr = dvr
        self.channel = channel
        self.name = name
        self.begin = begin
        self.finish = finish
        self.path = path
        self.cap = cap
        self.written = 0
        self.finished = False
        self.error = None
        self._stop = False

    def run(self):
        try:
            resp = self.dvr.download_stream(self.channel, self.name, self.begin, self.finish)
        except Exception as exc:
            self.error = exc
            self.finished = True
            return
        try:
            with open(self.path, 'wb', buffering=0) as handle:
                while not self._stop and self.written < self.cap:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    self.written += len(chunk)
        except Exception as exc:
            self.error = exc
        finally:
            try:
                resp.close()
            except Exception:
                pass
            self.finished = True

    def stop(self):
        self._stop = True


class Decoder(QThread):
    frame_ready = Signal(QImage)
    advanced = Signal(float)
    ended = Signal(float)

    def __init__(self, path, offset, size, fps):
        super().__init__()
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
                 '-ss', '%.3f' % max(0.0, self.offset), '-i', self.path, '-an',
                 '-vf', 'scale=%d:%d,setsar=1' % (self.width, self.height),
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
        self._dir = tempfile.mkdtemp(prefix='cctv-pb-')
        self._path = None
        self._file_start = None
        self._segment = None
        self._fps = DEFAULT_FPS
        self._downloader = None
        self._decoder = None
        self._position_dt = None
        self._pending_step = False
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
        return None

    def play_at(self, moment, keep_paused=False):
        segment = self._segment_for(moment)
        if segment is None:
            self.state.emit('no footage at that time')
            return False
        reuse = (self._path and self._file_start and self._segment
                 and self._segment[2] == segment[2]
                 and self._file_start <= moment <= (self._position_dt or self._file_start))
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
        self._path = os.path.join(self._dir, 'win-%d.mpg' % int(time.time() * 1000))
        self._file_start = moment
        self._segment = segment
        self._downloader = Downloader(self.dvr, self.channel, segment[2], moment, segment[1],
                                      self._path)
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
            self.state.emit('could not load footage')
            return
        if downloader.written >= PREBUFFER_BYTES or downloader.finished:
            self._prebuffer.stop()
            if not os.path.exists(self._path) or os.path.getsize(self._path) == 0:
                self.state.emit('no footage at that time')
                return
            self._fps = probe_fps(self._path)
            self._start_decoder(0.0)

    def _start_decoder(self, offset):
        self._teardown_decoder()
        decoder = Decoder(self._path, offset, self.size, self._fps)
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
        if segment is not None and reached < segment[1] - timedelta(seconds=1) \
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

    def shutdown(self):
        self.stop()
        try:
            for name in os.listdir(self._dir):
                try:
                    os.remove(os.path.join(self._dir, name))
                except OSError:
                    pass
            os.rmdir(self._dir)
        except OSError:
            pass
