"""目录工具：层级深度裁剪 + 统一规范化

规则（2026-09）：目录树只保留三个等级
  一级：第一章  二级：1  三级：1.1
四级及更深节点在落库前被裁剪，其标题并入父节点的 description，
避免内容线索丢失。
"""
from __future__ import annotations

import copy
import json
import logging
import re

logger = logging.getLogger("outline_utils")

MAX_OUTLINE_DEPTH = 3

# 已合并过的"（含：...）"后缀（用于重复裁剪时替换而非叠加）
_MERGE_SUFFIX_RE = re.compile(r"（含：[^（）]*）\s*$")
# 无父描述时写入的裸"含：..."形式
_BARE_MERGE_RE = re.compile(r"^含：[^（）]*$")


def clamp_outline_depth(outline: list[dict], max_depth: int = MAX_OUTLINE_DEPTH) -> list[dict]:
    """把目录树裁剪到 max_depth 级（就地修改并返回）。

    被裁剪的深层节点标题追加到其父节点 description，保留内容线索。
    """
    if not isinstance(outline, list):
        return outline
    return _clamp_nodes(outline, 1, max_depth)


def normalize_outline(outline: list[dict], max_depth: int = MAX_OUTLINE_DEPTH) -> list[dict]:
    """目录统一规范化入口：裁剪到 max_depth 级 + 重排编号 + 补全 children/level。

    设计目的：任何来源（AI 生成 / 上传识别 / 目录库 / 手动编辑）的目录树，
    在落库或回传前端之前都走同一套处理，避免各调用点各写一遍
    "clamp + renumber" 而遗漏其中一步（历史 BUG：部分路径只 clamp 未 renumber，
    或只 renumber 未补 children，导致前端编号错乱/空 children 判断失效）。

    仅接受 list；非 list 原样返回（由调用方负责报错）。

    注意：与 clamp_outline_depth 一样是"就地修改"语义——节点字典会被原地
    裁剪 children / 追加 description / 重排 id。调用方若需保留原始对象，
    请自行深拷贝。
    """
    if not isinstance(outline, list):
        return outline
    clamped = clamp_outline_depth(outline, max_depth)
    try:
        from app.services.ai.json_response import renumber_outline
        renumber_outline(clamped)
    except Exception as e:  # pragma: no cover - renumber 失败不应阻断主流程
        logger.warning("目录重排编号失败（保留裁剪结果）: %s", e)
    return clamped


def normalize_outline_json(payload, default: str = "[]") -> str:
    """把任意来源的目录载荷规范化为"三级裁剪 + 编号重排后"的 JSON 字符串。

    ✅ BUG 修复：目录库入库路径（upload_outline.save_as_library /
    outline_library.create_library / update_library / new_version）旧实现直接落库
    原始 JSON，未走 normalize_outline，导致目录库里可能存着：
      - 4 级及更深的节点（与"保存为方案目录"路径的观感不一致）
      - 标题里内嵌的编号（"第一章 工程概况"），详情/编辑页再套编号 → 双重编号
      - children=None / 字符串等畸形结构
    这里统一收口，保证"上传识别 / 手动编辑 / AI 生成"三条入库路径产出一致结构。

    payload 支持三种形态：
      - list：直接用
      - dict：自动解包 {"outline": [...]}
      - str：先 json.loads，失败则返回 default
    解析/规范化失败时返回 default（不抛异常，避免污染入库流程）。

    注意：内部做深拷贝，不修改调用方传入的对象。
    """
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return default
    if isinstance(data, dict):
        data = data.get("outline", data)
    if not isinstance(data, list):
        return default
    try:
        normalized = normalize_outline(copy.deepcopy(data))
    except Exception as e:  # pragma: no cover - 纯防御
        logger.warning("目录规范化失败，保留原始结构: %s", e)
        normalized = data
    return json.dumps(normalized, ensure_ascii=False)


def _collect_descendant_titles(node: dict) -> list[str]:
    """递归收集节点及其所有后代的标题（用于深度裁剪时保留内容线索）。

    ✅ BUG 修复：旧实现只收集直接子节点标题，孙节点及更深层标题全部丢失。
    例如 L3 节点下有 L4 子节，L4 下又有 L5 细项，旧实现只保留 L4 标题，
    L5 细项标题丢失，导致裁剪后内容线索不完整。
    """
    titles = []
    title = str(node.get("title", "")).strip()
    if title:
        titles.append(title)
    children = node.get("children") or []
    if isinstance(children, list):
        for c in children:
            if isinstance(c, dict):
                titles.extend(_collect_descendant_titles(c))
    return titles


def _strip_prev_merge(desc: str) -> str:
    """去掉 description 末尾已存在的"（含：...）"（或裸"含：..."）合并后缀。

    ✅ BUG-O3 增强：旧实现只在 `desc.endswith("）") and "（含：" in desc` 时替换，
    若描述后面又追加过其它内容（或被截断），判断失效 → 反复裁剪会叠加出
    "原描述（含：A）（含：B）"。改为用正则锚定末尾，覆盖两种历史写法。
    """
    m = _MERGE_SUFFIX_RE.search(desc)
    if m:
        return desc[:m.start()].rstrip()
    if _BARE_MERGE_RE.match(desc):
        return ""
    return desc


def _clamp_nodes(nodes: list, level: int, max_depth: int) -> list:
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            # ✅ 健壮性：非字典节点（模型偶发返回字符串/数字）直接丢弃，
            #    避免后续 node.get / node["children"] 抛 AttributeError
            continue
        children = node.get("children") or []
        if not isinstance(children, list):
            children = []
        if level >= max_depth:
            # 该节点已是最后一级：所有后代标题并入 description 后丢弃
            if children:
                # ✅ BUG 修复：递归收集所有后代标题（含孙节点及更深层），
                # 旧实现只收集直接子节点，深层标题丢失。
                all_titles: list[str] = []
                for c in children:
                    if isinstance(c, dict):
                        all_titles.extend(_collect_descendant_titles(c))
                # 去重保序
                seen = set()
                unique_titles = []
                for t in all_titles:
                    if t not in seen:
                        seen.add(t)
                        unique_titles.append(t)
                merged = "；".join(unique_titles)
                if merged:
                    desc = _strip_prev_merge(str(node.get("description", "")).strip())
                    node["description"] = f"{desc}（含：{merged}）" if desc else f"含：{merged}"
            node["children"] = []
            result.append(node)
        else:
            node["children"] = _clamp_nodes(children, level + 1, max_depth)
            result.append(node)
    return result
