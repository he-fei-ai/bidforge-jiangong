"""R185（2026-11-07）：全局事实唯一性闸门的**结构性棘轮**。

背景：R183 修掉了 5 条可绕过 T-01（同作用域同名不同值不得同时可注入）的写路径，
但它们仍是「逐点打补丁」的形状 —— 闸门散装在若干 handler 里。下一条新写路径
（第 8 条 INSERT/UPDATE）依旧可能绕过。本用例把「哪些代码能写 global_facts」
变成一张**显式登记表**：

1. 用 AST 反查全仓所有写 `global_facts` 的语句（含 f-string 动态 SET、以
   ``GLOBAL_FACTS_INSERT_SQL`` / ``MANUAL_FACT_INSERT_SQL`` / ``_GROUP_REBUILD_SQL``
   常量名执行的 INSERT、以及提取链缓冲后执行的 UPDATE），不依赖人工印象；
2. 每个（文件, 函数, 语句类型）位点必须在 ``_ENTRIES`` 登记，并标注处置：
   ``gated``（走统一闸门）、``protocol``（既有冲突/去重协议等价于闸门）、
   ``state``（只翻状态位，不改 key/value）、``delete``（只删不建）、
   ``cross_module``（跨模块登记）、``definition``（SQL 常量定义，非执行点）；
3. 任何新增写位点都会改变检测集合 → 本用例红，必须先登记并说明理由；
4. ``gated`` 位点必须真的调用统一闸门函数；``state`` 位点的 SQL 不得出现
   title/content/fact_key/group_id 等身份列；``update_fact`` 的字段白名单
   必须继续受 ``FactGroupUpdate`` 模型字段约束。

因此本用例锁的不是某一次修复，而是「以后不许再多出第 6 个绕过口」。
"""

import ast
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

_WRITE_RE = re.compile(
    r"INSERT\s+INTO\s+global_facts|UPDATE\s+global_facts|DELETE\s+FROM\s+global_facts", re.I
)
_INSERT_NAMES = {"GLOBAL_FACTS_INSERT_SQL", "MANUAL_FACT_INSERT_SQL", "_GROUP_REBUILD_SQL"}

#: 统一闸门入口（R183）。``gated`` 位点必须直接调用其中之一。
_GATE_FUNCS = {
    "_assert_no_conflicting_injectable_duplicate",
    "_collect_duplicate_injectable_conflicts",
    "_load_injectable_peers_for_gate",
    "_filter_create_fact_duplicates",
}

#: 写位点登记表。key 与检测器输出的 (相对路径, 函数名, 语句类型) 完全一致。
#: mode: gated / protocol / state / delete / cross_module / definition
_ENTRIES = [
    # ---- routers/global_facts.py：REST 手工写路径 ----
    {
        "file": "app/routers/global_facts.py",
        "func": "create_fact",
        "kind": "INSERT",
        "count": 1,
        "mode": "gated",
        "gate": "_filter_create_fact_duplicates",
        "reason": "新增落库前逐条判同名同值（幂等）/同名异值（409）/既有未过门控（409）",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "_apply_item_updates",
        "kind": "UPDATE",
        "count": 1,
        "mode": "gated",
        "gate": "_assert_no_conflicting_injectable_duplicate",
        "reason": "条目改名/改值/取消模拟后若可注入，必须重过唯一性闸门",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "update_fact",
        "kind": "UPDATE",
        "count": 1,
        "mode": "whitelist",
        "reason": (
            "仅非结构化正文/分类字段：title 与可回解成 name/value 的 content 已转入"
            " _apply_item_updates 走闸门；纯正文回解不出事实身份，且字段集受"
            " FactGroupUpdate 模型约束（见 test_05）"
        ),
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "update_fact",
        "kind": "DELETE",
        "count": 1,
        "mode": "gated",
        "gate": "_collect_duplicate_injectable_conflicts",
        "reason": "分组重建删除旧行前先算同名异值冲突，后出现者标记矛盾而非整组拒绝",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "update_fact",
        "kind": "INSERT",
        "count": 1,
        "mode": "gated",
        "gate": "_collect_duplicate_injectable_conflicts",
        "reason": "分组重建插入沿用冲突语义，同组/跨组同名异值只放行首行",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "resolve_fact",
        "kind": "UPDATE",
        "count": 1,
        "mode": "gated",
        "gate": "_assert_no_conflicting_injectable_duplicate",
        "reason": "确认事实会解除未审核闸门，必须确认可注入后不出现同名异值",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "resolve_conflict",
        "kind": "UPDATE",
        "count": 1,
        "mode": "gated",
        "gate": "_assert_no_conflicting_injectable_duplicate",
        "reason": "裁决换值后重新可注入，必须与同作用域其它可注入行比对",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "ack_fact_stale",
        "kind": "UPDATE",
        "count": 2,
        "mode": "gated",
        "gate": "_assert_no_conflicting_injectable_duplicate",
        "reason": "两条 UPDATE：模拟/矛盾行仅清过期；真实行同时置 is_resolved=1 前过闸门",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "batch_ack_stale",
        "kind": "UPDATE",
        "count": 1,
        "mode": "gated",
        "gate": "_collect_duplicate_injectable_conflicts",
        "reason": "批量解除过期前预检，冲突 id 以 NOT IN 排除并回 blocked_duplicates",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "batch_resolve",
        "kind": "UPDATE",
        "count": 1,
        "mode": "gated",
        "gate": "_collect_duplicate_injectable_conflicts",
        "reason": "批量确认按写入顺序对既有可注入行 + 本批计划判冲突，冲突行进 skipped_safety",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "clear_all_facts",
        "kind": "DELETE",
        "count": 1,
        "mode": "delete",
        "reason": "作用域清空，只删不建，不可能产生同名列",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "delete_fact",
        "kind": "DELETE",
        "count": 2,
        "mode": "delete",
        "reason": "单行删除 + 分组删除，只删不建",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "adjust_facts",
        "kind": "DELETE",
        "count": 1,
        "mode": "delete",
        "reason": "AI 调整删除操作，只删不建",
    },
    {
        "file": "app/routers/global_facts.py",
        "func": "adjust_facts",
        "kind": "INSERT",
        "count": 1,
        "mode": "protocol",
        "protocol": "adjust",
        "reason": (
            "AI 调整 add 走 _validate_adjust_ops(existing_keys=...)：命中既有同名键"
            "一律丢弃并登记理由，天然不会插出同键第二行"
        ),
    },
    # ---- 资料上传 / 方案复制 / 跨模块 ----
    {
        "file": "app/routers/global_facts_docs.py",
        "func": "_mark_project_facts_stale",
        "kind": "UPDATE",
        "count": 1,
        "mode": "state",
        "reason": "新资料落库只置 is_stale=1（把旧事实移出注入），不改 key/value",
    },
    {
        "file": "app/routers/schemes.py",
        "func": "duplicate_scheme",
        "kind": "INSERT",
        "count": 1,
        "mode": "cross_module",
        "reason": (
            "跨模块登记（方案复制）：写入全新 scheme_id 作用域；若源作用域本就不变量，"
            "复制后不变。风险：同项目共享层若已存在同名异值可注入事实，复制行可能与其并存 —— "
            "不在本模块修复范围，登记待方案模块评估"
        ),
    },
    {
        "file": "app/services/doc_pipeline/pipeline.py",
        "func": "run_cross_check",
        "kind": "UPDATE",
        "count": 2,
        "mode": "state",
        "reason": "跨来源校验只翻 has_conflict 0/1 状态位，不改 key/value",
    },
    # ---- 提取链（AI 落库） ----
    {
        "file": "app/services/facts_extractor.py",
        "func": "<module>",
        "kind": "INSERT",
        "count": 1,
        "mode": "definition",
        "reason": (
            "GLOBAL_FACTS_INSERT_SQL 常量定义，非执行点；执行点登记在"
            " create_fact / update_fact / _persist_tx_once"
        ),
    },
    {
        "file": "app/services/facts_extractor.py",
        "func": "_persist_tx_once",
        "kind": "DELETE",
        "count": 1,
        "mode": "protocol",
        "protocol": "extraction",
        "reason": "提取落库按指纹删除已消失行；保留/插入决策在 persist_extraction 的同键冲突协议内",
    },
    {
        "file": "app/services/facts_extractor.py",
        "func": "_persist_tx_once",
        "kind": "INSERT",
        "count": 1,
        "mode": "protocol",
        "protocol": "extraction",
        "reason": "insert_buf 仅包含 persist_extraction 判定为「新增键」的行；同键异值不新建，转 conflict_keys",
    },
    {
        "file": "app/services/facts_extractor.py",
        "func": "persist_extraction",
        "kind": "UPDATE",
        "count": 3,
        "mode": "protocol",
        "protocol": "extraction",
        "reason": (
            "同键同值合并只清 is_stale；同键异值写 conflict_keys + has_conflict=1 并保持"
            "非注入，不产生第二份可注入行（提取链的冲突协议，等价于闸门）"
        ),
    },
]


def _kind_of(text: str) -> str:
    if re.search("INSERT", text, re.I):
        return "INSERT"
    if re.search("DELETE", text, re.I):
        return "DELETE"
    return "UPDATE"


class _WriteWalker(ast.NodeVisitor):
    """收集写 global_facts 的语句位点（SQL 字面量 + 写 SQL 常量名执行）。"""

    def __init__(self, rel_path: str):
        self.rel_path = rel_path
        self.func_stack: list[str] = []
        self.hits: list[tuple[int, str, str, str]] = []

    def _current_func(self) -> str:
        return self.func_stack[-1] if self.func_stack else "<module>"

    def visit_FunctionDef(self, node) -> None:  # noqa: N802
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Expr(self, node) -> None:  # noqa: N802
        # 文档字符串不是可执行 SQL，排除，避免注释/说明误登记
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_Constant(self, node) -> None:  # noqa: N802
        if isinstance(node.value, str) and _WRITE_RE.search(node.value):
            self.hits.append((node.lineno, self._current_func(), _kind_of(node.value), node.value[:120]))
        self.generic_visit(node)

    def visit_JoinedStr(self, node) -> None:  # noqa: N802
        literal = "".join(
            part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
        if _WRITE_RE.search(literal):
            self.hits.append((node.lineno, self._current_func(), _kind_of(literal), literal[:120]))
        # 不再下探子 Constant，避免同一 f-string 被重复计入
        for part in node.values:
            if not isinstance(part, ast.Constant):
                self.visit(part)

    def visit_Call(self, node) -> None:  # noqa: N802
        callee = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if callee in ("execute", "executemany", "executescript") and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in _INSERT_NAMES:
                self.hits.append((node.lineno, self._current_func(), "INSERT", f"NAME:{first.id}"))
        self.generic_visit(node)


def _detect_from_sources(sources: list[tuple[str, str]]) -> dict[tuple[str, str, str], list[str]]:
    detected: dict[tuple[str, str, str], list[str]] = {}
    for rel_path, source in sources:
        walker = _WriteWalker(rel_path)
        walker.visit(ast.parse(source))
        for _lineno, func, kind, preview in walker.hits:
            detected.setdefault((rel_path, func, kind), []).append(preview)
    return detected


def _detect_repo() -> dict[tuple[str, str, str], list[str]]:
    sources = [
        (path.relative_to(APP_DIR.parent).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(APP_DIR.rglob("*.py"))
    ]
    return _detect_from_sources(sources)


def _expected_counts() -> dict[tuple[str, str, str], int]:
    return {(e["file"], e["func"], e["kind"]): e["count"] for e in _ENTRIES}


def _functions_of(rel_path: str) -> dict[str, ast.AST]:
    path = APP_DIR.parent / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            target = sub.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_01_all_write_sites_registered_exactly():
    """全仓写位点集合必须与登记表逐项一致：多一个写路径立即红。"""
    detected = _detect_repo()
    detected_counts = {key: len(previews) for key, previews in detected.items()}
    expected = _expected_counts()

    extra = sorted(set(detected_counts) - set(expected))
    missing = sorted(set(expected) - set(detected_counts))
    wrong = sorted(
        (key, expected[key], detected_counts[key])
        for key in set(expected) & set(detected_counts)
        if expected[key] != detected_counts[key]
    )
    assert not extra, f"发现未登记的 global_facts 写位点（新增写路径必须先登记并说明是否需要闸门）：{extra}"
    assert not missing, f"登记表位点在代码中已不存在（请同步删除/改名）：{missing}"
    assert not wrong, f"登记数量与检测数量不一致（同函数新增写语句会触发）：{wrong}"


def test_02_gated_sites_call_unified_gate():
    """每个 gated 位点必须在其函数体内真的调用统一闸门函数。"""
    funcs_by_file: dict[str, dict[str, ast.AST]] = {}
    problems = []
    for entry in _ENTRIES:
        if entry["mode"] != "gated":
            continue
        rel = entry["file"]
        if rel not in funcs_by_file:
            funcs_by_file[rel] = _functions_of(rel)
        node = funcs_by_file[rel].get(entry["func"])
        assert node is not None, f"登记表函数不存在：{rel}::{entry['func']}"
        calls = _called_names(node)
        if entry["gate"] not in calls:
            problems.append(f"{rel}::{entry['func']} 缺少闸门调用 {entry['gate']}（现有调用：{sorted(calls)}）")
    assert not problems, "\n".join(problems)


def test_03_gate_helper_is_real_single_exit():
    """统一闸门本身必须是单一出口：断言入口 → 读可注入 peers → 冲突计算。"""
    funcs = _functions_of("app/routers/global_facts.py")
    assert "_load_injectable_peers_for_gate" in funcs
    assert "_collect_duplicate_injectable_conflicts" in funcs
    assert "_assert_no_conflicting_injectable_duplicate" in funcs

    assert "_load_fact_rows" in _called_names(funcs["_load_injectable_peers_for_gate"])
    # 单条闸门与批量冲突计算必须共用同一 peers 出口，否则又是两份口径
    assert "_load_injectable_peers_for_gate" in _called_names(
        funcs["_assert_no_conflicting_injectable_duplicate"]
    )
    assert "_duplicate_injectable_detail" in _called_names(
        funcs["_assert_no_conflicting_injectable_duplicate"]
    )


def test_04_whitelist_reasons_and_state_sql_are_safe():
    """白名单必须写明理由；state 位点的 SQL 不得改身份列；delete 位点不得含 INSERT。"""
    detected = _detect_repo()
    identity_re = re.compile(r"(?:^|[,\s])(title|content|fact_key|group_id)\s*=")
    problems = []
    for entry in _ENTRIES:
        if entry["mode"] not in ("whitelist", "state", "delete", "cross_module", "definition", "protocol"):
            continue
        if len(entry["reason"].strip()) < 12:
            problems.append(f"{entry['file']}::{entry['func']} 白名单缺乏说明理由")
        previews = detected.get((entry["file"], entry["func"], entry["kind"]), [])
        if entry["mode"] == "state":
            for preview in previews:
                if identity_re.search(preview):
                    problems.append(f"state 位点触碰身份列：{entry['file']}::{entry['func']} {preview!r}")
        if entry["mode"] == "delete":
            for preview in previews:
                if "INSERT" in preview.upper():
                    problems.append(f"delete 位点含 INSERT：{entry['file']}::{entry['func']} {preview!r}")
    assert not problems, "\n".join(problems)


def test_05_update_fact_field_whitelist_stays_model_restricted():
    """``update_fact`` 非结构化字段白名单的安全前提：模型不得新增可改身份的字段。"""
    models_path = APP_DIR / "models.py"
    tree = ast.parse(models_path.read_text(encoding="utf-8"))
    allowed = {"id", "title", "content", "category", "item_updates"}
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "FactGroupUpdate":
            found = {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
    assert found is not None, "models.py 缺少 FactGroupUpdate"
    extra = sorted(found - allowed)
    assert not extra, (
        "FactGroupUpdate 新增字段可能绕过 update_fact 的闸门分流（如 is_simulated/is_resolved/fact_key）："
        f"{extra}；请把该字段改为走 _apply_item_updates 闸门或登记新理由"
    )


def test_06_detector_really_catches_a_new_ungated_write():
    """自检：向真实源码注入一条未登记写路径，检测集合必须变化（否则棘轮是空转）。"""
    rel = "app/routers/global_facts.py"
    source = (APP_DIR.parent / rel).read_text(encoding="utf-8")
    injected = source + (
        "\n\nasync def _synthetic_new_write_path(db):\n"
        "    await db.execute(\"UPDATE global_facts SET is_resolved=1 WHERE id=?\", (\"x\",))\n"
    )
    detected = _detect_from_sources([(rel, injected)])
    new_key = (rel, "_synthetic_new_write_path", "UPDATE")
    assert new_key in detected, "检测器未能发现新增写路径，棘轮失效"
    assert new_key not in _expected_counts(), "自检写路径不应出现在登记表"
