"""弹出菜单定位工具。"""

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QMenu, QWidget


def popup_above_global_pos(menu: QMenu, global_pos: QPoint):
    """以全局坐标为锚点，把菜单向上弹出。"""
    anchor = global_pos - QPoint(0, menu.sizeHint().height())
    return menu.exec(anchor)


def popup_above_widget(menu: QMenu, widget: QWidget):
    """把菜单贴着控件上边缘向上弹出。"""
    menu_size = menu.sizeHint()
    anchor = widget.rect().topLeft() - QPoint(0, menu_size.height())
    return menu.exec(widget.mapToGlobal(anchor))
