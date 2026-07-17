#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包 dist 下的单文件 exe 为 zip 便携版。"""

import zipfile
from pathlib import Path
from datetime import datetime

DIST = Path(r"D:\WorkBuddy\2026-06-26-14-36-49\dist")
DST = Path(r"D:\WorkBuddy\2026-06-26-14-36-49\release")
APP_NAME = "照片导入工具v3"

DST.mkdir(exist_ok=True)
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
zip_path = DST / f"{APP_NAME}_便携版_{stamp}.zip"

# 单文件模式：dist 下直接是 exe 文件
exe_path = DIST / f"{APP_NAME}.exe"
# 兼容旧版单目录模式：dist 下是目录
folder_path = DIST / APP_NAME

if exe_path.is_file():
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.write(exe_path, exe_path.name)
    print(f"已生成: {zip_path}")
    print(f"包含文件: {exe_path.name}")
elif folder_path.is_dir():
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in folder_path.rglob("*"):
            if p.is_file():
                arcname = p.relative_to(folder_path)
                zf.write(p, arcname)
    print(f"已生成: {zip_path}")
    print(f"包含文件数: {sum(1 for _ in folder_path.rglob('*') if _.is_file())}")
else:
    raise FileNotFoundError(f"找不到打包产物: {exe_path} 或 {folder_path}")
