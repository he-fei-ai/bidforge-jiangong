# -*- coding: utf-8 -*-
"""目录占位标题的「外科式重写」出口（R89 收口 · 分层下沉）。

本模块是 :func:`_try_outline_title_fix` 及其全部纯函数依赖的归属模块。
✅ 2026-10-17（分层债收口）：此前整组常驻 ``app/routers/sse_handlers.py``，
而 ``app/routers/sections.py``（目录落库前自动重写）**反向 import** 了
``sse_handlers._try_outline_title_fix`` —— 路由器反向 import 另一个超大路由器的
私有符号是本仓明确的分层债（护栏
``test_prompt_limits_sink_20261006.py::test_no_router_reverse_imports_sse_handlers_private_symbols``
明令禁止）。现整组下沉到本模块：

    · ``sse_handlers`` 改为从本模块 import（同名别名，调用点零改动）；
    · ``sections.py`` 改为从本模块 import —— 两条链路复用**同一实现**，
      口径不分叉。

设计要点（原文保留，勿失传）
---------------------------
- 体量小（最多 40 条 + 上下文），**不会触发节点覆盖率退化护栏**，长方案下远比
  「整份目录重写」可靠；
- 结构零风险：只回填 ``title``，绝不新增/删除/重排 ⇒ 编号与正文链不受影响；
- 失败返回 ``None``，调用方照旧回退完整 AI 审核链路（可回退，不删代码）。
- 受 ``settings.outline_title_fix``（默认 True）控制；置 False 即逐字回到本项
  引入前：不发这次小调用。
"""

from __future__ import annotations

import asyncio
import copy
import logging

from app.config import settings

# ✅ 单一事实源：预算常量沿用既有登记侧，绝不在此另写字面量。
from app.services.ai.json_response import OUTLINE_REPAIR_KEY, collect_json_response
from app.services.ai.prompts._limits import PROJECT_FACTS_LIMIT_OUTLINE
from app.services.ai.prompts._registry import render

# ✅ 占位检出走 outline_quality 的**配置门控 + fail-soft 单一出口**
#    （``detect_generic_titles_for_write``），与 sse_handlers._detect_generic_titles
#    同一开关 ``outline_check_generic_titles``，判据仍唯一来自 outline_quality。
from app.services.outline_quality import (
    detect_generic_titles,
    detect_generic_titles_for_write,
)

# ✅ R179：上下文截断统一走带留痕的实现（超限时写日志 + 计入截断指标）。
# ✅ G6（2026-11-07）：govern_text 为标题修复链路统一治理入口（围栏 → 脱敏 → 预算）。
from app.services.prompt_material_guard import clip_text_notice, govern_text

logger = logging.getLogger("outline_title_fix")


def _govern_prompt_text(text: str) -> str:
    """标题修复 prompt 的统一治理入口（与目录/审核/正文链路同序同开关）。

    ✅ G6（2026-11-07）：此前标题修复 prompt 由 ``_render`` 直接下发 ——
    项目资料摘要 / 项目关键事实未加围栏、凭据未脱敏、也不参与预算，属于
    治理盲区。三个开关（``prompt_injection_defense`` / ``prompt_redact_sensitive``
    / ``prompt_context_budget``）默认关闭，关闭时逐字节返回原文，既有行为不变。
    """
    try:
        return govern_text(text)
    except Exception:
        logger.warning("标题修复提示词治理失败（使用原文继续）", exc_info=True)
        return text


# ✅ 与 sse_handlers 同款口径（原 L222/L227）：下沉后 sse_handlers 从本模块
#    import 这两个名字（``test_outline_generation.py`` 锁 ``sse_handlers.OUTLINE_FIX_TIMEOUT >= 120``）。
OUTLINE_FIX_TIMEOUT = max(10, int(settings.outline_fix_timeout))
OUTLINE_FIX_MAX_TOKENS = int(settings.outline_fix_max_tokens) or None

#: 单条标题改写的长度上限（汉字）：超出几乎必然是模型把整句 description 塞进来。
_TITLE_FIX_MAX_LEN = 60


def _outline_path_index(outline: list) -> dict[str, dict]:
    """位置路径 → 节点上下文索引 ``{node, parent, siblings}``（只读，不改树）。

    path 口径与 ``outline_quality.collect_titles`` 一致（点分位置路径，1 基），
    它是**标题重写的唯一定位键**；编号本身也由位置派生，故改写标题
    不动 path、不触发重编号（位置是编号的唯一事实源）。
    """
    idx: dict[str, dict] = {}

    def _walk(nodes: list, prefix: str, parent_title: str) -> None:
        siblings = [str(n.get("title") or "").strip() for n in (nodes or []) if isinstance(n, dict)]
        siblings = [t for t in siblings if t]
        k = 0
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            k += 1
            path = f"{prefix}.{k}" if prefix else str(k)
            idx[path] = {"node": n, "parent": parent_title, "siblings": siblings}
            kids = n.get("children")
            if isinstance(kids, list) and kids:
                _walk(kids, path, str(n.get("title") or "").strip())

    _walk(outline or [], "", "")
    return idx


def _build_title_fix_items(outline: list, hits: list[dict]) -> str:
    """把占位节点连同上下文（上级章 / 同级兄弟 / 原说明）摊平成提示词清单。"""
    idx = _outline_path_index(outline)
    lines: list[str] = []
    for h in hits[:40]:  # 体量红线：单次调用最多带 40 条
        path = str(h.get("path") or "")
        info = idx.get(path)
        if not info:
            continue
        node = info["node"]
        desc = str(node.get("description") or "").strip()[:60]
        sib = "、".join(f"「{t}」" for t in info["siblings"] if t != str(node.get("title") or "").strip())[:400]
        lines.append(
            f"- path={path}｜层级 L{str(path).count('.') + 1}｜上级章节「{info['parent'] or '（一级）'}」"
            f"｜当前标题「{h.get('title')}」（类型 {h.get('kind')}）"
            f"｜同级兄弟：{sib or '（无）'}｜原说明：{desc or '（无）'}"
        )
    return "\n".join(lines)


#: 「全目录已有标题」清单的字符上限：超出说明目录过大，截断后仍远超模型可
#: 比对范围，再堆长只会挤占待改写清单的上下文预算。
_TITLE_FIX_TITLES_MAX_CHARS = 6000


def _build_existing_titles(outline: list) -> str:
    """摊平全目录已有标题清单（**供模型避让重名用**）。

    ✅ R89 G3：这是「外科式重写产不出具体标题」的根因之一。
    ``_apply_title_updates`` 有一条硬红线「与同级标题重名」拒收，模型若不知道该
    目录已有哪些标题，改写结果只能靠猜 —— 长方案（60+ 章）下猜中率为零，全批被
    拒收，``applied=0`` → 回退整份 AI 审核 → 原样残留。旧提示词只给了**被改节点
    的同级兄弟**，而全目录已有的其它标题（含非占位的正常标题）模型完全不可见，
    因此即便首试猜对也可能与不冲突的同级兄弟之外的既有标题撞上。

    纯只读摊平：不改树、不新增编号信息（展示编号由位置派生，此处只给裸标题）。
    超上限截断并标注，避免静默丢清单让模型误判「已看完全部标题」。
    """
    if not isinstance(outline, list) or not outline:
        return "（无）"
    titles: list[str] = []

    def _walk(nodes: list) -> None:
        for n in nodes:
            if not isinstance(n, dict):
                continue
            t = str(n.get("title") or "").strip()
            if t:
                titles.append(t)
            kids = n.get("children")
            if isinstance(kids, list) and kids:
                _walk(kids)

    _walk(outline)
    text = "、".join(titles)
    if len(text) > _TITLE_FIX_TITLES_MAX_CHARS:
        text = text[:_TITLE_FIX_TITLES_MAX_CHARS] + "…（目录标题过多已截断）"
    return text or "（无）"


def _title_fix_validate_fn(o) -> list[str]:
    """标题重写响应的校验函数：updates 必须是非空数组，每项含 path 与非空 title。

    只判形态，不判语义 —— 语义红线（新标题仍泛化 / 与兄弟重名 / 路径不存在）
    由 :func:`_apply_title_updates` 逐条把守并计入 rejected。
    """
    ups = o.get("updates")
    if not isinstance(ups, list) or not ups:
        return ["updates 必须为非空数组"]
    bad = [
        u
        for u in ups
        if not isinstance(u, dict) or not str(u.get("path") or "").strip() or not str(u.get("title") or "").strip()
    ]
    return [f"存在 {len(bad)} 个缺 path/title 的非法改写项"] if bad else []


def _apply_title_updates(outline: list, updates: list) -> tuple[list, int, list]:
    """按位置路径把新标题精确回填（**纯函数**：不改入参，返回新树）。

    返回 ``(tree, applied, rejected)``。五条保守红线（宁可不改，不可改坏）：
    ① **路径不存在** → 丢弃该条（绝不猜「最接近的节点」，猜错就是静默换标题）；
    ② **新标题自身仍泛化/占位** → 丢弃（否则检出闸门永远关不上）；
    ③ **与同父下任一兄弟（含本批已改的）归一化重名** → 丢弃（剥（N）后缀会造出
      完全同名重复，那是比占位更坏的内容损坏）；
    ④ **只改 title**：description / children / 层级 / 顺序一律原样，节点既不增也不删。
    ⑤ ✅ R73b F4：**先清洗再判定**——含换行的标题会破坏目录树渲染与导出标题行，
      整条拒收；内嵌编号（「2.1 …」「第一章 …」）必须经**唯一事实源**
      `numbering.strip_outline_numbering_tree` 剥离后落地，否则展示层按位置再套一次
      编号 → 「2.1 2.1 灌注桩支护施工」双重编号。本仓其余每一条标题写入路径
      （sections PATCH 改名 / save-outline / 上传落库 / 审核整份修复，均经
      `normalize_outline` 或显式剥离）都做这一步，唯独本出口此前漏写。
    """
    tree = copy.deepcopy(outline or [])
    if not isinstance(updates, list):
        return tree, 0, []
    try:
        from app.services.outline_quality import is_generic_title
    except Exception:  # pragma: no cover - 判据不可用时不做语义拒绝，只做形态校验

        def is_generic_title(_t: str) -> bool:
            return False

    try:
        # 归一化与检出侧共用同一把尺子（先剥「（N）」计数后缀再去空白/标点）
        from app.services.outline_quality import _norm as _q_norm
    except Exception:  # pragma: no cover - 归一化不可用时退化为 strip

        def _q_norm(t: str) -> str:  # type: ignore[no-redef]
            return str(t or "").strip()

    try:
        # ✅ R73b F4：内嵌编号剥离必须复用唯一实现，绝不在此自造正则。
        # ✅ R89 N3：改走 tree 档（strip_outline_numbering_tree），与
        #    `renumber_outline_nodes` / `outline_reorganize._strip_number` 三处口径
        #    收敛。原用单轮档 ⇒ 模型返回「第1章 1.1 工程概况」这类多重章节前缀时
        #    单轮只剥掉「第1章」、残留「1.1 工程概况」落库，展示层按位置再套一次
        #    编号 → 「2.1 1.1 工程概况」双重编号；而 tree 档第一轮严格等价单轮
        #    （所有既有单轮锁定契约逐字不变），仅单轮未收敛时多剥一轮，且每轮入口
        #    过 _SUB_ITEM_MARKER_RE 闸门 —— 目录树标题里的「（1）」是内容而非章节
        #    编号，剥掉会丢子项归属（剥净档 strip_outline_numbering_all 会吃掉，
        #    故不能用它）。
        from app.services.numbering import strip_outline_numbering_tree as _strip_num
    except Exception:  # pragma: no cover - 唯一实现不可用时退化为原样（不误杀）

        def _strip_num(t: str) -> str:  # type: ignore[no-redef]
            return str(t or "")

    try:
        # ✅ R79 C2：编号冲突后缀判据复用唯一事实源；判据不可用时退化为
        #    「不拒收」（与下方 is_generic_title 导入失败同一降级方向 ——
        #    语义拒绝全部让位给形态校验，绝不把外科式重写整体关掉）
        from app.services.outline_quality import has_trailing_counter_suffix as _counter_suffix
    except Exception:  # pragma: no cover - 判据不可用时不新增拒绝

        def _counter_suffix(_t: str) -> bool:  # type: ignore[no-redef]
            return False

    def _norm(t: str) -> str:
        return _q_norm(str(t or "").strip())

    rejected: list[dict] = []
    applied = 0
    # 位置路径索引只算一次（改写只动 title、不动结构，快照兄弟列表仍成立；
    # 同批新标题互撞另由 batch_taken 把守）
    tree_idx = _outline_path_index(tree)
    # 本批已落地的标题：按父路径分组，防「同批两条改成同一个名字」互撞
    batch_taken: dict[str, set[str]] = {}

    for up in updates:
        if not isinstance(up, dict):
            rejected.append({"path": "?", "title": "?", "reason": "非法项（非对象）"})
            continue
        raw_path = up.get("path")
        path = str(raw_path or "").strip()
        new_title = str(up.get("title") or "").strip()
        if not isinstance(raw_path, str) or not path:
            rejected.append({"path": path, "title": new_title, "reason": "path 缺失或非字符串"})
            continue
        if not new_title:
            rejected.append({"path": path, "title": new_title, "reason": "新标题为空"})
            continue
        # ✅ R73b F4：换行不进标题（目录树/导出标题行/编号折行都会被它打坏）
        if "\n" in new_title or "\r" in new_title:
            rejected.append({"path": path, "title": new_title[:20], "reason": "新标题含换行"})
            continue
        # ✅ R73b F4：剥离内嵌编号后再进入后续判定（剥完为空/仍泛化都会被拒）
        new_title = str(_strip_num(new_title) or "").strip()
        if not new_title:
            rejected.append({"path": path, "title": "", "reason": "剥离编号后标题为空"})
            continue
        if len(new_title) > _TITLE_FIX_MAX_LEN:
            rejected.append({"path": path, "title": new_title[:20], "reason": "新标题过长"})
            continue
        if is_generic_title(new_title):
            rejected.append({"path": path, "title": new_title, "reason": "新标题仍是泛化/占位名称"})
            continue
        # ✅ R79 C2：**第 6 条红线**——「（N）」编号冲突后缀一律拒收。
        #    检测侧 `detect_generic_titles` 的 conflict_suffix 判定要求
        #    「去后缀基名与其它节点碰撞」才报（刻意保守：防误伤
        #    「深基坑支护（2）」这类合法部位限定标题）；回写侧只看形态：
        #    否则 AI 把「专项6」改写成「钢内支撑安装（2）」后，单样本在
        #    复检里**恰好不碰撞 ⇒ 恒过**，用户报的症状经外科式重写原样
        #    落库。宁可不改（复检如实报残留 → 转人工），不可改出一个带
        #    编号冲突后缀的标题。
        if _counter_suffix(new_title):
            rejected.append({"path": path, "title": new_title[:20], "reason": "新标题带（N）编号冲突后缀"})
            continue
        info = tree_idx.get(path)
        if not info:
            rejected.append({"path": path, "title": new_title, "reason": "路径不存在"})
            continue
        node = info["node"]
        old_title = str(node.get("title") or "").strip()
        parent_path = path.rsplit(".", 1)[0] if "." in path else ""
        taken = batch_taken.setdefault(parent_path, set())
        cand = _norm(new_title)
        clash = [_norm(t) for t in info["siblings"] if _norm(t) and _norm(t) != _norm(old_title)]
        if cand in taken or cand in clash:
            rejected.append({"path": path, "title": new_title, "reason": "与同级标题重名"})
            continue
        node["title"] = new_title
        taken.add(cand)
        applied += 1

    return tree, applied, rejected


async def _try_outline_title_fix(
    outline: list,
    hits: list[dict],
    *,
    scheme_name: str = "",
    scheme_type: str = "",
    project_brief: str = "",
    project_facts: str = "",
    construction_scope: str = "",
    requirements: str = "",
    scheme_basis: str = "",
    standards_text: str = "",
    _collect=None,
    _render=None,
) -> list | None:
    """外科式标题重写：只把占位节点连同上下文送给模型，换回具体标题。

    与「整份目录重写」的取舍（本轮核心设计判断）：
    - 体量小（最多 40 条 + 上下文），**不会触发节点覆盖率退化护栏**，长方案下
      远比 ``outline_feedback_system`` 可靠；
    - 结构零风险：只回填 title，绝不新增/删除/重排 ⇒ 编号与正文链不受影响；
    - 失败返回 None，调用方照旧回退完整 AI 审核链路（可回退，不删代码）。

    受 ``settings.outline_title_fix``（默认 True）控制；置 False 即逐字回到
    本项引入前：不发这次小调用。

    ✅ R89 G3（提示词补料）：旧实现只传 scheme_name / scheme_type /
    project_brief（前 1500 字）/ project_facts / broken_items 五个变量，而模板
    里实际声明的 ``construction_scope`` / ``requirements`` / ``scheme_basis`` /
    ``standards_text`` / ``existing_titles`` 从未传值 ⇒ 渲染为空串。长方案（60+
    章）下模型唯一可依据的是 1500 字的项目摘要，**根本没有「这个专项具体是什么
    施工」的信息**，只能回出「专项6」「总体实施策划（2）」这类占位名 → 全部被
    六条保守红线拒收 → ``applied=0`` → 回退整份 AI 审核 → 占位标题原样残留落库
    （R79 起的加法式设计不阻断落库）。现在：
      ① 五个漏传变量全部接上（口径与 ``outline_short_system`` /
         ``outline_level1_system`` 同：为空时渲染为空串，不干扰模型）；
      ② 新增 ``existing_titles``（全目录已有标题清单），让模型避开重名，而不是靠猜。

    ✅ R89 G4（失败回灌重试）：复用 ``content_fact_autofix`` 的「失败原因回灌
    重试」口径 —— 首试后仍残留的条目，把**程序拒收原因**（仍是泛化名 / 与同级
    标题重名 / 剥离编号后为空 / 过长 / 含换行 / 带（N）后缀 / 路径不存在）回灌
    给模型再试一轮。受 ``settings.outline_autofix_generic_retries`` 控制
    （默认 2；置 0 即逐字回到单次调用），每次回灌都重新生成「仍待改写」清单，
    故成本中性（只在确有残留时才发起额外调用）。

    ✅ 2026-10-17（分层下沉）：整函数由 ``routers/sse_handlers.py`` 原样迁入本模块，
    行为逐字不变；占位复检改调 ``detect_generic_titles_for_write``
    （与 ``sse_handlers._detect_generic_titles`` 同一开关、同一判据出口）。
    """
    if not hits or not getattr(settings, "outline_title_fix", True):
        return None
    items = _build_title_fix_items(outline, hits)
    if not items:
        return None

    # ✅ 依赖注入（可选）：调用方（sse_handlers）显式传入自己的 collect/render 绑定，
    #    使针对 sse_handlers 的 monkeypatch（``sh.collect_json_response`` / ``sh.render``）
    #    仍然生效；未传时回落到本模块导入的单一事实源（sections.py 直连链路）。
    _collect = _collect or collect_json_response
    _render = _render or render

    try:
        _retries = max(0, int(getattr(settings, "outline_autofix_generic_retries", 2) or 0))
    except (TypeError, ValueError):
        _retries = 0
    _total = _retries + 1
    existing = _build_existing_titles(outline)
    feedback = ""
    tree = None
    applied_total = 0

    for attempt in range(1, _total + 1):
        try:
            prompt = _render(
                "outline_title_fix_system",
                scheme_name=scheme_name,
                scheme_type=scheme_type,
                construction_scope=construction_scope,
                # ✅ R179：硬切片改为带日志/指标的可见截断（超限时留痕）。
                project_brief=clip_text_notice(project_brief or "", 1500, "目录标题修复-project_brief"),
                project_facts=clip_text_notice(project_facts or "", PROJECT_FACTS_LIMIT_OUTLINE, "目录标题修复-project_facts"),
                scheme_basis=scheme_basis,
                standards_text=standards_text,
                requirements=requirements,
                existing_titles=existing,
                broken_items=items,
                rewrite_feedback=feedback,
            )
            # ✅ G6（2026-11-07）：标题修复链路接入围栏/脱敏/预算（默认关闭时逐字节不变）。
            prompt = _govern_prompt_text(prompt)
            obj, _raw = await asyncio.wait_for(
                _collect(
                    [{"role": "system", "content": prompt}],
                    _title_fix_validate_fn,
                    json_mode=True,
                    temperature=0.2,
                    scene="outline_fix",
                    repair_key=OUTLINE_REPAIR_KEY,
                    max_tokens=OUTLINE_FIX_MAX_TOKENS,
                ),
                timeout=OUTLINE_FIX_TIMEOUT,
            )
            updates = obj.get("updates") if isinstance(obj, dict) else None
            if not isinstance(updates, list) or not updates:
                if attempt == 1:
                    return None
                break
            tree, applied, rejected = _apply_title_updates(outline if tree is None else tree, updates)
            applied_total += applied
            # 残留 = 首试基线里仍未被改掉的条目（用程序自己的检出判据复判，
            # 而不是只信上一轮的红线拒绝列表 —— 模型可能「改了一个仍是占位名的
            # 标题」，那条不会进 rejected 但仍需回灌）
            if tree is not None:
                _still = detect_generic_titles_for_write(tree)
                if not _still and attempt == 1:
                    break
                if _still:
                    items = _build_title_fix_items(tree, _still)
                    if not items:
                        break
                _why = [f"path={r.get('path')}「{r.get('title')}」→ {r.get('reason')}" for r in (rejected or [])]
                if _still:
                    _why.insert(0, f"（上一轮后目录仍有 {len(_still)} 处泛化标题未被修正）")
                feedback = "\n".join(_why) if _why else ""
            if attempt >= _total:
                break
            if not feedback:
                break
        except asyncio.TimeoutError:
            logger.warning(
                "占位标题重写超时（%ds，第 %d/%d 轮），回退原链路（scheme=%s）",
                OUTLINE_FIX_TIMEOUT,
                attempt,
                _total,
                scheme_name,
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "占位标题重写失败（scheme=%s，第 %d/%d 轮）: %s，回退原链路", scheme_name, attempt, _total, e
            )
            return None

    if tree is None or not applied_total:
        logger.info("占位标题重写未生效（%d 轮全被保守红线拒绝），残留 %d 条待人工核对", _total, len(hits))
        return None
    _left = detect_generic_titles_for_write(tree)
    if _left:
        logger.info(
            "占位标题重写收敛后仍有 %d 处残留（已落地 %d 条，共 %d 轮）：%s",
            len(_left),
            applied_total,
            _total,
            [h.get("title") for h in _left[:5]],
        )
    return tree


def _tree_shape(nodes) -> tuple:
    """目录树**形态指纹**：只记「每节点的子节点数」，标题/id 均不参与。

    ✅ R101 加固：自动改写的契约是「只回填 title、绝不增删节点」。改写结果要被
    upload 路径按「子节点索引路径」还原改写前的原标题做正文匹配，**一旦结构错位
    就会整体张冠李戴**。此指纹用于改写前后比对，错位即丢弃改写结果。

    返回纯 tuple（不持有输入引用），故对「就地改写」与「重建新树」两种实现
    都成立：快照在调用前生成，比较值不受后续就地变更影响。
    """
    if not isinstance(nodes, list):
        return ()
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        kids = n.get("children")
        kids = kids if isinstance(kids, list) else []
        out.append((len(kids), _tree_shape(kids)))
    return tuple(out)


async def autofix_generic_titles_on_save(
    outline: list,
    hits: list[dict],
    *,
    scheme_name: str = "",
    scheme_type: str = "",
    context: str = "",
) -> tuple[list, list[dict], int]:
    """落库前自动外科式重写占位标题（**单一实现**，目录写路径共用）。

    ✅ R101 接线补齐（本轮）：该逻辑此前内联在
    ``sections._save_outline_to_db``，而 ``upload_outline.save_as_outline``
    自建 DELETE/INSERT、**不经** ``_save_outline_to_db`` ⇒ 上传识别入口零闸门，
    与 ``config.outline_autofix_generic_on_save`` 注释声明的「目录库套用 /
    上传识别 / AI 调整 / 手工编辑后保存」四类写入口全覆盖不符。
    现抽为单一实现，两条写路径共用**同一口径**（不再分叉）。

    契约（务必保持向后兼容）：
      - 受 ``settings.outline_autofix_generic_on_save``（默认 True）控制；
        置 False 即逐字回到 R89 之前：只检出、只回传、**不发起重写调用**；
      - ``hits`` 为空 → 直接原样返回，**零 AI 调用、零额外延迟**；
      - 任何异常 / 超时一律 fail-soft：原样返回，**绝不阻断保存**；
      - 返回 ``(重写后目录, 重写后残留明细, 自动改写条数)``；第三条仅在确有
        改写且残留减少时 > 0 —— 调用方据此**加法式**回传 ``generic_autofixed``，
        使用户感知到「标题已被自动改写」（否则静默改写，用户以为原样保存）；
      - **结构指纹守卫**（R101 加固）：改写结果的结构与输入不一致即整条丢弃，
        因为 upload 路径依赖「结构一一对应」才能把原标题映射回改写后节点。
    """
    if not hits or not getattr(settings, "outline_autofix_generic_on_save", True):
        return outline, hits, 0
    try:
        _before = len(hits)
        _shape_before = _tree_shape(outline)
        _fixed = await _try_outline_title_fix(outline, hits, scheme_name=scheme_name, scheme_type=scheme_type)
        if _fixed is None:
            return outline, hits, 0
        if not isinstance(_fixed, list) or _tree_shape(_fixed) != _shape_before:
            logger.warning(
                "目录落库前自动重写返回的结构与输入不一致（疑似增删节点），" "已丢弃改写结果、按原样保存（context=%s）",
                context or "-",
            )
            return outline, hits, 0
        _left = detect_generic_titles(_fixed)
        _count = max(0, _before - len(_left))
        logger.info(
            "目录落库前自动重写占位标题%s：%d 处 → 残留 %d 处（自动改写 %d 条）",
            f"（{context}）" if context else "",
            _before,
            len(_left),
            _count,
        )
        return _fixed, _left, _count
    except Exception as e:  # noqa: BLE001 - 自动修复失败不得阻断保存
        logger.warning("目录落库前自动重写失败（按原样保存，context=%s）: %s", context or "-", e, exc_info=True)
        return outline, hits, 0
