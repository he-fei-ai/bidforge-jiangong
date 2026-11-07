"""建树/遍历点棘轮锁（R184 后续 · 2026-11-07）。

背景：``sections._build_tree`` 的 ``pid != nid`` 只保护**一条**读路径。backend 里仍
有多处消费方自己从 sections 行集构造父子关系、自己递归，或按 ``parent_id`` 分组。
这些点位对「自父 / 环」没有统一守卫时不会崩，而是**静默丢节点**（比崩溃更难发现），
且新增点位无人拦截。

本文件用 AST 扫描**真实源码**做三件事（全程只读，不写盘）：

1. 统一入口锁：所有 ``_build_tree(`` 调用点必须登记，且理由非空；
2. 建树点棘轮：扫描器识别出的所有建树/遍历点必须在白名单内，裁定只能是
   ``unified``（复用统一入口）/ ``guard``（有等价守卫）/ ``gap``（暂无守卫，
   已登记后果形态，禁止新增同类点位而不登记）/ ``scope``（不消费行集父子关系）；
3. 检测器自检：把未登记的建树代码注入真实源码的**内存副本**，扫描器必须抓到；
   前后比对源文件字节，证明测试本身没有写盘。

扫描器覆盖四类模式（均为「sections 行集 → 父子结构」的典型写法）：
A. 按 ``parent_id`` 分组：``x.setdefault(<parent_id 表达式>, ...)``；
B. 子节点装配：``nodes[<parent_id>]["children"].append(...)``；
C. 自递归遍历：函数直接调用自身，且作用域内出现 children/parent 相关标识；
D. 向上父链：``while`` 条件引用的变量在循环体内由 ``parent_id`` 重新赋值。
"""

import ast
import hashlib
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: 扫描范围：所有会消费 sections 行集父子关系的模块（新增模块请一并纳入）。
SCANNED_FILES = (
    "routers/sections.py",
    "routers/export.py",
    "routers/_export_issue_scan.py",
    "routers/outline_library.py",
    "routers/sse_handlers.py",
    "routers/_sse_outline_build.py",
    "routers/upload_outline.py",
    "services/content_utils.py",
    "services/placeholder_inventory.py",
    "services/preflight_engine.py",
    "services/consistency_scanner.py",
    "services/numbering.py",
    "services/scheme_classification.py",
    "services/scoring_items.py",
    "services/outline_versions.py",
    "routers/schemes.py",
    "routers/compliance.py",
    "routers/_export_cache.py",
)

#: 扫描器识别出的点位 → (裁定, 理由)。理由必须非空且说清「为什么安全 / 后果形态」。
REGISTRY: dict[str, tuple[str, str]] = {
    # ---- 统一入口（sections._build_tree 及其树内递归） ----
    "routers/sections.py::_build_tree": (
        "unified",
        "唯一建树入口；已含 pid != nid 守卫，自父视根、环组件整体不可达",
    ),
    "routers/sections.py::_build_tree._sort_children": (
        "unified",
        "递归只从 roots 出发；自父/环组件不在 roots，整体不可达，不会成环",
    ),
    "routers/sections.py::_outline_structure_report._walk": (
        "unified",
        "_outline_structure_report 先调 _build_tree，入参树已无自父/环，DFS 不会成环",
    ),
    # ---- 有等价守卫 ----
    "routers/sections.py::_save_outline_to_db": (
        "guard",
        "children_map 仅用于删除级联；_collect_delete_ids 用 all_ids 集合去重，"
        "自父/环在第二次相遇时终止，可达节点全部删除",
    ),
    "services/content_utils.py::recompute_section_numbers": (
        "guard",
        "适配层显式 pid != sid，自父视根；与 _build_tree 守卫后同口径",
    ),
    "services/content_utils.py::order_sections_dfs": (
        "guard",
        "显式 pid != id；且构造后把未挂接行（自父/环/孤儿）追加到末尾，不丢节点",
    ),
    "services/content_utils.py::validate_section_tree": (
        "guard",
        "显式 self-parent / cycle / orphan 检测并产出 issues，具备 visited 守卫",
    ),
    "services/preflight_engine.py::build_section_tree_index": (
        "guard",
        "_walk 带 stack 集合 + child != sid；自父/环在命中时退化为只看自身正文",
    ),
    "services/preflight_engine.py::build_section_tree_index._walk": (
        "guard",
        "同上：stack 环保护 + child != sid，绝不无限递归",
    ),
    "services/preflight_engine.py::_l3_outline_stats": (
        "guard",
        "向上寻章带 seen 集合，自父/环在命中时 break；不会无限循环"
        "（自父节点会从 per_chapter 统计中缺席，属已知统计偏差）",
    ),
    "services/scheme_classification.py::top_ancestor": (
        "guard",
        "向上父链带 seen 集合，遇到重复节点即终止",
    ),
    "services/placeholder_inventory.py::build_rerun_plan_from_report": (
        "gap",
        "children_map 仅判叶子，无 pid != id；自父/环节点被判定为非叶子，"
        "占位补录计划静默漏掉该节点",
    ),
    "routers/upload_outline.py::save_as_outline._old_title_path": (
        "guard",
        "向上父链带 seen 集合 + guard < 50 上限，自父/环在第二次相遇时终止",
    ),
    # ---- 暂无守卫（已登记后果形态） ----
    "routers/export.py::_prepare_export": (
        "gap",
        "自建 children_map/roots 无 pid != sid；自父/环节点不进 roots，"
        "其整棵子树被导出写路径静默跳过（成稿缺章）",
    ),
    "routers/export.py::_compute_doc_order_rank._walk": (
        "gap",
        "只从 roots DFS；自父/环组件不在 roots，order_rank 缺失 → 跨章借图排位错误",
    ),
    "routers/_export_issue_scan.py::_detect_duplicate_sections": (
        "gap",
        "只判 pid in by_id 未排除自父；自父节点挂进自身 children，DFS 不可达 → 重复编号漏检",
    ),
    "routers/_export_issue_scan.py::_detect_duplicate_sections.walk": (
        "gap",
        "同上层：自父/环组件静默漏检",
    ),
    "routers/_export_issue_scan.py::_detect_section_number_mismatch": (
        "gap",
        "自建 children_map 无 pid != sid，自父/环组件不参与 DLV-12 结构漂移比对 → 漏检",
    ),
    "routers/_export_issue_scan.py::_detect_section_number_mismatch.walk": (
        "gap",
        "同上层：无法从 roots 到达的组件被静默跳过",
    ),
    "routers/_export_issue_scan.py::collect_figure_table_number_superset": (
        "gap",
        "自建 children_map 无 pid != sid；被排除子树中的图表号不在集合内，"
        "既可能漏检，也可能把合法引用误报成悬空引用",
    ),
    "routers/_export_issue_scan.py::collect_figure_table_number_superset.walk": (
        "gap",
        "同上层：DFS 只覆盖 roots 可达组件",
    ),
    "routers/outline_library.py::create_from_scheme": (
        "gap",
        "自建 by_parent 从根递归且无 pid != sid；自父/环节点在保存目录库时静默丢失",
    ),
    "routers/outline_library.py::create_from_scheme.build": (
        "gap",
        "同上层：只从空 parent 的根出发，自父/环组件永不进入结果",
    ),
    "routers/sse_handlers.py::generate_content": (
        "gap",
        "by_parent 分组无 pid != sid；自父/环节点不参与兄弟/父子上下文，提示词静默缺段",
    ),
    "routers/sse_handlers.py::generate_content.event_stream": (
        "gap",
        "同上层：正文生成上下文按 roots/by_parent 组织，自父/环组件被排除",
    ),
    "routers/sse_handlers.py::generate_content.event_stream._build_generation_context": (
        "gap",
        "向上父链 while _pid and _pid in nodes_map 无 seen/深度上限；"
        "自父或成环祖先会让任务**无限循环**（生成任务挂死，非静默丢节点）",
    ),
    "routers/sse_handlers.py::generate_content.event_stream._find_unit": (
        "gap",
        "word_budget_override 分支的向上寻单元无 seen/深度上限；"
        "level>2 的自父节点或环会让预算分配**无限循环**",
    ),
    "routers/_sse_outline_build.py::_build_parent_chain": (
        "gap",
        "while parent_id and parent_id in nodes 且循环体再次取 parent_id，"
        "无 seen/深度上限；自父或有叶子后代的两点环会让正文生成任务**无限循环**",
    ),
    "services/content_utils.py::select_target_leaves": (
        "gap",
        "children_map 无 pid != sid；自父/环节点的 id 落在 parent 集合，永远不被选为叶子，"
        "正文生成静默跳过（也不报缺章）",
    ),
    "routers/upload_outline.py::save_as_outline": (
        "scope",
        "此处按 (parent_id, level) 做标题匹配桶，不构造树、不递归；"
        "自父只影响桶键，不会丢节点",
    ),
    "services/scoring_items.py::build_outline_tree": (
        "gap",
        "自建 nodes/roots 仅判 pid in nodes、未排除自父；自父节点挂进自身 children，"
        "评分目录树从 roots 出发时静默丢掉该节点及其子树",
    ),
    "services/scoring_items.py::flatten_outline._walk": (
        "gap",
        "递归只覆盖 build_outline_tree 产出的 roots；自父/环组件不在 roots，"
        "评分项与章节的映射缺章（覆盖率报告少算章节）",
    ),
    # ---- 与本议题无关的递归（作用对象是请求体目录 JSON，不是 sections 行） ----
    "routers/sections.py::_save_outline_to_db._preserve_original_ids": (
        "scope",
        "递归对象是 save-outline 请求体的 outline 节点，节点无 parent_id 字段，"
        "不消费 sections 行集父子关系",
    ),
    "routers/sections.py::_save_outline_to_db._process_nodes": (
        "scope",
        "同上：outline 请求体 JSON 递归，父子关系由请求体 children 自带",
    ),
    "routers/sections.py::_save_outline_to_db._strip_internal_fields": (
        "scope",
        "同上：仅清理 outline 请求体内部字段，不涉及 sections 表",
    ),
    "routers/upload_outline.py::save_as_outline._snapshot_titles": (
        "scope",
        "递归对象是上传的 outline 节点，不读 sections.parent_id",
    ),
    "routers/upload_outline.py::save_as_outline.collect_rows": (
        "scope",
        "递归对象是上传的 outline 节点，不读 sections.parent_id",
    ),
}

#: 扫描器不覆盖、但人工枚举到的建树/遍历点 → (裁定, 理由)。
#: 仅做存在性校验，防止清单与源码漂移；新增点位仍必须登记到 REGISTRY 或本表。
MANUAL_INVENTORY: dict[str, tuple[str, str]] = {
    "routers/sections.py::sections_quality": (
        "gap",
        "只用 parent_id 集合判叶子（无守卫）；自父/环节点被当成父节点，"
        "无正文时不进 section_total → 覆盖率分母偏小、未检查数被低估",
    ),
    "routers/sections.py::reorder_sections": (
        "scope",
        "只构造 parent_of 映射用于同级 sort_order 局部排序，不递归、不丢节点",
    ),
    "routers/sections.py::delete_section": (
        "guard",
        "while pending 逐层收集后代，已删 id 不重复入队，自父/环可终止",
    ),
    "routers/upload_outline.py::save_as_outline._collect_subtree": (
        "guard",
        "栈式收集 + all_ids 集合去重，自父/环在第二次相遇时终止",
    ),
    "services/consistency_scanner.py::load_leaf_sections": (
        "gap",
        "parent_ids 集合判叶子（无守卫）；自父/环节点不被当叶子，"
        "一致性扫描静默跳过这些章节",
    ),
    "services/numbering.py::renumber_outline_nodes": (
        "guard",
        "_path 按 id(node) 记录当前递归路径 + _MAX_TREE_DEPTH 上限，环引用即时终止",
    ),
    "services/numbering.py::renumber_section_outline_ids._walk": (
        "guard",
        "visited 集合检测重复/环引用 + 深度上限，超限子树跳过并告警",
    ),
    "services/numbering.py::load_scheme_section_index": (
        "gap",
        "GROUP BY parent_id 统计 has_children；自父行会把自己计成自己的子节点，"
        "该章被当成父节点 → 正文子标题规范化走父分支，口径偏移",
    ),
    "services/scoring_items.py::ancestors_of": (
        "guard",
        "向上父链 while cur and guard < 20，自父/环最多迭代 20 次即停",
    ),
    "routers/export.py::_build_docx_sync": (
        "gap",
        "成稿正文按 roots/children_map 递归写出；自父/环组件不在 roots，"
        "整段章节静默不出现在导出文档（与 _prepare_export 同源）",
    ),
    "routers/_export_issue_scan.py::_detect_body_subheading_namespace_conflict": (
        "gap",
        "parent_ids 集合判「有无子章节」；自父行被误判为父章节，"
        "其正文里的点分子标题会被当成与子章节撞号（误报）",
    ),
    "routers/_sse_outline_build.py::_build_chapter_extraction_map._top_ancestor": (
        "guard",
        "向上寻根有 for _ in range(20) 深度上限，自父/环最多迭代 20 次即停",
    ),
    "routers/_sse_outline_build.py::_build_chapter_extraction_map": (
        "gap",
        "_has_children 判定自父节点有子 → 不注入章节抽取映射，该节点正文静默缺抽取",
    ),
    "services/outline_versions.py::restore_outline_snapshot": (
        "gap",
        "快照回退原样写回 parent_id（无自父/环清理），历史脏数据会被再次搬运落库",
    ),
    "routers/schemes.py::duplicate_scheme": (
        "gap",
        "方案复制按 id_map 逐行搬运 parent_id（无自父/环清理），源库脏数据原样扩散",
    ),
}

_ALLOWED_MODES = {"unified", "guard", "gap", "scope"}

#: `_build_tree(` 调用点 → 理由（统一入口的消费方，只允许出现在这个登记表里）。
BUILD_TREE_CALLERS: dict[str, str] = {
    "routers/sections.py::_outline_structure_report": "结构自检直接复用统一入口，不另建树",
    "routers/sections.py::_repair_outline_structure_numbering": "结构修复复用统一入口，不另建树",
    "routers/sections.py::_save_outline_to_db": "保存前复用统一入口做删除级联/结构判断",
    "routers/sections.py::adjust_outline": "目录调整复用统一入口，不另建树",
    "routers/sections.py::export_tree": "目录导出复用统一入口，不另建树",
    "routers/sections.py::list_sections": "目录列表读路径复用统一入口",
    "routers/sections.py::renumber_sections_after_reorder": "重排后复用统一入口取树再重编号",
    "routers/sections.py::reorder_sections": "拖拽排序复用统一入口，不另建树",
}


# ---------------------------------------------------------------------------
# AST 扫描器（与生产代码无关，纯静态）
# ---------------------------------------------------------------------------
def _identifiers(node: ast.AST) -> set[str]:
    """收集子树里的标识符/字符串常量，用于判定「是否涉及 parent/children」。"""
    found: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            found.add(n.id)
        elif isinstance(n, ast.Attribute):
            found.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            found.add(n.value)
    return found


def _parent_related(node: ast.AST) -> bool:
    return any("parent" in name for name in _identifiers(node))


def _direct_nodes(fn: ast.AST):
    """遍历函数体，但不下钻嵌套函数定义（嵌套函数单独登记）。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            stack.append(child)


def _tainted_names(fn: ast.AST) -> set[str]:
    """函数内被 parent_id 赋值污染的变量名（含 pid = n.get("parent_id")）。"""
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in _direct_nodes(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                value = node.value
                if _parent_related(value) or (_identifiers(value) & tainted):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id not in tainted:
                            tainted.add(target.id)
                            changed = True
    return tainted


def _is_parent_grouping(call: ast.Call, tainted: set[str]) -> bool:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "setdefault"):
        return False
    if not call.args:
        return False
    key = call.args[0]
    return _parent_related(key) or bool(_identifiers(key) & tainted)


def _is_children_assembly(node: ast.Call, tainted: set[str]) -> bool:
    if not (isinstance(node.func, ast.Attribute) and node.func.attr == "append"):
        return False
    container = node.func.value
    if not isinstance(container, ast.Subscript):
        return False
    if not (isinstance(container.slice, ast.Constant) and container.slice.value == "children"):
        return False
    inner = container.value
    if not isinstance(inner, ast.Subscript):
        return False
    return _parent_related(inner.slice) or bool(_identifiers(inner.slice) & tainted)


def _is_upward_parent_loop(node: ast.While) -> bool:
    test_names = _identifiers(node.test)
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Assign) and _parent_related(stmt.value):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id in test_names:
                    return True
    return False


def _is_self_recursive(fn: ast.AST) -> bool:
    called: set[str] = set()
    for node in _direct_nodes(fn):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return fn.name in called


def _mentions_tree_identity(fn: ast.AST, parent_scope: bool) -> bool:
    names = _identifiers(fn)
    if any(("children_map" in n or "by_parent" in n or "parent" in n) for n in names):
        return True
    # 嵌套在 parent 感知作用域内的纯 children 递归（如 _build_tree._sort_children）
    return parent_scope and any("children" in n for n in names)


class _SiteVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._stack: list[str] = []
        self._parent_scope: list[bool] = []
        self.sites: dict[str, list[str]] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._stack.append(node.name)
        self._parent_scope.append(_parent_related(node))
        qualified = ".".join(self._stack)
        tainted = _tainted_names(node)
        hits: list[str] = []
        for child in _direct_nodes(node):
            if isinstance(child, ast.Call):
                if _is_parent_grouping(child, tainted):
                    hits.append("group")
                elif _is_children_assembly(child, tainted):
                    hits.append("assemble")
            elif isinstance(child, ast.While) and _is_upward_parent_loop(child):
                hits.append("upward")
        if _is_self_recursive(node) and _mentions_tree_identity(node, any(self._parent_scope)):
            hits.append("recursive")
        if hits:
            self.sites[qualified] = sorted(set(hits))
        for child in node.body:
            self.visit(child)
        self._stack.pop()
        self._parent_scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815


def _detect(source: str) -> dict[str, list[str]]:
    visitor = _SiteVisitor()
    visitor.visit(ast.parse(source))
    return visitor.sites


def _function_names(source: str) -> set[str]:
    """返回带嵌套限定的函数名（outer.inner），与扫描器点位键保持同口径。"""
    names: set[str] = set()

    class _NameVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.stack.append(node.name)
            names.add(".".join(self.stack))
            for child in node.body:
                self.visit(child)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    _NameVisitor().visit(ast.parse(source))
    return names


def _build_tree_callers(source: str) -> set[str]:
    callers: set[str] = set()

    class _CallerVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self.stack.append(node.name)
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "_build_tree"
                ):
                    callers.add(".".join(self.stack))
            for child in node.body:
                self.visit(child)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    _CallerVisitor().visit(ast.parse(source))
    return callers


def _source(relpath: str) -> str:
    return (APP_ROOT / relpath).read_text(encoding="utf-8")


def _all_detected() -> dict[str, list[str]]:
    detected: dict[str, list[str]] = {}
    for relpath in SCANNED_FILES:
        for qualname, patterns in _detect(_source(relpath)).items():
            detected[f"{relpath}::{qualname}"] = patterns
    return detected


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_every_build_tree_call_site_is_registered():
    """统一入口锁：每个 _build_tree 调用点都必须登记「复用统一入口」。"""
    callers: dict[str, list[str]] = {}
    for relpath in SCANNED_FILES:
        for qualname in _build_tree_callers(_source(relpath)):
            callers.setdefault(f"{relpath}::{qualname}", []).append(relpath)
    assert callers, "_build_tree 调用点为空，扫描器失效"
    missing = sorted(set(callers) - set(BUILD_TREE_CALLERS))
    assert not missing, f"未登记的 _build_tree 调用点（新增读路径必须登记）：{missing}"
    stale = sorted(set(BUILD_TREE_CALLERS) - set(callers))
    assert not stale, f"统一入口登记表存在失效条目：{stale}"
    for site in callers:
        assert BUILD_TREE_CALLERS[site].strip(), f"{site} 理由为空（棘轮锁要求理由非空）"


def test_every_detected_tree_site_is_registered():
    """建树点棘轮：扫描器识别的每个点位都必须在白名单内且理由非空。"""
    detected = _all_detected()
    missing = sorted(set(detected) - set(REGISTRY))
    assert not missing, (
        "发现未登记的建树/遍历点（自父/环会静默丢节点，必须先登记裁定与理由）："
        f"{missing}"
    )
    stale = sorted(set(REGISTRY) - set(detected))
    assert not stale, f"白名单里存在扫描器已不再识别的失效条目：{stale}"
    for site in detected:
        mode, reason = REGISTRY[site]
        assert mode in _ALLOWED_MODES, f"{site} 裁定非法：{mode}"
        assert reason.strip(), f"{site} 理由为空（棘轮锁要求理由非空）"


def test_manual_inventory_names_exist_in_source():
    """人工枚举清单不得与源码漂移（防止函数改名/删除后清单变成假保护）。"""
    for site in MANUAL_INVENTORY:
        relpath, func_name = site.split("::", 1)
        source = _source(relpath)
        assert func_name in _function_names(source), f"人工清单点位在源码中不存在：{site}"
        mode, reason = MANUAL_INVENTORY[site]
        assert mode in _ALLOWED_MODES, f"{site} 裁定非法：{mode}"
        assert reason.strip(), f"{site} 理由为空"


def test_build_tree_static_guard_not_removed():
    """源码静态锁：_build_tree 的自父守卫不得被静默移除。"""
    source = _source("routers/sections.py")
    tree = ast.parse(source)
    segment = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_build_tree":
            segment = ast.get_source_segment(source, node)
            break
    assert segment, "未找到 _build_tree 定义"
    assert "pid != nid" in segment, "_build_tree 的 pid != nid 守卫被移除"


def test_audit_and_export_guard_reuse_structure_report():
    """审核 DLV-13 与导出严格守卫只能复用 _outline_structure_report，不得另建树。"""
    for relpath in ("routers/compliance.py", "routers/_export_cache.py"):
        source = _source(relpath)
        assert "_outline_structure_report" in source, (
            f"{relpath} 不再复用 sections._outline_structure_report（结构自检出现旁路）"
        )
        assert "_build_tree(" not in source and "def _build_tree" not in source, (
            f"{relpath} 自建 _build_tree（应复用 sections._outline_structure_report）"
        )


@pytest.mark.parametrize(
    "name,snippet,expected_pattern",
    [
        (
            "group",
            "def _injected_group(sections):\n"
            "    by_parent = {}\n"
            "    for row in sections:\n"
            '        by_parent.setdefault(row.get("parent_id", ""), []).append(row)\n',
            "group",
        ),
        (
            "assemble",
            "def _injected_assemble(nodes):\n"
            "    roots = []\n"
            "    for nid, node in nodes.items():\n"
            '        pid = node.get("parent_id")\n'
            '        nodes[pid]["children"].append(node)\n'
            "    return roots\n",
            "assemble",
        ),
        (
            "upward",
            "def _injected_upward(nodes, leaf):\n"
            '    pid = leaf.get("parent_id", "")\n'
            "    out = []\n"
            "    while pid and pid in nodes:\n"
            "        parent = nodes[pid]\n"
            "        out.append(parent)\n"
            '        pid = parent.get("parent_id", "")\n'
            "    return out\n",
            "upward",
        ),
        (
            "walk",
            "def _injected_walk(sid, children_map):\n"
            "    for child in children_map.get(sid, []):\n"
            "        _injected_walk(child, children_map)\n",
            "recursive",
        ),
    ],
)
def test_detector_catches_injected_unregistered_site(name, snippet, expected_pattern):
    """检测器自检：向真实源码的内存副本注入未登记建树点，必须被抓到且不写盘。"""
    path = APP_ROOT / "routers/sections.py"
    before = path.read_bytes()
    injected = _source("routers/sections.py") + "\n\n" + snippet
    sites = _detect(injected)
    assert f"_injected_{name}" in sites, f"扫描器漏检注入点：{name}"
    assert expected_pattern in sites[f"_injected_{name}"], (
        f"注入点 {name} 命中模式 {sites[f'_injected_{name}']}，期望 {expected_pattern}"
    )
    assert f"_injected_{name}" not in REGISTRY, "注入点不应预先存在于白名单"
    after = path.read_bytes()
    assert hashlib.sha256(before).hexdigest() == hashlib.sha256(after).hexdigest(), (
        "检测器自检不得写盘：源文件字节发生变化"
    )
