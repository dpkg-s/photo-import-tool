#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片导入工具 v3
根据《使用文档_v3.txt》逆向工程实现。

技术栈：Python 3.13 + PyQt5 + Pillow + PyInstaller
功能：
  - 单实例（Windows Kernel Mutex）
  - 配置持久化（Windows 注册表）
  - 检测含 DCIM 目录的存储设备
  - 按 EXIF/文件时间分类导入 RAW/JPG
  - 日志与进度直接显示在主窗口
  - 导入开始时自动打开目标文件夹
"""

import sys
import os
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import winreg
from ctypes import windll, WinError as WinErrorC

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QGroupBox, QTextEdit, QProgressBar,
    QFileDialog, QMessageBox, QFrame, QGridLayout, QSplitter
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread
from PyQt5.QtGui import QPalette, QColor, QFont

from PIL import Image

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------
APP_NAME = "照片导入工具"
APP_VERSION = "v3"
MUTEX_NAME = "Global\\PhotoImportTool_v30"
REG_KEY_PATH = r"Software\\PhotoImportTool"

RAW_EXTS = (".nef", ".raw", ".cr2", ".cr3", ".arw", ".orf", ".rw2", ".dng", ".nrw")
JPG_EXTS = (".jpg", ".jpeg")
DCIM_NAME = "DCIM"

DATE_FORMATS = {
    "YYYY/MMDD": "%Y\\%m%d",
    "YYYY-MM-DD": "%Y-%m-%d",
    "YYYY年MM月DD日": "%Y年%m月%d日",
}


# ---------------------------------------------------------------------------
# 单实例控制
# ---------------------------------------------------------------------------
class SingleInstance:
    """基于 Windows 命名互斥体的单实例控制。"""

    def __init__(self, name: str):
        self.name = name
        self._mutex = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False

    def acquire(self) -> bool:
        kernel32 = windll.kernel32
        self._mutex = kernel32.CreateMutexW(None, False, self.name)
        if not self._mutex:
            raise WinErrorC()
        last_err = kernel32.GetLastError()
        if last_err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True

    def release(self):
        if self._mutex:
            windll.kernel32.ReleaseMutex(self._mutex)
            windll.kernel32.CloseHandle(self._mutex)
            self._mutex = None


# ---------------------------------------------------------------------------
# 注册表配置
# ---------------------------------------------------------------------------
class RegistryConfig:
    """读写 HKCU\\Software\\PhotoImportTool。"""

    def __init__(self, path: str = REG_KEY_PATH):
        self.path = path

    def _open(self, write: bool = False):
        access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
        try:
            return winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.path, 0, access)
        except FileNotFoundError:
            if write:
                return winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.path)
            raise

    def load(self) -> dict:
        defaults = {
            "import_path": "",
            "date_format": "YYYY/MMDD",
            "excluded_drives": "CD",
            "open_folder": "1",
            "remark": "",
        }
        try:
            with self._open(False) as key:
                for name, default in defaults.items():
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        defaults[name] = value
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
        return defaults

    def save(self, data: dict):
        with self._open(True) as key:
            for name, value in data.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


# ---------------------------------------------------------------------------
# 日期读取
# ---------------------------------------------------------------------------
def parse_exif_datetime(value) -> datetime | None:
    """解析 EXIF 时间字符串。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def get_exif_date(image_path: Path) -> datetime | None:
    """从图像 EXIF 中读取 DateTimeOriginal (36867) 或 DateTime (306)。"""
    try:
        with Image.open(image_path) as img:
            exif = img._getexif()
            if not exif:
                return None
            dt = parse_exif_datetime(exif.get(36867))
            if dt:
                return dt
            dt = parse_exif_datetime(exif.get(306))
            if dt:
                return dt
    except Exception:
        pass
    return None


def get_file_date(image_path: Path) -> datetime:
    """按文档优先级：EXIF DateTimeOriginal → EXIF DateTime → mtime → ctime。"""
    exif_dt = get_exif_date(image_path)
    if exif_dt:
        return exif_dt

    stat = image_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime)
    ctime = datetime.fromtimestamp(stat.st_ctime)
    return mtime if mtime <= ctime else ctime


# ---------------------------------------------------------------------------
# 设备/文件发现
# ---------------------------------------------------------------------------
def get_drive_letters() -> list[str]:
    """返回 Windows 上所有可用盘符。"""
    drives = []
    bitmask = windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if bitmask & (1 << i):
            drives.append(chr(ord("A") + i))
    return drives


def has_dcim(path: Path) -> bool:
    """检查路径下是否存在 DCIM 文件夹。"""
    try:
        return (path / DCIM_NAME).is_dir()
    except (PermissionError, OSError):
        return False


def find_dcim_devices(excluded: set[str]) -> list[Path]:
    """找出含有 DCIM 目录且不在排除列表中的存储设备根目录。"""
    devices = []
    for letter in get_drive_letters():
        if letter.upper() in excluded:
            continue
        root = Path(f"{letter.upper()}:/")
        if has_dcim(root):
            devices.append(root)
    return devices


def collect_images(dcim_root: Path, exts: tuple[str, ...]) -> list[Path]:
    """递归扫描 DCIM 目录下指定扩展名的文件。"""
    images = []
    dcim = dcim_root / DCIM_NAME
    try:
        for path in dcim.rglob("*"):
            if path.is_file() and path.suffix.lower() in exts:
                images.append(path)
    except (PermissionError, OSError):
        pass
    return images


# ---------------------------------------------------------------------------
# 导入任务
# ---------------------------------------------------------------------------
class ImportSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(dict)


class ImportWorker(QThread):
    """后台执行导入工作。"""

    def __init__(self, import_path: Path, date_format: str, remark: str,
                 excluded_drives: set[str], open_folder: bool):
        super().__init__()
        self.signals = ImportSignals()
        self.import_path = import_path
        self.date_format = date_format
        self.remark = remark.strip()
        self.excluded_drives = excluded_drives
        self.open_folder = open_folder
        self._abort = False
        self._result = defaultdict(int)
        self._opened_dirs = set()

    def abort(self):
        self._abort = True

    def _log(self, msg: str):
        self.signals.log.emit(msg)

    def _format_date_dir(self, dt: datetime) -> Path:
        fmt = DATE_FORMATS.get(self.date_format, DATE_FORMATS["YYYY/MMDD"])
        date_part = dt.strftime(fmt)
        if self.remark:
            date_part = f"{date_part}_{self.remark}"
        return Path(date_part)

    def _copy_file(self, src: Path, dst: Path) -> str:
        """执行复制并返回状态：OK / SKIP / FAIL / ABORT。"""
        if self._abort:
            return "ABORT"
        try:
            src_stat = src.stat()
            if dst.exists():
                dst_stat = dst.stat()
                if dst_stat.st_size == src_stat.st_size:
                    return "SKIP"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            return "OK"
        except Exception as e:
            self._log(f"[ERROR] 复制失败 {src.name}: {e}")
            return "FAIL"

    def _open_dir_once(self, directory: Path):
        if self.open_folder and directory not in self._opened_dirs:
            self._opened_dirs.add(directory)
            self._log(f"[OPEN] 正在打开 {directory}")
            try:
                os.startfile(str(directory))
            except Exception as e:
                self._log(f"[WARNING] 无法打开目录 {directory}: {e}")

    def run(self):
        try:
            self._run()
        except Exception as e:
            self._log(f"[ERROR] 导入过程中发生异常: {e}")
        finally:
            self.signals.finished.emit(dict(self._result))

    def _run(self):
        # 步骤 1: 检测移动硬盘
        self._log("")
        self._log("[步骤 1/4] 检测移动硬盘...")
        devices = find_dcim_devices(self.excluded_drives)
        if not devices:
            self._log("[WARNING] 未检测到移动硬盘或存储卡（没有找到 DCIM 目录）")
            return

        self._log(f"[INFO] 发现 {len(devices)} 个存储设备:")
        for dev in devices:
            self._log(f"         - {dev}")

        # 步骤 2: 扫描照片
        self._log("")
        self._log("[步骤 2/4] 扫描照片文件...")
        raw_files = []
        jpg_files = []
        for dev in devices:
            raw_files.extend(collect_images(dev, RAW_EXTS))
            jpg_files.extend(collect_images(dev, JPG_EXTS))

        self._log(f"[INFO] RAW 文件: {len(raw_files)} 个")
        self._log(f"[INFO] JPG 文件: {len(jpg_files)} 个")
        total = len(raw_files) + len(jpg_files)
        self._log(f"[INFO] 总计:     {total} 个文件待处理")

        if total == 0:
            self._log("[WARNING] 未找到照片文件")
            return

        # 步骤 3: 开始导入
        self._log("")
        self._log("[步骤 3/4] 开始导入...")
        self._log(f"[INFO] 目标根目录: {self.import_path}")
        self._log(f"[INFO] 日期格式:   {self.date_format}")
        if self.remark:
            self._log(f"[INFO] 备注:       {self.remark}")
        self._log("─" * 64)

        processed = 0

        def process_group(name: str, files: list[Path], subdir: str):
            nonlocal processed
            self._log(f"[{name}] 共 {len(files)} 个文件")
            for src in files:
                if self._abort:
                    self._log("[ABORT] 用户取消了操作")
                    return

                dt = get_file_date(src)
                rel_dir = self._format_date_dir(dt) / subdir
                dst_dir = self.import_path / rel_dir
                dst = dst_dir / src.name

                if not dst_dir.exists():
                    dst_dir.mkdir(parents=True, exist_ok=True)
                    self._log(f"[MKDIR] 创建目录: {dst_dir}")
                    self._result["created_dirs"] += 1

                self._open_dir_once(dst_dir)

                status = self._copy_file(src, dst)
                size_mb = src.stat().st_size / (1024 * 1024)
                if status == "OK":
                    self._log(f"[ OK ] {src} -> {dst} ({size_mb:.1f}MB)")
                    self._result[f"{name.lower()}_success"] += 1
                elif status == "SKIP":
                    self._log(f"[SKIP] {src} -> {dst} (同名同大小)")
                    self._result["skipped"] += 1
                elif status == "FAIL":
                    self._log(f"[FAIL] {src} -> {dst}")
                    self._result["errors"] += 1
                elif status == "ABORT":
                    self._log("[ABORT] 用户取消了操作")
                    return

                processed += 1
                self.signals.progress.emit(processed, total)

        process_group("RAW", raw_files, "raw")
        process_group("JPG", jpg_files, "jpg")

        if self._abort:
            return

        # 步骤 4: 打开目标文件夹
        self._log("")
        self._log("[步骤 4/4] 打开目标文件夹...")

        raw_success = self._result.get("raw_success", 0)
        jpg_success = self._result.get("jpg_success", 0)
        skipped = self._result.get("skipped", 0)
        errors = self._result.get("errors", 0)
        created_dirs = self._result.get("created_dirs", 0)

        self._log("")
        self._log("═" * 64)
        self._log("    导入完成!")
        self._log(f"    RAW 成功:  {raw_success} 个")
        self._log(f"    JPG 成功:  {jpg_success} 个")
        self._log(f"    跳过(重复): {skipped} 个")
        self._log(f"    出错:      {errors} 个")
        self._log(f"    创建目录:  {created_dirs} 个")
        self._log("═" * 64)


# ---------------------------------------------------------------------------
# 主配置窗口（日志集成在主窗口）
# ---------------------------------------------------------------------------
class ConfigWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(700, 780)

        self.config = RegistryConfig()
        self.settings = self.config.load()

        self._build_ui()
        self._load_settings()

        self._worker = None

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(20, 16, 20, 16)

        # 标题区
        title_layout = QVBoxLayout()
        title_layout.setSpacing(4)
        title_label = QLabel("📷 照片导入工具 v3")
        title_font = QFont("Microsoft YaHei", 16, QFont.Bold)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignCenter)
        title_layout.addWidget(title_label)

        subtitle = QLabel("点击「开始导入」后下方显示进度，实时查看详细日志")
        subtitle.setStyleSheet("color: #666;")
        subtitle.setAlignment(Qt.AlignCenter)
        title_layout.addWidget(subtitle)
        main_layout.addLayout(title_layout)

        # 导入配置卡片
        config_card = QFrame()
        config_card.setStyleSheet("""
            QFrame {
                background-color: #f5f5f5;
                border: 1px solid #ddd;
                border-radius: 6px;
            }
            QLabel {
                background: transparent;
                border: none;
            }
        """)
        config_layout = QVBoxLayout(config_card)
        config_layout.setSpacing(10)
        config_layout.setContentsMargins(16, 16, 16, 16)

        section_title = QLabel("导入配置")
        section_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        config_layout.addWidget(section_title)

        form = QGridLayout()
        form.setSpacing(10)
        form.setColumnStretch(1, 1)

        # 导入路径
        form.addWidget(QLabel("导入路径:"), 0, 0)
        path_layout = QHBoxLayout()
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("照片要存放到哪里，例: E:\\本地资源库\\d800e")
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.setFixedWidth(70)
        self.btn_browse.clicked.connect(self.choose_path)
        path_layout.addWidget(self.edit_path)
        path_layout.addWidget(self.btn_browse)
        form.addLayout(path_layout, 0, 1)

        # 日期格式
        form.addWidget(QLabel("日期格式:"), 1, 0)
        self.combo_date = QComboBox()
        self.combo_date.addItems(list(DATE_FORMATS.keys()))
        self.combo_date.setMinimumWidth(180)
        form.addWidget(self.combo_date, 1, 1, alignment=Qt.AlignLeft)

        # 备注
        form.addWidget(QLabel("备注:"), 2, 0)
        self.edit_remark = QLineEdit()
        self.edit_remark.setPlaceholderText("可选，如：旅行、婚礼；会追加到日期目录名后")
        form.addWidget(self.edit_remark, 2, 1)

        # 排除盘符（使用网格流式布局，避免盘符过多时横向溢出；支持刷新）
        form.addWidget(QLabel("排除盘符（不扫描）:"), 3, 0, alignment=Qt.AlignTop)
        drive_container = QVBoxLayout()
        drive_container.setSpacing(6)
        drive_container.setContentsMargins(0, 0, 0, 0)

        drive_frame = QFrame()
        self.drive_grid = QGridLayout(drive_frame)
        self.drive_grid.setSpacing(8)
        self.drive_grid.setContentsMargins(0, 0, 0, 0)
        self.drive_grid.setColumnStretch(8, 1)
        self.check_drives = {}
        self._cols = 8
        drive_container.addWidget(drive_frame)

        self.btn_refresh = QPushButton("🔄 刷新盘符")
        self.btn_refresh.setFixedWidth(90)
        self.btn_refresh.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        self.btn_refresh.clicked.connect(self.refresh_drives)
        drive_container.addWidget(self.btn_refresh, alignment=Qt.AlignLeft)
        form.addLayout(drive_container, 3, 1)

        self._rebuild_drive_grid()

        config_layout.addLayout(form)

        # 自动打开
        self.chk_open = QCheckBox("导入时自动打开目标文件夹")
        self.chk_open.setChecked(True)
        config_layout.addWidget(self.chk_open)

        main_layout.addWidget(config_card)

        # 操作按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        self.btn_start = QPushButton("▶ 开始导入")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 28px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #6c757d; }
        """)
        self.btn_start.clicked.connect(self.start_import)

        self.btn_save = QPushButton("保存设置")
        self.btn_save.setStyleSheet("padding: 8px 20px;")
        self.btn_save.clicked.connect(self.save_settings_clicked)

        self.btn_default = QPushButton("默认设置")
        self.btn_default.setStyleSheet("padding: 8px 20px;")
        self.btn_default.clicked.connect(self.reset_settings)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_save)
        btn_layout.addWidget(self.btn_default)
        main_layout.addLayout(btn_layout)

        # 日志区
        log_label = QLabel("导入日志")
        log_label.setStyleSheet("font-weight: bold; font-size: 13px;")
        main_layout.addWidget(log_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ccc;
                text-align: center;
                background-color: #fff;
                color: #333;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background-color: #28a745;
                border-radius: 4px;
            }
        """)
        main_layout.addWidget(self.progress)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QTextEdit.NoWrap)
        self.log.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #444;
                border-radius: 4px;
                font-family: Consolas, "Microsoft YaHei", monospace;
                font-size: 12px;
            }
        """)
        main_layout.addWidget(self.log, 1)

        log_btn_layout = QHBoxLayout()
        self.btn_stop = QPushButton("停止导入")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_import)
        self.btn_clear = QPushButton("清空日志")
        self.btn_clear.clicked.connect(self.clear_log)
        log_btn_layout.addStretch()
        log_btn_layout.addWidget(self.btn_clear)
        log_btn_layout.addWidget(self.btn_stop)
        main_layout.addLayout(log_btn_layout)

        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: gray;")
        main_layout.addWidget(self.status_label)

    def choose_path(self):
        path = QFileDialog.getExistingDirectory(self, "选择导入目录")
        if path:
            self.edit_path.setText(path)

    def _rebuild_drive_grid(self):
        """根据当前系统盘符重新构建排除盘符网格，保留已勾选项。"""
        current_excluded = {
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        }
        # 清除旧控件
        while self.drive_grid.count():
            item = self.drive_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.check_drives.clear()

        letters = get_drive_letters()
        for idx, letter in enumerate(letters):
            cb = QCheckBox(f"{letter}:\\")
            cb.setChecked(letter.upper() in current_excluded)
            self.check_drives[letter] = cb
            self.drive_grid.addWidget(cb, idx // self._cols, idx % self._cols)

    def refresh_drives(self):
        """刷新盘符列表并提示用户。"""
        self._rebuild_drive_grid()
        count = len(self.check_drives)
        self.status_label.setText(f"已刷新盘符列表，当前检测到 {count} 个磁盘。")

    def _load_settings(self):
        self.edit_path.setText(self.settings.get("import_path", ""))
        fmt = self.settings.get("date_format", "YYYY/MMDD")
        idx = self.combo_date.findText(fmt)
        if idx >= 0:
            self.combo_date.setCurrentIndex(idx)

        self.edit_remark.setText(self.settings.get("remark", ""))

        excluded = set(self.settings.get("excluded_drives", "CD").upper())
        for letter, cb in self.check_drives.items():
            cb.setChecked(letter.upper() in excluded)

        self.chk_open.setChecked(self.settings.get("open_folder", "1") == "1")

    def _collect_settings(self) -> dict:
        excluded = "".join(
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        )
        return {
            "import_path": self.edit_path.text().strip(),
            "date_format": self.combo_date.currentText(),
            "excluded_drives": excluded,
            "open_folder": "1" if self.chk_open.isChecked() else "0",
            "remark": self.edit_remark.text().strip(),
        }

    def save_settings_clicked(self):
        self.config.save(self._collect_settings())
        self.status_label.setText("设置已保存。")

    def reset_settings(self):
        self.edit_path.setText("")
        self.combo_date.setCurrentIndex(0)
        self.edit_remark.setText("")
        self._rebuild_drive_grid()
        for cb in self.check_drives.values():
            cb.setChecked(False)
        self.check_drives.get("C", QCheckBox()).setChecked(True)
        self.check_drives.get("D", QCheckBox()).setChecked(True)
        self.chk_open.setChecked(True)
        self.status_label.setText("已恢复默认设置。")

    def append_log(self, text: str):
        self.log.append(text)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_log(self):
        self.log.clear()
        self.progress.setValue(0)
        self.progress.setFormat("%p%")

    def start_import(self):
        import_text = self.edit_path.text().strip()
        if not import_text:
            QMessageBox.warning(self, "缺少路径", "请先设置导入路径。")
            return
        import_path = Path(import_text)

        try:
            import_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "路径错误", f"无法创建目标目录:\n{e}")
            return

        self.save_settings_clicked()

        excluded = {
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        }
        date_format = self.combo_date.currentText()
        remark = self.edit_remark.text().strip()
        open_folder = self.chk_open.isChecked()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.status_label.setText("正在导入...")
        self.log.clear()
        self.progress.setValue(0)

        self.append_log("═" * 64)
        self.append_log(f"  {APP_NAME} {APP_VERSION}")
        self.append_log("═" * 64)

        self._worker = ImportWorker(import_path, date_format, remark, excluded, open_folder)
        self._worker.signals.log.connect(self._on_log)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self.btn_stop.clicked.connect(self._worker.abort)
        self._worker.start()

    def stop_import(self):
        if self._worker:
            self._worker.abort()
        self.append_log("[ABORT] 请求停止导入...")
        self.btn_stop.setEnabled(False)

    def _on_log(self, text: str):
        self.append_log(text)

    def _on_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current / total * 100)
            self.progress.setValue(pct)
            self.progress.setFormat(f"{pct}% ({current}/{total})")

    def _on_finished(self, result: dict):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_label.setText("导入完成。")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    single = SingleInstance(MUTEX_NAME)
    if not single.acquire():
        app = QApplication(sys.argv)
        QMessageBox.information(
            None, APP_NAME,
            f"{APP_NAME} 已经在运行，请勿重复启动。"
        )
        sys.exit(0)

    try:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        window = ConfigWindow()
        window.show()
        sys.exit(app.exec_())
    finally:
        single.release()


if __name__ == "__main__":
    main()
