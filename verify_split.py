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


def run_split(out, config, stats, card, use_run=False):
    """在屏蔽盘符探测的前提下跑一次分流。"""
    worker = pit.ImportWorker(out, "YYYY/MMDD", "测试", set(),
                              device_config=config, scanned=stats)
    real = pit.find_dcim_devices
    pit.find_dcim_devices = lambda excluded: [card]
    try:
        worker._run() if use_run else worker._run_split()
    finally:
        pit.find_dcim_devices = real


def main():
    missing = [k for k, p in FIXTURES.items() if not p.exists()]
    if missing:
        print(f"夹具缺失: {missing}")
        return 1

    print("=" * 68)
    print("1. 序列号读取")
    print("=" * 68)
    sn = {k: pit.read_body_serial(p) for k, p in FIXTURES.items()}
    check("本机 JPG = 9004185", sn["mine_jpg"] == "9004185", str(sn["mine_jpg"]))
    check("本机 NEF = 9004185", sn["mine_nef"] == "9004185", str(sn["mine_nef"]))
    check("他机 NEF = 8105412", sn["other_nef"] == "8105412", str(sn["other_nef"]))
    check("他机 JPG = 8105412", sn["other_jpg"] == "8105412", str(sn["other_jpg"]))
    check("无序列号返回 None（安全模式）", sn["noser"] is None, repr(sn["noser"]))

    print()
    print("=" * 68)
    print("2. 设备名清理（防路径注入）")
    print("=" * 68)
    check("剥离路径分隔符", pit.sanitize_device_name("a/b\\c") == "a_b_c")
    check("剥离非法字符", pit.sanitize_device_name('a:b*c?d"e') == "a_b_c_d_e")
    check("剥离首尾点号", pit.sanitize_device_name("...cam...") == "cam")
    check("空名返回空", pit.sanitize_device_name("   ") == "")

    print()
    print("=" * 68)
    print("3. 设备配置 JSON 往返")
    print("=" * 68)
    cfg = [
        {"serial": "9004185", "name": "D800E", "enabled": True, "path": r"D:\D800E"},
        {"serial": "8105412", "name": "Z-6_2", "enabled": False, "path": ""},
    ]
    text = pit.format_device_config(cfg)
    check("往返一致", pit.parse_device_config(text) == cfg, text[:44])
    check("损坏 JSON → 空列表", pit.parse_device_config("{not json") == [])
    check("非列表 → 空列表", pit.parse_device_config('{"a":1}') == [])
    check("空串 → 空列表", pit.parse_device_config("") == [])
    check("拒绝 UNKNOWN 序列号",
          pit.parse_device_config('[{"serial":"UNKNOWN","name":"x"}]') == [])
    check("重复序列号只留首个", len(pit.parse_device_config(
        '[{"serial":"1","name":"a"},{"serial":"1","name":"b"}]')) == 1)
    check("缺 enabled 默认关闭",
          pit.parse_device_config('[{"serial":"1","name":"a"}]')[0]["enabled"] is False)
    check("name 非法字符被清理",
          pit.parse_device_config('[{"serial":"1","name":"a/b"}]')[0]["name"] == "a_b")
    check("名字为空时回退成序列号",
          pit.parse_device_config('[{"serial":"777","name":"  "}]')[0]["name"] == "777")

    print()
    print("=" * 68)
    print("4. 落盘根目录解析 device_root_for")
    print("=" * 68)
    main_path = Path(r"D:\Lib")
    check("有独立路径 → 用它",
          pit.device_root_for({"name": "A", "path": r"E:\A"}, main_path) == Path(r"E:\A"))
    check("无独立路径 → 归档根目录\\设备名",
          pit.device_root_for({"name": "D800E", "path": ""}, main_path) == main_path / "D800E")
    check("路径空白视为未填",
          pit.device_root_for({"name": "A", "path": "   "}, main_path) == main_path / "A")

    tmp = Path(tempfile.mkdtemp(prefix="split_verify_"))
    try:
        # 构造假卡：DCIM/100TEST/ 下放 5 个真实夹具
        card = tmp / "card"
        dcim = card / pit.DCIM_NAME / "100TEST"
        dcim.mkdir(parents=True)
        for key, src in FIXTURES.items():
            shutil.copy2(src, dcim / src.name)

        print()
        print("=" * 68)
        print("5. 扫描分组")
        print("=" * 68)
        stats = pit.scan_devices([card])
        for s, e in sorted(stats.items(), key=lambda kv: -kv[1]["count"]):
            print(f"    {s}: {e['count']} 张, 机型={e['model']!r}")
        check("扫出 3 组（本机/他机/未识别）", len(stats) == 3, str(list(stats)))
        check("本机组 2 张", stats.get("9004185", {}).get("count") == 2)
        check("他机组 2 张", stats.get("8105412", {}).get("count") == 2)
        check("未识别组 1 张", stats.get(pit.UNKNOWN_DEVICE, {}).get("count") == 1)

        print()
        print("=" * 68)
        print("6. 分流：独立路径 + 未勾选 + 无序列号")
        print("=" * 68)
        out = tmp / "out"
        out.mkdir()
        own = tmp / "own_d800e"          # 本机的独立路径
        config = [
            {"serial": "9004185", "name": "D800E", "enabled": True, "path": str(own)},
            {"serial": "8105412", "name": "Z-6_2", "enabled": False, "path": ""},
        ]
        run_split(out, config, stats, card)

        print(f"  本机独立路径 {own}:")
        for p in sorted(own.rglob("*")):
            if p.is_file():
                print(f"    {p.relative_to(own)}")
        print(f"  归档根目录 {out}:")
        arch = [str(p.relative_to(out)) for p in sorted(out.rglob("*")) if p.is_file()]
        for f in arch:
            print(f"    {f}")

        own_files = [str(p.relative_to(own)) for p in sorted(own.rglob("*")) if p.is_file()]
        joined_own = "\n".join(own_files)
        joined_arch = "\n".join(arch)

        check("本机照片进独立路径", len(own_files) == 2, str(len(own_files)))
        check("本机路径含 raw/jpg 分层", "\\raw\\" in joined_own and "\\jpg\\" in joined_own)
        check("本机路径不含设备名子目录", "D800E" not in joined_own)
        check("未勾选设备进 _他机\\8105412",
              "_他机" in joined_arch and "8105412" in joined_arch)
        check("无序列号进 _未识别", "_未识别" in joined_arch)
        check("归档根目录只有他机与未识别", len(arch) == 3, str(len(arch)))
        check("他机文件未混入本机路径", not any("DSC_" in f for f in own_files))

        print()
        print("=" * 68)
        print("7. 分流：设备未填路径 → 归档根目录\\设备名")
        print("=" * 68)
        out2 = tmp / "out2"
        out2.mkdir()
        config2 = [{"serial": "9004185", "name": "D800E", "enabled": True, "path": ""}]
        run_split(out2, config2, stats, card)
        f2 = [str(p.relative_to(out2)) for p in sorted(out2.rglob("*")) if p.is_file()]
        for f in f2:
            print(f"    {f}")
        joined2 = "\n".join(f2)
        check("落到 归档根目录\\D800E", any("D800E" in f for f in f2))
        check("未勾选的他机仍进 _他机", "_他机" in joined2)
        check("未识别仍进 _未识别", "_未识别" in joined2)
        check("共 5 个文件", len(f2) == 5, str(len(f2)))

        print()
        print("=" * 68)
        print("8. 未启用任何设备 → 回落全量模式")
        print("=" * 68)
        out3 = tmp / "out3"
        out3.mkdir()
        run_split(out3, [], stats, card, use_run=True)
        f3 = [str(p.relative_to(out3)) for p in sorted(out3.rglob("*")) if p.is_file()]
        check("全量模式 5 个文件", len(f3) == 5, str(len(f3)))
        check("无设备分层，直接 日期/raw|jpg",
              all(("\\raw\\" in f or "\\jpg\\" in f) for f in f3)
              and not any("D800E" in f or "_他机" in f for f in f3))

        print()
        print("=" * 68)
        print("9. 设备存在但全部关闭 → 同样回落全量")
        print("=" * 68)
        out4 = tmp / "out4"
        out4.mkdir()
        run_split(out4, [{"serial": "9004185", "name": "D800E",
                          "enabled": False, "path": ""}], stats, card, use_run=True)
        f4 = [str(p.relative_to(out4)) for p in sorted(out4.rglob("*")) if p.is_file()]
        check("全关时走全量（5 个文件）", len(f4) == 5, str(len(f4)))

        print()
        print("=" * 68)
        print("10. EXIF 快路径：与历史直读对拍 + 目录换机预检")
        print("=" * 68)
        check("快路径与直读结果逐项一致（5 个真实夹具）",
              all(pit.read_photo_meta(p) == pit._read_photo_meta_direct(p)
                  for p in FIXTURES.values()))
        check("大块读窗口 >= 256KB（NEF MakerNote 实测下限）",
              pit.SCAN_HEAD_BYTES >= 256 * 1024, str(pit.SCAN_HEAD_BYTES))

        def fake_group(names):
            # _dir_name_restart 只看文件名，不做 IO，路径不必真实存在
            return [(dcim / n, 0.0) for n in names]

        check("序号单调递增 → 不判为换机",
              not pit._dir_name_restart(fake_group(
                  [f"XYL_{i}.JPG" for i in range(4900, 4913)])))
        check("序号大幅回退（换机重启）→ 判为换机",
              pit._dir_name_restart(fake_group(
                  [f"XYL_{i}.JPG" for i in range(4900, 4908)]
                  + ["XYL_0001.JPG", "XYL_0002.JPG"])))
        check("序号小回退（删除/重拍）→ 不误判",
              not pit._dir_name_restart(fake_group(
                  ["XYL_1706.JPG", "XYL_1707.JPG", "XYL_1708.JPG",
                   "XYL_1705.JPG", "XYL_1709.JPG"])))
        check("RAW/JPG 分段交界 → 不误判",
              not pit._dir_name_restart(fake_group(
                  [f"DSC_{i}.NEF" for i in range(1529, 1560)]
                  + [f"DSC_{i}.JPG" for i in range(1529, 1560)])))

        print()
        print("=" * 68)
        print("11. 目录采样：同机目录抽样识别，且与全读等价")
        print("=" * 68)
        pure = tmp / "card_pure"
        pdir = pure / pit.DCIM_NAME / "100PURE"
        pdir.mkdir(parents=True)
        for i in range(13):
            shutil.copy2(FIXTURES["mine_jpg"], pdir / f"XYL_{4900 + i}.JPG")

        reads = {"n": 0}
        real_meta = pit.read_photo_meta

        def counting_meta(path):
            reads["n"] += 1
            return real_meta(path)

        pit.read_photo_meta = counting_meta
        try:
            s_sample = pit.scan_devices([pure])
        finally:
            pit.read_photo_meta = real_meta
        sampled_reads = reads["n"]

        saved_probe = pit.DIR_PROBE_COUNT
        pit.DIR_PROBE_COUNT = 10 ** 9       # 强制逐张，等价于全读
        try:
            s_full = pit.scan_devices([pure])
        finally:
            pit.DIR_PROBE_COUNT = saved_probe

        print(f"    采样读取 {sampled_reads}/13 张；计数 采样="
              f"{s_sample.get('9004185', {}).get('count')} / 全读="
              f"{s_full.get('9004185', {}).get('count')}")
        check("同机目录 13 张全部归同一机身",
              s_sample.get("9004185", {}).get("count") == 13)
        check("抽样确实生效（读取张数 < 13）", sampled_reads < 13, str(sampled_reads))
        check("采样与全读的计数一致",
              s_sample.get("9004185", {}).get("count")
              == s_full.get("9004185", {}).get("count"))
        check("采样与全读的机型一致",
              s_sample.get("9004185", {}).get("model")
              == s_full.get("9004185", {}).get("model"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 68)
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    print("=" * 68)
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("全部断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
