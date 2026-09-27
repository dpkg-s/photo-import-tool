#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 dist 下的构建产物打成 zip 便携版（分享给朋友用）。

单目录模式（onedir）：dist/照片快速导入工具v5/ → 整个目录进 zip，
解压后得到同名文件夹，双击里面的 exe 即可，无需安装 Python。
单文件模式（onefile）：dist/照片快速导入工具v5.exe → 单个 exe 进 zip。

两种产物都能处理，优先单文件。路径相对本脚本，不再依赖某个固定工作区。
"""

import zipfile
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
DST = ROOT / "release"
APP_NAME = "照片快速导入工具v5"

DST.mkdir(exist_ok=True)
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
zip_path = DST / f"{APP_NAME}_便携版_{stamp}.zip"

exe_path = DIST / f"{APP_NAME}.exe"
folder_path = DIST / APP_NAME

if exe_path.is_file():
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(exe_path, exe_path.name)
    print(f"已生成: {zip_path}")
    print(f"包含文件: {exe_path.name}")
elif folder_path.is_dir():
    files = [p for p in folder_path.rglob("*") if p.is_file()]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in files:
            # 保留外层目录名，解压后是一个整文件夹而不是散落一地
            zf.write(p, p.relative_to(folder_path.parent))
    print(f"已生成: {zip_path}")
    print(f"包含文件数: {len(files)}")
    print(f"解压后双击: {APP_NAME}\\{APP_NAME}.exe")
else:
    raise FileNotFoundError(f"找不到打包产物: {exe_path} 或 {folder_path}")
