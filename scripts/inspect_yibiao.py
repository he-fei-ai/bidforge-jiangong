# -*- coding: utf-8 -*-
"""易标 Yibiao 安装目录结构探测脚本（第一轮：只读，不做任何修改）。

用途：
  1. 判断 resources\\resources.pak 的文件格式（Chromium pak / zip / 其他）
  2. 扫描 易标.exe 中的框架指纹（Electron / CEF / 是否直接内嵌 JS）
  3. 提取 exe 内的 ASCII 字符串，用于定位前端源码与 AI 服务模块

设计约束：
  - 纯只读：仅用 read_bytes / read 读取，绝不写入源目录。
  - 所有输出为 UTF-8，供 read_files 直接查看。
  - 不依赖第三方库，Python 3.8+ 标准库即可。
"""

import os
import re
import struct
import sys
from pathlib import Path

ROOT = Path(r"J:\编程\易标 - 招投标 AI 软件 - Yibiao 2.30.20-win-x64")


def list_files(base: Path, limit: int = 5000):
    """递归列出目录内文件，返回 (相对路径, 字节数) 列表。"""
    rows = []
    for dirpath, dirnames, filenames in os.walk(base):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                size = fp.stat().st_size
            except OSError:
                size = -1
            rows.append((str(fp.relative_to(base)), size))
        if len(rows) >= limit:
            break
    return rows


def probe_pak(pak: Path) -> str:
    """判断 resources.pak 的封装格式并给出头部信息。"""
    try:
        data = pak.read_bytes()
    except OSError as e:
        return "[read error] %s" % e
    n = len(data)
    lines = ["size = %d bytes" % n,
             "head hex = " + data[:64].hex(" ")]
    if data[:4] == b"PK\x03\x04":
        lines.append("=> ZIP container")
        return "\n".join(lines)
    if data[:4] == b"asar":
        lines.append("=> asar container")
        return "\n".join(lines)
    if n >= 32:
        magic = data[0:8]
        try:
            ver = struct.unpack_from("<q", data, 8)[0]
            offset = struct.unpack_from("<q", data, 16)[0]
            count = struct.unpack_from("<q", data, 24)[0]
            lines.append("pak magic=%r ver=%s offset=%s count=%s"
                         % (magic, ver, offset, count))
            if magic == b"Cr24Pack" and count > 0 and offset > 0:
                lines.append("=> Chromium pak (Cr24Pack layout)")
        except Exception as e:
            lines.append("pak parse failed: %s" % e)
    printable = sum(1 for b in data[:4096] if 0x20 <= b < 0x7F)
    lines.append("printable ratio first 4KB = %.2f%%" % (printable * 100.0 / 4096))
    lines.append("first 256 bytes repr = " + repr(data[:256]))
    return "\n".join(lines)
