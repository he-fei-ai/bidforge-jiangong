# -*- coding: utf-8 -*-
"""R167 · 提示词模块横切护栏（2026-11-01）。

本轮聚焦【提示词模块】深度审计，新增四类护栏：

1. **图表软提示与「前缀稳定化」解耦**（源码接线护栏）——
   旧实现把 ``build_section_chart_hints`` 整段关在 ``if _stable_prefix:`` 内，
   于是 content_chart_quota_hint_enabled / content_chart_trigger_hint_enabled
   两个独立开关被第三个开关 ``content_stable_prefix_enabled`` 暗中挟持：
   关闭前缀稳定化后「本章图表配额」「本章配图参考」两段 R58/R166 软提示
   既不查库也不注入（非 stable 分支也未补拼）。R169 D2 收口后本护栏升级为
   「无条件调用 + 单一装配点（_chapter_increments）双分支同源消费」。
2. **契约表覆盖率 = 100%** —— 每个已注册提示词都必须落在
   ``PROMPT_VARIABLE_CONTRACTS``（空列表 = 显式零变量声明），杜绝新模板脱管。
3. **共享片段引用完整性** —— 模板里的 ``{SHARED_*}`` 必须指向已注册片段；
   共享片段自身不得含业务变量（否则运行时会被当成「调用方需提供的变量」）。
"""
from __future__ import annotations

import inspect
import re

from app.routers import sse_handlers
from app.services.ai.prompts._registry import (
    _ALL_PROMPTS,
    PROMPT_VARIABLE_CONTRACTS,
    extract_variables,
)


def _generate_content_src() -> str:
    return inspect.getsource(sse_handlers.generate_content)


# ===========================================================================
# 1. 逐章增量单一装配点（R167 解耦 + R169 D2 收口）
# ===========================================================================
class TestChapterIncrementSingleAssembly:
    """图表软提示接线 + D2 单一装配点护栏。

    R167：``build_section_chart_hints`` 必须无条件调用（不得被
    ``if _stable_prefix:`` 挟持）。
    R169 D2：逐章增量片段只允许在 ``_chapter_increments`` 列表**恰一处**
    append，两条装配分支从同一列表派生消费 —— 历史 R74/R162/R167 三次
    踩中「片段只进 stable 分支、非 stable 分支静默丢弃」，根因即双口径。
    """

    #: (片段变量, stable_join 字面量) —— "" 仅配图参考（base 恒 \n 结尾）。
    _FRAGMENTS = (
        ('_chart_trigger_hint', '""'),
        ('_req_block', '"\\n"'),
        ('_plan_guidance', '"\\n"'),
        ('_table_gate_line', '"\\n"'),
        ('_l3_leaf_struct', '"\\n"'),
    )

    def test_hint_builder_called_unconditionally_once(self):
        """``build_section_chart_hints`` 必须无条件调用且每章恰一次。"""
        src = _generate_content_src()
        assert src.count("build_section_chart_hints(") == 1, (
            "图表软提示应只有唯一调用点（R166：与配额共用一次查询）")
        head = src.split("_chart_counts: dict[str, int] = {}", 1)
        assert len(head) == 2, "未找到 _chart_counts 初始化锚点"
        segment = head[1].split("build_section_chart_hints(", 1)[0]
        assert "if _stable_prefix" not in segment, (
            "图表软提示不得再依赖 content_stable_prefix_enabled（R167）")

    def test_each_fragment_appended_exactly_once(self):
        """五个任务差异增量片段各恰一次 append 进单一装配列表。"""
        src = _generate_content_src()
        assert src.count("_chapter_increments.append((") == len(self._FRAGMENTS), (
            "单一装配点的 append 次数与片段清单不符（新片段必须进列表，"
            "不得另开双口径）")
        for frag, join_prefix in self._FRAGMENTS:
            assert src.count(
                f"_chapter_increments.append(({join_prefix}, {frag}))") == 1, (
                f"片段 {frag} 未在单一装配点恰一次 append")

    def test_legacy_dual_path_concat_abolished(self):
        """旧双口径拼接必须绝迹（_chart_hint 为结构化槽位参数，豁免）。"""
        src = _generate_content_src()
        for legacy in (
                'user_content = user_content + "\\n\\n" + _plan_guidance',
                'user_content = user_content + "\\n\\n" + _table_gate_line',
                'user_content = user_content + "\\n\\n" + _l3_leaf_struct',
                'user_content = user_content + "\\n\\n" + _chart_trigger_hint',
                'user_content + "\\n\\n"\n'
                '                                        + _chart_trigger_hint',
                '_chapter_extras = _chapter_extras + "\\n" + _table_gate_line',
                '(_chapter_extras + "\\n") if _chapter_extras',
                'if _plan_guidance and _chapter_extras:'):
            assert legacy not in src, f"旧双口径拼接残留：{legacy!r}"

    def test_both_branches_consume_the_same_list(self):
        """stable 消费 \\n 形态、非 stable 消费 \\n\\n 形态，同源同序。"""
        src = _generate_content_src()
        # stable：派生文本必须在 layered 装配前并入 _chapter_extras
        idx_layered = src.find("build_chapter_layered_messages(")
        assert idx_layered != -1
        assert "_incs_stable_text" in src[:idx_layered], (
            "stable 分支未消费单一装配列表的派生文本")
        assert "chapter_extras=_chapter_extras" in src[idx_layered - 400:
                                                       idx_layered + 1200], (
            "stable 分支 chapter_extras 入参丢失（R57 前缀稳定性契约）")
        # 非 stable：同一列表的 \n\n 形态 + 配额提示补拼，均在 messages 组装前
        tail = src.split("user_content = build_chapter_user_content(", 1)
        assert len(tail) == 2, "未找到非 stable 装配分支锚点"
        branch = tail[1].split('messages = [{"role": "system"', 1)[0]
        assert "_incs_plain_text" in branch, (
            "非 stable 分支未消费单一装配列表（含配图参考/额外要求/编排引导语/"
            "表格闸门/L3 结构要求）")
        assert '_chart_hint' in branch, (
            "非 stable 分支未补拼「本章图表配额」提示（结构化槽位的显式消费）")

    def test_chart_quota_hint_consumed_in_stable_branch(self):
        """配额提示双消费之 stable 面：chart_quota_hint 结构化槽位入参。"""
        src = _generate_content_src()
        assert "chart_quota_hint=_chart_hint" in src, (
            "stable 分支未把配额提示传入 layered 的 chart_quota_hint 槽位")


# ===========================================================================
# 2. 契约表覆盖率（100%）
# ===========================================================================
class TestContractCoverage:
    def test_every_registered_prompt_has_contract(self):
        missing = sorted(set(_ALL_PROMPTS) - set(PROMPT_VARIABLE_CONTRACTS))
        assert not missing, (
            f"以下提示词未纳入变量契约表（新增模板即脱管）：{missing}")


# ===========================================================================
# 3. 共享片段引用完整性
# ===========================================================================
class TestSharedReferenceIntegrity:
    _SHARED_RE = re.compile(r"\{(SHARED_[A-Z0-9_]+)\}")

    def test_all_shared_refs_registered(self):
        dangling = {}
        for key, meta in _ALL_PROMPTS.items():
            refs = set(self._SHARED_RE.findall(meta.get("content") or ""))
            bad = sorted(r for r in refs if r not in _ALL_PROMPTS)
            if bad:
                dangling[key] = bad
        assert not dangling, (
            f"模板引用了未注册的共享片段（渲染时保留字面量并入模）：{dangling}")

    def test_shared_fragments_carry_no_business_variables(self):
        offenders = {}
        for key, meta in _ALL_PROMPTS.items():
            if not key.startswith("SHARED_"):
                continue
            plain = [v for v in extract_variables(meta.get("content") or "")
                     if not v.startswith("SHARED_")]
            if plain:
                offenders[key] = plain
        assert not offenders, (
            f"共享片段自身含待填业务变量（会被误当作调用方入参）：{offenders}")


# ===========================================================================
# 4. R168 · G4：校验基线 = 实际生效模板（DB 覆盖优先）
# ===========================================================================
import sqlite3  # noqa: E402

import app.db as _appdb  # noqa: E402
import pytest  # noqa: E402
from app.services.ai.prompts import _cache, _metrics  # noqa: E402
from app.services.ai.prompts._registry import (  # noqa: E402
    extract_user_variables,
    get_default_prompt,
    render_prompt,
    validate_prompt_variables,
)

#: 用一个带业务占位符的真实模板做基线（出厂形态稳定、契约已声明）。
BASE_KEY = "outline_short_system"
#: 只在 DB 定制版里出现的占位符（出厂模板没有）。
DB_ONLY_VAR = "db_only_flag"
DB_ONLY_PLACEHOLDER = "{" + DB_ONLY_VAR + "}"


def _seed_prompt_row(db_path, key: str, content: str) -> None:
    """在临时文件库写入/覆盖一行 prompt_templates（模拟外部直写）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS prompt_templates ("
            "key TEXT PRIMARY KEY, content TEXT DEFAULT '')")
        conn.execute(
            "INSERT OR REPLACE INTO prompt_templates(key, content) VALUES(?,?)",
            (key, content))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def prompt_db(tmp_path):
    """切到临时文件库并让提示词缓存指向它；用例结束恢复原库并重载。"""
    prev = _appdb.DB_PATH
    _cache.reload_prompt_cache()
    yield tmp_path
    _appdb.DB_PATH = prev
    _cache.reload_prompt_cache()


def _factory_kwargs(key: str) -> dict[str, str]:
    return {v: f"测试-{v}" for v in extract_user_variables(get_default_prompt(key))}


class TestEffectiveTemplateValidation:
    """缺变量判定必须跟随「实际生效模板」，而不是内存注册表的出厂基线。"""

    def test_factory_shape_matches_registry_baseline(self, prompt_db):
        """无 DB 覆盖时与旧实现逐字等价（防修复引入行为漂移）。

        空库（不含该 key）→ 自动选出的生效模板必须**逐字等于**出厂默认，
        因此隐式基线路径与显式传出厂正文的 `_template` 路径结果必须一致。
        """
        empty_db = prompt_db / "empty.sqlite"  # 不存在 → 缓存为空 → 全回退出厂
        _appdb.DB_PATH = empty_db
        _cache.reload_prompt_cache()
        factory = get_default_prompt(BASE_KEY)
        assert validate_prompt_variables(BASE_KEY) == validate_prompt_variables(
            BASE_KEY, _template=factory)
        assert validate_prompt_variables(BASE_KEY, **_factory_kwargs(BASE_KEY)) == []

    def test_db_added_placeholder_is_reported_missing(self, prompt_db):
        """外部直写 DB 新增占位符 → 必须报缺（旧实现漏报 = G4 根因）。"""
        factory = get_default_prompt(BASE_KEY)
        customized = factory + f"\n追加约束：{DB_ONLY_PLACEHOLDER}\n"
        path = prompt_db / "g4a.sqlite"
        _seed_prompt_row(path, BASE_KEY, customized)
        _appdb.DB_PATH = path
        _cache.reload_prompt_cache()

        missing = validate_prompt_variables(BASE_KEY, **_factory_kwargs(BASE_KEY))
        assert DB_ONLY_VAR in missing, (
            f"DB 定制模板新增的占位符必须进入缺失清单（G4），实际: {missing}")

    def test_db_removed_placeholder_is_not_reported(self, prompt_db):
        """外部直写 DB 删掉占位符 → 不得再报缺（旧实现误报的镜像面）。"""
        factory = get_default_prompt(BASE_KEY)
        assert "{construction_scope}" in factory, "前提失效：出厂模板无该占位符"
        customized = factory.replace("{construction_scope}", "施工范围：见项目概况")
        path = prompt_db / "g4.sqlite"
        _seed_prompt_row(path, BASE_KEY, customized)
        _appdb.DB_PATH = path
        _cache.reload_prompt_cache()

        kwargs = _factory_kwargs(BASE_KEY)
        kwargs.pop("construction_scope", None)
        assert "construction_scope" not in validate_prompt_variables(BASE_KEY, **kwargs), (
            "生效模板已删掉 {construction_scope}，不应再报缺")
        # 反证：以出厂正文为基线时仍会报缺（说明差异确实来自「生效模板」基线）
        assert "construction_scope" in validate_prompt_variables(
            BASE_KEY, _template=factory, **kwargs)

    def test_get_prompt_uses_same_baseline_as_render(self, prompt_db):
        """渲染路径（get_prompt）与校验路径看到同一份生效正文。"""
        factory = get_default_prompt(BASE_KEY)
        customized = factory + f"\n追加约束：{DB_ONLY_PLACEHOLDER}\n"
        path = prompt_db / "g4b.sqlite"
        _seed_prompt_row(path, BASE_KEY, customized)
        _appdb.DB_PATH = path
        _cache.reload_prompt_cache()
        _metrics.reset()
        try:
            _cache.get_prompt(BASE_KEY, **_factory_kwargs(BASE_KEY))
        finally:
            snap = _metrics.get_snapshot()
            _metrics.reset()
        assert snap["missing_variables_total"].get(DB_ONLY_VAR, 0) >= 1, (
            f"渲染路径未把 DB 新增占位符计入缺失：{snap['missing_variables_total']}")


# ===========================================================================
# 5. R168 · G5：死 API 下线（零生产调用方）
# ===========================================================================
class TestDeadApiRemoved:
    def test_package_no_longer_exports_it(self):
        import app.services.ai.prompts as pkg
        assert not hasattr(pkg, "get_prompt_with_validation")
        assert "get_prompt_with_validation" not in pkg.__all__

    def test_cache_module_no_longer_defines_it(self):
        assert not hasattr(_cache, "get_prompt_with_validation")

    def test_no_production_reference_remains(self):
        """按 AST 判「代码引用」：注释/文档里的历史说明不算引用。"""
        import ast
        from pathlib import Path
        target = "get_prompt_with_validation"
        app_dir = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for path in app_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                hit = (
                    isinstance(node, ast.Name) and node.id == target
                ) or (
                    isinstance(node, ast.Attribute) and node.attr == target
                )
                if hit:
                    offenders.append(
                        f"{path.relative_to(app_dir).as_posix()}:{node.lineno}")
        assert not offenders, f"生产代码仍在引用已下线 API：{offenders}"


# ===========================================================================
# 6. R168 · D4：缺变量 / 未解析占位符纳入运行时指标
# ===========================================================================
class TestPromptRuntimeMetrics:
    def test_missing_variables_counted_by_name(self, prompt_db):
        factory = get_default_prompt(BASE_KEY)
        customized = factory + f"\n追加约束：{DB_ONLY_PLACEHOLDER}\n"
        path = prompt_db / "d4.sqlite"
        _seed_prompt_row(path, BASE_KEY, customized)
        _appdb.DB_PATH = path
        _cache.reload_prompt_cache()
        _metrics.reset()
        try:
            _cache.get_prompt(BASE_KEY, **_factory_kwargs(BASE_KEY))
            snap = _metrics.get_snapshot()
        finally:
            _metrics.reset()
        assert snap["missing_variables_total"].get(DB_ONLY_VAR, 0) >= 1

    def test_unresolved_placeholders_counted_by_name(self):
        _metrics.reset()
        try:
            render_prompt("请检查 {zz_probe_var} 是否缺失", other="v")
            snap = _metrics.get_snapshot()
        finally:
            _metrics.reset()
        assert snap["unresolved_placeholders_total"].get("zz_probe_var", 0) >= 1, (
            f"残留占位符未进入指标：{snap['unresolved_placeholders_total']}")

    def test_snapshot_exposes_new_counters_and_reset_clears(self):
        snap = _metrics.get_snapshot()
        assert "missing_variables_total" in snap
        assert "unresolved_placeholders_total" in snap
        _metrics.reset()
        assert _metrics.get_snapshot()["missing_variables_total"] == {}
        assert _metrics.get_snapshot()["unresolved_placeholders_total"] == {}


# ===========================================================================
# 7. R169 · G6：惰性注册不再打 "partially initialized" WARNING
# ===========================================================================
class TestLazyRegistrationNoWarning:
    """旧时序：``_registry`` 尾部在自身加载中触发惰性钩子 → bid 顶部 import
    ``_cache`` 撞半初始化模块 → ImportError 被钩子吞掉 → 每进程首次导入
    prompts 包必打一条 WARNING（包 ``__init__`` 尾部重试才成功）。收口后
    首次触发点收敛到包 ``__init__``（彼时 ``_cache``/``_registry`` 均已完成
    加载），bid 导入首次即成功。"""

    def test_registry_tail_no_longer_triggers_hook(self):
        """``_registry`` 模块尾部不得再有导入期 ``_apply_variable_contracts()``。"""
        import re
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / "app" / "services" / "ai" / "prompts" / "_registry.py"
               ).read_bytes().decode("utf-8")
        tail = src.rsplit("from app.services.ai.prompts import (", 1)[1]
        # 只判**代码语句**（行首调用），注释里的历史说明不算（R168 G5 同口径）。
        assert not re.search(r"(?m)^\s*_apply_variable_contracts\(\)", tail), (
            "_registry 尾部不得再于模块加载期触发契约套用/惰性钩子（G6 回退）")

    def test_first_import_is_warning_free_and_complete(self):
        """新进程实证：首次导入 prompts 包零 WARNING、19 条 bid 键就位。"""
        import subprocess
        import sys
        probe = (
            "import io, logging\n"
            "buf = io.StringIO()\n"
            "h = logging.StreamHandler(buf)\n"
            "logging.getLogger().addHandler(h)\n"
            "logging.basicConfig(level=logging.WARNING)\n"
            "import app.services.ai.prompts\n"
            "from app.services.ai.prompts._registry import (\n"
            "    _ALL_PROMPTS, _LAZY_REGISTERED)\n"
            "bid = [k for k in _ALL_PROMPTS if k.startswith('bid_')]\n"
            "undeclared = [k for k, v in _ALL_PROMPTS.items()\n"
            "              if v.get('requires') is None]\n"
            "out = buf.getvalue()\n"
            "assert len(bid) == 19 and _LAZY_REGISTERED and not undeclared\n"
            "assert 'partially initialized' not in out, out\n"
            "print('PROBE_OK')\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            timeout=120,
            cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        assert r.returncode == 0, f"探针失败：{r.stdout}\n{r.stderr}"
        assert "PROBE_OK" in r.stdout
