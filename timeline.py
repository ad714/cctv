from datetime import datetime, timedelta

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

DAY = 86400.0
MIN_SPAN = 30.0

BACKGROUND = QColor('#141419')
TRACK = QColor('#1d1d24')
SEGMENT = QColor('#2f6f4a')
SEGMENT_EDGE = QColor('#3f8f60')
SELECTION = QColor(90, 150, 230, 90)
SELECTION_EDGE = QColor('#6ea8e6')
PLAYHEAD = QColor('#f0f0f4')
TICK = QColor('#3a3a46')
LABEL = QColor('#7a7a8a')
MARKER = QColor('#ffb347')
MARKER_BAND = QColor(255, 179, 71, 110)
OVERVIEW = QColor('#232330')
OVERVIEW_SEGMENT = QColor('#2a5540')
OVERVIEW_VIEW = QColor(110, 168, 230, 80)

STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600]


def seconds_of(moment):
    return moment.hour * 3600 + moment.minute * 60 + moment.second


def clock(seconds, with_seconds=True):
    seconds = int(max(0, min(DAY - 1, seconds)))
    if with_seconds:
        return '%02d:%02d:%02d' % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return '%02d:%02d' % (seconds // 3600, (seconds % 3600) // 60)


class TimelineBar(QWidget):
    seek_requested = Signal(float)
    selection_changed = Signal(object)
    view_changed = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(96)
        self.setFocusPolicy(Qt.StrongFocus)
        self.segments = []
        self.markers = []
        self.position = None
        self.selection = None
        self.view_start = 0.0
        self.view_end = DAY
        self._press_x = None
        self._dragging = False
        self._panning = False
        self._pan_origin = None

    def set_segments(self, segments, day):
        self.segments = []
        day_start = datetime.combine(day, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        for start, end in segments:
            start = max(start, day_start)
            end = min(end, day_end)
            if end > start:
                self.segments.append(((start - day_start).total_seconds(),
                                      (end - day_start).total_seconds()))
        self.update()

    def set_markers(self, spans):
        self.markers = [(float(a), float(b)) for a, b in spans]
        self.update()

    def set_position(self, seconds):
        self.position = seconds
        self.update()

    def clear_selection(self):
        self.selection = None
        self.selection_changed.emit(None)
        self.update()

    def span(self):
        return self.view_end - self.view_start

    def set_view(self, start, end):
        span = max(MIN_SPAN, min(DAY, end - start))
        start = max(0.0, min(DAY - span, start))
        self.view_start = start
        self.view_end = start + span
        self.view_changed.emit(self.view_start, self.view_end)
        self.update()

    def fit(self):
        self.set_view(0.0, DAY)

    def zoom(self, factor, anchor=None):
        if anchor is None:
            anchor = self.position if self.position is not None else (
                self.view_start + self.span() / 2)
        anchor = max(self.view_start, min(self.view_end, anchor))
        ratio = (anchor - self.view_start) / max(1e-6, self.span())
        span = max(MIN_SPAN, min(DAY, self.span() * factor))
        self.set_view(anchor - ratio * span, anchor - ratio * span + span)

    def centre_on(self, seconds):
        half = self.span() / 2
        self.set_view(seconds - half, seconds + half)

    def _track(self):
        return QRectF(8, 8, max(1.0, self.width() - 16), self.height() - 52)

    def _overview(self):
        track = self._track()
        return QRectF(track.left(), track.bottom() + 20, track.width(), 10)

    def _to_x(self, seconds):
        track = self._track()
        return track.left() + track.width() * ((seconds - self.view_start) / max(1e-6, self.span()))

    def _to_seconds(self, x):
        track = self._track()
        ratio = (x - track.left()) / max(1.0, track.width())
        return max(0.0, min(DAY, self.view_start + ratio * self.span()))

    def _tick_step(self):
        track = self._track()
        want = max(4, int(track.width() / 110))
        for step in STEPS:
            if self.span() / step <= want:
                return step
        return STEPS[-1]

    def paintEvent(self, event):
        painter = QPainter(self)
        track = self._track()
        painter.fillRect(self.rect(), BACKGROUND)
        painter.fillRect(track, TRACK)
        painter.setClipRect(track)

        for start, end in self.segments:
            left = self._to_x(start)
            right = self._to_x(end)
            if right < track.left() - 2 or left > track.right() + 2:
                continue
            rect = QRectF(left, track.top(), max(1.0, right - left), track.height())
            painter.fillRect(rect, SEGMENT)
            painter.setPen(QPen(SEGMENT_EDGE, 1))
            painter.drawRect(rect)

        for start, end in self.markers:
            left = self._to_x(start)
            right = self._to_x(end)
            if right < track.left() - 4 or left > track.right() + 4:
                continue
            width = max(3.0, right - left)
            painter.fillRect(QRectF(left, track.top(), width, track.height()), MARKER_BAND)
            painter.fillRect(QRectF(left, track.top(), width, 7.0), MARKER)

        if self.selection:
            left = self._to_x(self.selection[0])
            right = self._to_x(self.selection[1])
            rect = QRectF(left, track.top(), max(1.0, right - left), track.height())
            painter.fillRect(rect, SELECTION)
            painter.setPen(QPen(SELECTION_EDGE, 1))
            painter.drawRect(rect)

        if self.position is not None:
            x = self._to_x(self.position)
            painter.setPen(QPen(PLAYHEAD, 1))
            painter.drawLine(int(x), int(track.top()), int(x), int(track.bottom()))
        painter.setClipping(False)

        step = self._tick_step()
        first = int(self.view_start // step) * step
        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)
        mark = first
        while mark <= self.view_end + step:
            x = self._to_x(mark)
            if track.left() - 1 <= x <= track.right() + 1:
                painter.setPen(QPen(TICK, 1))
                painter.drawLine(int(x), int(track.bottom()), int(x), int(track.bottom() + 5))
                painter.setPen(QPen(LABEL, 1))
                painter.drawText(QRectF(x - 26, track.bottom() + 6, 52, 12),
                                 Qt.AlignCenter, clock(mark, step < 60))
            mark += step

        overview = self._overview()
        painter.fillRect(overview, OVERVIEW)
        for start, end in self.segments:
            left = overview.left() + overview.width() * (start / DAY)
            right = overview.left() + overview.width() * (end / DAY)
            painter.fillRect(QRectF(left, overview.top(), max(1.0, right - left),
                                    overview.height()), OVERVIEW_SEGMENT)
        for start, end in self.markers:
            left = overview.left() + overview.width() * (start / DAY)
            painter.fillRect(QRectF(left, overview.top(), 2.0, overview.height()), MARKER)
        left = overview.left() + overview.width() * (self.view_start / DAY)
        right = overview.left() + overview.width() * (self.view_end / DAY)
        window = QRectF(left, overview.top() - 1, max(2.0, right - left), overview.height() + 2)
        painter.fillRect(window, OVERVIEW_VIEW)
        painter.setPen(QPen(SELECTION_EDGE, 1))
        painter.drawRect(window)

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return
        anchor = self._to_seconds(event.position().x())
        if event.modifiers() & Qt.ShiftModifier:
            self.set_view(self.view_start - self.span() * 0.15 * (1 if delta > 0 else -1),
                          self.view_end - self.span() * 0.15 * (1 if delta > 0 else -1))
        else:
            self.zoom(0.75 if delta > 0 else 1 / 0.75, anchor)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton or (
                event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier):
            self._panning = True
            self._pan_origin = (event.position().x(), self.view_start)
            return
        if event.button() != Qt.LeftButton:
            return
        self._press_x = event.position().x()
        self._dragging = False

    def mouseMoveEvent(self, event):
        if self._panning and self._pan_origin is not None:
            track = self._track()
            moved = (event.position().x() - self._pan_origin[0]) / max(1.0, track.width())
            start = self._pan_origin[1] - moved * self.span()
            self.set_view(start, start + self.span())
            return
        if self._press_x is None:
            return
        if abs(event.position().x() - self._press_x) > 3:
            self._dragging = True
            a = self._to_seconds(self._press_x)
            b = self._to_seconds(event.position().x())
            self.selection = (min(a, b), max(a, b))
            self.selection_changed.emit(self.selection)
            self.update()

    def mouseReleaseEvent(self, event):
        if self._panning:
            self._panning = False
            self._pan_origin = None
            return
        if self._press_x is None:
            return
        if not self._dragging:
            self.selection = None
            self.selection_changed.emit(None)
            self.seek_requested.emit(self._to_seconds(event.position().x()))
        self._press_x = None
        self._dragging = False
        self.update()

    def mouseDoubleClickEvent(self, event):
        self.zoom(0.4, self._to_seconds(event.position().x()))
