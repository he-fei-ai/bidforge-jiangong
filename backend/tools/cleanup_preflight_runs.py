"""preflight_runs 伪历史行去重清理（T5 · 2026-10-03 · R36 遗留项收口）。

背景：R36 发现 autofix 三处重算调用点（_resolve_finding / collect / stage）曾通过
``_readiness_overview_compute(persist=True)`` 在每次「定位/修复」点击时落一条
运行记录 —— 与紧邻的真实总检**逐列相同**（同指纹同结论，只有 rowid/时间不同）。
后果是「总检历史」前 10 条被伪记录灌满，真实的分数变化被挤出窗口（趋势污染）。
代码已于 R36 收口（重算传 persist=False），本工具清理历史遗留的污染行。

判据（保守，宁可漏删不误删）：同方案下
(scheme_id, content_fingerprint, total, grade, verdict, released, blocked,
 counts, dimensions, findings, stats) 共 11 列**全等**的行视为
「零信息重复」，仅保留最早一条（rowid 最小 = 首次发生时刻留在趋势里），
其余删除。内容变过（指纹不同）或结论变过（分数/等级/findings 不同）的行
都不落同组，永不误删；AI 非确定性产生的同指纹不同结论行同样保留。

用法（默认 dry-run，只报告不动数据）：
    python tools/cleanup_preflight_runs.py
    python tools/cleanup_preflight_runs.py --apply
    python tools/cleanup_preflight_runs.py --apply --before "2026-10-03 20:00:00"

安全设计：
  - dry-run 默认：无 --apply 绝不改动任何数据；
  - --before 为清理截止时刻（建议设为 persist=False 修复上线时刻）：晚于该时刻的
    行不参与去重 —— 新代码已不再产生污染行，正常的手动重复总检不应被抹掉；
  - --apply 先用 sqlite backup API 制作整库一致性快照（在线安全），再删；
  - 硬编码上限：计划删除行数超过 DELETE_HARD_CAP 时拒绝执行，要求显式 --force
    （防止极端情况大面积误删）；
  - 零运行时挂钩：独立维护脚本，不接入任何服务进程。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_ROOT / "data" / "scheme_assistant.db"

# 单次清理量上限：污染行本质是「每点一次修复 +1 条」，正常量级为几十；
# 超限说明判据或数据异常，需人工确认后加 --force。
DELETE_HARD_CAP = 2000

# 去重比较列（11 列全等才算重复；created_at/rowid 差异正是被清掉的伪信息）
_DUP_COLUMNS = (
    "scheme_id", "content_fingerprint", "total", "grade", "verdict",
    "released", "blocked", "counts", "dimensions", "findings", "stats",
)


def find_duplicate_groups(conn: sqlite3.Connection,
                          before: str | None = None) -> list[dict]:
    """找出「零信息重复」组。返回 [{scheme_id, keep_rowid, delete_rowids}]。

    keep 恒为组内最早行（rowid 最小）——首次发生的运行才是趋势上的真实点。
    ``before``（``YYYY-MM-DD HH:MM:SS``，与 created_at 同格式）为清理截止时刻，
    晚于该时刻的行不参与去重。
    """
    cols = ", ".join(_DUP_COLUMNS)
    rows = conn.execute(
        f"SELECT rowid, {cols}, created_at FROM preflight_runs"
        " ORDER BY rowid").fetchall()
    groups: dict[tuple, list[int]] = {}
    for rowid, *rest in rows:
        created_at = rest[-1] or ""
        if before is not None and str(created_at) > before:
            continue
        key = tuple(rest[:-1])
        groups.setdefault(key, []).append(rowid)
    out: list[dict] = []
    for key, ids in groups.items():
        if len(ids) < 2:
            continue
        keep = min(ids)
        out.append({"scheme_id": key[0], "keep_rowid": keep,
                    "delete_rowids": sorted(i for i in ids if i != keep)})
    return out


def delete_rows(conn: sqlite3.Connection, groups: list[dict]) -> int:
    """按组删除重复行（单事务）。返回删除条数。"""
    ids = [i for g in groups for i in g["delete_rowids"]]
    if not ids:
        return 0
    conn.executemany("DELETE FROM preflight_runs WHERE rowid=?",
                     [(i,) for i in ids])
    conn.commit()
    return len(ids)


def backup_db(db_path: Path) -> Path:
    """sqlite backup API 在线一致性快照，返回备份文件路径。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_path.with_name(f"{db_path.stem}_preflight_gc_bak_{stamp}.db")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def main(argv: list[str] | None = None, db_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="preflight_runs 零信息重复行去重（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际删除（默认只报告）")
    parser.add_argument("--before", default=None,
                        help="清理截止时刻（YYYY-MM-DD HH:MM:SS），晚于该时刻的行不动")
    parser.add_argument("--force", action="store_true",
                        help="计划删除量超过硬编码上限时仍强制执行")
    args = parser.parse_args(argv)

    db = db_path or DB_PATH
    if not db.exists():
        print(f"DB not found: {db}")
        return 2

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        groups = find_duplicate_groups(conn, before=args.before)
    finally:
        conn.close()

    n_delete = sum(len(g["delete_rowids"]) for g in groups)
    schemes = len({g["scheme_id"] for g in groups})
    print(f"groups={len(groups)} rows_to_delete={n_delete} schemes={schemes} "
          f"before={args.before or '(不限)'}")
    for g in groups[:5]:
        print(f"  scheme={g['scheme_id']} keep={g['keep_rowid']} "
              f"delete={g['delete_rowids']}")
    if len(groups) > 5:
        print(f"  ...（其余 {len(groups) - 5} 组略）")

    if n_delete > DELETE_HARD_CAP and not args.force:
        print(f"ABORT: rows_to_delete={n_delete} exceeds cap {DELETE_HARD_CAP}"
              "（确认无误后加 --force）")
        return 3
    if not args.apply:
        print("DRY-RUN: 未做任何改动（--apply 执行，执行前自动制作整库备份）")
        return 0
    if n_delete == 0:
        print("无需删除（0 组重复）")
        return 0

    bak = backup_db(db)
    print(f"backup -> {bak}")
    conn = sqlite3.connect(str(db))
    try:
        deleted = delete_rows(conn, groups)
    finally:
        conn.close()
    print(f"APPLY: deleted={deleted}（备份可整库回退：{bak}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
