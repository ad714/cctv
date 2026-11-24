import subprocess

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QLabel, QLayout, QSizePolicy

import runtime
import theme


TILE_SIZE = (480, 360)
FOCUS_SIZE = (1024, 768)

NAV_PATHS = {
    'back': [[(19, 12), (5, 12)], [(12, 5), (5, 12), (12, 19)]],
    'prev': [[(15, 6), (9, 12), (15, 18)]],
    'next': [[(9, 6), (15, 12), (9, 18)]],
}


def nav_icon(kind, px=48, stroke=2.4, color='#ececf4'):
    scale = px / 24.0
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), stroke * scale)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    for poly in NAV_PATHS[kind]:
        painter.drawPolyline(QPolygonF([QPointF(x * scale, y * scale) for x, y in poly]))
    painter.end()
    return QIcon(pixmap)


class FlowLayout(QLayout):
    def __init__(self, spacing=6):
        super().__init__()
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect, apply):
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() and line_height > 0:
                x = rect.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class CallWorker(QThread):
    done = Signal(object, object)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.done.emit(self.func(*self.args, **self.kwargs), None)
        except Exception as exc:
            runtime.log.warning('%s failed: %s', getattr(self.func, '__name__', '?'), exc)
            self.done.emit(None, exc)


class AudioPlayer:
    def __init__(self):
        self.proc = None
        self.slot = None

    def play(self, slot, url):
        self.stop()
        self.proc = runtime.spawn(
            ['ffplay', '-nodisp', '-loglevel', 'error', '-vn',
             '-rtsp_transport', 'tcp', '-timeout', str(runtime.SOCKET_TIMEOUT_US),
             '-fflags', 'nobuffer', '-i', url],
            stdout=subprocess.DEVNULL)
        self.slot = slot

    def play_local(self, path, offset, speed=1.0):
        self.stop()
        tempo = [] if speed == 1.0 else ['-af', runtime.atempo_chain(speed)]
        self.proc = runtime.spawn(
            ['ffplay', '-autoexit', '-nodisp', '-loglevel', 'error', '-vn',
             '-follow', '1', '-rw_timeout', '4000000',
             '-ss', '%.3f' % offset] + tempo + ['-i', path],
            stdout=subprocess.DEVNULL)
        self.slot = 'local'

    def stop(self):
        runtime.stop_process(self.proc)
        self.proc = None
        self.slot = None


class VideoLabel(QLabel):
    clicked = Signal()

    def __init__(self, title):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.setStyleSheet('background:%s; color:%s; border:0; border-radius:6px;'
                           % (theme.INK, theme.TEXT_3))
        self.setText('%s\nconnecting' % title)
        self.frame = None
        self.overlay_text = ''
        self.zoom = 1.0
        self.zoom_cx = 0.5
        self.zoom_cy = 0.5

    def set_frame(self, image):
        self.frame = image
        self.update()

    def reset_zoom(self):
        if self.zoom != 1.0:
            self.zoom = 1.0
            self.zoom_cx = 0.5
            self.zoom_cy = 0.5
            self.update()

    def _source_rect(self):
        w = self.frame.width() / self.zoom
        h = self.frame.height() / self.zoom
        x = min(max(self.zoom_cx * self.frame.width() - w / 2, 0), self.frame.width() - w)
        y = min(max(self.zoom_cy * self.frame.height() - h / 2, 0), self.frame.height() - h)
        return QRectF(x, y, w, h)

    def paintEvent(self, event):
        if self.frame is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.INK))
        area = self.rect()
        source = self._source_rect()
        size = source.size().toSize().scaled(area.size(), Qt.KeepAspectRatio)
        painter.setRenderHint(QPainter.SmoothPixmapTransform,
                              size.width() * 1.15 < source.width())
        target = QRectF(area.x() + (area.width() - size.width()) / 2,
                        area.y() + (area.height() - size.height()) / 2,
                        size.width(), size.height())
        painter.drawImage(target, self.frame, source)
        if self.overlay_text:
            badge = QRectF(area.x() + 10, area.y() + 10,
                           painter.fontMetrics().horizontalAdvance(self.overlay_text) + 20, 26)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(12, 12, 11, 200))
            painter.setRenderHint(QPainter.Antialiasing)
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(badge, Qt.AlignCenter, self.overlay_text)

    def mousePressEvent(self, event):
        self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        if self.frame is None:
            return
        if self.zoom > 1.0:
            self.reset_zoom()
            return
        area = self.rect().adjusted(1, 1, -1, -1)
        size = self.frame.size().scaled(area.size(), Qt.KeepAspectRatio)
        if size.width() > 0 and size.height() > 0:
            tx = area.x() + (area.width() - size.width()) / 2
            ty = area.y() + (area.height() - size.height()) / 2
            pos = event.position()
            self.zoom_cx = min(max((pos.x() - tx) / size.width(), 0.0), 1.0)
            self.zoom_cy = min(max((pos.y() - ty) / size.height(), 0.0), 1.0)
        self.zoom = 2.0
        self.update()
