#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
照片快速导入工具 v5
根据《使用文档_v3.txt》逆向工程实现。

技术栈：Python 3.13 + PyQt5 + Pillow + PyInstaller
功能：
  - 单实例（Windows Kernel Mutex）
  - 配置持久化（Windows 注册表）
  - 检测含 DCIM 目录的存储设备（跳过网络盘/光驱，避免扫描卡顿）
  - 按 EXIF/文件时间分类导入 RAW/JPG
  - 多设备分流导入：按机身序列号识别相机，认领后分流，
    未认领归 _他机，无序列号归 _未识别（安全模式）
  - 日志与进度直接显示在主窗口
  - 导入后操作：不打开 / 打开（全部或最新，可选 RAW/JPG）
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
    QFileDialog, QMessageBox, QFrame, QGridLayout, QSplitter, QRadioButton,
    QDialog, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread
from PyQt5.QtGui import QPalette, QColor, QFont

from PIL import Image

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------
APP_NAME = "照片导入工具"
APP_VERSION = "v5"
MUTEX_NAME = "Global\\PhotoImportTool_v50"
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
            "after_import": "open",
            "open_scope": "all",
            "open_types": "raw,jpg",
            "remark": "",
            "device_map": "",
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


# 设备白名单序列化：``序列号=设备名;序列号=设备名``。
# 设备名会参与路径拼接，故必须过滤 Windows 非法字符，避免建目录失败。
_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'


def sanitize_device_name(name: str) -> str:
    """清理设备名，使其可安全用作目录名。"""
    cleaned = "".join("_" if c in _ILLEGAL_NAME_CHARS else c for c in str(name))
    cleaned = cleaned.replace("\x00", "").strip().strip(".")
    return cleaned


def parse_device_map(text: str) -> dict[str, str]:
    """解析注册表中的设备白名单，返回 {序列号: 设备名}。"""
    result: dict[str, str] = {}
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        serial, _, name = chunk.partition("=")
        serial = serial.strip()
        name = sanitize_device_name(name)
        if serial and name and serial != UNKNOWN_DEVICE:
            result[serial] = name
    return result


def format_device_map(mapping: dict[str, str]) -> str:
    """把 {序列号: 设备名} 序列化回注册表字符串。"""
    return ";".join(f"{s}={n}" for s, n in mapping.items())


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
            exif = img.getexif()
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
# 机身识别（多设备分流）
# ---------------------------------------------------------------------------
# 标准 EXIF tag：BodySerialNumber(42033) / Model(272)。
# 尼康 D800E 等机型不写标准 tag，序列号只存在 MakerNote 里。
TAG_BODY_SERIAL = 42033
TAG_MODEL = 272
TAG_EXIF_IFD = 34665
TAG_MAKERNOTE = 37500

# Nikon MakerNote(0x1d) 的 TIFF 头前缀，须剥掉后才能作为独立 TIFF 解析。
_NIKON_MN_PREFIX = b"Nikon\x00"

UNKNOWN_DEVICE = "UNKNOWN"


def _to_serial_text(value) -> str | None:
    """把 EXIF 里的序列号规整成去空白字符串；空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii", "ignore")
        except Exception:
            return None
    text = str(value).replace("\x00", "").strip()
    return text or None


def _read_nikon_makernote_serial(makernote: bytes) -> str | None:
    """从 Nikon MakerNote 中取序列号。

    MakerNote 结构为 ``'Nikon\\x00\\x02\\x10\\x00'`` 再接一个完整的 TIFF 头
    （偏移 10 处）。直接把它交给 Pillow 会报 not a TIFF file，必须先剥掉
    这 10 字节前缀。序列号位于 MakerNote IFD 的 0x001d（Nikon 私有 tag）。
    """
    if not makernote or not makernote.startswith(_NIKON_MN_PREFIX):
        return None
    if len(makernote) <= 10:
        return None
    try:
        inner = Image.Exif()
        inner.load(makernote[10:])
    except Exception:
        return None
    return _to_serial_text(inner.get(0x001D))


def read_body_serial(image_path: Path) -> str | None:
    """读取照片的机身序列号，识别是哪台相机拍的。

    优先级：标准 BodySerialNumber(42033) → Nikon MakerNote(0x1d)。
    读不到（截图、无水印导出、被剥离 EXIF 的图）返回 None，
    调用方应据此走「安全模式」——宁可多导入，也不漏片。
    """
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                return None

            serial = _to_serial_text(exif.get(TAG_BODY_SERIAL))
            if serial:
                return serial

            exif_ifd = exif.get_ifd(TAG_EXIF_IFD) or {}
            serial = _read_nikon_makernote_serial(exif_ifd.get(TAG_MAKERNOTE))
            if serial:
                return serial
    except Exception:
        pass
    return None


def read_camera_model(image_path: Path) -> str:
    """读取机型名（如 NIKON D800E），仅用于设备清单展示。"""
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                return ""
            return str(exif.get(TAG_MODEL) or "").strip()
    except Exception:
        return ""


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


# GetDriveTypeW 返回值
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6


def get_drive_type(letter: str) -> int:
    """查询盘符类型；失败返回 0。"""
    try:
        return windll.kernel32.GetDriveTypeW(f"{letter.upper()}:\\")
    except Exception:
        return 0


def get_dcim_candidate_letters() -> list[str]:
    """筛选值得探测 DCIM 的盘符。

    跳过网络盘与光驱：相机的存储卡一定是可移动盘或本地盘，而失效的
    网络映射盘（如断开的 Y:/Z:）在 ``is_dir()`` 上会阻塞到 SMB 超时，
    实测每块耗时十余秒，是扫描卡顿的主因。
    """
    letters = []
    for letter in get_drive_letters():
        dtype = get_drive_type(letter)
        if dtype in (DRIVE_REMOTE, DRIVE_CDROM):
            continue
        letters.append(letter)
    return letters


def has_dcim(path: Path) -> bool:
    """检查路径下是否存在 DCIM 文件夹。"""
    try:
        return (path / DCIM_NAME).is_dir()
    except (PermissionError, OSError):
        return False

def find_dcim_devices(excluded: set[str]) -> list[Path]:
    """找出含有 DCIM 目录且不在排除列表中的存储设备根目录。

    仅探测可移动盘与本地盘；网络盘 / 光驱被跳过，避免失效映射盘拖慢扫描。
    """
    devices = []
    for letter in get_dcim_candidate_letters():
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


def scan_devices(devices: list[Path], log=None) -> dict:
    """扫描存储设备，按机身序列号统计出「哪台机器拍了多少张」。

    返回 {serial|UNKNOWN: {serial, model, count, raw, jpg, dmin, dmax, sample, files}}
    其中 files 为该设备下的全部文件路径，供后续分流直接复用，避免二次扫描。
    """
    stats: dict = {}

    def emit(msg: str):
        if log:
            log(msg)

    for dev in devices:
        files = collect_images(dev, RAW_EXTS) + collect_images(dev, JPG_EXTS)
        emit(f"[SCAN] {dev} 共 {len(files)} 个文件，正在识别机身...")
        for path in files:
            serial = read_body_serial(path) or UNKNOWN_DEVICE
            entry = stats.get(serial)
            if entry is None:
                entry = stats[serial] = {
                    "serial": serial,
                    "model": "",
                    "count": 0,
                    "raw": 0,
                    "jpg": 0,
                    "dmin": None,
                    "dmax": None,
                    "sample": path.name,
                    "files": [],
                }
            entry["count"] += 1
            entry["files"].append(path)
            if path.suffix.lower() in RAW_EXTS:
                entry["raw"] += 1
            else:
                entry["jpg"] += 1
            if not entry["model"]:
                entry["model"] = read_camera_model(path)
            dt = get_file_date(path)
            if dt:
                if entry["dmin"] is None or dt < entry["dmin"]:
                    entry["dmin"] = dt
                if entry["dmax"] is None or dt > entry["dmax"]:
                    entry["dmax"] = dt

    return stats


# ---------------------------------------------------------------------------
# 导入任务
# ---------------------------------------------------------------------------
class ImportSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(dict)
    devices = pyqtSignal(dict)


class ScanWorker(QThread):
    """后台扫描卡内设备清单，供「多设备分流」弹窗展示。"""

    def __init__(self, excluded_drives: set[str]):
        super().__init__()
        self.signals = ImportSignals()
        self.excluded_drives = excluded_drives

    def run(self):
        try:
            devices = find_dcim_devices(self.excluded_drives)
            if not devices:
                self.signals.log.emit("[WARNING] 未检测到移动硬盘或存储卡（没有找到 DCIM 目录）")
                self.signals.devices.emit({})
                return
            stats = scan_devices(devices, log=self.signals.log.emit)
            self.signals.devices.emit(stats)
        except Exception as e:
            self.signals.log.emit(f"[ERROR] 扫描设备失败: {e}")
            self.signals.devices.emit({})


class ImportWorker(QThread):
    """后台执行导入工作。"""

    def __init__(self, import_path: Path, date_format: str, remark: str,
                 excluded_drives: set[str], device_map: dict[str, str] | None = None,
                 scanned: dict | None = None):
        super().__init__()
        self.signals = ImportSignals()
        self.import_path = import_path
        self.date_format = date_format
        self.remark = remark.strip()
        self.excluded_drives = excluded_drives
        # device_map: 已认领的机身序列号 -> 设备名；为空时退化为原有全量导入行为
        self.device_map = device_map or {}
        # scanned: 已扫描结果，复用以避免二次读卡（分流模式下由 ScanWorker 提供）
        self.scanned = scanned
        self._abort = False
        self._result = defaultdict(int)
        self._created_dirs: list[str] = []
        self._seen_dirs: set[str] = set()

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

    def run(self):
        try:
            self._run()
        except Exception as e:
            self._log(f"[ERROR] 导入过程中发生异常: {e}")
        finally:
            self.signals.finished.emit(dict(self._result))

    def _run(self):
        if self.device_map:
            self._run_split()
        else:
            self._run_flat()

    def _scan(self, devices: list[Path]) -> dict:
        """取扫描结果：优先复用外部传入，否则就地扫描。"""
        if self.scanned is not None:
            return self.scanned
        return scan_devices(devices, log=self._log)

    def _run_split(self):
        """多设备分流导入：按机身序列号决定落盘目录。

        落盘规则：
          - 已认领设备 → ``<导入路径>/<设备名>/<日期目录>/<raw|jpg>``
          - 未认领设备 → ``<导入路径>/_他机/<设备名或序列号>/<日期目录>/<raw|jpg>``
          - 读不到序列号 → 走安全模式，按主目录落盘（宁可多导，不漏片）
        """
        self._log("")
        self._log("[步骤 1/4] 检测移动硬盘...")
        devices = find_dcim_devices(self.excluded_drives)
        if not devices:
            self._log("[WARNING] 未检测到移动硬盘或存储卡（没有找到 DCIM 目录）")
            return
        for dev in devices:
            self._log(f"         - {dev}")

        self._log("")
        self._log("[步骤 2/4] 按机身序列号分流...")
        stats = self._scan(devices)

        groups = []          # [(输出前缀 Path, 设备标签, [文件...])]
        claimed_total = 0
        unclaimed_total = 0
        unknown_total = 0

        for serial, entry in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
            files = entry["files"]
            if serial == UNKNOWN_DEVICE:
                unknown_total = entry["count"]
                label = "未识别"
                prefix = Path("_未识别")
            elif serial in self.device_map:
                claimed_total += entry["count"]
                label = self.device_map[serial]
                prefix = Path(sanitize_device_name(label))
            else:
                unclaimed_total += entry["count"]
                label = f"{entry['model'] or '未知机型'} [{serial}]"
                prefix = Path("_他机") / sanitize_device_name(serial)

            span = ""
            if entry["dmin"]:
                span = f"，{entry['dmin']:%Y-%m-%d} ~ {entry['dmax']:%Y-%m-%d}"
            self._log(
                f"         {label}: {entry['count']} 张 "
                f"(RAW {entry['raw']} / JPG {entry['jpg']}){span}"
            )
            self._log(f"            → {self.import_path / prefix}")
            groups.append((prefix, label, files))

        if unknown_total:
            self._log(f"[INFO] 其中 {unknown_total} 张无法读取序列号，已按安全模式归入 _未识别")
        if unclaimed_total:
            self._log(f"[INFO] 其中 {unclaimed_total} 张属于未认领设备，已归入 _他机")

        total = sum(len(f) for _, _, f in groups)
        if total == 0:
            self._log("[WARNING] 未找到照片文件")
            return

        # 步骤 3: 导入
        self._log("")
        self._log("[步骤 3/4] 开始导入...")
        self._log(f"[INFO] 目标根目录: {self.import_path}")
        if self.remark:
            self._log(f"[INFO] 备注:       {self.remark}")
        self._log(f"[INFO] 分流模式:   多设备（已认领 {len(self.device_map)} 台）")
        self._log("─" * 64)

        processed = 0
        for prefix, label, files in groups:
            processed = self._copy_group(label, files, prefix, processed, total)
            if self._abort:
                return

        self._finish(total)

    def _run_flat(self):
        """原有全量导入（不分流），行为与历史版本一致。"""
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
        processed = self._copy_group("RAW", raw_files, Path("."), processed, total)
        if self._abort:
            return
        processed = self._copy_group("JPG", jpg_files, Path("."), processed, total)
        if self._abort:
            return

        self._finish(total)

    def _copy_group(self, label: str, files: list[Path], prefix: Path,
                    processed: int, total: int) -> int:
        """拷贝一组文件到 ``<导入路径>/<prefix>/<日期目录>/<raw|jpg>``。

        prefix 为空（Path('.')）时退化为 ``<日期目录>/<raw|jpg>``，
        与原版全量导入的目录结构完全一致。
        """
        self._log(f"[{label}] 共 {len(files)} 个文件")
        for src in files:
            if self._abort:
                self._log("[ABORT] 用户取消了操作")
                return processed

            dt = get_file_date(src)
            subdir = "raw" if src.suffix.lower() in RAW_EXTS else "jpg"
            rel_dir = prefix / self._format_date_dir(dt) / subdir
            dst_dir = self.import_path / rel_dir
            dst = dst_dir / src.name

            if not dst_dir.exists():
                dst_dir.mkdir(parents=True, exist_ok=True)
                self._log(f"[MKDIR] 创建目录: {dst_dir}")
                self._result["created_dirs"] += 1

            # 去重：同一目录会处理多个文件，若每个文件都 append，
            # 「导入后打开」环节会对着同一目录重复调用 os.startfile 几十次。
            # 用 set 守卫，同时保留首次出现的顺序（便于「最新」判定取末级）。
            key = str(dst_dir)
            if key not in self._seen_dirs:
                self._seen_dirs.add(key)
                self._created_dirs.append(key)

            status = self._copy_file(src, dst)
            size_mb = src.stat().st_size / (1024 * 1024)
            if status == "OK":
                self._log(f"[ OK ] {src} -> {dst} ({size_mb:.1f}MB)")
                self._result["success"] += 1
                if src.suffix.lower() in RAW_EXTS:
                    self._result["raw_success"] += 1
                else:
                    self._result["jpg_success"] += 1
            elif status == "SKIP":
                self._log(f"[SKIP] {src} -> {dst} (同名同大小)")
                self._result["skipped"] += 1
            elif status == "FAIL":
                self._log(f"[FAIL] {src} -> {dst}")
                self._result["errors"] += 1
            elif status == "ABORT":
                self._log("[ABORT] 用户取消了操作")
                return processed

            processed += 1
            self.signals.progress.emit(processed, total)
        return processed

    def _finish(self, total: int):
        raw_success = self._result.get("raw_success", 0)
        jpg_success = self._result.get("jpg_success", 0)
        skipped = self._result.get("skipped", 0)
        errors = self._result.get("errors", 0)
        created_dirs = self._result.get("created_dirs", 0)

        # 步骤 4: 汇总
        self._log("")
        self._log("[步骤 4/4] 汇总结果...")
        self._log("")
        self._log("═" * 64)
        self._log("    导入完成!")
        self._log(f"    RAW 成功:  {raw_success} 个")
        self._log(f"    JPG 成功:  {jpg_success} 个")
        self._log(f"    跳过(重复): {skipped} 个")
        self._log(f"    出错:      {errors} 个")
        self._log(f"    创建目录:  {created_dirs} 个")
        self._result["created_dirs_list"] = self._created_dirs
        self._log("═" * 64)


# ---------------------------------------------------------------------------
# 设备认领对话框
# ---------------------------------------------------------------------------
class DeviceDialog(QDialog):
    """展示卡内检测到的设备，由主人勾选认领并命名。

    认领的设备按所填设备名分流入库；未认领的归入 ``_他机/<序列号>``，
    读不到序列号的归入 ``_未识别``（安全模式，不漏片）。
    """

    def __init__(self, stats: dict, device_map: dict, import_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("多设备分流 - 认领相机")
        self.setMinimumWidth(760)
        self.stats = stats
        self.device_map = device_map
        self.import_path = import_path
        self.result_map: dict[str, str] = {}
        self._rows = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        tip = QLabel(
            "检测到以下设备。勾选属于你自己的相机并填写设备名，\n"
            "已认领的照片将导入「设备名/日期/raw|jpg」，未认领的归入「_他机」。"
        )
        tip.setStyleSheet("color: #444; line-height: 160%;")
        layout.addWidget(tip)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["认领", "序列号", "机型", "张数", "拍摄时间", "导入到（设备名）"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        entries = sorted(self.stats.items(), key=lambda kv: -kv[1]["count"])
        self.table.setRowCount(len(entries))

        for row, (serial, entry) in enumerate(entries):
            known = serial != UNKNOWN_DEVICE
            claimed = known and serial in self.device_map

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Checked if claimed else Qt.Unchecked)
            if not known:
                chk.setFlags(Qt.NoItemFlags)   # 未识别设备不可认领
            self.table.setItem(row, 0, chk)

            self.table.setItem(row, 1, QTableWidgetItem(serial if known else "无法读取"))
            self.table.setItem(row, 2, QTableWidgetItem(entry["model"] or "-"))
            self.table.setItem(row, 3, QTableWidgetItem(
                f"{entry['count']} (RAW {entry['raw']}/JPG {entry['jpg']})"))

            span = "-"
            if entry["dmin"]:
                span = f"{entry['dmin']:%Y-%m-%d} ~ {entry['dmax']:%Y-%m-%d}"
            self.table.setItem(row, 4, QTableWidgetItem(span))

            name_edit = QLineEdit()
            name_edit.setPlaceholderText("取消勾选则归入 _他机")
            if claimed:
                name_edit.setText(self.device_map[serial])
            elif known:
                # 用机型给出默认建议名，省去手输
                name_edit.setText(self._suggest_name(entry["model"], serial))
            else:
                name_edit.setEnabled(False)
                name_edit.setPlaceholderText("未识别设备，导入到 _未识别")
                chk.setCheckState(Qt.Unchecked)
            self.table.setCellWidget(row, 5, name_edit)
            self._rows.append((row, serial, name_edit, chk))

        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 50)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 130)
        self.table.setColumnWidth(3, 120)
        self.table.setColumnWidth(4, 180)
        layout.addWidget(self.table)

        hint = QLabel(f"导入根目录：{self.import_path}")
        hint.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        self.btn_all = QPushButton("全部认领")
        self.btn_all.clicked.connect(lambda: self._set_all(True))
        self.btn_none = QPushButton("全不认领")
        self.btn_none.clicked.connect(lambda: self._set_all(False))
        btn_row.addWidget(self.btn_all)
        btn_row.addWidget(self.btn_none)
        btn_row.addStretch()

        self.btn_ok = QPushButton("开始分流导入")
        self.btn_ok.setStyleSheet("""
            QPushButton {
                background-color: #28a745; color: white; border: none;
                border-radius: 4px; padding: 8px 22px; font-weight: bold;
            }
            QPushButton:hover { background-color: #218838; }
        """)
        self.btn_ok.clicked.connect(self._on_accept)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(self.btn_ok)
        layout.addLayout(btn_row)

    @staticmethod
    def _suggest_name(model: str, serial: str) -> str:
        """从机型名猜一个默认设备名（如 NIKON D800E -> D800E）。"""
        if not model:
            return f"相机{serial[-4:]}" if serial else "相机"
        name = model.strip()
        for prefix in ("NIKON ", "NIKON", "Canon ", "SONY ", "Sony "):
            if name.upper().startswith(prefix.upper()):
                name = name[len(prefix):]
                break
        # 机型里的空格（如 "Z 6_2"）不适合做目录名，压成短横
        name = name.strip().replace(" ", "-")
        return sanitize_device_name(name) or f"相机{serial[-4:]}"

    def _set_all(self, checked: bool):
        for _row, serial, _edit, chk in self._rows:
            if serial == UNKNOWN_DEVICE:
                continue
            chk.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def _on_accept(self):
        mapping: dict[str, str] = {}
        for _row, serial, edit, chk in self._rows:
            if serial == UNKNOWN_DEVICE or chk.checkState() != Qt.Checked:
                continue
            name = sanitize_device_name(edit.text())
            if not name:
                QMessageBox.warning(
                    self, "缺少设备名",
                    f"序列号 {serial} 已勾选但未填写设备名。\n"
                    "请填写设备名，或取消勾选使其归入 _他机。"
                )
                return
            mapping[serial] = name

        # 同名冲突会导致两台设备写进同一目录，提前拦下
        seen: dict[str, str] = {}
        for serial, name in mapping.items():
            if name in seen:
                QMessageBox.warning(
                    self, "设备名重复",
                    f"「{name}」被多台设备使用（{seen[name]} 与 {serial}）。\n"
                    "请改成互不相同的设备名。"
                )
                return
            seen[name] = serial

        self.result_map = mapping
        self.accept()


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
        title_label = QLabel("📷 照片快速导入工具 v5")
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

        # 导入后操作
        after_frame = QFrame()
        after_frame.setStyleSheet("background: transparent; border: none;")
        after_layout = QVBoxLayout(after_frame)
        after_layout.setContentsMargins(0, 4, 0, 0)
        after_layout.setSpacing(6)
        after_title = QLabel("导入后操作")
        after_title.setStyleSheet("font-weight: bold; font-size: 12px;")
        after_layout.addWidget(after_title)
        radio_row = QHBoxLayout()
        radio_row.setSpacing(10)
        self.radio_not_open = QRadioButton("不打开")
        self.radio_open = QRadioButton("打开")
        self.radio_open.setChecked(True)
        radio_row.addWidget(self.radio_not_open)
        radio_row.addWidget(self.radio_open)
        radio_row.addStretch()
        after_layout.addLayout(radio_row)
        open_opts_frame = QFrame()
        open_opts_frame.setStyleSheet("background: #ffffff; border: 1px solid #ddd; border-radius: 4px;")
        open_opts_layout = QVBoxLayout(open_opts_frame)
        open_opts_layout.setContentsMargins(12, 8, 12, 8)
        open_opts_layout.setSpacing(6)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("作用范围:"))
        self.radio_scope_all = QRadioButton("全部")
        self.radio_scope_all.setChecked(True)
        self.radio_scope_latest = QRadioButton("最新")
        scope_row.addWidget(self.radio_scope_all)
        scope_row.addWidget(self.radio_scope_latest)
        scope_row.addStretch()
        open_opts_layout.addLayout(scope_row)
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("打开类型:"))
        self.chk_open_raw = QCheckBox("RAW")
        self.chk_open_raw.setChecked(True)
        self.chk_open_jpg = QCheckBox("JPG")
        self.chk_open_jpg.setChecked(True)
        type_row.addWidget(self.chk_open_raw)
        type_row.addWidget(self.chk_open_jpg)
        type_row.addStretch()
        open_opts_layout.addLayout(type_row)
        after_layout.addWidget(open_opts_frame)
        def toggle_open_opts():
            enabled = self.radio_open.isChecked()
            open_opts_frame.setEnabled(enabled)
        self.radio_not_open.toggled.connect(toggle_open_opts)
        config_layout.addWidget(after_frame)

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

        self.btn_split = QPushButton("🔍 多设备分流导入")
        self.btn_split.setToolTip(
            "扫描卡内照片，按机身序列号识别是哪台相机拍的。\n"
            "认领自己的相机后按其分流，未认领的归入 _他机。\n"
            "适用于：借来的卡上有别人的照片。"
        )
        self.btn_split.setStyleSheet("""
            QPushButton {
                background-color: #007bff;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 20px;
                font-weight: bold;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #0069d9; }
            QPushButton:disabled { background-color: #6c757d; }
        """)
        self.btn_split.clicked.connect(self.start_split_import)

        self.btn_save = QPushButton("保存设置")
        self.btn_save.setStyleSheet("padding: 8px 20px;")
        self.btn_save.clicked.connect(self.save_settings_clicked)

        self.btn_default = QPushButton("默认设置")
        self.btn_default.setStyleSheet("padding: 8px 20px;")
        self.btn_default.clicked.connect(self.reset_settings)

        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_split)
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

        self.radio_open.setChecked(self.settings.get("after_import", "open") == "open")
        self.radio_not_open.setChecked(self.settings.get("after_import", "open") == "none")
        self.radio_scope_all.setChecked(self.settings.get("open_scope", "all") == "all")
        self.radio_scope_latest.setChecked(self.settings.get("open_scope", "all") == "latest")
        open_types = self.settings.get("open_types", "raw,jpg").split(",")
        self.chk_open_raw.setChecked("raw" in open_types)
        self.chk_open_jpg.setChecked("jpg" in open_types)

    def _collect_settings(self) -> dict:
        excluded = "".join(
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        )
        return {
            "import_path": self.edit_path.text().strip(),
            "date_format": self.combo_date.currentText(),
            "excluded_drives": excluded,
            "remark": self.edit_remark.text().strip(),
            "after_import": "open" if self.radio_open.isChecked() else "none",
            "open_scope": "all" if self.radio_scope_all.isChecked() else "latest",
            "open_types": ",".join(t for t, c in [("raw", self.chk_open_raw), ("jpg", self.chk_open_jpg)] if c.isChecked()),
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
        self.radio_open.setChecked(True)
        self.radio_not_open.setChecked(False)
        self.radio_scope_all.setChecked(True)
        self.radio_scope_latest.setChecked(False)
        self.chk_open_raw.setChecked(True)
        self.chk_open_jpg.setChecked(True)
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
        import_path = self._validate_path()
        if import_path is None:
            return

        self.save_settings_clicked()

        excluded = {
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        }
        date_format = self.combo_date.currentText()
        remark = self.edit_remark.text().strip()

        self._set_busy(True, "正在导入...")
        self.log.clear()
        self.progress.setValue(0)

        self.append_log("═" * 64)
        self.append_log(f"  {APP_NAME} {APP_VERSION}")
        self.append_log("═" * 64)

        self._worker = ImportWorker(import_path, date_format, remark, excluded)
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

    # ------------------------------------------------------------------
    # 多设备分流导入
    # ------------------------------------------------------------------
    def _validate_path(self) -> Path | None:
        """校验并准备导入路径，失败返回 None。"""
        import_text = self.edit_path.text().strip()
        if not import_text:
            QMessageBox.warning(self, "缺少路径", "请先设置导入路径。")
            return None
        import_path = Path(import_text)
        try:
            import_path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, "路径错误", f"无法创建目标目录:\n{e}")
            return None
        return import_path

    def _set_busy(self, busy: bool, status: str):
        self.btn_start.setEnabled(not busy)
        self.btn_split.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.status_label.setText(status)

    def start_split_import(self):
        import_path = self._validate_path()
        if import_path is None:
            return

        excluded = {
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        }

        self.save_settings_clicked()
        self._set_busy(True, "正在扫描设备...")
        self.log.clear()
        self.progress.setValue(0)
        self.append_log("═" * 64)
        self.append_log(f"  {APP_NAME} {APP_VERSION} · 多设备分流")
        self.append_log("═" * 64)

        self._scanner = ScanWorker(excluded)
        self._scanner.signals.log.connect(self._on_log)
        self._scanner.signals.devices.connect(
            lambda stats: self._on_scan_done(stats, import_path, excluded))
        self._scanner.finished.connect(self._scanner.deleteLater)
        self._scanner.start()

    def _on_scan_done(self, stats: dict, import_path: Path, excluded: set):
        if not stats:
            self._set_busy(False, "未检测到设备。")
            QMessageBox.information(
                self, "未找到设备",
                "没有检测到含 DCIM 目录的存储设备。\n"
                "请确认已插入存储卡，并检查「排除盘符」设置。"
            )
            return

        claimed = parse_device_map(self.settings.get("device_map", ""))
        dialog = DeviceDialog(stats, claimed, import_path, self)
        if dialog.exec_() != QDialog.Accepted:
            self._set_busy(False, "已取消。")
            self.append_log("[ABORT] 用户取消了设备认领。")
            return

        device_map = dialog.result_map
        # 认领结果写回注册表，下次插同一张卡无需重填
        self.settings["device_map"] = format_device_map(device_map)
        self.config.save(self._collect_settings())

        if device_map:
            self.append_log("[INFO] 已认领设备: " + ", ".join(
                f"{name}[{serial}]" for serial, name in device_map.items()))
        else:
            self.append_log("[INFO] 未认领任何设备，全部归入 _他机。")

        self._set_busy(True, "正在分流导入...")
        self.progress.setValue(0)

        self._worker = ImportWorker(
            import_path,
            self.combo_date.currentText(),
            self.edit_remark.text().strip(),
            excluded,
            device_map=device_map,
            scanned=stats,
        )
        self._worker.signals.log.connect(self._on_log)
        self._worker.signals.progress.connect(self._on_progress)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self.btn_stop.clicked.connect(self._worker.abort)
        self._worker.start()

    def _on_log(self, text: str):
        self.append_log(text)

    def _on_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current / total * 100)
            self.progress.setValue(pct)
            self.progress.setFormat(f"{pct}% ({current}/{total})")

    def _on_finished(self, result: dict):
        self._set_busy(False, "导入完成。")

        # 导入后操作：根据设置打开文件夹
        if self.radio_open.isChecked() and result.get("created_dirs_list"):
            created = [Path(d) for d in result["created_dirs_list"]]
            if self.radio_scope_latest.isChecked():
                import_path = Path(self.edit_path.text().strip())
                # 「最新」以「日期目录」为单位，而不是路径第一级。
                # 目标结构固定为 <日期目录>/<raw|jpg>，故 date_dir = d.parent。
                # 不能取 rel.parts[0]：YYYY/MMDD 的格式串是 %Y\%m%d（反斜杠），
                # Windows 上会被当作路径分隔符、生成两级目录（2026\0912_漫展），
                # 取第一级会退化成「整年」，导致当年所有日期目录都被判为「最新」。
                date_dirs = set()
                for d in created:
                    try:
                        d.relative_to(import_path)
                    except ValueError:
                        continue          # 不在目标根目录下，忽略
                    if d.parent != import_path:
                        date_dirs.add(d.parent)
                if date_dirs:
                    latest_date_dir = max(date_dirs)
                    # 用父目录相等判定，避免 startswith 的前缀误匹配（2026\09 与 2026\0912）
                    created = [d for d in created if d.parent == latest_date_dir]
            open_types_set = set()
            if self.chk_open_raw.isChecked():
                open_types_set.add("raw")
            if self.chk_open_jpg.isChecked():
                open_types_set.add("jpg")
            for d in created:
                if d.name in open_types_set:
                    self.append_log(f"[OPEN] 正在打开 {d}")
                    try:
                        os.startfile(str(d))
                    except Exception as e:
                        self.append_log(f"[WARNING] 无法打开目录 {d}: {e}")


def selftest():
    """Headless 自检：模拟一次完整导入流程（ctypes + PIL + 拷贝），用于打包后验证闪退问题。"""
    import tempfile
    import shutil as _sh

    def log(msg):
        print(msg, flush=True)

    log("[SELFTEST] 1/4 ctypes GetLogicalDrives 调用...")
    drives = get_drive_letters()
    log(f"           OK, 盘符: {''.join(drives)}")

    log("[SELFTEST] 2/4 查找含 DCIM 的设备...")
    devices = find_dcim_devices({'C', 'D', 'E'})
    log(f"           发现 {len(devices)} 个设备: {devices}")

    if not devices:
        log("[SELFTEST] FAIL 未找到含 DCIM 设备")
        return 1

    log("[SELFTEST] 3/4 PIL 打开 NEF/JPG 读 EXIF...")
    dcim = devices[0] / DCIM_NAME
    test_file = None
    for p in dcim.rglob('*'):
        if p.suffix.lower() in RAW_EXTS or p.suffix.lower() in JPG_EXTS:
            test_file = p
            break
    if not test_file:
        log("[SELFTEST] FAIL 未找到测试照片")
        return 1
    dt = get_file_date(test_file)
    log(f"           打开 {test_file.name} -> 日期 {dt}  OK")

    log("[SELFTEST] 4/4 拷贝文件到临时目录...")
    tmp = Path(tempfile.mkdtemp(prefix='phototool_selftest_'))
    try:
        dst = tmp / test_file.name
        shutil.copy2(str(test_file), str(dst))
        if dst.exists() and dst.stat().st_size == test_file.stat().st_size:
            log(f"           拷贝 {test_file.name} ({dst.stat().st_size} bytes)  OK")
        else:
            log("[SELFTEST] FAIL 拷贝后大小不一致")
            return 1
    finally:
        _sh.rmtree(tmp, ignore_errors=True)

    log("[SELFTEST] 全部通过！")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    if '--selftest' in sys.argv:
        sys.exit(selftest())
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
