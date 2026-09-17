import atexit
import ctypes
import faulthandler
import logging
import logging.handlers
import os
import random
import subprocess
import sys
import threading

import config

WINDOWS = sys.platform == 'win32'
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WINDOWS else 0

SOCKET_TIMEOUT_US = 10000000
PROBE_SIZE = '100000'
ANALYZE_DURATION = '500000'

BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0

log = logging.getLogger('cctv')
_crash_file = None
_job_handle = None


def setup_logging(level=logging.INFO):
    global _crash_file
    if log.handlers:
        return log
    log.setLevel(level)
    path = config.data_path('cctv.log')
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s'))
    log.addHandler(handler)
    if sys.stderr is not None:
        log.addHandler(logging.StreamHandler(sys.stderr))

    _crash_file = open(config.data_path('crash.log'), 'ab', buffering=0)
    faulthandler.enable(file=_crash_file, all_threads=True)

    sys.excepthook = _on_exception
    threading.excepthook = _on_thread_exception
    log.info('--- started, pid %d, frozen=%s ---', os.getpid(),
             getattr(sys, 'frozen', False))
    return log


def _on_exception(kind, value, trace):
    log.critical('unhandled exception', exc_info=(kind, value, trace))


def _on_thread_exception(args):
    if args.exc_type is SystemExit:
        return
    log.critical('unhandled exception in thread %s',
                 getattr(args.thread, 'name', '?'),
                 exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def install_qt_message_handler():
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    levels = {QtMsgType.QtDebugMsg: logging.DEBUG,
              QtMsgType.QtInfoMsg: logging.INFO,
              QtMsgType.QtWarningMsg: logging.WARNING,
              QtMsgType.QtCriticalMsg: logging.ERROR,
              QtMsgType.QtFatalMsg: logging.CRITICAL}

    def handler(mode, context, message):
        logging.getLogger('qt').log(levels.get(mode, logging.INFO), '%s', message)

    qInstallMessageHandler(handler)


class _JobLimits(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_int64),
        ('PerJobUserTimeLimit', ctypes.c_int64),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _JobExtended(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _JobLimits),
        ('IoInfo', ctypes.c_uint64 * 6),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


def claim_job_object():
    global _job_handle
    if not WINDOWS:
        return False
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        log.warning('CreateJobObject failed (%d)', ctypes.get_last_error())
        return False
    info = _JobExtended()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
            ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
        log.warning('SetInformationJobObject failed (%d)', ctypes.get_last_error())
        return False
    if not kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(job), kernel32.GetCurrentProcess()):
        log.warning('AssignProcessToJobObject failed (%d) - children may outlive us',
                    ctypes.get_last_error())
        return False
    _job_handle = job
    atexit.register(lambda: None)
    log.info('job object active: children die with this process')
    return True


def rtsp_input(url, hardware=False, duration=None):
    args = ['-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp', '-timeout', str(SOCKET_TIMEOUT_US),
            '-fflags', 'nobuffer', '-flags', 'low_delay',
            '-probesize', PROBE_SIZE, '-analyzeduration', ANALYZE_DURATION,
            '-threads', '1']
    if hardware:
        args += ['-hwaccel', 'd3d11va', '-hwaccel_output_format', 'd3d11']
    args += ['-i', url]
    if duration:
        args += ['-t', str(int(duration))]
    return args


def spawn(args, stdout=None, stdin=None):
    return subprocess.Popen(
        args, stdout=stdout, stdin=stdin,
        stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


def stop_process(proc, graceful=False, timeout=5):
    if proc is None or proc.poll() is not None:
        return
    try:
        if graceful and proc.stdin is not None:
            try:
                proc.stdin.write(b'q')
                proc.stdin.flush()
                proc.wait(timeout=timeout)
                return
            except Exception:
                pass
        proc.kill()
        proc.wait(timeout=timeout)
    except Exception as exc:
        log.debug('stop_process: %s', exc)


def process_rss_mb():
    if not WINDOWS:
        return 0.0
    class Counters(ctypes.Structure):
        _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t)]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    if not psapi.GetProcessMemoryInfo(ctypes.c_void_p(kernel32.GetCurrentProcess()),
                                      ctypes.byref(counters), counters.cb):
        return 0.0
    return counters.WorkingSetSize / 1048576.0


def backoff_delay(attempt, cap=BACKOFF_CAP):
    ceiling = min(cap, BACKOFF_BASE * (2 ** min(attempt, 10)))
    return random.uniform(0.0, ceiling)
