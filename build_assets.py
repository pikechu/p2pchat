"""仅裁剪本项目未使用的 Qt 资源，保留界面和媒体功能的运行依赖。"""

from pathlib import PurePosixPath


def _name(path: str) -> str:
    return PurePosixPath(str(path).replace("\\", "/")).name.lower()


def filter_qt_binaries(binaries: list) -> list:
    """普通 Widgets 界面不使用软件 OpenGL、PDF 图片解码或 TUIO 输入。"""
    unused = {
        "opengl32sw.dll",
        "qpdf.dll", "libqpdf.so", "libqpdf.dylib",
        "qtuiotouchplugin.dll", "libqtuiotouchplugin.so", "libqtuiotouchplugin.dylib",
    }
    return [entry for entry in binaries if _name(entry[0]) not in unused]


def filter_qt_datas(datas: list) -> list:
    """保留中英文翻译；非翻译资源保持原样。"""
    locales = ("_zh_cn.qm", "_zh_tw.qm", "_en.qm", "_en_us.qm", "_en_gb.qm")
    return [
        entry for entry in datas
        if not _name(entry[0]).endswith(".qm") or _name(entry[0]).endswith(locales)
    ]
