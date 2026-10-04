"""R39 · 提示词模块遗留技术债护栏（投标分析域 T1 + 全域覆盖不变量）。

对应 R39 报告：18 条投标解析 user 提示词 + 1 条通用 system 提示词此前硬编码在
``bid_analysis_service.py``，用户在提示词后台看不到也改不到，也无变量契约与
启动期漂移检测。本文件锁住修复后的行为不变量：

  ① 全域覆盖：注册表每个模板都有契约；无「未声明 requires」的模板；
  ② T1：19 个新键已注册、category 正确、requires 显式空声明；
  ③ T1：注册内容与出厂字面量逐字一致（零行为变化红线）；
  ④ T1：读取路径逐字等价（get_item_prompt / build_item / build_system_*）；
  ⑤ T1：DB 有覆盖时用户编辑生效（DB 优先语义），无覆盖时回退出厂字面量；
  ⑥ 反向护栏：把读取改回硬编码 → ④/⑤ 必失败（防退化）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SVC = ROOT / "app" / "services" / "bid_analysis_service.py"
ITEM_KEY_PREFIX = "bid_item_"
SYSTEM_KEY = "bid_analysis_system"
EXPECTED_ITEM_COUNT = 18


def _module_defs(path: Path) -> tuple[str | None, dict[str, str]]:
    """从源文件静态抽出 STABLE_SYSTEM_PROMPT 与 _ITEM_PROMPTS 字面量。"""
    t = ast.parse(path.read_text(encoding="utf-8"))
    system: str | None = None
    items: dict[str, str] = {}
    for n in ast.walk(t):
        if isinstance(n, ast.Assign) and any(
                getattr(x, "id", "") == "STABLE_SYSTEM_PROMPT" for x in n.targets):
            if isinstance(n.value, ast.Constant):
                system = n.value.value
        if isinstance(n, ast.AnnAssign) and \
                getattr(n.target, "id", "") == "_ITEM_PROMPTS":
            for k, v in zip(n.value.keys, n.value.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    items[k.value] = v.value
    return system, items


@pytest.fixture(scope="module")
def factory():
    from app.services import bid_analysis_service as svc
    return svc


@pytest.fixture(scope="module")
def literals():
    return _module_defs(SVC)


@pytest.fixture(scope="module", autouse=True)
def _ensure_registered():
    """确保投标分析域提示词已进注册表。

    ✅ 注册时机是「模块 import 时」或「prompts 包惰性钩子补注册时」（R39 T1）。
    单跑本护栏文件时两条路都不必然发生 —— 护栏必须**显式走一次生产读取
    路径**来触发，而不是直接读注册表：否则测的是「测试自己先 import 了一下
    service」的巧合，覆盖不到「管理接口在未导入 service 时能否列出这批模板」
    这个真实场景。
    """
    from app.services.ai.prompts._registry import register_lazy_prompts
    register_lazy_prompts()
    from app.services.ai.prompts import get_prompt
    # 走真实读取出口：未命中时 _cache 会触发补注册
    get_prompt("bid_analysis_system")
    yield


# =====================================================================
# ① 全域覆盖不变量
# =====================================================================
class TestGlobalCoverage:
    def test_every_registered_prompt_has_contract(self):
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, PROMPT_VARIABLE_CONTRACTS)
        missing = sorted(set(_ALL_PROMPTS) - set(PROMPT_VARIABLE_CONTRACTS))
        assert not missing, "已注册但无契约的模板：%s" % missing

    def test_no_prompt_has_undeclared_requires(self):
        """``requires`` 不得为 None —— 未声明会被「跳过校验」掩盖（R38 D6 语义）。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        undeclared = sorted(k for k, v in _ALL_PROMPTS.items()
                            if v.get("requires") is None)
        assert not undeclared, "requires 未声明（跳过契约校验）的模板：%s" % undeclared

    def test_contract_has_no_orphan_keys(self):
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, PROMPT_VARIABLE_CONTRACTS)
        orphan = sorted(set(PROMPT_VARIABLE_CONTRACTS) - set(_ALL_PROMPTS))
        assert not orphan, "契约里有、注册表里没有的键：%s" % orphan

    def test_zero_placeholder_templates_are_explicitly_declared(self):
        """零占位符模板必须显式声明空契约，不得靠「未声明」蒙混过关。

        ⚠️ 判据**不能**是「正文里有没有 ``{``」—— 零占位 system 模板普遍内嵌
        JSON 输出示例（``consistency_scan_system`` 的 ``{"conflicts":[]}``），
        按花括号判会误报。正确判据是仓库既有的权威函数
        :func:`extract_user_variables`（提取器已正确排除 JSON 键上下文）。

        ⚠️ ``bid_item_*`` 声明的是 ``["CONTEXT"]`` 而非 ``[]``：
        ``__CONTEXT__`` 是**真实占位符**（由 ``build_item`` 用 ``.replace``
        注入、不经 render），如实登记才能让启动期漂移校验归零。
        """
        from app.services.ai.prompts._registry import (
            _ALL_PROMPTS, extract_user_variables)
        zero = [k for k, v in _ALL_PROMPTS.items() if v.get("requires") == []]
        assert SYSTEM_KEY in zero, "通用 system 提示词未显式声明零变量"
        assert len(zero) >= 10, "显式零声明的模板数异常偏少：%d" % len(zero)
        for k in zero:
            body = _ALL_PROMPTS[k].get("default_content", "")
            assert extract_user_variables(body) == [], (
                "requires=[] 的模板 %s 实际含调用方变量 %s"
                % (k, extract_user_variables(body)))


# =====================================================================
# ②③ T1：注册完整性 + 零行为变化
# =====================================================================
class TestBidPromptsRegistered:
    def test_all_item_prompts_registered(self, literals):
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        _, items = literals
        assert len(items) == EXPECTED_ITEM_COUNT, (
            "解析项数量变化，护栏基线需同步（当前 %d）" % len(items))
        missing = [k for k in items
                   if ITEM_KEY_PREFIX + k not in _ALL_PROMPTS]
        assert not missing, "未注册的解析项提示词：%s" % missing

    def test_system_prompt_registered(self):
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        assert SYSTEM_KEY in _ALL_PROMPTS, "通用 system 提示词未注册"

    def test_category_visible_in_list_prompts(self):
        """用户后台按分类能看到这批模板（否则等于没接进编辑器）。"""
        from app.services.ai.prompts import list_prompts
        items = list_prompts("bid_analysis")
        assert len(items) == EXPECTED_ITEM_COUNT + 1, (
            "bid_analysis 分类下模板数异常：%d" % len(items))
        assert any(p["key"] == SYSTEM_KEY for p in items)

    def test_registered_content_is_byte_identical(self, literals):
        """注册内容必须与模块内出厂字面量逐字一致（零行为变化红线）。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        system, items = literals
        for iid, body in items.items():
            got = _ALL_PROMPTS[ITEM_KEY_PREFIX + iid]["default_content"]
            assert got == body, "注册内容与出厂字面量不一致：%s" % iid
        assert _ALL_PROMPTS[SYSTEM_KEY]["default_content"] == system

    def test_registration_happens_once(self, factory):
        """重复调用 _reg_bid_prompts 不得改变内容（幂等）。"""
        before = factory.get_bid_system_prompt()
        factory._reg_bid_prompts()
        assert factory.get_bid_system_prompt() == before


# =====================================================================
# ④⑤ T1：读取路径逐字等价 + DB 优先
# =====================================================================
class TestBidPromptReadPath:
    def test_get_item_prompt_matches_literal(self, factory, literals):
        _, items = literals
        for iid, body in items.items():
            assert factory.get_item_prompt(iid) == body, iid

    def test_build_item_injects_context(self, factory, literals):
        _, items = literals
        content = "招标文件正文片段 ABC"
        iid = "projectBasicInfo"
        got = factory.build_item(content, {"item_id": iid, "label": "项目级基本信息"})
        assert got == items[iid].replace("__CONTEXT__", content)
        assert content in got and "__CONTEXT__" not in got

    def test_build_system_prompt_appends_hints(self, factory, literals):
        system, _ = literals
        got = factory.build_system_prompt(section_hint="标段1",
                                          classification_hint="危大")
        assert got.startswith(system)
        assert "【当前处理标段上下文】标段1" in got
        assert "【本方案危大工程分类结论（提取重点参考）】危大" in got

    def test_build_system_prompt_empty_hints_is_verbatim(self, factory, literals):
        """空 hint 必须与出厂字面量逐字一致（向后兼容红线）。"""
        system, _ = literals
        assert factory.build_system_prompt() == system
        assert factory.build_system_messages()[0]["content"] == system

    def test_unknown_item_falls_back(self, factory):
        assert factory.get_item_prompt("__nope__") is None
        out = factory.build_item("X", {"item_id": "__nope__", "label": "标签"})
        assert out.startswith("请从以下项目资料文本中提取")
        assert "X" in out

    def test_db_override_takes_effect(self, factory, monkeypatch, literals):
        """DB 覆盖必须即时生效（T1 的核心价值：用户后台改得到）。"""
        _, items = literals
        marker = "【R39 测试覆盖】"
        monkeypatch.setattr(factory, "get_prompt",
                            lambda key: marker + items["projectBasicInfo"])
        assert factory.get_item_prompt("projectBasicInfo") == \
            marker + items["projectBasicInfo"]

    def test_registry_failure_falls_back_to_literal(self, factory, monkeypatch,
                                                    literals):
        """注册表读取抛异常必须降级为出厂字面量，不得阻断提取。"""
        system, items = literals

        def _boom(_key):
            raise RuntimeError("db down")

        monkeypatch.setattr(factory, "get_prompt", _boom)
        assert factory.get_bid_system_prompt() == system
        assert factory.get_item_prompt("projectBasicInfo") == \
            items["projectBasicInfo"]

    def test_empty_db_content_falls_back(self, factory, monkeypatch, literals):
        """DB 内容为空串时回退出厂（不把空提示词下发给模型）。"""
        system, _ = literals
        monkeypatch.setattr(factory, "get_prompt", lambda key: "")
        assert factory.get_bid_system_prompt() == system


# =====================================================================
# ⑥ 反向护栏：读取不得退回硬编码
# =====================================================================
class TestNoRegressionToHardcoded:
    def test_get_item_prompt_goes_through_registry(self, factory):
        """源码级锁定：get_item_prompt 必须经 get_prompt，不得直接读 _ITEM_PROMPTS。"""
        src = SVC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "get_item_prompt")
        called = {getattr(x.func, "id", None) for x in ast.walk(fn)
                  if isinstance(x, ast.Call)}
        assert "get_prompt" in called, "get_item_prompt 未走统一注册表读取"

    def test_system_prompt_readers_go_through_registry(self, factory):
        """build_system_prompt / build_system_messages 不得直接引用 STABLE_SYSTEM_PROMPT。"""
        src = SVC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for name in ("build_system_prompt", "build_system_messages"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == name)
            direct = {x.id for x in ast.walk(fn)
                      if isinstance(x, ast.Name) and x.id == "STABLE_SYSTEM_PROMPT"
                      and isinstance(x.ctx, ast.Load)}
            assert not direct, (
                "%s 直接引用了出厂字面量，绕过注册表" % name)

    def test_every_registration_declares_requires(self, factory):
        """bid_item_* 登记 ``CONTEXT``（build_item 注入的真实占位符）。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        items = [k for k in _ALL_PROMPTS if k.startswith(ITEM_KEY_PREFIX)]
        assert len(items) == EXPECTED_ITEM_COUNT
        for k in items:
            assert _ALL_PROMPTS[k].get("requires") == ["CONTEXT"], (
                "%s 的 requires 不是 ['CONTEXT']" % k)
        assert _ALL_PROMPTS[SYSTEM_KEY].get("requires") == [], (
            "system 模板应为零变量显式声明")

    def test_reg_preserves_explicit_empty_declaration(self, factory):
        """``_reg(requires=[])`` 必须落库为 ``[]``，不得压成 None。

        ✅ R39 A/B R1 的判别点。这里必须**直接调 _reg** 而不能看注册表里
        既有模板的 requires：既有模板的 requires 会被
        ``_apply_variable_contracts`` 用**契约表**覆盖（契约表才是权威源），
        变异 ``_reg`` 的判别对它们不可见 —— 头两版 A/B 正是因此拿到 RC=0
        的假阴性。直接注册一个探针键才能真正判别这一处。
        """
        from app.services.ai.prompts._registry import _ALL_PROMPTS, _reg
        probe = "__r39_probe_zero_decl__"
        try:
            _reg(probe, "r39_probe", "探针", "零占位，无花括号。", requires=[])
            assert _ALL_PROMPTS[probe]["requires"] == [], (
                "显式空声明被压成 %r" % _ALL_PROMPTS[probe]["requires"])
            # 未声明（None）必须仍是 None，两者不得混淆
            probe2 = "__r39_probe_undeclared__"
            _reg(probe2, "r39_probe", "探针", "x", requires=None)
            assert _ALL_PROMPTS[probe2]["requires"] is None, (
                "未声明被误判为显式空声明")
        finally:
            _ALL_PROMPTS.pop(probe, None)
            _ALL_PROMPTS.pop("__r39_probe_undeclared__", None)

    def test_startup_contract_check_is_clean(self, factory):
        """启动期契约校验必须零漂移（否则 strict 模式会阻断启动）。"""
        from app.services.ai.prompts._registry import check_prompt_variables
        assert check_prompt_variables() == [], (
            "契约漂移：%s" % check_prompt_variables()[:3])