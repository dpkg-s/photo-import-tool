# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for 照片导入工具 v5 —— 单目录模式（onedir / COLLECT）。

为什么从 onefile 改成 onedir：
onefile 每次启动都要把整个 exe（约 56MB）解压到 %TEMP%\\_MEIxxxxx，
且 bootloader 会 fork 两个进程、解压两遍。实测「双击到主窗口出现」需要
1.73s，而这部分开销每次运行都要付。改成单目录后直接就地加载，实测
0.78~1.34s，约省 0.9s。

代价：产物从单个 exe 变成一个文件夹（分享给朋友时整包压缩即可）。
旧版单文件 exe 备份在 backup/照片快速导入工具v5_扫描优化前.exe。
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

# exclude_binaries=True：依赖不再塞进 exe，改由 COLLECT 平铺到目录里
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='照片快速导入工具v5',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='照片快速导入工具v5',
)
