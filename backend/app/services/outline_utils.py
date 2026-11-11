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

#: 折叠引导的**显式标记**（2026-11-10 二次收口）。
#: 之前仅凭「description 尾部全是（n）条目」判断是否已折叠 —— 作者自己写的
#: 「（1）（2）」编号列表会被误判成旧折叠后缀而剥掉。现改为：折叠时在引导前
#: 写入本标记，重复折叠只剥标记段（严格幂等）；无标记的历史数据仅当尾部与
#: 「本次将要生成的引导」逐字一致时才视为旧折叠产出，否则原样保留。
#: 标记会随 description 进入正文提示词的【当前章节】，语义上也是正向指令。
FOLD_GUIDANCE_MARKER = "【按以下引导展开】"

# ✅ R187（2026-11-08 · 编号命名空间护栏）：落库前 fail-closed 校验的唯一判据。
# 与 numbering.renumber_outline_nodes 的结构口径一致（点分数字、子承父系、
# 段数=层级），但**只校验不改写** —— 让「重排失败」不再静默产出脏编号。
_DOTTED_ID_RE = re.compile(r"^\d+(?:\.\d+)*$")
# 不可见字符 / C0 控制字符：来源真实（Word / 网页复制、AI 偶发零宽）。
# 与 outline_quality._INVISIBLE_RE 同一字符族，另补 C0 控制字符（它们会
# 破坏 DOCX XML 与提示词模板）。只用于写路径清洗，检测侧仍用原判据。
_INVISIBLE_TITLE_RE = re.compile(
    r"[\u200b-\u200f\u2060\ufeff\u00ad\u180e\u034f\x00-\x08\x0b\x0c\x0e-\x1f]+"
)
#: 章节标题长度上限（超出按「可行动 400」拒绝，绝不静默截断用户数据）。
OUTLINE_TITLE_MAX_LEN = 200

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
        # ✅ R187：从 WARNING 升为 ERROR。旧文案「保留裁剪结果」掩盖了「编号未重排」
        #   这一事实；落库口另有 validate_outline_numbering fail-closed 兜底，
        #   这里必须让「重排失败」在日志里可被检索到。
        logger.error("目录重排编号失败（返回未编号树，落库口须拒绝）: %s", e, exc_info=True)
    return clamped


def validate_outline_numbering(outline: list) -> list[str]:
    """落库前编号合法性校验（纯函数，**只校验不改写**）。

    ✅ R187（2026-11-08 · 编号命名空间 fail-closed）：``normalize_outline`` 的
    ``renumber_outline`` 被 try/except 吞掉后，会返回「已裁剪但未编号」的树；
    ``_process_nodes`` 又把 ``node["id"]`` 原样写进 ``outline_json``，于是脏/重复
    编号落库、前端双重编号、正文与导出编号分叉都无告警。本函数返回问题列表
    （空列表 = 合法），由落库口据此以 400 拒绝，绝不静默放行。

    判据与 ``numbering.renumber_outline_nodes`` 同一结构口径：
      1. id 必须是点分数字串，段数 ≤ MAX_OUTLINE_DEPTH；
      2. 全树 id 唯一；
      3. 子承父系 + 等级正传：id 等于其树位置期望号，level 等于段数；
      4. 标题长度不超过 OUTLINE_TITLE_MAX_LEN（超限给可行动 400，不静默截断）。
    ⚠️ 不可见/控制字符由 ``sanitize_outline_title`` 在写路径剥离，不在此判红。
    """
    problems: list[str] = []
    if not isinstance(outline, list):
        return ["目录不是数组"]
    seen: dict[str, str] = {}

    def _walk(nodes: list, prefix: str, path: str) -> None:
        for idx, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                problems.append(f"{path}[{idx}] 不是字典节点")
                continue
            nid = str(node.get("id") or "").strip()
            loc = f"{path}/{nid or '(空编号)'}"
            expect = f"{prefix}.{idx}" if prefix else str(idx)
            if not _DOTTED_ID_RE.fullmatch(nid):
                problems.append(f"{loc}：编号格式非法（应为点分数字，如 {expect}）")
            else:
                segs = nid.count(".") + 1
                if segs > MAX_OUTLINE_DEPTH:
                    problems.append(f"{loc}：编号层级 {segs} 超过 {MAX_OUTLINE_DEPTH} 级")
                elif nid != expect:
                    problems.append(f"{loc}：编号与树位置不一致（应为 {expect}）")
                if "level" in node:
                    try:
                        if int(node["level"]) != segs:
                            problems.append(f"{loc}：level={node['level']} 与编号段数 {segs} 不一致")
                    except (TypeError, ValueError):
                        problems.append(f"{loc}：level 不是数字")
            if nid in seen:
                problems.append(f"{loc}：编号重复（与 {seen[nid]} 冲突）")
            else:
                seen[nid] = loc
            title = str(node.get("title") or "")
            if len(title) > OUTLINE_TITLE_MAX_LEN:
                problems.append(f"{loc}：标题 {len(title)} 字超过 {OUTLINE_TITLE_MAX_LEN} 字上限")
            elif not title.strip():
                # ✅ R209（2026-11-10 · 空标题闸门）：空标题此前**穿过全部闸门**
                # 落库 —— 判据清单只查长度上限；`detect_generic_titles` 对空标题
                # 直接 continue 跳过；`_detect_empty_top_chapters` 只覆盖
                # 「一级且无子节点」。后果：前端树里出现无名节点、导出成稿出现
                # 空标题行，且编制要求覆盖/重复标题判据对该节点全部失效。
                # 前端已拦（"章节标题不能为空"），直连 API / AI 兜底 / 上传识别
                # 不受约束 ⇒ 补齐落库口的 fail-closed 判据（同标题超长口径）。
                problems.append(f"{loc}：标题为空（请填写章节标题）")
            children = node.get("children") or []
            if not isinstance(children, list):
                problems.append(f"{loc}：children 不是数组")
                continue
            _walk(children, expect, loc)

    _walk(outline, "", "outline")
    return problems


def sanitize_outline_title(title) -> tuple[str, bool]:
    """写路径标题清洗：剥离不可见/控制字符，返回 ``(清洗后, 是否改变)``。

    ✅ R187：这些字符用户完全不可见，却会让编制要求覆盖判据、重复标题判据失效，
    并原样进入 DOCX XML（控制字符可致导出件损坏）。
    ⚠️ 刻意**不**做 NFKC 归一、不截断长度 —— 归一/截断属于改写用户可见数据，
    长度超限交由 ``validate_outline_numbering`` 以可行动 400 拒绝。
    """
    original = str(title or "")
    cleaned = _INVISIBLE_TITLE_RE.sub("", original)
    return cleaned, cleaned != original


def _sanitize_outline_titles(nodes: list) -> int:
    """递归清洗目录树中所有标题的不可见/控制字符，返回清洗处数（就地修改）。"""
    changed = 0
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        title, dirty = sanitize_outline_title(node.get("title", ""))
        if dirty:
            node["title"] = title
            changed += 1
        children = node.get("children")
        if isinstance(children, list):
            changed += _sanitize_outline_titles(children)
    return changed


def normalize_outline_for_library(payload) -> tuple[str, list[str]]:
    """目录库**入库**专用规范化（单一事实源）。

    ✅ R209（2026-11-10 · 目录库入库闸门）：目录库三条入库路径
    （``outline_library.create_library`` / ``update_library`` /
    ``upload_outline.save_as_library``）此前只做「三级裁剪 + 编号重排」
    （``normalize_outline_json``），**不**做标题清洗、**不**做落库闸门校验 ——
    于是「库里能存、套用时被拒」：

    * 标题 250 字（> ``OUTLINE_TITLE_MAX_LEN``=200）→ 入库成功、套用时
      ``_save_outline_to_db`` 的落库闸门 400，且错误文案说的是「编号校验未通过」
      （**归因误导**，用户不知该改标题长度）；
    * 标题含零宽字符（U+200B / U+FEFF / U+2060）→ **原样入库**，目录库导出
      的 .md/.txt 文件里带着不可见字符。

    现统一收口：解析 → **清洗不可见字符** → 三级裁剪 + 编号重排 →
    ``validate_outline_numbering`` 校验 → 返回 ``(json, problems)``。
    ``problems`` 非空表示**不可入库**，由调用方以可行动 400 拒绝
    （与 ``sections._save_outline_to_db`` 的落库闸门同口径）。

    Returns:
        ``(规范化后的 JSON 字符串, 问题列表)``；载荷无法解析时返回
        ``("[]", ["目录内容为空或格式非法（应为非空节点数组）"])``。
    """
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return "[]", ["目录内容为空或格式非法（应为非空节点数组）"]
    if isinstance(data, dict):
        data = data.get("outline", data)
    if not isinstance(data, list):
        return "[]", ["目录内容为空或格式非法（应为非空节点数组）"]
    if not data:
        return "[]", ["目录内容为空或格式非法（应为非空节点数组）"]
    try:
        tree = normalize_outline(copy.deepcopy(data))
    except Exception as e:  # pragma: no cover - 纯防御
        # ✅ R213（FIX-1）：异常分支不得 fail-open —— 旧实现把「未经裁剪、
        #    未重排、未校验」的原始载荷原样入库且返回空 problems，与本函数
        #    「绝不静默放行」的契约直接冲突。改为 fail-closed：空目录 + 可行动
        #    错误，由调用方以 400 拒绝（与 sections 落库闸门同口径）。
        logger.warning("目录库入库规范化失败: %s", e, exc_info=True)
        return "[]", [f"目录规范化失败：{e}"]
    _sanitize_outline_titles(tree)
    problems = validate_outline_numbering(tree)
    return json.dumps(tree, ensure_ascii=False), problems


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
        # ✅ R213（FIX-1）：同族异常分支同样不得「保留原始结构」—— 那会把
        #    4 级节点 / 重复编号 / 内嵌编号标题原样写回目录库。改为返回
        #    default（各调用方已有「空目录即拒绝」闸门），失败可见而非静默。
        logger.warning("目录规范化失败，按空目录处理（不落原始结构）: %s", e)
        return default
    return json.dumps(normalized, ensure_ascii=False)


def outline_depth_stats(nodes: list, max_depth: int = MAX_OUTLINE_DEPTH) -> tuple[int, int]:
    """统计目录树的**实际深度**与「超出上限将被折叠」的节点数。

    ✅ R209（2026-11-10 · 裁剪可观测性）：``clamp_outline_depth`` 会把超过
    ``MAX_OUTLINE_DEPTH`` 的深层节点**静默**折叠进父节点 description
    （标题并入父描述，内容不丢但**层级消失**）。前端在保存前有二次确认，
    但直连 API / 脚本 / AI 兜底链**看不到任何后端信号** ⇒ 用户从响应无法
    得知「目录已被降级」。本函数供落库口在折叠前取样，把「折叠了几层、
    折掉了几个节点」如实回传（加法式，不阻断保存）。

    Returns:
        ``(实际最大深度, 超出上限的节点数)``。
    """
    max_seen = 0
    deep_nodes = 0

    def _walk(ns: list, depth: int) -> None:
        nonlocal max_seen, deep_nodes
        for node in ns or []:
            if not isinstance(node, dict):
                continue
            if depth > max_seen:
                max_seen = depth
            if depth > max_depth:
                deep_nodes += 1
            children = node.get("children")
            if isinstance(children, list) and children:
                _walk(children, depth + 1)

    _walk(nodes or [], 1)
    return max_seen, deep_nodes


def _collect_descendant_titles(node: dict) -> list[str]:
    """递归收集节点及其所有后代的标题/说明（用于深度裁剪时保留内容线索）。

    ✅ BUG 修复：旧实现只收集直接子节点标题，孙节点及更深层标题全部丢失。
    例如 L3 节点下有 L4 子节，L4 下又有 L5 细项，旧实现只保留 L4 标题，
    L5 细项标题丢失，导致裁剪后内容线索不完整。
    ✅ 2026-11-05：标题后附 description（若有），确保折叠为段首引导时
    原有文字内容不丢失；同时剥掉标题内嵌的章节编号（"1.1.1.1 建筑面积"
    → "建筑面积"），否则引导文本会带出一串无意义的点分编号。
    """
    from app.services.numbering import strip_outline_numbering_tree

    titles = []
    title = strip_outline_numbering_tree(str(node.get("title") or "")).strip()
    desc = str(node.get("description") or "").strip()
    text = f"{title}：{desc}" if title and desc else (title or desc)
    if text:
        titles.append(text)
    children = node.get("children") or []
    if isinstance(children, list):
        for c in children:
            if isinstance(c, dict):
                titles.extend(_collect_descendant_titles(c))
    return titles


def _strip_prev_merge(desc: str, expected_lead: str = "") -> str:
    """去掉 description 中**已由折叠生成**的后缀（历史与现行写法）。

    ✅ BUG-O3 增强：旧实现只在 `desc.endswith("）") and "（含：" in desc` 时替换，
    若描述后面又追加过其它内容（或被截断），判断失效 → 反复裁剪会叠加出
    "原描述（含：A）（含：B）"。改为用正则锚定末尾，覆盖两种历史写法。
    ✅ 2026-11-10（误剥边界收口）：旧实现把「尾部全是（n）条目」一律当折叠后缀，
    作者自己写的「（1）（2）」编号列表会被整段剥掉。现分三档：
      1. 有显式标记 ``FOLD_GUIDANCE_MARKER`` → 只剥标记及其后的引导（严格幂等）；
      2. 无标记（历史数据）→ 仅当尾部与 ``expected_lead``（本次按当前子节点
         将要生成的引导）**逐字一致**才剥，否则视为作者原文保留；
      3. 其余「（n）列表」一律不动。
    """
    m = _MERGE_SUFFIX_RE.search(desc)
    if m:
        return desc[: m.start()].rstrip()
    if _BARE_MERGE_RE.match(desc):
        return ""

    idx = desc.find(FOLD_GUIDANCE_MARKER)
    if idx >= 0:
        base = desc[:idx].rstrip()
        return base[:-1].rstrip() if base.endswith("；") else base

    if expected_lead and desc.endswith(expected_lead):
        base = desc[: -len(expected_lead)].rstrip()
        return base[:-1].rstrip() if base.endswith("；") else base
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
                if unique_titles:
                    # ✅ 2026-11-05（L4 及以下折叠）：标题+说明改写为父级 L3 的
                    #    「（1）（2）」段首引导，供正文生成在同一节内分层展开；
                    #    不用「（含：…）」形式，以免模型再当作标题层级输出。
                    # ✅ 2026-11-10：引导前写入显式标记，重复折叠只剥标记段；
                    #    作者原有「（1）」列表不再被误剥（见 _strip_prev_merge）。
                    lead = "；".join(f"（{i}）{t}" for i, t in enumerate(unique_titles, 1))
                    desc = _strip_prev_merge(str(node.get("description", "")).strip(), expected_lead=lead)
                    guided = f"{FOLD_GUIDANCE_MARKER}{lead}"
                    node["description"] = f"{desc}；{guided}" if desc else guided
            node["children"] = []
            result.append(node)
        else:
            node["children"] = _clamp_nodes(children, level + 1, max_depth)
            result.append(node)
    return result
