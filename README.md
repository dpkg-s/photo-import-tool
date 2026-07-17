# 照片导入工具 v3

Windows 桌面工具，从相机存储卡（含 DCIM 目录）导入 RAW/JPG 照片，按 EXIF 日期自动分类存放。

## 功能

- 自动检测含 `DCIM` 目录的存储设备（相机存储卡、读卡器、移动硬盘等）
- 按 EXIF `DateTimeOriginal` 自动分类导入（回退到文件修改时间）
- 支持 RAW（.nef/.cr2/.arw/.dng 等）和 JPG 格式
- 三种日期目录格式：`YYYY/MMDD`、`YYYY-MM-DD`、`YYYY年MM月DD日`
- 备注功能：给日期目录追加自定义标签（如"旅行"、"婚礼"）
- 排除盘符：选择不扫描特定磁盘
- 单实例保护：防止重复启动
- 导入完成后自动打开目标文件夹

## 安装

从 [Releases](../../releases/latest) 下载 `照片导入工具v3.exe`，双击运行，**无需安装 Python 环境**。

## 从源码构建

```bash
pip install pyqt5 pillow pyinstaller
pyinstaller build.spec
```

## 技术栈

Python 3.13 + PyQt5 + Pillow + PyInstaller

## 许可证

MIT
