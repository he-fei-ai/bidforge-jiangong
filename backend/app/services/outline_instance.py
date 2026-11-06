"""目录模板实例化（R179 Phase B · 确定性，零 LLM）。

职责
----
- 读取方案名称 → 选中标准模板 → 按标题派生 ``content_type`` / ``fact_deps``；
- 只消费**已确认**事实（``is_resolved=1 且 has_conflict=0``，并沿用全局事实
  注入门控 ``get_facts_inject_where()`` 的模拟值/过期值剔除）填充依赖项；
- 未命中的依赖项标 ``fact_status="missing"``，**不得**输出「根据实际情况确定」
  之类的无信息量占位；
- 返回的树可直接交给 ``sections._save_outline_to_db`` 落库（元数据随节点写入
  ``sections.outline_json``，不新增列、不动 ``db.py``）。
"""

from __future__ import annotations

import copy
import logging

from app.services.facts_extractor import get_facts_inject_where
from app.services.outline_templates import (
    FACT_DEP_ALIASES,
    build_outline,
    content_type_of,
    fact_deps_of,
    match_template,
)

logger = logging.getLogger(__name__)


def _fact_text(fact: dict) -> str:
    """事实匹配文本：标题 + 内容 + fact_key（统一小写）。"""
    return " ".join(str(fact.get(k) or "") for k in ("title", "content", "fact_key")).lower()


def _match_dep(dep: str, facts: list[dict]) -> dict | None:
    """在已确认事实中为单个依赖键找最佳匹配（置信度高者优先）。"""
    aliases = FACT_DEP_ALIASES.get(dep) or ()
    if not aliases:
        return None
    candidates: list[dict] = []
    for fact in facts:
        text = _fact_text(fact)
        if any(str(a).lower() in text for a in aliases):
            candidates.append(fact)
    if not candidates:
        return None
    # confidence 缺省按 1.0；并列时取 id 稳定排序，保证可复现
    candidates.sort(key=lambda f: (-float(f.get("confidence") or 1.0), str(f.get("id") or "")))
    return candidates[0]


def instantiate(nodes: list, facts: list[dict]) -> tuple[list, dict]:
    """用已确认事实实例化目录树（深拷贝，不修改模板对象）。

    Returns:
        ``(实例化后的树, 统计)``；统计含 ``nodes / resolved / partial / missing /
        no_dependency``，用于验收「覆盖率」与「未命中可见性」。
    """
    tree = copy.deepcopy(nodes)
    stats = {"nodes": 0, "resolved": 0, "partial": 0, "missing": 0, "no_dependency": 0}

    def _walk(items: list) -> None:
        for node in items or []:
            if not isinstance(node, dict):
                continue
            stats["nodes"] += 1
            title = str(node.get("title") or "")
            # 防御性兜底：调用方若传了未标注的树，这里补齐两标签。
            node.setdefault("content_type", content_type_of(title))
            deps = node.get("fact_deps")
            if not isinstance(deps, list):
                deps = fact_deps_of(title)
                node["fact_deps"] = deps

            if not deps:
                node["fact_status"] = "no_dependency"
                stats["no_dependency"] += 1
            else:
                values: dict[str, dict] = {}
                missing: list[str] = []
                for dep in deps:
                    fact = _match_dep(dep, facts)
                    if fact is None:
                        missing.append(dep)
                        continue
                    values[dep] = {
                        "value": str(fact.get("content") or ""),
                        "source_ref": str(fact.get("source_ref") or ""),
                        "fact_id": str(fact.get("id") or ""),
                    }
                if values:
                    node["fact_values"] = values
                    refs = [v["source_ref"] for v in values.values() if v["source_ref"]]
                    if refs:
                        # 去重保序
                        node["fact_source_ref"] = "；".join(dict.fromkeys(refs))
                if values and not missing:
                    node["fact_status"] = "resolved"
                    stats["resolved"] += 1
                elif values:
                    node["fact_status"] = "partial"
                    node["missing_fact_deps"] = missing
                    stats["partial"] += 1
                else:
                    node["fact_status"] = "missing"
                    node["missing_fact_deps"] = missing
                    stats["missing"] += 1

            children = node.get("children")
            if isinstance(children, list) and children:
                _walk(children)

    _walk(tree)
    return tree, stats


async def _load_confirmed_facts(db, scheme_id: str, project_id: str) -> list[dict]:
    """读取作用域内的已确认事实（fail-soft：异常返回空，节点标 missing）。"""
    where = get_facts_inject_where()
    sql = (
        "SELECT id, title, content, fact_key, source_ref, confidence, value_unit "
        "FROM global_facts "
        # 事实确认口径**唯一来源**：get_facts_inject_where() 已含
        # has_conflict=0 AND is_resolved=1（并额外剔除模拟值/过期值），
        # 此处不重复字面量，避免与主口径分叉。
        f"WHERE {where} AND (scheme_id=? OR (scheme_id='' AND project_id=?))"
    )
    try:
        cur = await db.execute(sql, (scheme_id, project_id))
        return [dict(r) for r in await cur.fetchall()]
    except Exception as e:  # noqa: BLE001 - 事实不可用不得阻断目录实例化
        logger.warning("目录实例化读取事实失败（按缺失处理）: %s", e)
        return []


async def instantiate_for_scheme(db, scheme_id: str, project_id: str = "") -> dict:
    """按方案名称选模板、读事实、实例化；返回树与统计（不落库）。"""
    from app.services.facts_extractor import resolve_scheme_project_id

    pid = await resolve_scheme_project_id(db, scheme_id)
    # 项目归属校验：路由带了 project_id 时必须与反查结果一致（防跨项目越权）。
    if not pid or (project_id and pid != project_id):
        raise ValueError("方案不存在")
    cur = await db.execute("SELECT name FROM schemes WHERE id=?", (scheme_id,))
    row = await cur.fetchone()
    if not row:
        raise ValueError("方案不存在")
    name = str(row[0] or "")
    if not project_id:
        project_id = pid

    facts = await _load_confirmed_facts(db, scheme_id, pid)
    outline = build_outline(name, with_meta=True)
    tree, stats = instantiate(outline, facts)
    return {
        "template_key": match_template(name),
        "facts_total": len(facts),
        "tree": tree,
        "stats": stats,
    }
