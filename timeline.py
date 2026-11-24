from datetime import datetime, timedelta

from PySide6.QtCore import QPoint, QRectF, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QLabel, QToolTip, QWidget

import theme

DAY = 86400.0
MIN_SPAN = 30.0

BACKGROUND = QColor(theme.SURFACE)
TRACK = QColor(theme.RAISED)
SEGMENT = QColor(theme.FOOTAGE)
SEGMENT_EDGE = QColor(theme.FOOTAGE_EDGE)
SELECTION = QColor(235, 232, 225, 46)
SELECTION_EDGE = QColor(theme.TEXT_2)
PLAYHEAD = QColor(theme.TEXT)
TICK = QColor(theme.LINE)
LABEL = QColor(theme.TEXT_3)
MARKER = QColor(theme.PERSON)
MARKER_BAND = QColor(227, 165, 82, 70)
OVERVIEW = QColor(theme.RAISED)
OVERVIEW_SEGMENT = QColor('#2c5a40')
OVERVIEW_VIEW = QColor(235, 232, 225, 40)
EDGE_GRAB = 6
GLYPH = 14
BASE_HEIGHT = 104
LANE_HEIGHT = 16
BOOKMARK = QColor(theme.TEXT)
LANE_LABEL_WIDTH = 64

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
    lane_clicked = Signal(int, float)
    bookmark_menu = Signal(int, object)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(BASE_HEIGHT)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.segments = []
        self.markers = []
        self.marker_info = []
        self.bookmarks = []
        self.lanes = []
        self.current_lane = None
        self.position = None
        self.selection = None
        self.view_start = 0.0
        self.view_end = DAY
        self._hover_x = None
        self._scrubbing = False
        self._selecting = False
        self._sel_anchor = None
        self._panning = False
        self._pan_origin = None
        self._edge = None
        self._preview = None
        self._preview_path = None
        self._pixmaps = {}

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

    def set_markers(self, spans, info=None):
        self.markers = [(float(a), float(b)) for a, b in spans]
        self.marker_info = list(info) if info else [(None, None)] * len(self.markers)
        self.update()

    def set_bookmarks(self, bookmarks):
        self.bookmarks = [(mark_id, float(seconds), note) for mark_id, seconds, note in bookmarks]
        self.update()

    def set_lanes(self, lanes, current=None):
        self.lanes = list(lanes)
        self.current_lane = current
        self.setMinimumHeight(BASE_HEIGHT + self._lanes_height())
        self.updateGeometry()
        self.update()

    def _lanes_height(self):
        return len(self.lanes) * LANE_HEIGHT + 8 if self.lanes else 0

    def set_position(self, seconds):
        if self._scrubbing:
            return
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
        return QRectF(8, 8, max(1.0, self.width() - 16),
                      max(20.0, self.height() - 52 - self._lanes_height()))

    def _lane_rect(self, index):
        track = self._track()
        top = self._overview().bottom() + 8 + index * LANE_HEIGHT
        return QRectF(track.left(), top, track.width(), LANE_HEIGHT - 3)

    def _lane_at(self, y):
        for index, lane in enumerate(self.lanes):
            rect = self._lane_rect(index)
            if rect.top() - 1 <= y <= rect.bottom() + 2:
                return lane
        return None

    def _bookmark_at(self, x):
        best, gap = None, EDGE_GRAB + 1
        for mark in self.bookmarks:
            distance = abs(self._to_x(mark[1]) - x)
            if distance < gap:
                best, gap = mark, distance
        return best

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

    def _snap(self, seconds):
        track = self._track()
        gap = self.span() / max(1.0, track.width()) * 7
        best = seconds
        candidates = [edge for start, end in self.segments for edge in (start, end)]
        candidates += [start for start, _end in self.markers]
        candidates += [seconds for _id, seconds, _note in self.bookmarks]
        for edge in candidates:
            distance = abs(edge - seconds)
            if distance < gap:
                gap = distance
                best = edge
        return best

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
            painter.fillRect(QRectF(left, track.top(), width, 3.0), MARKER)

        painter.setRenderHint(QPainter.Antialiasing)
        last_glyph = None
        for (start, _end), (target, _snap) in zip(self.markers, self.marker_info):
            x = self._to_x(start)
            if x < track.left() - GLYPH or x > track.right() + GLYPH:
                continue
            if last_glyph is not None and x - last_glyph < GLYPH + 3:
                continue
            last_glyph = x
            theme.draw_kind(painter, QRectF(x - GLYPH / 2 + 1, track.top() + 6, GLYPH, GLYPH),
                            target)
        painter.setRenderHint(QPainter.Antialiasing, False)

        painter.setRenderHint(QPainter.Antialiasing)
        for _id, seconds, _note in self.bookmarks:
            x = self._to_x(seconds)
            if x < track.left() - 6 or x > track.right() + 6:
                continue
            painter.setPen(QPen(BOOKMARK, 1))
            painter.drawLine(QPointF(x, track.top() + 2), QPointF(x, track.bottom()))
            painter.setPen(Qt.NoPen)
            painter.setBrush(BOOKMARK)
            painter.drawPolygon(QPolygonF([
                QPointF(x, track.bottom() - 11), QPointF(x + 8, track.bottom() - 7.5),
                QPointF(x, track.bottom() - 4)]))
        painter.setRenderHint(QPainter.Antialiasing, False)

        if self.selection:
            left = self._to_x(self.selection[0])
            right = self._to_x(self.selection[1])
            rect = QRectF(left, track.top(), max(1.0, right - left), track.height())
            painter.fillRect(rect, SELECTION)
            painter.setPen(QPen(SELECTION_EDGE, 1))
            painter.drawRect(rect)

        if self.position is not None:
            x = self._to_x(self.position)
            painter.setPen(QPen(PLAYHEAD, 2))
            painter.drawLine(int(x), int(track.top()), int(x), int(track.bottom()))
            painter.setPen(Qt.NoPen)
            painter.setBrush(PLAYHEAD)
            painter.drawPolygon(QPolygonF([
                QPointF(x - 6, track.top() - 1), QPointF(x + 6, track.top() - 1),
                QPointF(x, track.top() + 8)]))
        painter.setClipping(False)

        step = self._tick_step()
        first = int(self.view_start // step) * step
        font = painter.font()
        font.setPointSizeF(8)
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

        if self.lanes:
            font.setPointSizeF(8)
            painter.setFont(font)
        for index, (channel, label, spans) in enumerate(self.lanes):
            rect = self._lane_rect(index)
            painter.fillRect(rect, TRACK if channel == self.current_lane else OVERVIEW)
            painter.setClipRect(rect)
            for start, end, target in spans:
                left = self._to_x(start)
                if left < rect.left() - 4 or left > rect.right() + 4:
                    continue
                width = max(3.0, self._to_x(end) - left)
                painter.fillRect(QRectF(left, rect.top() + 2, width, rect.height() - 4),
                                 theme.kind_color(target))
            if self.position is not None and channel == self.current_lane:
                x = self._to_x(self.position)
                painter.setPen(QPen(PLAYHEAD, 1))
                painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))
            painter.setClipping(False)
            painter.setPen(QPen(PLAYHEAD if channel == self.current_lane else LABEL, 1))
            painter.drawText(QRectF(rect.left() + 4, rect.top(), LANE_LABEL_WIDTH, rect.height()),
                             Qt.AlignLeft | Qt.AlignVCenter, label)

        cursor_x = self._hover_x
        if self._scrubbing and self.position is not None:
            cursor_x = self._to_x(self.position)
        if cursor_x is not None and track.left() - 1 <= cursor_x <= track.right() + 1:
            seconds = self.position if self._scrubbing else self._to_seconds(cursor_x)
            font.setPointSize(8)
            painter.setFont(font)
            width, height = 60.0, 17.0
            box = QRectF(min(max(cursor_x - width / 2, track.left()), track.right() - width),
                         track.top() + 2, width, height)
            painter.setBrush(QColor(12, 12, 16, 235))
            painter.setPen(QPen(SELECTION_EDGE, 1))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QPen(PLAYHEAD, 1))
            painter.drawText(box, Qt.AlignCenter, clock(int(seconds), True))

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
        x = event.position().x()
        if event.button() == Qt.RightButton:
            mark = self._bookmark_at(x)
            if mark is not None:
                self.bookmark_menu.emit(mark[0], event.globalPosition().toPoint())
            return
        if event.button() != Qt.LeftButton:
            return
        lane = self._lane_at(event.position().y()) if self.lanes else None
        if lane is not None and event.position().y() > self._overview().bottom():
            self.lane_clicked.emit(lane[0], self._snap_lane(lane, self._to_seconds(x)))
            return
        self._hover_x = x
        self._hide_preview()
        edge = self._edge_at(x)
        if edge is not None:
            self._edge = edge
            return
        if event.modifiers() & Qt.ShiftModifier:
            self._selecting = True
            self._sel_anchor = x
            self.selection = None
        else:
            self._scrubbing = True
            self.position = self._to_seconds(x)
        self.update()

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if self._panning and self._pan_origin is not None:
            track = self._track()
            moved = (x - self._pan_origin[0]) / max(1.0, track.width())
            start = self._pan_origin[1] - moved * self.span()
            self.set_view(start, start + self.span())
            return
        self._hover_x = x
        if self._edge is not None and self.selection:
            seconds = self._to_seconds(x)
            low, high = self.selection
            if self._edge == 'start':
                low = min(seconds, high - 1)
            else:
                high = max(seconds, low + 1)
            self.selection = (low, high)
            self.selection_changed.emit(self.selection)
            self.update()
            return
        self.setCursor(Qt.SizeHorCursor if self._edge_at(x) else Qt.ArrowCursor)
        self._show_preview(x)
        mark = self._bookmark_at(x)
        if mark is not None and mark[2] and event.position().y() <= self._track().bottom():
            QToolTip.showText(event.globalPosition().toPoint(), mark[2], self)
        if self._selecting and self._sel_anchor is not None:
            a = self._to_seconds(self._sel_anchor)
            b = self._to_seconds(x)
            self.selection = (min(a, b), max(a, b))
            self.selection_changed.emit(self.selection)
        elif self._scrubbing:
            self.position = self._to_seconds(x)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._panning:
            self._panning = False
            self._pan_origin = None
            return
        if event.button() != Qt.LeftButton:
            return
        x = event.position().x()
        if self._edge is not None:
            self._edge = None
            return
        if self._selecting:
            self._selecting = False
            self._sel_anchor = None
            if self.selection is None or self.selection[1] - self.selection[0] < 1:
                self.selection = None
                self.selection_changed.emit(None)
            self.update()
            return
        if self._scrubbing:
            self._scrubbing = False
            seconds = self._snap(self._to_seconds(x))
            self.position = seconds
            self.seek_requested.emit(seconds)
            self.update()

    def leaveEvent(self, event):
        self._hover_x = None
        self._hide_preview()
        self.update()

    def _snap_lane(self, lane, seconds):
        gap = self.span() / max(1.0, self._track().width()) * 8
        for start, _end, _target in lane[2]:
            if abs(start - seconds) < gap:
                return max(0.0, start - 3)
        return seconds

    def _edge_at(self, x):
        if not self.selection:
            return None
        if abs(x - self._to_x(self.selection[0])) <= EDGE_GRAB:
            return 'start'
        if abs(x - self._to_x(self.selection[1])) <= EDGE_GRAB:
            return 'end'
        return None

    def _marker_at(self, x):
        best, gap = None, EDGE_GRAB + 2
        for (start, end), (_target, snap) in zip(self.markers, self.marker_info):
            left, right = self._to_x(start), self._to_x(end)
            distance = 0 if left - 2 <= x <= right + 2 else min(abs(x - left), abs(x - right))
            if distance < gap and snap:
                best, gap = snap, distance
        return best

    def _show_preview(self, x):
        if self._scrubbing or self._selecting:
            self._hide_preview()
            return
        path = self._marker_at(x)
        if not path:
            self._hide_preview()
            return
        if self._preview is None:
            self._preview = QLabel(None, Qt.ToolTip | Qt.FramelessWindowHint)
            self._preview.setStyleSheet('border:1px solid %s; background:%s;'
                                        % (theme.LINE, theme.INK))
        if path != self._preview_path:
            pixmap = self._pixmaps.get(path)
            if pixmap is None:
                pixmap = QPixmap(path)
                if pixmap.isNull():
                    self._hide_preview()
                    return
                pixmap = pixmap.scaledToWidth(240, Qt.SmoothTransformation)
                if len(self._pixmaps) > 64:
                    self._pixmaps.clear()
                self._pixmaps[path] = pixmap
            self._preview.setPixmap(pixmap)
            self._preview.adjustSize()
            self._preview_path = path
        spot = self.mapToGlobal(QPoint(int(x), 0))
        self._preview.move(spot.x() - self._preview.width() // 2,
                           spot.y() - self._preview.height() - 8)
        self._preview.show()

    def _hide_preview(self):
        if self._preview is not None:
            self._preview.hide()
        self._preview_path = None

    def mouseDoubleClickEvent(self, event):
        self.zoom(0.4, self._to_seconds(event.position().x()))
