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
import io
import re
import json
import shutil
import warnings
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import winreg
from ctypes import windll, WinError as WinErrorC

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QCheckBox, QGroupBox, QTextEdit, QProgressBar,
    QFileDialog, QMessageBox, QFrame, QGridLayout, QSplitter, QRadioButton,
    QScrollArea, QSizePolicy
)
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread, QTimer
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

# 大块读窗口。EXIF 里的 Nikon MakerNote 是一整块（实测 NEF 最大约 180KB、
# JPG 约 40KB），Pillow 会把它完整读进内存 —— 直读文件时这摊成每张数百次
# 小块 read+seek，冷态（刚插卡）下每次都是一趟 USB 往返，实测 0.5ms/次，
# 629 张累计约 12 万次 read ⇒ 约 60s。先把文件头一次读进内存再交给 Pillow，
# 可把每张的 read 次数压到 1 次。
# 窗口边界为真实卡实测：NEF 需 >=256KB（192KB 时 3 个样本只命中 2 个）、
# JPG 需 >=128KB（64KB 时全部失败）。
SCAN_HEAD_BYTES = 256 * 1024

# 目录级采样。相机写卡按目录顺序递增，同一个 DCIM 子目录几乎必然出自同一
# 机身（实测本机卡两组分别 539 张 / 90 张，各自 100% 纯）。先逐张确认前
# DIR_PROBE_COUNT 张，锁定单机身之后每 DIR_SAMPLE_STRIDE 张抽查 1 张，
# 一旦抽查到别的机身就整目录回退全读，保证结果与全读一致。
DIR_PROBE_COUNT = 8
DIR_SAMPLE_STRIDE = 12

# 文件名序号「大幅回退」的判定阈值：相机换机身之后序号会重启
# （实测场景如 2000 → 0001），而删除/重拍/目录项乱序只会造成小回退
# （本机卡实测出现过 1708 → 1705，幅度仅 3）。用幅度阈值把两者分开，
# 避免误判把整个目录推去全读。
DIR_NAME_BACKSTEP = 50

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
            "device_config": "[]",
        }
        try:
            with self._open(False) as key:
                for name, default in defaults.items():
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        defaults[name] = value
                    except FileNotFoundError:
                        pass
                # 兼容 v5 早期版本：那时设备白名单存成 "序列号=设备名;..."
                if defaults.get("device_config", "[]") in ("", "[]"):
                    try:
                        legacy, _ = winreg.QueryValueEx(key, "device_map")
                        if legacy:
                            migrated = [
                                {"serial": s, "name": n, "enabled": True, "path": ""}
                                for s, n in parse_device_map_legacy(legacy).items()
                            ]
                            if migrated:
                                defaults["device_config"] = format_device_config(migrated)
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
        return defaults

    def save(self, data: dict):
        with self._open(True) as key:
            for name, value in data.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, str(value))


# 设备名会参与路径拼接，故必须过滤 Windows 非法字符，避免建目录失败。
_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'


def sanitize_device_name(name: str) -> str:
    """清理设备名，使其可安全用作目录名。"""
    cleaned = "".join("_" if c in _ILLEGAL_NAME_CHARS else c for c in str(name))
    cleaned = cleaned.replace("\x00", "").strip().strip(".")
    return cleaned


def parse_device_config(text: str) -> list[dict]:
    """解析设备配置 JSON。

    返回 ``[{"serial","name","enabled","path"}, ...]``。
    损坏 / 非列表的输入一律当作空配置，避免把配置错误升级成导入事故。
    """
    try:
        raw = json.loads(text or "[]")
    except Exception:
        return []
    if not isinstance(raw, list):
        return []

    result: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serial", "")).strip()
        if not serial or serial == UNKNOWN_DEVICE or serial in seen:
            continue
        seen.add(serial)
        result.append({
            "serial": serial,
            "name": sanitize_device_name(item.get("name", "")) or serial,
            "enabled": bool(item.get("enabled", False)),
            "path": str(item.get("path", "")).strip(),
        })
    return result


def format_device_config(config: list[dict]) -> str:
    """把设备配置序列化为注册表用的 JSON 字符串。"""
    return json.dumps(config, ensure_ascii=False)


def parse_device_map_legacy(text: str) -> dict[str, str]:
    """解析 v5 早期的 ``序列号=设备名;...`` 旧格式，仅用于升级迁移。"""
    result: dict[str, str] = {}
    for chunk in str(text or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        serial, _, name = chunk.partition("=")
        serial = serial.strip()
        name = sanitize_device_name(name)
        if serial and name:
            result[serial] = name
    return result


def device_root_for(cfg: dict, main_path: Path) -> Path:
    """算出某台设备的落盘根目录。

    配了独立路径就用它（适合每台相机一个独立库）；否则退到
    ``主导入路径/设备名``，与其他设备并列但不混装。
    """
    own = str(cfg.get("path", "")).strip()
    if own:
        return Path(own)
    return main_path / sanitize_device_name(cfg.get("name", ""))


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


def read_head_bytes(image_path: Path, size: int = SCAN_HEAD_BYTES) -> bytes | None:
    """把文件头一次性读进内存；失败返回 None。

    这是「一次大块读」的落点：相比让 Pillow 直接对着文件对象反复 seek+read，
    一次顺序读只产生 1 次 IO 往返（冷态下每趟约 0.5ms）。
    """
    try:
        with open(image_path, "rb") as f:
            return f.read(size)
    except OSError:
        return None


def _meta_from_buffer(data: bytes, may_truncate: bool):
    """从内存缓冲解析 (序列号, 机型, EXIF 时间, 是否需要直读重试)。"""
    try:
        with warnings.catch_warnings():
            # 缓冲被截断时 Pillow 会对尾部报 Truncated File Read，
            # 这是预期行为（我们只要文件头），不必打扰用户。
            warnings.simplefilter("ignore")
            with Image.open(io.BytesIO(data)) as img:
                exif = img.getexif()
                if not exif:
                    return None, "", None, False
                model = str(exif.get(TAG_MODEL) or "").strip()
                # 取值顺序与历史实现严格一致：主 IFD 的 36867 → 主 IFD 的 306。
                # 注意不要「顺手」改成从 Exif 子 IFD 取 36867 —— JPG 的 36867
                # 在子 IFD 里，改动会改变部分机型的日期来源。
                dt = parse_exif_datetime(exif.get(36867)) or parse_exif_datetime(exif.get(306))
                serial = _to_serial_text(exif.get(TAG_BODY_SERIAL))
                if serial:
                    return serial, model, dt, False
                exif_ifd = exif.get_ifd(TAG_EXIF_IFD) or {}
                makernote = exif_ifd.get(TAG_MAKERNOTE)
                serial = _read_nikon_makernote_serial(makernote)
                # MakerNote 实实在在存在、却读不出序列号，同时缓冲还可能是被
                # 切断的 ⇒ 值得回退直读确认一次（例如将来机型 MakerNote 更大）。
                retry = bool(may_truncate and makernote is not None and not serial)
                return serial, model, dt, retry
    except Exception:
        return None, "", None, may_truncate


def _read_photo_meta_direct(image_path: Path):
    """Pillow 直读文件（历史实现），作为大块读的兜底路径。"""
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            if not exif:
                return None, "", None
            model = str(exif.get(TAG_MODEL) or "").strip()
            dt = parse_exif_datetime(exif.get(36867)) or parse_exif_datetime(exif.get(306))
            serial = _to_serial_text(exif.get(TAG_BODY_SERIAL))
            if not serial:
                exif_ifd = exif.get_ifd(TAG_EXIF_IFD) or {}
                serial = _read_nikon_makernote_serial(exif_ifd.get(TAG_MAKERNOTE))
            return serial, model, dt
    except Exception:
        return None, "", None


def read_photo_meta(image_path: Path):
    """一次读出 (序列号, 机型, EXIF 拍摄时间)。

    先用 SCAN_HEAD_BYTES 大块读把文件头搬进内存再解析，是扫描/导入的
    统一取数入口（历史实现分开调 read_body_serial / get_exif_date /
    read_camera_model，会各自把文件从头解析一遍，read 次数直接翻倍）。

    序列号读不出且缓冲可能被截断时回退直读，保证结果不劣于历史行为。
    """
    data = read_head_bytes(image_path)
    if data is None:
        return _read_photo_meta_direct(image_path)
    serial, model, dt, retry = _meta_from_buffer(data, len(data) >= SCAN_HEAD_BYTES)
    if retry:
        s2, m2, d2 = _read_photo_meta_direct(image_path)
        if s2:
            return s2, m2 or model, d2 or dt
    return serial, model, dt


def get_exif_date(image_path: Path) -> datetime | None:
    """从图像 EXIF 中读取拍摄时间（走大块读快路径）。"""
    return read_photo_meta(image_path)[2]


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
    取数走 read_photo_meta 的大块读快路径。
    """
    return read_photo_meta(image_path)[0]


def read_camera_model(image_path: Path) -> str:
    """读取机型名（如 NIKON D800E），仅用于设备清单展示。"""
    return read_photo_meta(image_path)[1]


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


def collect_image_entries(dcim_root: Path) -> tuple[list, list]:
    """一次遍历 DCIM，按 RAW / JPG 分流返回 ``[(路径, mtime)]``。

    历史实现是连着调两次 collect_images，目录树因此被完整枚举两遍；这里
    合并成一次，并且用 os.scandir 的 DirEntry 判类型（Windows 上目录项已带
    文件属性，省掉每个条目的额外 stat），顺带把 mtime 一起带出来 —— 目录
    采样跳过 EXIF 读取时要靠它估算日期，而它本身不额外产生 IO。
    """
    raw: list = []
    jpg: list = []
    stack = [dcim_root / DCIM_NAME]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in RAW_EXTS:
                            target = raw
                        elif ext in JPG_EXTS:
                            target = jpg
                        else:
                            continue
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            mtime = 0.0
                        target.append((Path(entry.path), mtime))
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue
    return raw, jpg


def collect_images(dcim_root: Path, exts: tuple[str, ...]) -> list[Path]:
    """递归扫描 DCIM 目录下指定扩展名的文件（保留历史签名）。"""
    raw, jpg = collect_image_entries(dcim_root)
    if exts == RAW_EXTS:
        picked = raw
    elif exts == JPG_EXTS:
        picked = jpg
    else:
        picked = raw + jpg
    return [path for path, _ in picked]


def _mtime_dt(mtime: float) -> datetime | None:
    """把 scandir 带出的 mtime 转成 datetime（采样模式下免读 EXIF 的日期兜底）。"""
    if not mtime:
        return None
    try:
        return datetime.fromtimestamp(mtime)
    except (OSError, OverflowError, ValueError):
        return None


def _dir_name_restart(group) -> bool:
    """用文件名序号做零成本预检：这个目录中途换过机身吗？

    相机换机身之后另起序号（实测如 2000 → 0001 这种大幅回退），所以同一目录
    内若出现「序号大幅小于前一个」，说明中途换过机器，不应按「同机」处理。
    判断只看已枚举到的文件名，不产生任何 IO。

    两个要点：
      - **按扩展名分开看**。RAW 与 JPG 各自是一段独立序号，直接拼起来会在
        两段交界处产生一次假回退（实测 3798 → 1529）。
      - **只认大幅回退**。删除照片、重拍、目录项乱序都会造成小回退，实测本卡
        出现过 1708 → 1705（幅度 3），这类噪声必须容忍，否则整个目录会被
        误推进全读、采样彻底失效。
    判不准时宁可返回 True —— 多读几张只是慢一点，但漏判会把别机照片分错目录。
    """
    by_ext: dict = {}
    for path, _ in group:
        found = re.search(r"(\d+)", path.stem)
        if found:
            by_ext.setdefault(path.suffix.lower(), []).append(int(found.group(1)))
    for numbers in by_ext.values():
        if len(numbers) < 3:
            continue
        for prev, cur in zip(numbers, numbers[1:]):
            if cur < prev - DIR_NAME_BACKSTEP:
                return True
    return False


def _scan_one_dir_fully(group, records: dict, tick=None):
    """整目录逐张识别：混合目录，或抽查发现另一个机身时使用。

    已经读过的文件直接复用 records 里的结果，不重复读卡。
    """
    ordered = []
    for path, mtime in group:
        hit = records.get(path)
        if hit is None:
            serial, model, dt = read_photo_meta(path)
            hit = (path, serial or UNKNOWN_DEVICE, model, dt or _mtime_dt(mtime))
            records[path] = hit
            if tick:
                tick()
        ordered.append(hit)
    return ordered


def _scan_one_dir(group, records: dict, tick=None):
    """识别同一目录下的文件，返回 ``(按目录内顺序排列的记录, 是否走了抽样)``。

    记录形如 ``(路径, 序列号, 机型, 日期)``。

    相机写卡时按目录顺序递增，同一个 DCIM 子目录几乎必然出自同一机身，
    所以先逐张确认前 DIR_PROBE_COUNT 张：
      - 前几张就冒出多个机身 ⇒ 判定为混合目录，整目录全读；
      - 锁定单机身 ⇒ 之后每 DIR_SAMPLE_STRIDE 张抽查 1 张，抽查到别的
        机身立刻整目录回退全读。

    进入采样前还有一道零成本预检：文件名序号出现多次回退 ⇒ 中途换过机身，
    直接逐张识别。加上这道预检后，结果与全读一致（本卡实测 100% 成立），
    采样只影响耗时。未抽查到的文件沿用锁定机身的序列号与机型，日期用 mtime
    兜底 —— 实测 mtime 与 EXIF 拍摄时间只差 0~12 秒，不会跨越日期边界。
    """
    total = len(group)
    if total == 0:
        return [], False

    if _dir_name_restart(group):
        return _scan_one_dir_fully(group, records, tick), False

    probe = min(DIR_PROBE_COUNT, total)
    serials = []
    for path, mtime in group[:probe]:
        serial, model, dt = read_photo_meta(path)
        serial = serial or UNKNOWN_DEVICE
        records[path] = (path, serial, model, dt or _mtime_dt(mtime))
        serials.append(serial)
        if tick:
            tick()

    if len(set(serials)) > 1:
        return _scan_one_dir_fully(group, records, tick), False

    locked = serials[0]
    for idx in range(probe, total, DIR_SAMPLE_STRIDE):
        path, mtime = group[idx]
        serial, model, dt = read_photo_meta(path)
        if tick:
            tick()
        if (serial or UNKNOWN_DEVICE) != locked:
            return _scan_one_dir_fully(group, records, tick), False
        records[path] = (path, locked, model, dt or _mtime_dt(mtime))

    locked_model = records[group[0][0]][2]
    ordered = []
    for path, mtime in group:
        hit = records.get(path)
        if hit is None:
            # 被采样跳过：机身已锁定，日期用 mtime 兜底。
            # 这里也要推进进度 —— 跳过的文件同样已经判定了归属，
            # 否则进度条只会走到「已读张数」就停住，看着像没跑完。
            hit = (path, locked, locked_model, _mtime_dt(mtime))
            if tick:
                tick()
        ordered.append(hit)
    return ordered, True


def scan_devices(devices: list[Path], log=None, progress=None) -> dict:
    """扫描存储设备，按机身序列号统计出「哪台机器拍了多少张」。

    返回 {serial|UNKNOWN: {serial, model, count, raw, jpg, dmin, dmax, sample, files}}
    其中 files 为该设备下的全部文件路径，供后续分流直接复用，避免二次扫描。

    progress: 可选 ``callable(done, total)``，用于向 UI 报告已识别张数。
    冷态（刚插卡）下扫描要跑几秒到几十秒，没有进度反馈时用户会以为卡死。
    """
    stats: dict = {}

    def emit(msg: str):
        if log:
            log(msg)

    for dev in devices:
        raw, jpg = collect_image_entries(dev)
        files = raw + jpg
        total = len(files)
        emit(f"[SCAN] {dev} 共 {total} 个文件，正在识别机身...")

        done = [0]

        def tick():
            done[0] += 1
            if progress and (done[0] % 16 == 0 or done[0] >= total):
                progress(done[0], total)

        # 按目录分组：相机写卡按目录顺序递增，同目录几乎必然同机身
        by_dir: dict = {}
        for item in files:
            by_dir.setdefault(item[0].parent, []).append(item)

        for parent in sorted(by_dir, key=lambda p: str(p).lower()):
            group = by_dir[parent]
            records, sampled = _scan_one_dir(group, {}, tick)
            emit(f"        - {parent.name}: {len(group)} 张"
                 f"（{'抽样识别' if sampled else '逐张识别'}）")
            for path, serial, model, dt in records:
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
                if not entry["model"] and model:
                    entry["model"] = model
                if dt:
                    if entry["dmin"] is None or dt < entry["dmin"]:
                        entry["dmin"] = dt
                    if entry["dmax"] is None or dt > entry["dmax"]:
                        entry["dmax"] = dt

        if progress:
            progress(total, total)

    return stats


# ---------------------------------------------------------------------------
# 导入任务
# ---------------------------------------------------------------------------
class ImportSignals(QObject):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    scan_progress = pyqtSignal(int, int)
    finished = pyqtSignal(dict)
    devices = pyqtSignal(dict)


class ScanWorker(QThread):
    """后台扫描卡内设备清单，供「多设备分流」面板展示。"""

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
            stats = scan_devices(devices, log=self.signals.log.emit,
                                 progress=self.signals.scan_progress.emit)
            self.signals.devices.emit(stats)
        except Exception as e:
            self.signals.log.emit(f"[ERROR] 扫描设备失败: {e}")
            self.signals.devices.emit({})


class ImportWorker(QThread):
    """后台执行导入工作。"""

    def __init__(self, import_path: Path, date_format: str, remark: str,
                 excluded_drives: set[str], device_config: list[dict] | None = None,
                 scanned: dict | None = None):
        super().__init__()
        self.signals = ImportSignals()
        self.import_path = import_path
        self.date_format = date_format
        self.remark = remark.strip()
        self.excluded_drives = excluded_drives
        # device_config: 设备配置列表，每项 {serial,name,enabled,path}。
        # 没有任何「已启用」设备时退化为全量导入，与历史版本行为一致。
        self.device_config = device_config or []
        # scanned: 已扫描结果，复用以避免二次读卡
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
        # 有任意一台设备被勾选 → 走分流；否则保持历史的全量行为不变
        if any(c.get("enabled") for c in self.device_config):
            self._run_split()
        else:
            self._run_flat()

    def _scan(self, devices: list[Path]) -> dict:
        """取扫描结果：优先复用外部传入，否则就地扫描。"""
        if self.scanned is not None:
            return self.scanned
        return scan_devices(devices, log=self._log)

    def _run_split(self):
        """按设备配置分流导入。

        落盘根目录（base）后固定接 ``<日期目录>/<raw|jpg>``：
          - 已启用设备 → 该设备自己的路径（留空则 ``<主导入路径>/<设备名>``）
          - 未勾选的设备 → ``<主导入路径>/_他机/<序列号>``（不混进主库，但也不丢）
          - 读不到序列号 → ``<主导入路径>/_未识别``（安全模式，宁可多导）
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
        self._log("[步骤 2/4] 按设备配置分流...")
        stats = self._scan(devices)

        cfg_by_serial = {c["serial"]: c for c in self.device_config}
        enabled_count = sum(1 for c in self.device_config if c.get("enabled"))

        groups = []          # [(落盘根 Path, 设备标签, [文件...])]
        mine_total = 0
        other_total = 0
        unknown_total = 0

        for serial, entry in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
            files = entry["files"]
            cfg = cfg_by_serial.get(serial)
            span = ""
            if entry["dmin"]:
                span = f"，{entry['dmin']:%Y-%m-%d} ~ {entry['dmax']:%Y-%m-%d}"
            detail = (f"{entry['count']} 张 "
                      f"(RAW {entry['raw']} / JPG {entry['jpg']}){span}")

            if serial == UNKNOWN_DEVICE:
                unknown_total += entry["count"]
                label = "未识别"
                base = self.import_path / "_未识别"
                self._log(f"         {label}: {detail}")
                self._log("            └ 无序列号，按安全模式放行（宁可多导，不漏片）")
            elif cfg and cfg.get("enabled"):
                mine_total += entry["count"]
                label = cfg["name"]
                base = device_root_for(cfg, self.import_path)
                self._log(f"         {label}: {detail}")
            elif cfg:
                other_total += entry["count"]
                label = f"{cfg['name']} [{serial}]"
                base = self.import_path / "_他机" / sanitize_device_name(serial)
                self._log(f"         {label}: {detail}")
                self._log("            └ 未勾选「导入此设备的照片」，归入 _他机")
            else:
                other_total += entry["count"]
                label = f"{entry['model'] or '未知机型'} [{serial}]"
                base = self.import_path / "_他机" / sanitize_device_name(serial)
                self._log(f"         {label}: {detail}")
                self._log("            └ 未配置设备，归入 _他机")

            self._log(f"            → {base}")
            groups.append((base, label, files))

        if unknown_total:
            self._log(f"[INFO] {unknown_total} 张读不到序列号，按安全模式归入 _未识别")
        if other_total:
            self._log(f"[INFO] {other_total} 张不属于已启用设备，归入 _他机（未丢弃）")
        if mine_total == 0:
            self._log("[WARNING] 本次没有命中任何已启用设备")
            self._log("         如需导入自己的照片，请在「多设备分流」里勾选对应相机")

        total = sum(len(files) for _, _, files in groups)
        if total == 0:
            self._log("[WARNING] 未找到照片文件")
            return

        # 步骤 3: 导入
        self._log("")
        self._log("[步骤 3/4] 开始导入...")
        self._log(f"[INFO] 分流模式:   多设备（已启用 {enabled_count} 台）")
        self._log(f"[INFO] 日期格式:   {self.date_format}")
        if self.remark:
            self._log(f"[INFO] 备注:       {self.remark}")
        self._log("─" * 64)

        processed = 0
        for base, label, files in groups:
            processed = self._copy_group(label, files, base, processed, total)
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
        processed = self._copy_group("RAW", raw_files, self.import_path, processed, total)
        if self._abort:
            return
        processed = self._copy_group("JPG", jpg_files, self.import_path, processed, total)
        if self._abort:
            return

        self._finish(total)

    def _copy_group(self, label: str, files: list[Path], base: Path,
                    processed: int, total: int) -> int:
        """拷贝一组文件到 ``<base>/<日期目录>/<raw|jpg>``。

        base 是绝对落盘根目录：全量模式下即主导入路径；分流模式下为
        各设备自己的路径，或 _他机/_未识别 目录。raw/jpg 分层由本方法统一补上。
        """
        self._log(f"[{label}] 共 {len(files)} 个文件")
        for src in files:
            if self._abort:
                self._log("[ABORT] 用户取消了操作")
                return processed

            dt = get_file_date(src)
            subdir = "raw" if src.suffix.lower() in RAW_EXTS else "jpg"
            dst_dir = base / self._format_date_dir(dt) / subdir
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
# 主配置窗口（日志集成在主窗口）
# ---------------------------------------------------------------------------
class ConfigWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        # 上半部分可滚动，最低高度可以放宽；默认给一个舒展的初始尺寸
        self.setMinimumSize(780, 720)
        self.resize(820, 1040)

        self.config = RegistryConfig()
        self.settings = self.config.load()

        # 设备配置（每项 {serial,name,enabled,path}）与最近一次扫描结果。
        # 必须在 _build_ui 之前就位：设备下拉框在构建期即会触发选中回调。
        self.device_config = parse_device_config(self.settings.get("device_config", "[]"))
        self._detected: dict = {}
        self._loading = False      # 程序化填充控件时抑制回写
        self._viewed_serial = None  # 上一次已滚动到视野的设备，避免重复跳转

        self._build_ui()
        self._load_settings()

        self._worker = None

    def _build_ui(self):
        # 外层：上半部分可滚动（多设备面板展开后高度会变），日志区固定在底部
        outer_layout = QVBoxLayout(self)
        outer_layout.setSpacing(12)
        outer_layout.setContentsMargins(20, 16, 20, 16)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }\n"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        top_host = QWidget()
        top_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        main_layout = QVBoxLayout(top_host)
        main_layout.setSpacing(12)
        main_layout.setContentsMargins(0, 0, 6, 0)
        self.scroll.setWidget(top_host)
        outer_layout.addWidget(self.scroll, 1)

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

        # 日期格式
        form.addWidget(QLabel("日期格式:"), 0, 0)
        self.combo_date = QComboBox()
        self.combo_date.addItems(list(DATE_FORMATS.keys()))
        self.combo_date.setMinimumWidth(180)
        form.addWidget(self.combo_date, 0, 1, alignment=Qt.AlignLeft)

        # 备注
        form.addWidget(QLabel("备注:"), 1, 0)
        self.edit_remark = QLineEdit()
        self.edit_remark.setPlaceholderText("可选，如：旅行、婚礼；会追加到日期目录名后")
        form.addWidget(self.edit_remark, 1, 1)

        # 排除盘符（使用网格流式布局，避免盘符过多时横向溢出；支持刷新）
        form.addWidget(QLabel("排除盘符（不扫描）:"), 2, 0, alignment=Qt.AlignTop)
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
        form.addLayout(drive_container, 2, 1)

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

        # ------------------------------------------------------------------
        # 多设备分流（常驻主界面，不做独立弹窗）
        # ------------------------------------------------------------------
        dev_card = QFrame()
        dev_card.setStyleSheet("""
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
        dev_layout = QVBoxLayout(dev_card)
        dev_layout.setSpacing(10)
        dev_layout.setContentsMargins(16, 16, 16, 16)

        dev_head = QHBoxLayout()
        dev_title = QLabel("多设备分流")
        dev_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        dev_head.addWidget(dev_title)
        dev_head.addStretch()
        self.btn_scan = QPushButton("🔄 扫描卡内设备")
        self.btn_scan.setStyleSheet("padding: 4px 12px; font-size: 12px;")
        self.btn_scan.setToolTip("读取卡上照片的 EXIF，列出每台相机的序列号、机型与张数")
        self.btn_scan.clicked.connect(self.scan_card_devices)
        dev_head.addWidget(self.btn_scan)
        dev_layout.addLayout(dev_head)

        dev_hint = QLabel(
            "按机身序列号分流：勾选属于你的相机 → 进各自路径；未勾选的归入 _他机；"
            "全不勾选则按归档根目录全量导入。"
        )
        dev_hint.setStyleSheet("color: #666; font-size: 12px;")
        dev_hint.setWordWrap(True)
        dev_layout.addWidget(dev_hint)

        # 归档根目录：全局唯一的路径，负责默认落盘与 _他机/_未识别 归档
        arch_grid = QGridLayout()
        arch_grid.setSpacing(10)
        arch_grid.setColumnStretch(1, 1)
        arch_grid.addWidget(QLabel("归档根目录:"), 0, 0)
        arch_row = QHBoxLayout()
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("例: D:\\D800E")
        self.btn_browse = QPushButton("浏览...")
        self.btn_browse.setFixedWidth(70)
        self.btn_browse.clicked.connect(self.choose_path)
        arch_row.addWidget(self.edit_path)
        arch_row.addWidget(self.btn_browse)
        arch_grid.addLayout(arch_row, 0, 1)
        dev_layout.addLayout(arch_grid)

        arch_note = QLabel(
            "用途：① 无勾选时全量导入 ② 设备未填路径时用「本目录\\设备名」"
            "③ _他机 / _未识别 归档于此。"
        )
        arch_note.setStyleSheet("color: #888; font-size: 12px;")
        arch_note.setWordWrap(True)
        dev_layout.addWidget(arch_note)

        # 设备选择（下拉）
        dev_sel = QHBoxLayout()
        dev_sel.addWidget(QLabel("设备:"))
        self.combo_device = QComboBox()
        self.combo_device.setMinimumWidth(340)
        self.combo_device.currentIndexChanged.connect(self._on_device_selected)
        dev_sel.addWidget(self.combo_device, 1)
        self.btn_forget = QPushButton("移除")
        self.btn_forget.setFixedWidth(70)
        self.btn_forget.setToolTip("从配置中删掉这台设备，之后按未配置处理")
        self.btn_forget.clicked.connect(self.forget_device)
        dev_sel.addWidget(self.btn_forget)
        dev_layout.addLayout(dev_sel)

        # 二级面板：随下拉选择切换内容
        self.dev_panel = QFrame()
        self.dev_panel.setStyleSheet(
            "background: #ffffff; border: 1px solid #ddd; border-radius: 4px;")
        dp = QGridLayout(self.dev_panel)
        dp.setContentsMargins(12, 10, 12, 10)
        dp.setSpacing(8)
        dp.setColumnStretch(1, 1)

        self.chk_dev_enabled = QCheckBox("导入此设备的照片")
        self.chk_dev_enabled.setToolTip(
            "总开关：取消勾选则该设备照片不导入主库，改归入 _他机")
        self.chk_dev_enabled.toggled.connect(self._on_dev_enabled_toggled)
        dp.addWidget(self.chk_dev_enabled, 0, 0, 1, 2)

        dp.addWidget(QLabel("设备名:"), 1, 0)
        self.edit_dev_name = QLineEdit()
        self.edit_dev_name.setPlaceholderText("用作目录名")
        self.edit_dev_name.editingFinished.connect(self._on_dev_field_edited)
        dp.addWidget(self.edit_dev_name, 1, 1)

        dp.addWidget(QLabel("导入路径:"), 2, 0)
        dev_path_row = QHBoxLayout()
        self.edit_dev_path = QLineEdit()
        self.edit_dev_path.setPlaceholderText("留空 = 归档根目录\\设备名")
        self.edit_dev_path.editingFinished.connect(self._on_dev_field_edited)
        self.btn_dev_browse = QPushButton("浏览...")
        self.btn_dev_browse.setFixedWidth(70)
        self.btn_dev_browse.clicked.connect(self.choose_device_path)
        dev_path_row.addWidget(self.edit_dev_path)
        dev_path_row.addWidget(self.btn_dev_browse)
        dp.addLayout(dev_path_row, 2, 1)

        self.lbl_device_facts = QLabel("")
        self.lbl_device_facts.setStyleSheet("color: #888; font-size: 12px;")
        self.lbl_device_facts.setWordWrap(True)
        dp.addWidget(self.lbl_device_facts, 3, 0, 1, 2)

        dev_layout.addWidget(self.dev_panel)

        main_layout.addWidget(dev_card)

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

        # 按钮行 + 日志区脱离滚动区：多设备面板展开时上半区会滚动，
        # 但「开始导入」和日志必须始终可见，不能被顶出视口。
        main_layout = outer_layout
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
        self.log.setMinimumHeight(120)
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
        main_layout.addWidget(self.log, 0)

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
        path = QFileDialog.getExistingDirectory(self, "选择归档根目录")
        if path:
            self.edit_path.setText(path)

    def choose_device_path(self):
        """为当前选中的设备挑一个独立路径。"""
        start = self.edit_dev_path.text().strip() or self.edit_path.text().strip()
        path = QFileDialog.getExistingDirectory(self, "选择该设备的导入目录", start)
        if path:
            self.edit_dev_path.setText(path)
            self._on_dev_field_edited()

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
        self._loading = True
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

        self._loading = False
        self._refresh_device_combo()

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
            "device_config": format_device_config(self.device_config),
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
        # 设备认领属于「身份认定」而非普通设置项：默认设置不清空它，
        # 只把总开关全部关掉，避免误操作后还要重新扫描认领一遍。
        for cfg in self.device_config:
            cfg["enabled"] = False
        self._refresh_device_combo()
        self.status_label.setText("已恢复默认设置（设备认领已保留、开关已关闭）。")

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

        # 把设备面板里可能还没提交的编辑先落进内存配置
        self._on_dev_field_edited()
        self.save_settings_clicked()

        excluded = {
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        }
        date_format = self.combo_date.currentText()
        remark = self.edit_remark.text().strip()

        self._set_busy(True, "正在导入...")
        self.log.clear()
        self.progress.setValue(0)

        enabled = [c for c in self.device_config if c.get("enabled")]
        self.append_log("═" * 64)
        self.append_log(f"  {APP_NAME} {APP_VERSION}")
        self.append_log("═" * 64)
        if enabled:
            names = "、".join(f"{c['name']}[{c['serial']}]" for c in enabled)
            self.append_log(f"[INFO] 模式: 多设备分流（已启用 {len(enabled)} 台: {names}）")
            self.append_log(f"[INFO] 归档根目录: {import_path}")
            self.append_log("[INFO] 未启用设备的照片将归入 _他机，不会混进主库")
        else:
            self.append_log("[INFO] 模式: 全量导入（未启用任何设备）")
            self.append_log(f"[INFO] 目标目录: {import_path}")

        self._worker = ImportWorker(import_path, date_format, remark, excluded,
                                    device_config=self.device_config)
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
        self.btn_scan.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.status_label.setText(status)

    # ------------------------------------------------------------------
    # 多设备分流：扫描 / 认领 / 二级面板
    # ------------------------------------------------------------------
    def _cfg_for(self, serial: str | None, create: bool = False) -> dict | None:
        """取某序列号的配置条目；create=True 时不存在则新建。"""
        if not serial:
            return None
        for cfg in self.device_config:
            if cfg["serial"] == serial:
                return cfg
        if not create:
            return None
        cfg = {"serial": serial, "name": serial, "enabled": False, "path": ""}
        self.device_config.append(cfg)
        return cfg

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

    def _device_serials(self) -> list[str]:
        """下拉框内容：已配置的设备在前，本次扫描到但未配置的在后。"""
        serials = [c["serial"] for c in self.device_config]
        for s in self._detected:
            if s != UNKNOWN_DEVICE and s not in serials:
                serials.append(s)
        return serials

    def _refresh_device_combo(self, keep_serial: str | None = None):
        """重建设备下拉框，尽量保留当前选中项。"""
        current = keep_serial or self.combo_device.currentData()
        cfg_by_serial = {c["serial"]: c for c in self.device_config}
        serials = self._device_serials()

        self.combo_device.blockSignals(True)
        self.combo_device.clear()
        if not serials:
            self.combo_device.addItem("（尚未扫描，点上方「扫描卡内设备」）", None)
        for s in serials:
            cfg = cfg_by_serial.get(s)
            det = self._detected.get(s)
            if cfg:
                mark = "" if cfg.get("enabled") else "（未启用）"
                label = f"{cfg['name']} · {s}{mark}"
            else:
                model = (det or {}).get("model") or "未知机型"
                label = f"{model} · {s}（未配置）"
            self.combo_device.addItem(label, s)
        idx = self.combo_device.findData(current) if current else -1
        self.combo_device.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_device.blockSignals(False)

        self._on_device_selected()

    def _on_device_selected(self):
        """二级面板跟随下拉选择刷新。"""
        serial = self.combo_device.currentData()
        # 没选中设备时二级面板整体隐藏，避免出现无意义的空壳表单
        need_reveal = (
            not self.dev_panel.isVisible() or serial != self._viewed_serial
        )
        self.dev_panel.setVisible(bool(serial))
        self.dev_panel.setEnabled(bool(serial))
        self.btn_forget.setEnabled(self._cfg_for(serial) is not None)
        if not serial:
            self._viewed_serial = None
            self.lbl_device_facts.setText("")
            return
        if need_reveal:
            # 面板在下半区，切换/展开后很可能落在视口之外。
            # 延到下一轮事件循环（等布局算完）再滚进视野，否则用户会以为没反应。
            self._viewed_serial = serial
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(
                self.dev_panel, 0, 16))

        cfg = self._cfg_for(serial)
        det = self._detected.get(serial) or {}

        self._loading = True
        self.chk_dev_enabled.setChecked(bool(cfg and cfg.get("enabled")))
        self.edit_dev_name.setText(
            cfg["name"] if cfg else self._suggest_name(det.get("model", ""), serial))
        self.edit_dev_path.setText(cfg["path"] if cfg else "")
        self._loading = False

        # 占位提示要反映当前归档根目录，避免被误解成「路径必填」
        main = self.edit_path.text().strip() or "归档根目录"
        self.edit_dev_path.setPlaceholderText(
            f"留空 = {main}\\{self.edit_dev_name.text()}")

        facts = [f"序列号 {serial}"]
        if det.get("model"):
            facts.append(det["model"])
        if det:
            span = ""
            if det.get("dmin"):
                span = f"，{det['dmin']:%Y-%m-%d} ~ {det['dmax']:%Y-%m-%d}"
            facts.append(f"本次检测到 {det['count']} 张"
                         f"（RAW {det['raw']} / JPG {det['jpg']}）{span}")
        else:
            facts.append("本次未检测到此设备")
        self.lbl_device_facts.setText(" · ".join(facts))

    def _on_dev_enabled_toggled(self, checked: bool):
        """总开关：勾选即把该设备写进配置并启用。"""
        if self._loading:
            return
        serial = self.combo_device.currentData()
        if not serial:
            return
        cfg = self._cfg_for(serial, create=checked)
        if cfg is None:
            return
        cfg["enabled"] = checked
        # 勾选瞬间把面板里已填的值一起落进配置，避免用户以为白填了
        name = sanitize_device_name(self.edit_dev_name.text())
        if name:
            cfg["name"] = name
        cfg["path"] = self.edit_dev_path.text().strip()
        self._refresh_device_combo(keep_serial=serial)

    def _on_dev_field_edited(self):
        """设备名 / 独立路径改完后写回内存配置。"""
        if self._loading:
            return
        serial = self.combo_device.currentData()
        cfg = self._cfg_for(serial)
        if cfg is None:
            # 未配置的设备先不动，等勾选总开关时再建档
            return
        name = sanitize_device_name(self.edit_dev_name.text())
        if name:
            cfg["name"] = name
        cfg["path"] = self.edit_dev_path.text().strip()
        self._refresh_device_combo(keep_serial=serial)

    def forget_device(self):
        """从配置里移除当前设备，之后按「未配置」处理（归入 _他机）。"""
        serial = self.combo_device.currentData()
        cfg = self._cfg_for(serial)
        if cfg is None:
            return
        if QMessageBox.question(
            self, "移除设备",
            f"要从配置中移除「{cfg['name']}」（序列号 {serial}）吗？\n"
            "移除后它的照片会归入 _他机，可随时重新扫描认领。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return
        name = cfg["name"]
        self.device_config = [c for c in self.device_config if c["serial"] != serial]
        self._refresh_device_combo()
        self.status_label.setText(f"已移除设备「{name}」。")

    def scan_card_devices(self):
        """扫描卡内照片，按机身序列号统计，结果填进下拉框。"""
        excluded = {
            letter for letter, cb in self.check_drives.items() if cb.isChecked()
        }
        self.btn_scan.setEnabled(False)
        self.btn_start.setEnabled(False)
        self.status_label.setText("正在扫描卡内设备...")
        self.append_log("[SCAN] 开始扫描卡内设备...")

        self._scanner = ScanWorker(excluded)
        self._scanner.signals.log.connect(self._on_log)
        self._scanner.signals.scan_progress.connect(self._on_scan_progress)
        self._scanner.signals.devices.connect(self._on_scan_done)
        self._scanner.finished.connect(self._scanner.deleteLater)
        self._scanner.start()

    def _on_scan_progress(self, done: int, total: int):
        """扫描期间持续刷新状态栏 —— 冷态（刚插卡）下要跑几秒到几十秒。"""
        if total:
            self.status_label.setText(f"正在扫描卡内设备... 已识别 {done}/{total}")

    def _on_scan_done(self, stats: dict):
        self.btn_scan.setEnabled(True)
        self.btn_start.setEnabled(True)
        self._detected = stats or {}

        if not self._detected:
            self.status_label.setText("未检测到设备。")
            self.append_log("[WARNING] 未检测到含 DCIM 目录的设备。")
            self._refresh_device_combo()
            QMessageBox.information(
                self, "未找到设备",
                "没有检测到含 DCIM 目录的存储设备。\n"
                "请确认已插入存储卡，并检查「排除盘符」设置。"
            )
            return

        # 新扫描到的设备先按机型给个建议名建档（默认不启用），
        # 这样下拉框里能直接看懂是哪台，勾一下就能用。
        for serial, entry in self._detected.items():
            if serial == UNKNOWN_DEVICE or self._cfg_for(serial):
                continue
            model = entry.get("model", "")
            if model:
                self.device_config.append({
                    "serial": serial,
                    "name": self._suggest_name(model, serial),
                    "enabled": False,
                    "path": "",
                })

        self._refresh_device_combo()
        # 立刻落盘，避免用户忘了点「保存设置」而丢掉这次认领结果
        self.config.save(self._collect_settings())

        self.status_label.setText(
            f"扫描完成，检测到 {len(self._detected)} 组设备，请勾选要导入的相机。")
        self.append_log(
            f"[SCAN] 完成，共 {len(self._detected)} 组设备，请勾选要导入的相机。")

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
