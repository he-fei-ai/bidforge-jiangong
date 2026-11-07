"""R76b · G2 —— 历史存量正文的围栏缺陷**只读**盘点（零回填、零推断）。

背景：R76 把围栏闭合判据收敛到 `fence_common` 并在**写入前**补齐长度错配，
但已落库的历史正文不做自动回填（无法区分「人工改值」与「被误吞的正文」，
且回填本身就是新的写路径）。本脚本只做一件事：把存量按判据分类计数，
让「要不要手工修」成为有数据的决策项。

⚠️ 只读纪律（护栏 `tests/test_r76b_followup_guards_20261014.py` G2 组锁住）：
  · 连接必须以 sqlite URI `mode=ro` 打开；
  · 源码里不得出现任何写语句 / `execute(` 之外的 DML / `commit`；
  · 不得 import 任何会打开写连接的路径（`app.db` / `get_conn`）。
  目的：防止这份脚本被后续会话"顺手"改成推断回填工具。

盘点口径（全部经 `fence_common` 唯一事实源，不自写判据）：
  A `kind == ""`                —— 无缺陷
  B `kind == "length_mismatch"` —— 闭合标记长度错配（R76 后新写入不会再产生）
  C `kind == "unclosed"`        —— 真未闭合，且**尾部像散文**（正文被困块内的形态）
  D 块内含带信息串的"伪闭合"行 —— BUG-F2 的历史受害者形态
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.services import fence_common as fc  # noqa: E402

DB = BACKEND / "data" / "scheme_assistant.db"


def main() -> int:
    if not DB.exists():
        print(f"DB_NOT_FOUND {DB}")
        return 2
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sections)")}
        if "content" not in cols:
            print("NO_CONTENT_COLUMN", sorted(cols))
            return 2
        rows = conn.execute(
            "SELECT id, scheme_id, title, content FROM sections "
            "WHERE content IS NOT NULL AND content <> '' ORDER BY id"
        ).fetchall()

        counts = {"total": len(rows), "A_clean": 0, "B_length_mismatch": 0,
                  "C_unclosed": 0, "C_unclosed_prose_tail": 0, "D_labeled_pseudo_close": 0}
        samples: list[str] = []
        for r in rows:
            content = r["content"] or ""
            if "`" not in content and "~" not in content:
                counts["A_clean"] += 1
                continue
            kind = fc.classify_fence_defects(content)["kind"]
            if kind == "":
                counts["A_clean"] += 1
            elif kind == "length_mismatch":
                counts["B_length_mismatch"] += 1
            elif kind == "unclosed":
                counts["C_unclosed"] += 1
                # 未闭合块的最后一行像散文 → 「正文被困在代码块里」的实际形态
                blocks = fc.iter_all_blocks(content)
                tail_line = ""
                if blocks:
                    b = blocks[-1]
                    stop = b["close_line"] if b["close_line"] >= 0 else len(content.split("\n")) - 1
                    lines = content.split("\n")
                    tail_line = lines[min(stop, len(lines) - 1)]
                if fc.looks_like_prose(tail_line):
                    counts["C_unclosed_prose_tail"] += 1
                    if len(samples) < 10:
                        samples.append(f"  section_id={r['id']} scheme={r['scheme_id']} "
                                       f"title={(r['title'] or '')[:24]!r} tail={tail_line[:40]!r}")
            # D：块内出现带信息串的围栏行（历史上会被误当闭合）
            for b in blocks_of(content):
                if has_labeled_line_inside(content, b):
                    counts["D_labeled_pseudo_close"] += 1
                    break
    finally:
        conn.close()

    print("R76B_G2_INVENTORY_READONLY")
    for k in ("total", "A_clean", "B_length_mismatch", "C_unclosed",
              "C_unclosed_prose_tail", "D_labeled_pseudo_close"):
        print(f"  {k}={counts[k]}")
    if samples:
        print("  prose-trapped samples:")
        print("\n".join(samples))
    return 0


def blocks_of(content: str) -> list[dict]:
    return fc.iter_all_blocks(content)


def has_labeled_line_inside(content: str, block: dict) -> bool:
    """块内部（开围栏与闭合之间）是否存在带信息串的围栏行 —— BUG-F2 的历史受害形态。"""
    lines = content.split("\n")
    start = block["open_line"] + 1
    stop = block["close_line"] if block["close_line"] >= 0 else len(lines) - 1
    for i in range(start, stop + 1):
        if i >= len(lines):
            break
        pf = fc.parse_fence_line(lines[i])
        if pf and pf[2]:  # 是围栏行且带信息串
            return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
