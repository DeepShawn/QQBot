#!/usr/bin/env python3
"""把 aapt2 输出的 base.apk + classes.dex + assets 重新打包为 4 字节对齐的 APK（简易 zipalign）。

用法: python3 repack.py base.apk classes.dex assets_dir out.apk
"""
from __future__ import annotations

import struct
import sys
import zipfile
import zlib
from pathlib import Path


def read_entries(path: Path):
    entries = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.filename.endswith("/"):
                continue
            data = z.read(info.filename)
            entries.append((info.filename, data, info.compress_type == zipfile.ZIP_DEFLATED))
    return entries


def write_zip(entries, out_path: Path):
    """entries: [(name, data, compress)]，所有条目数据 4 字节对齐。"""
    local = bytearray()
    central = []
    for name, data, compress in entries:
        if name == "resources.arsc":
            compress = False  # Android 11+：resources.arsc 必须未压缩
        name_b = name.encode("utf-8")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        if compress:
            co = zlib.compressobj(6, zlib.DEFLATED, -15)
            cdata = co.compress(data) + co.flush()
            method = 8
        else:
            cdata = data
            method = 0

        # 局部头固定 30 字节 + 文件名；先用 4 字节 extra 头，再补 pad 让数据 4 字节对齐
        header_len = 30 + len(name_b)
        pad = (-(len(local) + header_len + 4)) % 4
        extra = struct.pack("<HH", 0x0000, pad) + b"\x00" * pad if pad else struct.pack("<HH", 0x0000, 0)

        local_header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50, 20, 0, method, 0, 0x21,
            crc, len(cdata), len(data), len(name_b), len(extra),
        )
        offset = len(local)
        local += local_header + name_b + extra + cdata
        central.append((name_b, method, crc, len(cdata), len(data), offset))

    cd = bytearray()
    for name_b, method, crc, csize, usize, offset in central:
        cd += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 20, 20, 0, method, 0, 0x21,
            crc, csize, usize, len(name_b), 0, 0, 0, 0, 0, offset,
        ) + name_b

    eocd = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(central), len(central), len(cd), len(local), 0
    )
    out_path.write_bytes(bytes(local) + bytes(cd) + eocd)


def main():
    base, dex, assets_dir, out = sys.argv[1:5]
    entries = read_entries(Path(base))
    entries.append(("classes.dex", Path(dex).read_bytes(), False))
    ad = Path(assets_dir)
    if ad.is_dir():
        for p in sorted(ad.rglob("*")):
            if p.is_file():
                rel = p.relative_to(ad).as_posix()
                entries.append((f"assets/{rel}", p.read_bytes(), True))
    write_zip(entries, Path(out))
    print(f"[repack] {len(entries)} 个条目 -> {out} ({Path(out).stat().st_size} bytes)")


if __name__ == "__main__":
    main()
