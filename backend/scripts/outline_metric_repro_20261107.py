"""目录编号指标复现脚本（合成样本，非生产库）。

对应审计报告中的三个数字：
  M1 编号正确率        8887/8887
  M2 层级完整率        8887/8887
  M3 落库重算一致性    2251/2251（save=1185, mutate=1066）

重要说明：
  1. 本指标**不读任何 .db 文件**。样本由固定种子 random.Random(20261107) 在内存里
     合成；M3 使用 aiosqlite.connect(":memory:") 建临时库，跑完即销毁。
     因此任何人在 backend/data/*.db 里都找不到 8887 这个量级的数据集。
  2. 三个指标共用同一条 RNG 流，**必须从冷进程按 M1 -> M3 顺序执行**；单独跑
     metric_3_db 会得到不同的分母。
  3. 这些数字是「规范化/重算逻辑自洽性」的合成回归基线，不是生产目录的准确率。
     依据：同一脚本在旧提交 8be8be7 与修复后 HEAD 上输出逐字一致（见报告）。
  4. 分母/分子定义：
     M1/M2 分母 = 300 棵合成树经 normalize_outline 后的节点总数；
           分子 = 节点 id 等于其位置推导路径（如 1.2.1）——M2 另要求 level 等于深度。
     M3    分母 = 40 个 scheme ×（首次保存 + 结构变更后再存）两相位，_build_tree
             回读的节点总数；分子 = 回读节点的 (id, level) 与 renumber_section_outline_ids
             重算结果一致，且 level 等于实际深度。

用法（在 backend 目录下）：
    set PYTHONDONTWRITEBYTECODE=1
    python scripts/outline_metric_repro_20261107.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import random
import sys
from pathlib import Path

import aiosqlite

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import _migrate
from app.routers.sections import _build_tree, _save_outline_to_db
from app.schema_sql import SCHEMA_SQL
from app.services.numbering import renumber_section_outline_ids
from app.services.outline_utils import normalize_outline

RNG = random.Random(20261107)  # 固定种子：数字完全由种子 + 代码路径决定
FRAG = ["工程概况", "施工组织", "施工方案", "安全保证", "质量验收", "测量放线", "钢筋工程", "模板支架"]
PRE = ["", "第一章 ", "1.1 ", "第1章 1.1.1 ", "（1）", "1）", "一、"]


def title() -> str:
    return RNG.choice(PRE) + RNG.choice(FRAG)


def rand_tree() -> list[dict]:
    roots = []
    for _ in range(RNG.randint(3, 9)):
        kids = [{"title": title(), "children": []} for _ in range(RNG.randint(1, 3))]
        for k in kids:
            k["children"] = [{"title": title(), "children": []} for _ in range(RNG.randint(0, 2))]
        roots.append({"title": title(), "children": kids})
    return roots


def flatten(nodes, depth=1, parent=""):
    out = []
    for i, n in enumerate(nodes, 1):
        if not isinstance(n, dict):
            continue
        sid = f"{parent}.{i}" if parent else str(i)
        out.append((n, sid, depth))
        out.extend(flatten(n.get("children") or [], depth + 1, sid))
    return out


def metric_1_2() -> tuple[int, int, int]:
    """编号正确率 / 层级完整率：AI 目录规范化入口 normalize_outline。"""
    total = ok_no = ok_lv = 0
    for _ in range(300):
        res = normalize_outline(copy.deepcopy(rand_tree()))
        for n, sid, depth in flatten(res):
            total += 1
            if str(n.get("id")) == sid:
                ok_no += 1
                if str(n.get("level")) == str(depth) == str(sid.count(".") + 1):
                    ok_lv += 1
    return total, ok_no, ok_lv


async def make_db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_SQL)
    await _migrate(conn)
    await conn.commit()
    return conn


async def seed(conn, i: int) -> str:
    pid, sid = f"p{i}", f"s{i}"
    await conn.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "P"))
    await conn.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "S"))
    await conn.commit()
    return sid


async def metric_3_db() -> tuple[int, int, dict]:
    """落库往返 + 插入/删除/移动/改标题后的重算一致性。"""
    conn = await make_db()
    total = ok = 0
    detail = {"save": 0, "mutate": 0}
    try:
        for i in range(40):
            sid = await seed(conn, i)
            tree = rand_tree()
            await _save_outline_to_db(conn, sid, copy.deepcopy(tree))
            for phase in ("save", "mutate"):
                if phase == "mutate":
                    # 结构变更：第 1 章的一个子章节移到第 2 章；删除第 3 章；新增一个根章
                    t = copy.deepcopy(tree)
                    if len(t[0].get("children") or []) >= 2:
                        moved = t[0]["children"].pop(0)
                        moved["children"] = moved.get("children") or []
                        t[1]["children"].insert(0, moved)
                    t.pop(2)
                    t.append({"title": "新增收尾章", "children": [{"title": title(), "children": []}]})
                    await _save_outline_to_db(conn, sid, copy.deepcopy(t))
                built = await _build_tree(conn, sid)
                canonical = {x[2]: (json.loads(x[0])["id"], str(x[1])) for x in renumber_section_outline_ids(built)}
                for n, esid, depth in flatten(built):
                    total += 1
                    want = canonical.get(n["id"])
                    if want and want[0] == esid and want[1] == str(depth) and str(n.get("level")) == str(depth):
                        ok += 1
                        detail[phase] += 1
    finally:
        await conn.close()
    return total, ok, detail


async def main() -> None:
    total, ok_no, ok_lv = metric_1_2()
    print(f"M1 编号正确率: {ok_no}/{total} = {ok_no / total * 100:.2f}%")
    print(f"M2 层级完整率: {ok_lv}/{total} = {ok_lv / total * 100:.2f}%")
    total3, ok3, detail = await metric_3_db()
    print(f"M3 落库重算一致性: {ok3}/{total3} = {ok3 / total3 * 100:.2f}%  (save={detail['save']}, mutate={detail['mutate']})")


if __name__ == "__main__":
    asyncio.run(main())
