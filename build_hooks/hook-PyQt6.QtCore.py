"""使用官方 Qt 依赖分析，裁剪可选插件与无用翻译。"""

from PyInstaller.utils.hooks.qt import add_qt6_dependencies
from build_assets import filter_qt_binaries, filter_qt_datas

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = filter_qt_binaries(binaries)
datas = filter_qt_datas(datas)
