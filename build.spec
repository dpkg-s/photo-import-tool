# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for 照片导入工具 v4.0
使用单文件模式（--onefile），无需解压，双击即可运行。
"""

import sys

block_cipher = None

a = Analysis(
    ['PhotoImportTool.py'],
    pathex=[r'C:\Users\dpkg_\Documents\Codex\photo-import-tool'],
    binaries=[],
    datas=[],
    hiddenimports=['PIL', 'PIL._imaging', 'PIL.ExifTags'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirect=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='照片快速导入工具v4',
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
    icon=None,
)

