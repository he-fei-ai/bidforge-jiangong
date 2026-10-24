# -*- coding: utf-8 -*-
"""读取 Electron asar 包的清单（不解压），用于定位前端源码与工具调用逻辑。

asar 布局：
  [UInt32LE a] a = sizeof(UInt32 b)  -> 通常是 4
  [UInt32LE b] b = sizeof(UInt32 c)  -> 通常是 4
  [UInt32LE c] c = header 字符串长度
  [UInt32LE d] d = header 对象长度（已 4 字节对齐）
  header 字符串（JSON）
  文件数据区（偏移基准 = 12 + d）
"""
import json
import struct
import sys
from pathlib import Path


def read_asar_entries(asar_path):
    p = Path(asar_path)
    data = p.read_bytes()
    a = struct.unpack_from("<I", data, 0)[0]
    b = struct.unpack_from("<I", data, a)[0]
    c = struct.unpack_from("<I", data, a + b)[0]
    d = struct.unpack_from("<I", data, a + b + 4)[0]
    header_off = a + b + 8
    header_str = data[header_off:header_off + c].decode("utf-8")
    header = json.loads(header_str)
    data_off = header_off + d
    return header, data_off, len(data)


def walk(node, base, out):
    """遍历 header 里的 files 树，收集每个文件的大小与偏移。"""
    for name, child in sorted(node.get("files", {}).items()):
        path = f"{base}/{name}" if base else name
        if "files" in child:
            walk(child, path, out)
        elif "size" in child:
            out.append((child["size"], path, int(child["offset"])))
    return out


def main():
    asar_path = sys.argv[1]
    header, data_off, total = read_asar_entries(asar_path)
    entries = walk(header, "", [])
    print("# " + asar_path)
    print("# asar 总字节 %d  数据区起点 %d  条目数 %d" % (total, data_off, len(entries)))
    pattern = sys.argv[2].lower() if len(sys.argv) > 2 else ""
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    hits = [e for e in entries if pattern in e[1].lower()] if pattern else entries
    hits.sort(key=lambda e: (-e[0], e[1]))
    for size, path, off in hits[:limit]:
        print("%10d  %s" % (size, path))
    if len(hits) > limit:
        print("... 另有 %d 条未列出" % (len(hits) - limit))


if __name__ == "__main__":
    main()
