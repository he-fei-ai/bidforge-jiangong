# -*- coding: utf-8 -*-
"""
R74 后续修改项（G2/G3/G4）静态接线 + 行为护栏（2026-10-12）。

背景：R74 §8 遗留四项后续修改（G1 前端呈现 / G2 提示词补齐逐条覆盖 /
G3 _plan_guidance 静默丢弃 / G4 DB_PATH 跨用例污染治理）。G1/G5 由前端
Vitest 护栏（outlineReviewBasisR74.test.ts / importParseWarningWiringR72）
守护；本文件守后端三项，核心是防「实现了但链路不生效」回退
（AGENTS §5 同族：D1 编排 100% 空转就是只有端到端/形态护栏能抓的缺陷）。

分组：
- A 组：G3 —— stable-prefix 关闭分支必须消费 _plan_guidance；变量必须
  在 try 外初始化（NameError 防御）；
- B 组：G2 —— outline_patch_system 必须含「逐条覆盖」硬要求关键判据，
  且既有占位符一个不少（迁移契约由 r38/r39 双向护栏另守）；
- C 组：G4 —— conftest autouse 复位 fixture 在场（静态）+ 行为实证
  （改脏 DB_PATH 的测试之后，下一支测试读到的是原值）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
_BACKEND = _TESTS.parent
_SRC = _BACKEND / "app" / "routers" / "sse_handlers.py"
_PROMPTS = _BACKEND / "app" / "services" / "ai" / "prompts" / "outline.py"
_CONFTEST = _TESTS / "conftest.py"
_FIP = _TESTS / "test_file_import_pipeline.py"


def _read(p: Path) -> str:
    # ⚠️ 仓库惯例：字节读 + 显式 decode，不经 pathlib.read_text 的
    # universal-newlines（CRLF 会把锚串判据带偏，R70 Round7 教训）。
    return p.read_bytes().decode("utf-8")


# 行为实证的基线存储（由 c3a 在改脏**之前**写入）。
# ⚠️ 绝不能在模块收集期快照：若 conftest autouse 失效（A/B M4 变异形态），
# 先前污染文件的残值会被当成基线，c3b 变成永真式、变异漏检。
_DB_BASELINE = {"path": None}


# ============================================================
# A 组 · G3 _plan_guidance 在 stable-prefix 关闭链路必须可达
# ============================================================
class TestPlanGuidanceWiringR74:
    def setup_method(self):
        self.src = _read(_SRC)

    def test_a1_guidance_initialized_outside_try(self):
        """`_plan_guidance = ""` 必须在 D1 开关判断之前初始化。

        旧实现只在 try 内赋值：编排中途抛异常 → else 分支引用 NameError。
        """
        init = self.src.find('_plan_guidance = ""')
        gate = self.src.find("if _plan_enabled and bool(getattr(settings,")
        assert init >= 0, "_plan_guidance 初始化语句丢失（G3 回退）"
        assert gate >= 0
        assert init < gate, "_plan_guidance 必须初始化在 D1 开关判断之前"

    def test_a2_disabled_branch_concatenates_guidance(self):
        """stable-prefix 关闭分支必须把引导语拼进 user_content（修复本体）。"""
        assert 'user_content = user_content + "\\n\\n" + _plan_guidance' in self.src, (
            "else 分支对 _plan_guidance 的拼接丢失 —— 引导语将再次静默丢弃"
        )

    def test_a3_concat_before_message_assembly(self):
        """拼接必须发生在该分支 messages 组装之前（否则引导语进不了请求体）。"""
        cat = self.src.find('user_content + "\\n\\n" + _plan_guidance')
        asm = self.src.find(
            'messages = [{"role": "system", "content": sys_prompt}', cat)
        assert cat >= 0 and asm > cat, "引导语拼接必须在 else 分支 messages 组装之前"

    def test_a4_chapter_extras_if_elif_still_present(self):
        """stable-prefix 开启链路的 _chapter_extras 消费结构不因 G3 破坏。"""
        assert "if _plan_guidance and _chapter_extras:" in self.src
        assert "elif _plan_guidance:" in self.src

    def test_a5_ab_reverse_anchor(self):
        """反向锚：模拟「摘除 else 拼接」的旧形态，判据必须归零（证明在守）。"""
        stripped = self.src.replace(
            'user_content = user_content + "\\n\\n" + _plan_guidance', "")
        assert "_plan_guidance" in stripped  # 其余消费点仍在场
        assert 'user_content = user_content + "\\n\\n" + _plan_guidance' not in stripped


# ============================================================
# B 组 · G2 outline_patch_system「逐条覆盖缺失项」硬要求
# ============================================================
@pytest.fixture(scope="module")
def patch_tpl() -> str:
    src = _read(_PROMPTS)
    m = re.search(r'_reg\("outline_patch_system".*?"""(.*?)"""', src, re.S)
    assert m, "outline_patch_system 模板注册丢失"
    return m.group(1)


class TestPatchPromptCoverageR74:
    def test_b1_item_by_item_requirement(self, patch_tpl):
        for phrase in ("逐条对应、一项不漏", "new_chapters 条数不得少于缺失项条数",
                       "严禁用无关章节充数"):
            assert phrase in patch_tpl, f"补齐提示词缺少关键判据「{phrase}」"

    def test_b2_self_check_clause_present(self, patch_tpl):
        assert "输出前自检" in patch_tpl, "补齐提示词的逐条自检条款丢失"

    def test_b3_placeholders_untouched(self, patch_tpl):
        """迁移护栏：占位符集合是渲染契约（r38/r39），G2 只改正文不得动变量。"""
        for var in ("scheme_name", "scheme_type", "project_brief",
                    "project_facts", "chapter_titles", "missing_items"):
            assert "{" + var + "}" in patch_tpl, f"占位符 {{{var}}} 丢失"


# ============================================================
# C 组 · G4 DB_PATH 跨用例污染的中心化治理
# ============================================================
class TestDbPathRestoreGovernanceR74:
    def test_c1_autouse_fixture_defined(self):
        src = _read(_CONFTEST)
        assert "@pytest.fixture(autouse=True)" in src
        assert "def _restore_app_db_path" in src, (
            "conftest 的 DB_PATH 快照-复位 autouse fixture 丢失（G4 治理回退）"
        )

    def test_c2_file_import_pipeline_uses_monkeypatch(self):
        src = _read(_FIP)
        assert "monkeypatch.setattr(_appdb, \"DB_PATH\"" in src, (
            "gf_ctx 必须经 monkeypatch 改 DB_PATH（裸赋值即污染源）"
        )
        assert not re.search(r"^\s*_appdb\.DB_PATH\s*=", src, re.M), (
            "test_file_import_pipeline.py 不得残留对 DB_PATH 的裸赋值"
        )


class TestDbPathRestoreBehaviorR74:
    """行为实证：autouse 真的把脏值复位（顺序依赖同文件内定义序，pytest 保证）。

    基线在 c3a 改脏之前取样（见 _DB_BASELINE 注释）；c3b 与基线比对。
    autouse 失效时：若会话内有先前污染文件，c3a 采到的已是脏值→c3b 红；
    若无，c3b 仍红（c3a 自己的脏值未被复位）⇒ 变异必抓。
    """

    def test_c3a_dirty_db_path(self):
        import app.db as _appdb
        from pathlib import Path as _P
        _DB_BASELINE["path"] = str(_appdb.DB_PATH)
        _appdb.DB_PATH = _P("__r74_pollution_marker__.sqlite")
        assert "pollution" in str(_appdb.DB_PATH)

    def test_c3b_autouse_restored_original(self):
        import app.db as _appdb
        assert _DB_BASELINE["path"] is not None, (
            "c3a 未先运行（不得单跑本用例，基线无从而来）")
        assert str(_appdb.DB_PATH) == _DB_BASELINE["path"], (
            "上一用例改脏的 DB_PATH 未被 conftest autouse 复位 —— "
            "跨用例污染治理（G4）已失效"
        )
