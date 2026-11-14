"""进模型请求的截断棘轮（R186 · 2026-11-07 · 防复发）。

背景：R179 / R186 把目录、事实、正文链路里的静默硬切片逐点换成了
``clip_text_notice`` / ``clip_text_logged`` / ``serialize_bounded_json_array``
等共享留痕实现。但"下一处新代码"没有护栏 —— 扫描显示历史积累的硬切点
仍在增长。本用例把这条线固化成棘轮：

* **只锁进模型请求的路径**：``render(...)`` / ``get_prompt(...)`` 的实参，
  以及名字含 prompt / messages / instruction 的局部变量赋值。全仓扫 ``[:N]``
  会把 SSE 预览、诊断输出、图表标题等无关限长一起卷进来，只能靠大名单豁免。
* **合规留痕点自动通过**：切片只要位于 ``_LEDGER_WRAPPERS`` 任一调用的实参内
  （例如 ``clip_text_notice(x, 3000, ...)``），即视为已留痕，无需白名单。
* **例外必须写理由**：白名单只剩 ``_IRRELEVANT_LIMITS``（与方案文本完整性
  无关的限长）。R186 清零轮已把原 ``_BASELINE_DEBT`` 三条存量真实截断修掉，
  条目随之删除 —— 白名单只允许变短。空理由不被接受；白名单条目必须命中
  当前源码，失效条目会让测试红，以此强制"修好后删条目"。
* **新增裸切片必须红**：见 ``test_ratchet_catches_new_bare_slice``（在真实源码
  内存副本里注入裸切片）与 ``test_ratchet_ignores_non_prompt_paths``（证明
  范围没有外溢到非模型请求路径）。

R186 收紧（2026-11-07）
----------------------
模型可见路径必须让模型看到账目，因此：

* ``clip_text`` **不再默认合规**：它只写日志、模型仍收到半截文本，调用即红。
  合规包装只保留 ``clip_text_notice`` / ``serialize_bounded_json_array`` /
  ``govern_text``。
* ``clip_text_logged`` 只允许出现在 ``_LOGGED_ONLY_IN`` 登记的内部判定串
  （事实启发式兜底、危大参数匹配）：这些文本只喂正则/关键词判定，**插标记会
  改变匹配结果**，日志留痕是唯一正确的可观测面。登记按「文件 + 函数作用域」
  生效，未登记作用域同样红；条目失效（函数搬迁/删除）同样让测试红。
"""

import ast
import pathlib
import re

_APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

#: 模型请求的提示词入口。
_SINK_FUNCS = {"render", "get_prompt"}

#: 自身即"截断 + 留痕"的共享实现；切片落在它们的实参内视为合规。
#: R186 收紧：``clip_text``（只写日志、模型不可见）与 ``clip_text_logged``
#: （仅限内部判定作用域）已移出默认合规集合。
_LEDGER_WRAPPERS = {
    "clip_text_notice",
    "serialize_bounded_json_array",
    "govern_text",
}

#: 模型可见路径禁止出现的调用：它们是"静默变体"，模型看不到账目。
_FORBIDDEN_CALLS = {"clip_text"}

#: ``clip_text_logged`` 仅允许的内部判定作用域：(相对路径, 函数名) → 理由。
#: 理由必须是"为什么不能插标记"的具体原因，而不是"历史原因"。
_LOGGED_ONLY_IN: dict[tuple[str, str], str] = {
    (
        "app/services/facts_extractor.py",
        "run_extraction_pipeline",
    ): "该文本只喂事实启发式兜底的正则/关键词判定（工期规则、类目兜底）；"
    "插标记会改变匹配结果，等于用可观测性换另一种静默错误。",
    (
        "app/services/preflight_engine.py",
        "check_hazard_params",
    ): "该文本只用于危大分类的关键词/正则匹配；插标记会改变匹配结果，"
    "从而影响危大/超规模的判定，宁可不留标记。",
}

#: "构造 prompt 的局部变量"：名字含以下词即纳入扫描。
_PROMPT_VAR_RE = re.compile(r"prompt|messages|instruction", re.I)

#: 与方案文本完整性无关的限长（每条必须写理由）。
_IRRELEVANT_LIMITS: dict[tuple[str, str, str], str] = {
    (
        "app/routers/charts.py",
        "generate_ai_image",
        "_optimized[:600]",
    ): "图像生成接口对提示词长度有 600 字硬上限（外部图像模型的接口约束），"
    "不是文本方案上下文；超限部分由图像模型侧丢弃，不承担方案事实一致性。",
    (
        "app/routers/_sse_outline_review.py",
        "_try_outline_patch",
        "missing[:20]",
    ): "清单条数上限（最多列 20 条缺失项），不是字符截断；超过 20 条时穷举清单本身"
    "会让提示词失去可执行性，且缺失项由审核链路另行持有。",
    (
        "app/services/ai/omp_workflow.py",
        "_run_one",
        "issues[:8]",
    ): "校验问题条数上限（最多列 8 条），不是字符截断；超过 8 条时只取前 8 条驱动定向修复，"
    "完整问题清单仍由 errors 列表与修复轮次状态另行持有，不承担方案事实一致性。",
}


def _call_name(node: ast.AST) -> str | None:
    func = getattr(node, "func", None)
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_int_slice(node: ast.AST) -> bool:
    """``x[:N]``（N 为整型常量、无下界/步长）—— 静默硬切的典型形态。"""
    if not isinstance(node, ast.Subscript):
        return False
    sl = node.slice
    if not isinstance(sl, ast.Slice):
        return False
    return (
        sl.lower is None
        and sl.step is None
        and isinstance(sl.upper, ast.Constant)
        and isinstance(sl.upper.value, int)
    )


class _Scanner(ast.NodeVisitor):
    """遍历单个模块，收集"模型请求路径上的裸切片"与不合规截断调用。"""

    def __init__(self, rel: str, source: str):
        self.rel = rel
        self.source = source
        self.lines = source.splitlines()
        self.scope: list[str] = []
        self.hits: list[dict] = []
        self._seen: set[tuple] = set()

    # -- 作用域：模块 / 函数 / 嵌套函数 ------------------------------------
    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # -- 入口 ------------------------------------------------------------
    def visit_Assign(self, node):
        for target in node.targets:
            if isinstance(target, ast.Name) and _PROMPT_VAR_RE.search(target.id):
                self._collect(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and _PROMPT_VAR_RE.search(node.target.id):
            self._collect(node.value)
        self.generic_visit(node)

    def visit_Call(self, node):
        name = _call_name(node)
        if name in _SINK_FUNCS:
            self._collect(node)
        if name in _FORBIDDEN_CALLS or name == "clip_text_logged":
            self._record_call(node, name)
        self.generic_visit(node)

    # -- 收集切片：包装函数内的切片豁免 ------------------------------------
    def _collect(self, expr) -> None:
        if expr is None:
            return
        scope_name = ".".join(self.scope) or "<module>"
        stack: list[tuple[ast.AST, bool]] = [(expr, False)]
        while stack:
            node, wrapped = stack.pop()
            if isinstance(node, ast.Call) and _call_name(node) in _LEDGER_WRAPPERS:
                wrapped = True
            if _is_int_slice(node) and not wrapped:
                segment = ast.get_source_segment(self.source, node) or ast.unparse(node)
                key = (self.rel, scope_name, segment)
                if key not in self._seen:
                    self._seen.add(key)
                    self.hits.append(
                        {
                            "kind": "slice",
                            "rel": self.rel,
                            "scope": scope_name,
                            "key": key,
                            "line": node.lineno,
                            "snippet": self._snippet(node),
                        }
                    )
            for child in ast.iter_child_nodes(node):
                stack.append((child, wrapped))

    # -- 收集截断调用：clip_text 一律红；clip_text_logged 按作用域白名单 ----
    def _record_call(self, node: ast.Call, name: str) -> None:
        scope_name = ".".join(self.scope) or "<module>"
        key = (self.rel, scope_name, f"call:{name}")
        if key in self._seen:
            return
        self._seen.add(key)
        self.hits.append(
            {
                "kind": "call",
                "name": name,
                "rel": self.rel,
                "scope": scope_name,
                "key": key,
                "line": node.lineno,
                "snippet": self._snippet(node),
            }
        )

    def _snippet(self, node: ast.AST) -> str:
        return self.lines[node.lineno - 1].strip() if node.lineno else ""


def _scan_sources(sources: dict[str, str]) -> list[dict]:
    """扫描给定源码集合；``sources`` 为 {相对路径: 源码}，便于自检注入。"""
    hits: list[dict] = []
    for rel, source in sorted(sources.items()):
        tree = ast.parse(source, filename=rel)
        scanner = _Scanner(rel, source)
        scanner.visit(tree)
        hits.extend(scanner.hits)
    return hits


def _real_sources() -> dict[str, str]:
    return {
        path.relative_to(_APP_DIR.parent).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(_APP_DIR.rglob("*.py"))
    }


def _describe(hits: list[dict]) -> str:
    lines: list[str] = []
    for h in hits:
        if h["kind"] == "slice":
            lines.append(f"  {h['rel']}:{h['line']} [{h['scope']}] {h['key'][2]}\n      {h['snippet']}")
        else:
            lines.append(f"  {h['rel']}:{h['line']} [{h['scope']}] {h['name']}(...)\n      {h['snippet']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. 棘轮主体：未登记的模型请求裸切片必须为零
# ---------------------------------------------------------------------------
def test_no_unlisted_bare_slice_in_model_requests():
    hits = [h for h in _scan_sources(_real_sources()) if h["kind"] == "slice"]
    allowed = set(_IRRELEVANT_LIMITS)
    violations = [h for h in hits if h["key"] not in allowed]
    assert not violations, (
        "发现未登记的「进模型请求的裸切片」：\n"
        + _describe(violations)
        + "\n请改用 clip_text_notice / serialize_bounded_json_array / govern_text，"
        "或在 _IRRELEVANT_LIMITS 中写明理由。"
    )


# ---------------------------------------------------------------------------
# 2. 白名单必须"活"：修好后要删条目，不能留僵尸豁免
# ---------------------------------------------------------------------------
def test_allowlist_entries_are_live():
    reasons = list(_IRRELEVANT_LIMITS.values())
    assert all(isinstance(r, str) and len(r.strip()) >= 20 for r in reasons), "白名单理由不得为空或敷衍"
    hits = _scan_sources(_real_sources())
    live = {h["key"] for h in hits if h["kind"] == "slice"}
    stale = [key for key in set(_IRRELEVANT_LIMITS) if key not in live]
    assert not stale, (
        "以下白名单条目已失效（源码里不再有该切片）：\n"
        + "\n".join(f"  {k[0]} [{k[1]}] {k[2]}" for k in stale)
        + "\n请删除对应条目（棘轮只允许收紧）。"
    )


# ---------------------------------------------------------------------------
# 3. R186 收紧：clip_text 不得出现在任何模型可见路径（实际为全 app 禁止）
# ---------------------------------------------------------------------------
def test_clip_text_calls_are_forbidden():
    hits = _scan_sources(_real_sources())
    violations = [h for h in hits if h["kind"] == "call" and h["name"] in _FORBIDDEN_CALLS]
    assert not violations, (
        "发现静默截断调用 clip_text（模型看不到截断账目）：\n"
        + _describe(violations)
        + "\n请改用 clip_text_notice（模型可见）；确属内部判定串再用 clip_text_logged "
        "并登记 _LOGGED_ONLY_IN。"
    )


# ---------------------------------------------------------------------------
# 4. R186 收紧：clip_text_logged 只允许登记的内部判定作用域
# ---------------------------------------------------------------------------
def test_clip_text_logged_only_in_allowlisted_internal_scopes():
    hits = [
        h
        for h in _scan_sources(_real_sources())
        if h["kind"] == "call" and h["name"] == "clip_text_logged"
    ]
    violations = [
        h for h in hits if (h["rel"], h["scope"]) not in _LOGGED_ONLY_IN
    ]
    assert not violations, (
        "以下 clip_text_logged 调用不在登记的内部判定作用域（模型可见路径必须用 "
        "clip_text_notice）：\n" + _describe(violations)
    )


def test_logged_allowlist_entries_are_live():
    reasons = list(_LOGGED_ONLY_IN.values())
    assert all(
        isinstance(r, str) and "改变匹配" in r for r in reasons
    ), "内部判定白名单理由必须写明「插标记会改变匹配结果」"
    hits = [
        h
        for h in _scan_sources(_real_sources())
        if h["kind"] == "call" and h["name"] == "clip_text_logged"
    ]
    live = {(h["rel"], h["scope"]) for h in hits}
    stale = [key for key in set(_LOGGED_ONLY_IN) if key not in live]
    assert not stale, (
        "以下 clip_text_logged 白名单条目已失效（对应函数里不再有该调用）：\n"
        + "\n".join(f"  {k[0]} [{k[1]}]" for k in stale)
        + "\n请删除对应条目（棘轮只允许收紧）。"
    )


# ---------------------------------------------------------------------------
# 5. 自检：真实源码内存副本里注入裸切片 → 棘轮必须红
# ---------------------------------------------------------------------------
def test_ratchet_catches_new_bare_slice():
    rel = "app/routers/sse_handlers.py"
    source = (_APP_DIR.parent / rel).read_text(encoding="utf-8")
    # 注入 ①：prompt 局部变量赋值里的裸切片
    mutated_assign = source.replace(
        "user_prompt = f\"【方案名称】：",
        "user_prompt = project_brief[:999] + f\"【方案名称】：",
        1,
    )
    # 注入 ②：render(...) 实参里的裸切片
    mutated_sink = source.replace(
        'project_brief=clip_text_notice(project_brief, 2000, "项目资料摘要")',
        "project_brief=project_brief[:999]",
        1,
    )
    assert mutated_assign != source and mutated_sink != source

    hits = _scan_sources({rel: mutated_assign})
    assert any(h["kind"] == "slice" and h["key"][2] == "project_brief[:999]" for h in hits), _describe(hits)

    hits = _scan_sources({rel: mutated_sink})
    assert any(h["kind"] == "slice" and h["key"][2] == "project_brief[:999]" for h in hits), _describe(hits)


# ---------------------------------------------------------------------------
# 6. 自检：注入 clip_text / 越界 clip_text_logged → 棘轮必须红
# ---------------------------------------------------------------------------
def test_ratchet_catches_silent_clip_variants():
    synthetic = """
def render(key, **kwargs):
    return key

def leaked_prompt(project_brief):
    user_prompt = render("k", project_brief=clip_text(project_brief, 1500, "测试"))
    return user_prompt

def internal_ok(full_text):
    return clip_text_logged(full_text, 3000, "测试")
"""
    hits = _scan_sources({"app/services/synthetic.py": synthetic})
    assert any(h["kind"] == "call" and h["name"] == "clip_text" for h in hits), _describe(hits)
    assert any(h["kind"] == "call" and h["name"] == "clip_text_logged" for h in hits), _describe(hits)


def test_ratchet_ignores_non_prompt_paths():
    synthetic = """
def render(key, **kwargs):
    return key

def unrelated(x):
    preview = x[:200]          # SSE 预览：不进模型请求
    return preview
"""
    assert _scan_sources({"synthetic.py": synthetic}) == []
