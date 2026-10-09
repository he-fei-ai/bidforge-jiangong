"""审核规则命名空间收口回归护栏（2026-10-03）。

覆盖的修复点（audit_rules.py）：

1. **STD-05 改判 program 通道**：preflight_engine.check_standards（L446 起）
   与 content_checkpoint 一直在程序化判定 STD-05，注册表却登记为 ai ——
   /check 的 fallback 清单来自 ``ai_rules()``，导致「程序已判的规则再送 AI」：
   浪费 token + 可能给出与程序结论矛盾的判定 + 双通道各报一份被
   merge_findings 去重掩盖（用户无从感知）。
2. **_PROGRAM_EMITTED_RULE_IDS 恢复诚实声明**：该集合的契约是「引擎实际
   产出的基编号全集」，此前既虚登了引擎从不产出的 AI 规则
   （SAF-01/02/07），又漏登了引擎确实产出的 SAF-08 —— 两个方向的漂移都
   会让「产出 ⇒ program」一致性自检失去判据。
3. **validate_rule_registry 新不变量**：引擎产出的规则必须登记为
   mode=program，漂移从「不可检测」变为「启动即报错」。
4. **静态扫引擎源码**：产出集合 ↔ 登记表**双向相等**（写死的字符串断言
   会被新增产出静默绕过，AST 扫描让任何新 _finding 编号必须过登记这一关）。
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib
import re

from app.services.audit_rules import (
    _PROGRAM_EMITTED_RULE_IDS,
    CHECK_MODE_AI,
    CHECK_MODE_PROGRAM,
    RULE_VERSION,
    ai_rules,
    get_rule,
    program_rules,
    validate_rule_registry,
)

_ENGINE_SRC = (
    pathlib.Path(__file__).resolve().parent.parent
    / "app" / "services" / "preflight_engine.py")

_BASE_RE = re.compile(r"^[A-Z]{3}-\d{2}$")
_DERIVED_PREFIX_RE = re.compile(r"^([A-Z]{3}-\d{2})-$")


def _scan_engine_emitted() -> set[str]:
    """AST 扫描 preflight_engine 源码中所有**会产出**的基规则编号。

    收集三类常量形态（均排除 docstring —— 模块/函数文档里大量出现
    「STD-01 漏判」这类示例串，不得计入产出）：
    - ``_finding("XXX-NN", ...)`` 与 ``{"rule_id": "XXX-NN", ...}`` 的完整编号；
    - f-string 前缀 ``"CON-05-"`` / ``"CON-06-"``（派生编号 → 基编号）；
    - ``f"CMP-{idx:02d}"`` 的族前缀 ``"CMP-"``（循环产出 CMP-01~09）。
    """
    tree = ast.parse(_ENGINE_SRC.read_text(encoding="utf-8"))
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in doc_ids:
            continue
        v = node.value
        if _BASE_RE.match(v):
            found.add(v)
        elif _DERIVED_PREFIX_RE.match(v):
            found.add(_DERIVED_PREFIX_RE.match(v).group(1))
        elif v == "CMP-":
            found.update(f"CMP-{i:02d}" for i in range(1, 10))
    return found


# ===========================================================================
# 一、STD-05 通道归属（判据单一事实源）
# ===========================================================================
class TestStd05Namespace:
    def test_std05_mode_is_program(self):
        assert get_rule("STD-05").mode == CHECK_MODE_PROGRAM

    def test_std05_not_in_ai_rules(self):
        """ai_rules() 是 /check fallback 清单唯一来源 —— STD-05 必须在程序侧。"""
        assert "STD-05" not in {r.rule_id for r in ai_rules()}

    def test_std05_in_program_rules(self):
        assert "STD-05" in {r.rule_id for r in program_rules()}

    def test_check_fallback_checklist_excludes_std05(self):
        """compliance.py L227 的 `[r.title for r in ai_rules()]` 逐字复算。"""
        checklist = [r.title for r in ai_rules()]
        assert get_rule("STD-05").title not in checklist

    def test_std_sibling_rules_consistent(self):
        """STD-01~05 全部为 program（本缺陷正是 STD-05 曾是唯一例外）。"""
        for i in range(1, 6):
            assert get_rule(f"STD-0{i}").mode == CHECK_MODE_PROGRAM


# ===========================================================================
# 二、_PROGRAM_EMITTED_RULE_IDS 的诚实性
# ===========================================================================
class TestEmittedSetHonesty:
    def test_saf08_registered_as_emitted(self):
        """check_hazard_params（2026-10-03）产出 SAF-08，登记必须同步。"""
        assert "SAF-08" in _PROGRAM_EMITTED_RULE_IDS

    def test_ai_only_safety_rules_not_in_emitted_set(self):
        """SAF-01/02/07 是 AI 语义通道规则，引擎从不产出 —— 不得混入产出全集。"""
        for rid in ("SAF-01", "SAF-02", "SAF-07"):
            assert rid not in _PROGRAM_EMITTED_RULE_IDS, f"{rid} 不应登记为引擎产出"
            assert get_rule(rid).mode == CHECK_MODE_AI

    def test_registry_healthy(self):
        assert validate_rule_registry(
            emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS) == []

    def test_rule_version_bumped(self):
        assert tuple(int(x) for x in RULE_VERSION.split(".")[:2]) >= (1, 8)


# ===========================================================================
# 三、静态扫引擎：产出集合 ↔ 登记表双向相等（防未来漂移）
# ===========================================================================
class TestEngineScanParity:
    def test_scanned_set_not_empty(self):
        """防扫描器失灵空转（AGENTS §5.14：阳性断言前先确认判据真的能判）。"""
        scanned = _scan_engine_emitted()
        assert len(scanned) >= 30

    def test_scanned_equals_registered(self):
        """引擎产出的编号必须与登记表完全相等：
        - 漏登记 → 新增 _finding 后 mode 一致性自检覆盖不到（SAF-08 教训）；
        - 虚登记 → 「引擎产出全集」不再诚实，且若该规则仍是 ai 会误伤。"""
        scanned = _scan_engine_emitted()
        registered = set(_PROGRAM_EMITTED_RULE_IDS)
        assert scanned - registered == set(), (
            f"引擎产出但未登记: {sorted(scanned - registered)}")
        assert registered - scanned == set(), (
            f"登记为引擎产出但源码中找不到产出点: {sorted(registered - scanned)}")


# ===========================================================================
# 四、validate_rule_registry 的 mode 不变量（A/B 反向验证锚点）
# ===========================================================================
class TestModeInvariantGuard:
    def test_drift_detected_when_emitted_rule_registered_as_ai(self):
        """把 STD-05 改回 ai（还原缺陷形态），自检必须报出该问题。"""
        import app.services.audit_rules as ar

        saved = (ar.ALL_RULES, ar.RULE_MAP)
        try:
            drifted = tuple(
                dataclasses.replace(r, mode=CHECK_MODE_AI)
                if r.rule_id == "STD-05" else r
                for r in saved[0])
            ar.ALL_RULES = drifted
            ar.RULE_MAP = {r.rule_id: r for r in drifted}
            problems = validate_rule_registry(
                emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS)
            assert any("STD-05" in p and "mode" in p for p in problems), \
                f"STD-05 改回 ai 后自检未报 mode 漂移: {problems}"
        finally:
            ar.ALL_RULES, ar.RULE_MAP = saved
        # 还原后必须重新健康（防残留）
        assert validate_rule_registry(
            emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS) == []

    def test_unresolved_emitted_id_still_reported(self):
        """既有「不可解析」断言不受新不变量影响（回退兼容）。"""
        problems = validate_rule_registry(
            emitted_rule_ids=_PROGRAM_EMITTED_RULE_IDS | {"ZZZ-99"})
        assert any("ZZZ-99" in p for p in problems)


# ===========================================================================
# 五、族前缀别名（CON-SCAN → CON-07）：遗留收口 2026-10-03
#    背景：总检聚合把一致性扫描未解决冲突编为 ``CON-SCAN-<n>``
#    （routers/compliance.py），该族串不符合 XXX-NN 格式红线不能直接登记，
#    此前在注册表查不到 → 规则详情反查/能力归一落空。别名接回后，
#    生成侧字符串不变（历史行/前端契约零破坏）。
# ===========================================================================
class TestFamilyPrefixAlias:
    def test_con_scan_derived_resolves_to_registered_base(self):
        """CON-SCAN-<n> / CON-SCAN 均须回退到已登记的 CON-07。"""
        from app.services.audit_rules import _resolve_base_rule

        for rid in ("CON-SCAN-1", "CON-SCAN-7", "CON-SCAN"):
            base = _resolve_base_rule(rid)
            assert base is not None, f"{rid} 仍无法在注册表解析"
            assert base.rule_id == "CON-07", f"{rid} 归一到意外规则 {base.rule_id}"
        rule = _resolve_base_rule("CON-SCAN-1")
        assert rule.mode == CHECK_MODE_PROGRAM, "扫描器程序化产出的规则不得登记为 ai"
        assert rule.dimension == "consistency"

    def test_alias_targets_must_be_registered(self):
        """别名表每个目标必须真实存在于注册表（防指向幽灵规则）。"""
        from app.services.audit_rules import _RULE_ID_ALIASES, RULE_MAP

        for fam, target in _RULE_ID_ALIASES.items():
            assert target in RULE_MAP, f"别名 {fam} 指向未注册规则 {target}"
            assert RULE_MAP[target].rule_id == target

    def test_capability_of_con_scan_normalizes_without_behavior_change(self):
        """autofix 能力归一：CON-SCAN-<n> 解析到 CON-07 后仍无修复能力，
        兑底文案与归一前同为 CON 族 —— 用户可见行为零变化是本轮收口的
        硬性前提，若未来给 CON-07 登记真实能力，本例会定向失败提醒同步。"""
        from app.services.review_autofix import FIX_MODE_MANUAL, capability_of

        cap = capability_of("CON-SCAN-1")
        assert cap.mode == FIX_MODE_MANUAL, cap
        assert "一致性" in cap.reason or "一致性问题" in cap.reason, cap.reason

    def test_emitted_family_strings_have_alias_or_registration(self):
        """静态锁：生成侧（routers/compliance.py）新造的非 XXX-NN 族串
        必须同步登记别名，否则「规则说明」反查再次落空（本缺陷复发预防爆）。"""
        import pathlib
        import re

        from app.services.audit_rules import _RULE_ID_ALIASES, RULE_MAP

        src = (pathlib.Path(__file__).resolve().parents[1] / "app" /
               "routers" / "compliance.py").read_text(encoding="utf-8")
        # f"XXX-YY-<n>" 形态的族前缀（两段大写字母 + 连字符）
        families = set(re.findall(r'f"([A-Z]{3}-[A-Z]+)-\{', src))
        for fam in families:
            assert fam in _RULE_ID_ALIASES or fam in RULE_MAP, (
                f"生成侧产出族编号 {fam}-<n>，但既未登记也未建别名，"
                f"「规则说明」抽屉将再次查不到该串")
        assert "CON-SCAN" in families, "扫描基准样本失效（生成侧字符串已变？）"

