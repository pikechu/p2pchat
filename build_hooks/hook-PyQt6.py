"""沿用 Qt 绑定收集规则，裁剪本项目未使用的额外渲染库。"""

from PyInstaller.utils.hooks.qt import pyqt6_library_info, ensure_single_qt_bindings_package
from build_assets import filter_qt_binaries

ensure_single_qt_bindings_package("PyQt6")
if pyqt6_library_info.version is not None:
    hiddenimports = ["PyQt6.sip", "pkgutil"]
    binaries = filter_qt_binaries(pyqt6_library_info.collect_extra_binaries())
