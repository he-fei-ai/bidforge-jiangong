# -*- coding: utf-8 -*-
"""R122: 用原导出预设为 43b4612e（室外汽车吊吊装施工专项方案）重新生成 DOCX。

背景：WPS 打开 19:36 的导出件报「文字程序意外中断」——根因是 docx_math
空 OMML 参数元素（已修复）。本脚本用用户原本使用的默认预设「标准」
（config_hash=971e9f89，与 export_cache 记录一致）走完整导出管线重出文件，
并对产物跑空 OMML 复扫，证明崩溃诱因已消除。
"""
import asyncio
import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(r"j:\编程\专项方案工具箱\backend")
sys.path.insert(0, str(ROOT))

SCHEME_ID = "43b4612e-afda-4200-b70d-ba8c79c96367"
PROJECT_ID = "18b97cc8-3064-4f5e-b1f1-de7ba39bc8b7"
DB = ROOT / "data" / "scheme_assistant.db"
OUT_DESKTOP = Path(r"C:\Users\Administrator\Desktop")


def load_preset_config() -> dict:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT name, config_json, is_default FROM export_presets WHERE project_id=?",
        (PROJECT_ID,)).fetchall()
    con.close()
    for name, cj, dflt in rows:
        cfg = json.loads(cj) if cj else {}
        import hashlib
        h = hashlib.md5(json.dumps(
            {k: v for k, v in cfg.items()
             if not k.startswith("_") and v not in (None, "")},
            sort_keys=True).encode()).hexdigest()
        if h.startswith("971e9f89"):
            print(f"[预设命中] {name!r} default={dflt} hash={h[:8]}")
            return cfg
    raise SystemExit("未找到 config_hash=971e9f89 的预设")


def scan_docx(path: Path) -> int:
    """复扫产物：返回空 OMML 参数元素计数。"""
    import re
    from lxml import etree
    M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    with zipfile.ZipFile(path) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    bad = 0
    for tag in ("num", "den", "e", "deg"):
        for el in root.iter(f"{{{M}}}{tag}"):
            if len(el) == 0:
                bad += 1
    return bad


async def main():
    from app.routers.export import export_docx
    from app.db import get_conn

    cfg = load_preset_config()
    con = sqlite3.connect(DB)
    proj = con.execute("SELECT name FROM projects WHERE id=?", (PROJECT_ID,)).fetchone()
    con.close()

    body = {
        "project_name": proj[0] if proj else "",
        "bid_section": "",
        "config": cfg,
        "chart_images": [],
        "fe_codes": [],
    }
    conn = await get_conn()
    resp = await export_docx(SCHEME_ID, body, db=conn)
    out = Path(resp.path)
    print(f"[导出完成] {out}  ({out.stat().st_size} bytes)")

    bad = scan_docx(out)
    print(f"[OMML 复扫] 空参数元素 = {bad}")
    assert bad == 0, "产物仍含空 OMML 参数元素！"

    # 统计公式保留情况
    from lxml import etree
    M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    with zipfile.ZipFile(out) as zf:
        root = etree.fromstring(zf.read("word/document.xml"))
    n_omath = len(list(root.iter(f"{{{M}}}oMath")))
    n_frac = len(list(root.iter(f"{{{M}}}f")))
    print(f"[公式统计] oMath={n_omath}  分式={n_frac}")

    dest = OUT_DESKTOP / f"室外汽车吊吊装施工专项方案_20261021_修复版.docx"
    shutil.copy2(out, dest)
    print(f"[桌面副本] {dest}")
    return dest


if __name__ == "__main__":
    asyncio.run(main())
