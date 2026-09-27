#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多设备分流功能验证：用真实 NEF/JPG 夹具跑完整分流路径。

不依赖 GUI 事件循环，直接驱动 ImportWorker 的核心拷贝逻辑。
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.argv = ["verify"]

import PhotoImportTool as pit  # noqa: E402

LIB = Path(r"D:\D800E\2026")
FIXTURES = {
    "mine_jpg": LIB / r"0926_外景\jpg\XYL_4880.JPG",      # SN 9004185
    "mine_nef": LIB / r"0926_外景\raw\XYL_4880.NEF",      # SN 9004185
    "other_nef": LIB / r"0726_漫展\raw\DSC_4289.NEF",     # SN 8105412
    "other_jpg": LIB / r"0726_漫展\raw\DSC_4289.JPG",     # SN 8105412
    "noser": LIB / r"已修\2026-0620_导出\1_XYL3877.jpg",  # 无 SN
}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))


def main():
    missing = [k for k, p in FIXTURES.items() if not p.exists()]
    if missing:
        print(f"夹具缺失: {missing}")
        return 1

    print("=" * 66)
    print("1. 序列号读取")
    print("=" * 66)
    sn = {k: pit.read_body_serial(p) for k, p in FIXTURES.items()}
    check("本机 JPG 序列号 = 9004185", sn["mine_jpg"] == "9004185", sn["mine_jpg"])
    check("本机 NEF 序列号 = 9004185", sn["mine_nef"] == "9004185", sn["mine_nef"])
    check("他机 NEF 序列号 = 8105412", sn["other_nef"] == "8105412", sn["other_nef"])
    check("他机 JPG 序列号 = 8105412", sn["other_jpg"] == "8105412", sn["other_jpg"])
    check("无序列号图返回 None（安全模式）", sn["noser"] is None, repr(sn["noser"]))

    print()
    print("=" * 66)
    print("2. 设备名清理（防路径注入 / 非法字符）")
    print("=" * 66)
    check("剥离路径分隔符", pit.sanitize_device_name("a/b\\c") == "a_b_c")
    check("剥离冒号等非法字符", pit.sanitize_device_name('a:b*c?d"e') == "a_b_c_d_e")
    check("剥离首尾点号", pit.sanitize_device_name("...cam...") == "cam")
    check("空名返回空", pit.sanitize_device_name("   ") == "")

    print()
    print("=" * 66)
    print("3. 白名单序列化往返")
    print("=" * 66)
    m = {"9004185": "D800E", "1234567": "Z6II"}
    text = pit.format_device_map(m)
    check("序列化后再解析一致", pit.parse_device_map(text) == m, text)
    check("空串解析为空字典", pit.parse_device_map("") == {})
    check("垃圾串被忽略", pit.parse_device_map(";;garbage;=noname;999=ok") == {"999": "ok"})
    check("拒绝 UNKNOWN 键", pit.parse_device_map("UNKNOWN=x") == {})

    print()
    print("=" * 66)
    print("4. 端到端分流拷贝")
    print("=" * 66)

    tmp = Path(tempfile.mkdtemp(prefix="split_verify_"))
    try:
        # 构造一个假卡：DCIM/100TEST/ 下放 5 个真实夹具
        card = tmp / "card"
        dcim = card / pit.DCIM_NAME / "100TEST"
        dcim.mkdir(parents=True)
        for key, src in FIXTURES.items():
            shutil.copy2(src, dcim / src.name)

        out = tmp / "out"
        out.mkdir()

        stats = pit.scan_devices([card])
        print(f"  扫描到 {len(stats)} 台设备:")
        for s, e in stats.items():
            print(f"    {s}: {e['count']} 张, 机型={e['model']!r}")
        check("扫出 3 组（本机/他机/未识别）", len(stats) == 3, str(list(stats)))
        check("本机组 2 张", stats.get("9004185", {}).get("count") == 2)
        check("他机组 2 张", stats.get("8105412", {}).get("count") == 2)
        check("未识别组 1 张", stats.get(pit.UNKNOWN_DEVICE, {}).get("count") == 1)

        worker = pit.ImportWorker(
            out, "YYYY/MMDD", "测试", set(),
            device_map={"9004185": "D800E"},
            scanned=stats,
        )
        real_find = pit.find_dcim_devices
        pit.find_dcim_devices = lambda excluded: [card]
        try:
            worker._run_split()                   # 直接跑核心逻辑，不触发 Qt 信号
        finally:
            pit.find_dcim_devices = real_find

        print(f"  目标树 ({out}):")
        found = []
        for p in sorted(out.rglob("*")):
            if p.is_file():
                rel = p.relative_to(out)
                found.append(str(rel))
                print(f"    {rel}")

        joined = "\n".join(found)
        check("本机照片进 D800E/", "D800E" in joined)
        check("他机照片进 _他机/8105412/", "_他机" in joined and "8105412" in joined)
        check("未识别照片进 _未识别/", "_未识别" in joined)
        check("保留 raw/jpg 分层", "\\raw\\" in joined and "\\jpg\\" in joined)
        check("他机文件未混入本机目录",
              not any("D800E" in f and "DSC_" in f for f in found))
        check("共计 5 个文件落盘", len(found) == 5, str(len(found)))

        print()
        print("=" * 66)
        print("5. 向后兼容：device_map 为空时走全量模式")
        print("=" * 66)
        out2 = tmp / "out2"
        out2.mkdir()
        # 屏蔽盘符探测：本机 Y:/Z: 是失效网络盘，探测会挂起 28s
        real_find = pit.find_dcim_devices
        pit.find_dcim_devices = lambda excluded: [card]
        try:
            w2 = pit.ImportWorker(out2, "YYYY/MMDD", "测试", set(), scanned=stats)
            w2._run_flat()
        finally:
            pit.find_dcim_devices = real_find
        found2 = [str(p.relative_to(out2)) for p in sorted(out2.rglob("*")) if p.is_file()]
        check("全量模式落盘 5 个文件", len(found2) == 5, str(len(found2)))
        check("全量模式无设备分层",
              not any("D800E" in f or "_他机" in f for f in found2))
        check("全量模式结构为 日期/raw|jpg",
              all(("\\raw\\" in f or "\\jpg\\" in f) for f in found2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 66)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 66)
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
