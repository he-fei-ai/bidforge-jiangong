"""目录库 → 参考文本渲染服务

用途：把 outline_library 里的目录树渲染成**人（和模型）可读**的紧凑文本，
供以下场景复用，避免各调用方各写一遍、也避免把裸 JSON 塞进 prompt：

- AI 生成目录时作为【目录库参考】注入 prompt（sse_handlers）
- 目录导出（outline_library.export）的 txt / markdown 渲染

设计要点：
1. **只渲染标题 + description（编写要点）**：裸 JSON 里的 id / level / children:[]
   对模型是纯噪声，同等信息量下 token 浪费近 3 倍；
2. **按库分配预算**：旧实现把 3 个库的 JSON 拼一起后统一截断 2000 字符，
   导致第 2、3 个库整份丢失。这里改为每个库单独渲染并各自限长；
3. **可只取子树**：二三级目录生成时只需当前章对应的目录库分支。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: 单个目录库渲染的默认字符预算
DEFAULT_PER_LIB_BUDGET = 1800


def render_outline_text(nodes: list, prefix: str = "", max_chars: int = 0) -> str:
    """把目录树渲染成多级编号文本：`1 工程概况 —— 编写要点`

    Args:
        nodes: 目录树节点数组
        prefix: 编号前缀（递归用）
        max_chars: 0 表示不限；>0 时按行累加并在超限时停止（保证截断在行边界）
    """
    lines: list[str] = []
    used = 0

    def walk(ns: list, pre: str) -> bool:
        """返回 False 表示已达预算上限，调用方应停止继续渲染"""
        nonlocal used
        for i, n in enumerate(ns or [], 1):
            if not isinstance(n, dict):
                continue
            code = f"{pre}{i}"
            title = str(n.get("title") or "").strip()
            desc = str(n.get("description", "")).strip()
            line = f"{code} {title}" + (f" —— {desc}" if desc else "")
            if max_chars and used + len(line) > max_chars:
                return False
            lines.append(line)
            used += len(line) + 1
            if n.get("children"):
                if not walk(n["children"], f"{code}."):
                    return False
        return True

    walk(nodes, prefix)
    return "\n".join(lines)


def render_outline_md(nodes: list, level: int = 1) -> str:
    """把目录树渲染成 Markdown（标题层级 + 描述引用）"""
    lines: list[str] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        desc = str(n.get("description", "")).strip()
        lines.append(f"{'#' * min(level, 6)} {title}")
        if desc:
            lines.append(f"> {desc}")
            lines.append("")
        if n.get("children"):
            lines.append(render_outline_md(n["children"], level + 1))
    return "\n".join(lines)


def parse_outline(raw) -> list:
    """把 outline_json（字符串 / dict 包装 / 数组）统一解析为数组"""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, dict):
        raw = raw.get("outline", [])
    return raw if isinstance(raw, list) else []


def find_subtree(nodes: list, chapter_title: str) -> list:
    """按标题模糊匹配，取出该一级章节下的子树（用于二三级生成时精确定向参考）。

    匹配规则：章节标题包含于目录库节点标题，或反之；取首个命中。
    """
    if not chapter_title:
        return []
    target = str(chapter_title).strip()
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        if target in title or title in target:
            return n.get("children") or []
    return []


async def fetch_libraries(db, library_ids: list[str], only_approved: bool = True) -> list[dict]:
    """批量取目录库（默认只取「已通过」，避免停用/待审核库污染 AI 参考）"""
    result: list[dict] = []
    for lid in (library_ids or [])[:5]:
        if not lid:
            continue
        sql = "SELECT id, name, type, version, outline_json FROM outline_library WHERE id=?"
        if only_approved:
            sql += " AND review_status='已通过'"
        cur = await db.execute(sql, (lid,))
        row = await cur.fetchone()
        if row:
            result.append(dict(row))
    return result


async def build_reference_outline(
    db,
    library_ids: list[str],
    per_lib_budget: int = DEFAULT_PER_LIB_BUDGET,
    chapter_title: str = "",
    only_approved: bool = True,
) -> tuple[str, list[str]]:
    """构建 AI 生成用的【目录库参考】文本。

    Args:
        chapter_title: 非空时只取该章节对应的子树（二三级生成场景），
            取不到则回退为整棵树。

    Returns:
        (参考文本, 实际命中的 library_id 列表) —— 调用方可据此累加 ref_count。
    """
    libs = await fetch_libraries(db, library_ids, only_approved=only_approved)
    if not libs:
        return "", []

    blocks: list[str] = []
    hit_ids: list[str] = []
    for lib in libs:
        outline = parse_outline(lib.get("outline_json"))
        if not outline:
            continue
        target = find_subtree(outline, chapter_title) if chapter_title else []
        # 章节定向失败时回退整棵树，保证"有参考总比没参考好"
        nodes = target or outline
        body = render_outline_text(nodes, max_chars=per_lib_budget)
        if not body.strip():
            continue
        head = f"《{lib.get('name', '未命名目录')}》"
        if lib.get("version"):
            head += f"（{lib['version']}）"
        if target:
            head += f" · {chapter_title} 章节参考"
        blocks.append(f"{head}\n{body}")
        hit_ids.append(lib["id"])

    return "\n\n".join(blocks), hit_ids


async def build_category_reference_outline(
    db,
    category_ids: list[str],
    per_lib_budget: int = DEFAULT_PER_LIB_BUDGET,
) -> tuple[str, list[str]]:
    """按危大工程类别自动匹配目录库，拼成【目录库参考】文本（供目录生成追加注入）。

    与 build_reference_outline 的差异：后者按用户显式指定的 library_ids 取库；
    本函数按方案自动分类结果（category_ids，如 ["foundation_pit"]）批量匹配
    outline_library.type 命中其一的「已通过」目录库，用于把「该类别危大工程的
    标准目录模板」自动纳入目录生成参考。仅追加、不替换用户选库结果。

    Returns:
        (参考文本, 实际命中的 library_id 列表)
    """
    if not category_ids:
        return "", []
    # type 有索引（schema_sql.idx_outline_library_type）；IN 列表按实参数化。
    placeholders = ",".join("?" for _ in category_ids)
    sql = (
        f"SELECT id, name, type, version, outline_json FROM outline_library "
        f"WHERE type IN ({placeholders}) AND review_status='已通过'"
    )
    try:
        cur = await db.execute(sql, tuple(category_ids))
        rows = await cur.fetchall()
    except Exception as e:
        logger.warning("按类别匹配目录库失败（不影响生成）: %s", e)
        return "", []
    libs = [dict(r) for r in rows]
    if not libs:
        return "", []

    blocks: list[str] = []
    hit_ids: list[str] = []
    for lib in libs:
        outline = parse_outline(lib.get("outline_json"))
        if not outline:
            continue
        body = render_outline_text(outline, max_chars=per_lib_budget)
        if not body.strip():
            continue
        head = f"《{lib.get('name', '未命名目录')}》"
        if lib.get("version"):
            head += f"（{lib['version']}）"
        head += " · 危大工程类别匹配参考"
        blocks.append(f"{head}\n{body}")
        hit_ids.append(lib["id"])
    return "\n\n".join(blocks), hit_ids


async def load_library_trees(
    db,
    library_ids: list[str],
    only_approved: bool = True,
) -> list[dict]:
    """一次性把目录库解析成内存树，供生成链路多次复用（避免长任务反复查库）。

    返回 [{"id","name","version","nodes"}]，已剔除解析失败 / 空目录的库。
    """
    libs = await fetch_libraries(db, library_ids, only_approved=only_approved)
    trees: list[dict] = []
    for lib in libs:
        nodes = parse_outline(lib.get("outline_json"))
        if nodes:
            trees.append({
                "id": lib["id"],
                "name": lib.get("name", "未命名目录"),
                "version": lib.get("version") or "",
                "nodes": nodes,
            })
    return trees


def render_reference_from_trees(
    trees: list[dict],
    per_lib_budget: int = DEFAULT_PER_LIB_BUDGET,
    chapter_title: str = "",
) -> str:
    """把内存中的目录库树渲染成参考文本（纯函数，可在生成循环内安全调用）。

    chapter_title 非空时只取该章节对应的子树，取不到则回退整棵树。
    """
    blocks: list[str] = []
    for lib in trees or []:
        nodes = find_subtree(lib["nodes"], chapter_title) if chapter_title else []
        nodes = nodes or lib["nodes"]
        body = render_outline_text(nodes, max_chars=per_lib_budget)
        if not body.strip():
            continue
        head = f"《{lib.get('name', '未命名目录')}》"
        if lib.get("version"):
            head += f"（{lib['version']}）"
        if chapter_title and nodes is not lib["nodes"]:
            head += f" · {chapter_title} 参考"
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


async def bump_ref_count(db, library_ids: list[str]) -> None:
    """累加目录库引用次数（AI 参考 / 创建套用均计入，修复统计失真）"""
    for lid in library_ids or []:
        if not lid:
            continue
        await db.execute(
            "UPDATE outline_library SET ref_count=ref_count+1 WHERE id=?", (lid,))
