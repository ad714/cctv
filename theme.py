from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap, QPolygonF

INK = '#0c0c0b'
SURFACE = '#141412'
RAISED = '#1d1c1a'
HOVER = '#272623'
LINE = '#2e2d29'
TEXT = '#ebe8e1'
TEXT_2 = '#aaa69c'
TEXT_3 = '#87847b'
FOOTAGE = '#3d7a57'
FOOTAGE_EDGE = '#4b8f68'
ON = '#1e3a2a'
ON_TEXT = '#a6dcbb'
ON_EDGE = '#33604a'
PERSON = '#e3a552'
VEHICLE = '#86aac0'
DANGER = '#dd6a54'

FAMILY = ['Segoe UI Variable Text', 'Segoe UI']


def app_font(point=10.0, weight=QFont.Normal, tabular=False):
    font = QFont()
    font.setFamilies(FAMILY)
    font.setPointSizeF(point)
    font.setWeight(weight)
    if tabular:
        font.setFeature(QFont.Tag('tnum'), 1)
    return font


def kind_of(target):
    tag = target or ''
    if 'human' in tag:
        return 'person'
    if 'vehicle' in tag:
        return 'vehicle'
    return 'motion'


def kind_label(target):
    tag = target or ''
    if 'human' in tag and 'vehicle' in tag:
        return 'Person and vehicle'
    return {'person': 'Person', 'vehicle': 'Vehicle'}.get(kind_of(target), 'Motion')


def kind_color(target):
    return QColor({'person': PERSON, 'vehicle': VEHICLE}.get(kind_of(target), TEXT_2))


def draw_person(painter, rect, color):
    unit = min(rect.width(), rect.height())
    cx = rect.center().x()
    top = rect.center().y() - unit / 2
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(cx, top + unit * 0.25), unit * 0.19, unit * 0.19)
    body = QPainterPath()
    body.moveTo(cx - unit * 0.36, top + unit * 0.98)
    body.cubicTo(cx - unit * 0.36, top + unit * 0.56, cx - unit * 0.2, top + unit * 0.5,
                 cx, top + unit * 0.5)
    body.cubicTo(cx + unit * 0.2, top + unit * 0.5, cx + unit * 0.36, top + unit * 0.56,
                 cx + unit * 0.36, top + unit * 0.98)
    body.closeSubpath()
    painter.drawPath(body)
    painter.restore()


def draw_vehicle(painter, rect, color):
    unit = min(rect.width(), rect.height())
    left = rect.center().x() - unit / 2
    top = rect.center().y() - unit / 2
    painter.save()
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    body = QPainterPath()
    body.moveTo(left + unit * 0.04, top + unit * 0.74)
    body.lineTo(left + unit * 0.04, top + unit * 0.52)
    body.lineTo(left + unit * 0.2, top + unit * 0.47)
    body.lineTo(left + unit * 0.32, top + unit * 0.27)
    body.lineTo(left + unit * 0.7, top + unit * 0.27)
    body.lineTo(left + unit * 0.84, top + unit * 0.47)
    body.lineTo(left + unit * 0.96, top + unit * 0.52)
    body.lineTo(left + unit * 0.96, top + unit * 0.74)
    body.closeSubpath()
    painter.drawPath(body)
    painter.setBrush(QColor(INK))
    for x in (0.27, 0.73):
        painter.drawEllipse(QPointF(left + unit * x, top + unit * 0.76), unit * 0.13, unit * 0.13)
    painter.setBrush(color)
    for x in (0.27, 0.73):
        painter.drawEllipse(QPointF(left + unit * x, top + unit * 0.76), unit * 0.08, unit * 0.08)
    painter.restore()


def draw_kind(painter, rect, target):
    color = kind_color(target)
    if kind_of(target) == 'vehicle':
        draw_vehicle(painter, rect, color)
    else:
        draw_person(painter, rect, color)


def kind_icon(target, px=32):
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    draw_kind(painter, QRectF(2, 2, px - 4, px - 4), target)
    painter.end()
    return QIcon(pixmap)


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor('#1a1917'))
    painter.drawRoundedRect(1, 1, 62, 62, 13, 13)
    painter.setBrush(QColor(FOOTAGE))
    painter.drawRoundedRect(10, 23, 30, 21, 5, 5)
    painter.drawPolygon(QPolygonF([QPointF(42, 30), QPointF(55, 23), QPointF(55, 44),
                                   QPointF(42, 37)]))
    painter.setBrush(QColor(PERSON))
    painter.drawEllipse(QPointF(20, 33), 5, 5)
    painter.end()
    return QIcon(pixmap)


STYLE = """
QWidget {{ color:{TEXT}; }}
QMainWindow, QDialog {{ background:{INK}; }}
QToolTip {{ background:{RAISED}; color:{TEXT}; border:1px solid {LINE}; padding:4px 6px; }}
QLabel {{ background:transparent; }}
QLabel[role="muted"] {{ color:{TEXT_2}; }}
QLabel[role="faint"] {{ color:{TEXT_3}; }}
QLabel[role="title"] {{ font-size:15pt; font-weight:600; }}
QLabel[role="heading"] {{ font-size:11pt; font-weight:600; }}
QLabel[role="badge"] {{ color:{PERSON}; font-weight:600; }}

QPushButton {{ background:{RAISED}; color:{TEXT}; border:1px solid {LINE}; border-radius:5px;
               padding:5px 11px; }}
QPushButton:hover {{ background:{HOVER}; }}
QPushButton:pressed {{ background:{LINE}; }}
QPushButton:focus {{ border-color:{TEXT_3}; }}
QPushButton:checked {{ background:{ON}; color:{ON_TEXT}; border-color:{ON_EDGE}; }}
QPushButton:disabled {{ color:{TEXT_3}; background:{SURFACE}; border-color:{SURFACE}; }}
QPushButton[role="primary"] {{ background:{FOOTAGE}; color:#f4fbf6; border-color:{FOOTAGE_EDGE};
                               font-weight:600; }}
QPushButton[role="primary"]:hover {{ background:#356b4c; }}
QPushButton[role="quiet"] {{ background:transparent; border-color:transparent; color:{TEXT_2}; }}
QPushButton[role="quiet"]:hover {{ color:{TEXT}; background:{RAISED}; }}
QPushButton[role="quiet"]:checked {{ color:{ON_TEXT}; background:{ON}; border-color:{ON_EDGE}; }}
QPushButton[role="danger"] {{ color:{DANGER}; }}

QComboBox, QDateEdit, QTimeEdit, QSpinBox, QLineEdit {{
    background:{RAISED}; color:{TEXT}; border:1px solid {LINE}; border-radius:5px;
    padding:4px 8px; min-height:18px; }}
QComboBox:hover, QDateEdit:hover, QTimeEdit:hover, QSpinBox:hover {{ background:{HOVER}; }}
QComboBox:focus, QDateEdit:focus, QTimeEdit:focus, QSpinBox:focus, QLineEdit:focus {{
    border-color:{TEXT_3}; }}
QComboBox {{ combobox-popup:0; }}
QComboBox::drop-down, QDateEdit::drop-down {{ border:0; width:18px; }}
QComboBox QAbstractItemView {{ background:{RAISED}; color:{TEXT}; border:1px solid {LINE};
    outline:0; selection-background-color:{ON}; selection-color:{ON_TEXT}; }}
QComboBox QAbstractItemView::item {{ min-height:24px; padding:2px 10px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width:0; border:0; }}

QCalendarWidget QWidget {{ alternate-background-color:{RAISED}; }}
QCalendarWidget QAbstractItemView {{ background:{SURFACE}; color:{TEXT};
    selection-background-color:{ON}; selection-color:{ON_TEXT}; }}
QCalendarWidget QToolButton {{ background:transparent; color:{TEXT}; padding:4px; }}
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background:{RAISED}; }}

QTabWidget::pane {{ border:0; }}
QTabBar {{ background:{INK}; }}
QTabBar::tab {{ background:transparent; color:{TEXT_2}; padding:9px 18px 8px 18px;
                border:0; border-bottom:2px solid transparent; font-size:10.5pt; }}
QTabBar::tab:hover {{ color:{TEXT}; }}
QTabBar::tab:selected {{ color:{TEXT}; border-bottom:2px solid {TEXT}; font-weight:600; }}

QListWidget, QListView {{ background:{SURFACE}; border:0; outline:0; }}
QListWidget::item:selected {{ background:{RAISED}; }}

QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
QScrollBar::handle:vertical {{ background:{LINE}; border-radius:3px; min-height:30px; }}
QScrollBar::handle:vertical:hover {{ background:{TEXT_3}; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
QScrollBar::handle:horizontal {{ background:{LINE}; border-radius:3px; min-width:30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}

QCheckBox, QRadioButton {{ spacing:7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width:15px; height:15px;
    border:1px solid {TEXT_3}; background:{RAISED}; }}
QCheckBox::indicator {{ border-radius:3px; }}
QRadioButton::indicator {{ border-radius:8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background:{FOOTAGE_EDGE}; border-color:{FOOTAGE_EDGE}; }}
QCheckBox:disabled {{ color:{TEXT_3}; }}

QGroupBox {{ border:1px solid {LINE}; border-radius:8px; margin-top:14px;
             padding:12px 10px 10px 10px; font-weight:600; }}
QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 4px; color:{TEXT_2}; }}

QProgressBar {{ background:{RAISED}; border:0; border-radius:3px; height:6px; text-align:center;
                color:transparent; max-height:6px; }}
QProgressBar::chunk {{ background:{FOOTAGE_EDGE}; border-radius:3px; }}

QStatusBar {{ background:{INK}; color:{TEXT_3}; }}
QMenu {{ background:{RAISED}; border:1px solid {LINE}; padding:4px; }}
QMenu::item {{ padding:6px 18px; border-radius:4px; }}
QMenu::item:selected {{ background:{HOVER}; }}
QSplitter::handle {{ background:{INK}; }}
QScrollArea {{ background:{INK}; border:0; }}
QWidget#page {{ background:{INK}; }}
QFrame[role="panel"] {{ background:{SURFACE}; border-radius:8px; }}
""".format(**globals())


def apply(app):
    app.setFont(app_font())
    app.setStyleSheet(STYLE)


def set_role(widget, role):
    widget.setProperty('role', role)
    return widget
