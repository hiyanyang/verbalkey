# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

用目录模式（onedir）而不是单文件：单文件每次启动要解压 200MB+ 到临时目录，
托盘常驻程序每次开机都等好几秒不值得；而且模型和配置本来就放在外面。
"""

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# 这三个都带数据文件或原生库，自动分析经常漏
for pkg in ("opencc", "sounddevice", "sherpa_onnx"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

datas += [("assets/app.ico", "assets")]
# Rime 拼音输入法：引擎安装包 + 雾凇拼音词库。
# 引擎本身必须装进系统（TSF 要求），所以连安装包一起带着；
# 词库是纯数据，由程序铺到 %APPDATA%\Rime，用户不用自己折腾 Rime。
datas += [("assets/rime", "assets/rime")]

a = Analysis(
    ["entry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 体积大头，一个都用不上
        "tkinter", "matplotlib", "scipy", "pandas", "PIL", "IPython",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
        "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtPdf",
        "PySide6.QtBluetooth", "PySide6.QtPositioning", "PySide6.QtSerialPort",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Verbalkey",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序，不要黑框
    disable_windowed_traceback=False,
    icon="assets/app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Verbalkey",
)
