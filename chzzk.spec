# PyInstaller: Windows용 단일 chzzk.exe (GUI, 콘솔 창 없음)
# 빌드: pyinstaller --noconfirm --clean chzzk.spec
# ※ CLI(터미널 출력)는 파이썬으로 chzzk.py 를 실행하는 편이 안전합니다.

# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["chzzk.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "tkinter.commondialog",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="chzzk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
