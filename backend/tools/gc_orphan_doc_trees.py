"""孤儿数据 GC 维护工具（T4 · 2026-10-03 · R32 §4.30.6 遗留项收口）。

背景：删除链路已在 R32 补齐显式清理，但历史遗留的孤儿数据（数据迁移类遗留）
此前无人清理：
  ① `uploaded_outlines` 中 project_id 已不存在于 projects 的孤儿行
     （R32 实证 88 行；生产库重建后已消失，本逻辑兜底未来回归）；
  ② `data/projects/` 下目录名不对应任何现存 project_id 的四层文档树
     （生产库实测 8372 棵，属历史已删项目残留）。

用法（默认 dry-run，只报告不动数据）：
    python tools/gc_orphan_doc_trees.py                 # dry-run 报告
    python tools/gc_orphan_doc_trees.py --apply         # 移入回收站（可回退）
    python tools/gc_orphan_doc_trees.py --apply --mode delete   # 硬删（不可回退）

安全设计：
  - dry-run 默认：无 --apply 绝不改动任何数据；
  - 回收站模式（默认）：移动到 `data/projects/.trash/orphan-gc-<日期>/` 并写
    manifest.json 留档，可整体移回（与 doc_storage 既有 .trash 回收站设计一致）；
  - `.trash` 与现存 project_id 永不命中；
  - 逐条目 fail-soft：单个目录移动失败（如句柄占用）只记录不中断，最终汇报。
  - 零运行时挂钩：不接入任何服务进程；删除项目时的实时清理已由
    delete_project_docs_root（R32）覆盖，本工具只处理历史残留。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_ROOT / "data" / "scheme_assistant.db"
DOCS_ROOT = BACKEND_ROOT / "data" / "projects"
PROTECTED_NAMES = {".trash"}

# 硬编码上限护栏：单次清理量异常（如 live_ids 为空导致全量误判）时拒绝执行，
# 要求显式 --force 确认 —— 防止「projects 表恰为空」时把整个存储树当孤儿搬走。
GC_HARD_CAP = 50000


def classify_orphans(
    names: list[str], live_ids: set[str], protected_names: set[str] = PROTECTED_NAMES
) -> tuple[list[str], list[str]]:
    """纯函数：目录名分类为（孤儿, 保留）。

    孤儿 = 不在现存 project_id 集合、且不在保护名单（.trash）内的条目。
    """
    orphans = sorted(n for n in names if n not in live_ids and n not in protected_names)
    kept = sorted(n for n in names if n in live_ids or n in protected_names)
    return orphans, kept


def find_orphan_uploaded_outlines(conn: sqlite3.Connection) -> list[str]:
    """uploaded_outlines 孤儿行 id —— **保守判据**：

    孤儿 = project_id 非空且已不存在于 projects，**且** scheme_id 为空或已不存在。
    - project_id='' 不是孤儿：无项目上下文入口的合法待关联态，
      save-as-outline 时才回写 project_id（upload_outline.py）；
    - scheme_id 仍存活的不是孤儿：可通过方案反查恢复归属，宁漏勿误删。
    """
    cur = conn.execute(
        "SELECT id FROM uploaded_outlines o"
        " WHERE o.project_id != ''"
        "   AND o.project_id NOT IN (SELECT id FROM projects)"
        "   AND (o.scheme_id = '' OR o.scheme_id NOT IN (SELECT id FROM schemes))")
    return [r[0] for r in cur.fetchall()]


def move_to_trash(docs_root: Path, names: list[str], trash_dir: Path) -> tuple[list[str], list[str]]:
    """逐目录移入回收站，写 manifest.json 留档。返回（成功, 失败）。

    trash_dir 必须在 docs_root 之下（同卷 rename 原子性）；不存在时自动创建。
    """
    moved: list[str] = []
    failed: list[str] = []
    trash_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = docs_root / name
        dst = trash_dir / name
        try:
            if not src.exists():
                # 并发消失不算失败（可能被其他清理处理）
                moved.append(name)
                continue
            if dst.exists():
                failed.append(name)
                continue
            shutil.move(str(src), str(dst))
            moved.append(name)
        except OSError:
            failed.append(name)
    manifest = {
        "gc_time": datetime.now().isoformat(timespec="seconds"),
        "source": "gc_orphan_doc_trees.py",
        "moved": moved,
        "failed": failed,
    }
    (trash_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return moved, failed


def delete_trees(docs_root: Path, names: list[str]) -> tuple[list[str], list[str]]:
    """逐目录硬删（不可回退）。返回（成功, 失败）。"""
    removed: list[str] = []
    failed: list[str] = []
    for name in names:
        target = docs_root / name
        try:
            if not target.exists():
                removed.append(name)
                continue
            shutil.rmtree(target)
            removed.append(name)
        except OSError:
            failed.append(name)
    return removed, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="孤儿文档树 / 孤儿行 GC（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="实际执行（默认只报告）")
    parser.add_argument("--mode", choices=["trash", "delete"], default="trash",
                        help="trash=移入 .trash 回收站（默认，可回退）；delete=硬删")
    parser.add_argument("--force", action="store_true",
                        help="孤儿数量超过硬编码上限时仍强制执行")
    args = parser.parse_args(argv)

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return 2
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        live_ids = {r[0] for r in conn.execute("SELECT id FROM projects")}
        orphan_rows = find_orphan_uploaded_outlines(conn)
    finally:
        conn.close()

    dir_names = [d.name for d in DOCS_ROOT.iterdir() if d.is_dir()] if DOCS_ROOT.exists() else []
    orphans, kept = classify_orphans(dir_names, live_ids)

    print(f"docs_root={DOCS_ROOT}")
    print(f"live_projects={len(live_ids)} dirs={len(dir_names)} "
          f"orphans={len(orphans)} kept={len(kept)}")
    print(f"uploaded_outlines_orphan_rows={len(orphan_rows)}")
    if orphans:
        print("sample:", ", ".join(orphans[:3]))

    if len(orphans) > GC_HARD_CAP and not args.force:
        print(f"ABORT: orphans={len(orphans)} exceeds hard cap {GC_HARD_CAP}（防全量误判，"
              "确认无误后加 --force）")
        return 3
    if not args.apply:
        print("DRY-RUN: 未做任何改动（--apply 执行；默认 trash 模式可回退）")
        return 0

    if args.mode == "delete":
        moved, failed = delete_trees(DOCS_ROOT, orphans)
        print(f"APPLY(delete): removed={len(moved)} failed={len(failed)}")
        if failed:
            print("failed_sample:", ", ".join(failed[:5]))
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_dir = DOCS_ROOT / ".trash" / f"orphan-gc-{stamp}"
    moved, failed = move_to_trash(DOCS_ROOT, orphans, trash_dir)
    print(f"APPLY(trash): moved={len(moved)} failed={len(failed)} -> {trash_dir}")

    # 孤儿行清理（数量通常为 0，兜底未来回归）
    if orphan_rows:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            ph = ",".join("?" * len(orphan_rows))
            conn.execute(f"DELETE FROM uploaded_outlines WHERE id IN ({ph})", orphan_rows)
            conn.commit()
        finally:
            conn.close()
        print(f"APPLY(db): deleted uploaded_outlines rows={len(orphan_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
