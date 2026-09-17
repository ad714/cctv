from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import runtime

DISPLAY_ASPECT = 4.0 / 3.0

VERTEX = """
attribute vec2 position;
attribute vec2 uv;
varying vec2 coord;
void main() {
    coord = uv;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

FRAGMENT = """
varying vec2 coord;
uniform sampler2D plane_y;
uniform sampler2D plane_u;
uniform sampler2D plane_v;
void main() {
    float y = texture2D(plane_y, coord).r;
    float u = texture2D(plane_u, coord).r - 0.5;
    float v = texture2D(plane_v, coord).r - 0.5;
    y = (y - 0.0625) * 1.164383;
    gl_FragColor = vec4(y + 1.596027 * v,
                        y - 0.391762 * u - 0.812968 * v,
                        y + 2.017232 * u,
                        1.0);
}
"""

QUAD_UV_FLAT = [0.0, 1.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]


class GLVideoWidget(QOpenGLWidget):
    clicked = Signal()

    def __init__(self, title, aspect=DISPLAY_ASPECT):
        super().__init__()
        self.title = title
        self.aspect = aspect
        self.placeholder = '%s\nconnecting' % title
        self.frame = None
        self.frame_width = 0
        self.frame_height = 0
        self.dirty = False
        self.program = None
        self.textures = []
        self.failed = False

    def set_frame(self, data, width, height):
        self.frame = data
        self.frame_width = width
        self.frame_height = height
        self.dirty = True
        self.update()

    def set_placeholder(self, text):
        self.placeholder = text
        if self.frame is None:
            self.update()

    def initializeGL(self):
        self.program = QOpenGLShaderProgram(self)
        ok = (self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
              and self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT)
              and self.program.link())
        if not ok:
            self.failed = True
            runtime.log.error('video shader failed to build: %s',
                              self.program.log().strip() or 'no driver message')
            self.program = None
        else:
            runtime.log.debug('video shader ready for %s', self.title)

    def _ensure_textures(self):
        sizes = [(self.frame_width, self.frame_height),
                 (self.frame_width // 2, self.frame_height // 2),
                 (self.frame_width // 2, self.frame_height // 2)]
        if len(self.textures) == 3 and self.textures[0].width() == sizes[0][0] \
                and self.textures[0].height() == sizes[0][1]:
            return sizes
        for texture in self.textures:
            texture.destroy()
        self.textures = []
        for width, height in sizes:
            texture = QOpenGLTexture(QOpenGLTexture.Target2D)
            texture.setFormat(QOpenGLTexture.R8_UNorm)
            texture.setSize(width, height)
            texture.setMinificationFilter(QOpenGLTexture.Linear)
            texture.setMagnificationFilter(QOpenGLTexture.Linear)
            texture.setWrapMode(QOpenGLTexture.ClampToEdge)
            texture.allocateStorage(QOpenGLTexture.Red, QOpenGLTexture.UInt8)
            self.textures.append(texture)
        return sizes

    def _upload(self, sizes):
        view = memoryview(self.frame)
        offset = 0
        for index, (width, height) in enumerate(sizes):
            count = width * height
            self.textures[index].setData(
                QOpenGLTexture.Red, QOpenGLTexture.UInt8, view[offset:offset + count])
            offset += count

    def _quad(self):
        width, height = self.width(), self.height()
        if width <= 0 or height <= 0:
            return None
        target = self.aspect
        if width / height > target:
            x = (target * height) / width
            y = 1.0
        else:
            x = 1.0
            y = (width / target) / height
        return [-x, -y, x, -y, -x, y, x, y]

    def paintGL(self):
        functions = self.context().functions()
        functions.glClearColor(0.063, 0.063, 0.078, 1.0)
        functions.glClear(0x00004000)

        if self.frame is None or self.program is None or self.failed:
            self._paint_placeholder()
            return
        if self.frame_width <= 0 or self.frame_height <= 0:
            return

        sizes = self._ensure_textures()
        if self.dirty:
            functions.glPixelStorei(0x0CF5, 1)
            self._upload(sizes)
            self.dirty = False

        corners = self._quad()
        if corners is None:
            return

        self.program.bind()
        for index, name in enumerate(('plane_y', 'plane_u', 'plane_v')):
            functions.glActiveTexture(0x84C0 + index)
            self.textures[index].bind()
            self.program.setUniformValue1i(self.program.uniformLocation(name), index)
        functions.glActiveTexture(0x84C0)

        position = self.program.attributeLocation('position')
        uv = self.program.attributeLocation('uv')
        self.program.enableAttributeArray(position)
        self.program.enableAttributeArray(uv)
        self.program.setAttributeArray(position, corners, 2)
        self.program.setAttributeArray(uv, QUAD_UV_FLAT, 2)
        functions.glDrawArrays(0x0005, 0, 4)
        self.program.disableAttributeArray(position)
        self.program.disableAttributeArray(uv)
        self.program.release()

    def _paint_placeholder(self):
        painter = QPainter(self)
        painter.setPen(QColor('#6f6f80'))
        painter.drawText(self.rect(), Qt.AlignCenter, self.placeholder)
        painter.end()

    def mousePressEvent(self, event):
        self.clicked.emit()
