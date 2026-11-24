import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import timedelta

from PySide6.QtCore import QThread, Signal

import runtime

TILE = (640, 480)


def _segment_for(dvr, channel, moment):
    window_start = moment - timedelta(hours=24)
    for start, end, name in dvr.playback_segments(channel, window_start,
                                                   moment + timedelta(minutes=1)):
        if name and start <= moment <= end:
            return start, name
    return None


def fast_clip(dvr, channel, seg_start, begin, end, out_path, with_audio,
              aspect_args, cancel, on_seconds=None):
    duration = int((end - begin).total_seconds())
    if duration <= 0:
        raise ValueError('end must be after start')
    pre = min(3.0, max(0.0, (begin - seg_start).total_seconds()))
    url = dvr.playback_url(channel, begin - timedelta(seconds=pre), end)
    audio = ['-c:a', 'aac', '-b:a', '64k'] if with_audio else ['-an']
    cmd = (['ffmpeg', '-y', '-nostats', '-progress', 'pipe:1'] + runtime.rtsp_input(url)
           + ['-ss', '%.3f' % pre, '-t', str(duration),
              '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21']
           + audio + aspect_args + ['-movflags', '+faststart', out_path])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=runtime.NO_WINDOW)
    watcher = threading.Thread(target=_read_progress, args=(proc.stdout, on_seconds),
                               daemon=True)
    watcher.start()
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancel():
                runtime.stop_process(proc)
                raise RuntimeError('cancelled')
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError('no footage in that range for camera %d' % channel)
    return out_path


def _read_progress(stream, on_seconds):
    for line in stream:
        if on_seconds is None or not line.startswith(b'out_time_us='):
            continue
        try:
            on_seconds(max(0.0, int(line.split(b'=', 1)[1]) / 1e6))
        except ValueError:
            pass


def _layout(count):
    rows = max(1, int(count ** 0.5))
    base, extra = divmod(count, rows)
    return [base + 1 if row < extra else base for row in range(rows)]


def _tiles(count):
    counts = _layout(count)
    width, height = TILE
    canvas = max(counts) * width
    specs = []
    index = 0
    for row, per_row in enumerate(counts):
        each = int((canvas / per_row) // 2 * 2)
        widths = [each] * (per_row - 1) + [canvas - each * (per_row - 1)]
        x = 0
        for column in range(per_row):
            tile_w = widths[column] - (widths[column] % 2)
            specs.append((index, x, row * height, tile_w, height))
            x += widths[column]
            index += 1
    return specs


def build_mosaic(clips, audio_indices, out_path, cancel):
    parts = []
    if len(clips) == 1:
        width, height = TILE
        parts.append('[0:v]scale=%d:%d,setsar=1[vout]' % (width, height))
    else:
        specs = _tiles(len(clips))
        for index, _x, _y, width, height in specs:
            parts.append('[%d:v]scale=%d:%d,setsar=1[v%d]' % (index, width, height, index))
        vins = ''.join('[v%d]' % index for index, _x, _y, _w, _h in specs)
        layout = '|'.join('%d_%d' % (x, y) for _i, x, y, _w, _h in specs)
        parts.append('%sxstack=inputs=%d:layout=%s[vout]' % (vins, len(specs), layout))

    maps = ['-map', '[vout]']
    audio_args = ['-an']
    if audio_indices:
        if len(audio_indices) == 1:
            parts.append('[%d:a]anull[aout]' % audio_indices[0])
        else:
            ains = ''.join('[%d:a]' % i for i in audio_indices)
            parts.append('%samix=inputs=%d:duration=longest:normalize=0[aout]'
                         % (ains, len(audio_indices)))
        maps += ['-map', '[aout]']
        audio_args = ['-c:a', 'aac', '-b:a', '96k']

    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error']
    for clip in clips:
        cmd += ['-i', clip]
    cmd += ['-filter_complex', ';'.join(parts)] + maps + [
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
        '-movflags', '+faststart'] + audio_args + [out_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, creationflags=runtime.NO_WINDOW)
    _out, err = proc.communicate()
    if cancel():
        raise RuntimeError('cancelled')
    if proc.returncode != 0:
        raise RuntimeError('mosaic failed: %s' % (err.decode('utf-8', 'replace')[-200:]))
    return out_path


class ExportJob(QThread):
    progress = Signal(str)
    fraction = Signal(float, float)
    done = Signal(bool, str)

    def __init__(self, dvr, cameras, begin, finish, mode, audio_cameras, out_dir, aspect_args):
        super().__init__()
        self.dvr = dvr
        self.cameras = cameras
        self.begin = begin
        self.finish = finish
        self.mode = mode
        self.audio_cameras = set(audio_cameras)
        self.out_dir = out_dir
        self.aspect_args = aspect_args
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _cancelled(self):
        return self._cancel

    def run(self):
        tmp = tempfile.mkdtemp(prefix='cctv-exp-')
        made = []
        span = max(1.0, (self.finish - self.begin).total_seconds())
        total = span * len(self.cameras)
        started = time.monotonic()

        def report(done):
            share = min(1.0, done / total)
            spent = time.monotonic() - started
            remaining = spent / share - spent if share > 0.02 else -1.0
            self.fraction.emit(share, remaining)

        try:
            clips = []
            for index, channel in enumerate(self.cameras):
                if self._cancel:
                    raise RuntimeError('cancelled')
                self.progress.emit('fetching camera %d (%d/%d)'
                                   % (channel, index + 1, len(self.cameras)))
                found = _segment_for(self.dvr, channel, self.begin)
                if found is None:
                    raise RuntimeError('camera %d has no footage at that time' % channel)
                seg_start, _name = found
                tmp_clip = os.path.join(tmp, 'ch%d.mp4' % channel)
                with_audio = channel in self.audio_cameras
                fast_clip(self.dvr, channel, seg_start, self.begin, self.finish,
                          tmp_clip, with_audio, self.aspect_args, self._cancelled,
                          lambda seconds, base=index * span: report(base + min(seconds, span)))
                clips.append((channel, tmp_clip, with_audio))

            stamp = self.begin.strftime('%Y%m%d-%H%M%S')
            until = self.finish.strftime('%H%M%S')

            if self.mode in ('separate', 'both'):
                for channel, tmp_clip, _audio in clips:
                    final = os.path.join(self.out_dir, 'clip-ch%d-%s-%s.mp4'
                                         % (channel, stamp, until))
                    shutil.copyfile(tmp_clip, final)
                    made.append(final)

            if self.mode in ('mosaic', 'both'):
                self.progress.emit('building mosaic')
                audio_idx = [i for i, (ch, _p, _a) in enumerate(clips)
                             if ch in self.audio_cameras]
                channels = '-'.join(str(ch) for ch, _p, _a in clips)
                final = os.path.join(self.out_dir, 'mosaic-ch%s-%s-%s.mp4'
                                     % (channels, stamp, until))
                build_mosaic([p for _c, p, _a in clips], audio_idx, final, self._cancelled)
                made.append(final)

            self.done.emit(True, made[0] if made else self.out_dir)
        except Exception as exc:
            for path in made:
                try:
                    os.remove(path)
                except OSError:
                    pass
            ok = str(exc) == 'cancelled'
            self.done.emit(False, 'cancelled' if ok else str(exc))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
