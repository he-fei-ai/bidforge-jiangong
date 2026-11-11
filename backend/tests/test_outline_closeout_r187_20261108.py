"""R187（2026-11-08）目录生成模块深度审计收口 · 护栏集。

本轮修复面（全部有 file:line 证据，见 docs/目录生成模块深度审计收口_R187_20261108.md）：
  FIX-1 移动章节漏算子树高度 → level=4 / id="x.y.z.w" 从三级界面消失
  FIX-2 短方案 checkpoint 写失败仍静默 completed（R89 S2 只修长路径）
  FIX-3 审核超时/失败/结构异常被写成 passed=True 且日志宣称「跳过审核直接完成」
  FIX-4 auto_confirm 的 numbering_invalid 硬前置恒 0（issue_counts 嵌套层级错位）
  FIX-5 normalize_outline 重排异常被吞 → 脏/重复 id 落库（fail-open）
  FIX-6 oneshot→stepwise 切档把 last_p 退回 0.05 → SSE 进度 0.55→0.1 可见回退
  FIX-7 零宽/控制字符标题在写路径零清洗
  FIX-8 R184 护栏子进程 stdout 编码（GBK 输出 / UTF-8 解码 → 中文冒号假红）
  OPT-1 L0/L1 提示词补齐「编号命名空间」硬约束（此前只有 L2/L3 与 feedback 有）

护栏三层：纯函数行为实证 / 源码 AST·文本棘轮 / 非空转自证。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from app.models import SectionUpdate
from app.routers import sections as sec
from app.services import outline_auto_confirm as oac
from app.services import outline_utils as ou
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[1]
SECTIONS_SRC = (BACKEND / "app" / "routers" / "sections.py").read_text(encoding="utf-8")
SSE_SRC = (BACKEND / "app" / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
REVIEW_SRC = (BACKEND / "app" / "routers" / "_sse_outline_review.py").read_text(encoding="utf-8")
PROMPT_SRC = (BACKEND / "app" / "services" / "ai" / "prompts" / "outline.py").read_text(encoding="utf-8")
ANCESTOR_TEST_SRC = (BACKEND / "tests" / "test_outline_ancestor_guard_20261107.py").read_text(encoding="utf-8")


def _legal_tree() -> list[dict]:
    """合法三级树：子承父系 + 等级正传。"""
    return [
        {
            "id": "1", "level": 1, "title": "工程概况",
            "children": [
                {
                    "id": "1.1", "level": 2, "title": "编制依据",
                    "children": [
                        {"id": "1.1.1", "level": 3, "title": "规范清单", "children": []},
                    ],
                },
                {"id": "1.2", "level": 2, "title": "施工部署", "children": []},
            ],
        },
        {
            "id": "2", "level": 1, "title": "施工工艺",
            "children": [{"id": "2.1", "level": 2, "title": "工艺流程", "children": []}],
        },
    ]


# ---------------------------------------------------------------- A 编号校验
class TestNumberingFailClosed:
    """FIX-5：validate_outline_numbering 纯函数行为实证。"""

    def test_legal_three_level_tree_passes(self):
        assert ou.validate_outline_numbering(_legal_tree()) == []

    def test_dirty_client_ids_rejected(self):
        tree = [
            {"id": "abc1.2", "level": 2, "title": "A", "children": []},
            {"id": "1..2", "level": 2, "title": "B", "children": []},
            {"id": "", "level": 2, "title": "C", "children": []},
        ]
        problems = ou.validate_outline_numbering(tree)
        assert len(problems) >= 3
        assert all("编号格式非法" in p for p in problems)

    def test_duplicate_ids_rejected(self):
        tree = [
            {"id": "1", "level": 1, "title": "A", "children": []},
            {"id": "1", "level": 1, "title": "B", "children": []},
        ]
        assert any("编号重复" in p for p in ou.validate_outline_numbering(tree))

    def test_id_not_matching_tree_position_rejected(self):
        tree = [
            {
                "id": "1", "level": 1, "title": "A",
                "children": [{"id": "1.2", "level": 2, "title": "B", "children": []}],
            }
        ]
        assert any("编号与树位置不一致" in p for p in ou.validate_outline_numbering(tree))

    def test_level_not_matching_segments_rejected(self):
        tree = [{"id": "1.1", "level": 1, "title": "A", "children": []}]
        assert any("level" in p and "不一致" in p for p in ou.validate_outline_numbering(tree))

    def test_fourth_level_rejected(self):
        tree = [
            {
                "id": "1", "level": 1, "title": "A",
                "children": [{
                    "id": "1.1", "level": 2, "title": "B",
                    "children": [{
                        "id": "1.1.1", "level": 3, "title": "C",
                        "children": [{"id": "1.1.1.1", "level": 4, "title": "D", "children": []}],
                    }],
                }],
            }
        ]
        assert any("超过 3 级" in p for p in ou.validate_outline_numbering(tree))

    def test_overlong_title_rejected_not_silently_truncated(self):
        tree = [{"id": "1", "level": 1, "title": "措" * 201, "children": []}]
        problems = ou.validate_outline_numbering(tree)
        assert any("字上限" in p for p in problems)

    def test_self_proof_validator_is_not_vacuously_green(self):
        """非空转自证：把合法树的 id 全部改成同一个，判据必须变红。"""
        tree = _legal_tree()
        assert ou.validate_outline_numbering(tree) == []
        for node in tree:
            node["id"] = "1"
        assert ou.validate_outline_numbering(tree), "判据恒真（护栏形同虚设）"


# ------------------------------------------- B 编号正确率 / 层级完整率量化锚
class TestNormalizeInvariants:
    """用户要求的「编号正确率 / 层级完整率」可复算锚。

    五组入站树（脏 id / 四级深树 / 非 dict 混合 / 中文编号标题 / children=None）
    经唯一规范化入口后，必须 5/5 满足：编号与树位置一致、全树唯一、
    level=点分段数、层级 ≤3 —— 即正确率 100%、完整率 100%。
    """

    @staticmethod
    def _samples() -> list[list]:
        return [
            [{"title": "工程概况", "children": [{"title": "编制依据", "children": []}]}],
            [
                {"title": "第一章 施工工艺", "children": [
                    {"title": "1.1 流程", "children": [
                        {"title": "1.1.1 参数", "children": [
                            {"title": "1.1.1.1 越界节点", "children": []},
                        ]},
                    ]},
                ]},
            ],
            ["非法字符串", {"title": "合法章", "children": []}],
            [{"title": "一、工程概况", "children": [{"title": "（一）依据", "children": []}]}],
            [{"title": "空 children", "children": None}, {"title": "验收要求", "children": []}],
        ]

    def test_all_samples_normalize_to_legal_numbering(self):
        results = []
        for sample in self._samples():
            normalized = ou.normalize_outline(sample)
            results.append(ou.validate_outline_numbering(normalized) == [])
        assert len(results) == 5
        assert all(results), f"编号正确率/层级完整率未达 100%: {results}"

    def test_normalized_tree_has_no_duplicate_or_deep_ids(self):
        for sample in self._samples():
            ids: list[str] = []

            def walk(nodes):
                for n in nodes:
                    ids.append(str(n.get("id") or ""))
                    walk(n.get("children") or [])

            walk(ou.normalize_outline(sample))
            assert len(ids) == len(set(ids))
            assert all(i.count(".") + 1 <= ou.MAX_OUTLINE_DEPTH for i in ids)

    def test_renumber_failure_log_is_error_level(self):
        """FIX-5 可观测性棘轮：重排异常不再用 WARNING「保留裁剪结果」掩盖。"""
        assert 'logger.error("目录重排编号失败（返回未编号树，落库口须拒绝）' in (
            BACKEND / "app" / "services" / "outline_utils.py"
        ).read_text(encoding="utf-8")


# ---------------------------------------------------------------- C 标题清洗
class TestTitleSanitize:
    """FIX-7：写路径剥离不可见/控制字符，不改写用户可见字符。"""

    def test_zero_width_stripped(self):
        assert ou.sanitize_outline_title("措\u200b施1") == ("措施1", True)

    def test_control_chars_stripped(self):
        assert ou.sanitize_outline_title("A\x01B\ufeffC") == ("ABC", True)

    def test_normal_cjk_and_fullwidth_untouched(self):
        for t in ("深基坑支护", "监测措施（2）", "第一章 总则"):
            assert ou.sanitize_outline_title(t) == (t, False)

    def test_idempotent(self):
        cleaned, _ = ou.sanitize_outline_title("\u200b措施\u00ad1")
        assert ou.sanitize_outline_title(cleaned) == (cleaned, False)

    def test_self_proof_long_title_is_not_truncated_by_sanitizer(self):
        """长度超限由 validate 以可行动 400 拒绝，清洗函数绝不静默截断。"""
        long_title = "措" * 250
        cleaned, changed = ou.sanitize_outline_title(long_title)
        assert cleaned == long_title and changed is False


# ---------------------------------------------------------------- D 源码棘轮
class TestSourceRatchets:
    def test_save_outline_wires_validator_before_write(self):
        fn_src = SECTIONS_SRC.split("async def _save_outline_to_db(", 1)[1].split(
            "\nasync def ", 1
        )[0]
        assert "validate_outline_numbering(outline)" in fn_src
        assert "normalize_outline(outline)" in fn_src
        assert fn_src.index("normalize_outline(outline)") < fn_src.index(
            "validate_outline_numbering(outline)"
        ), "校验必须发生在规范化之后、落库之前"
        assert 'raise HTTPException(400,' in fn_src

    def test_title_sanitizer_wired_in_process_nodes(self):
        assert "title, _title_changed = sanitize_outline_title(title)" in SECTIONS_SRC

    def test_title_sanitized_is_registered_in_additive_keys(self):
        """R88 双向棘轮：新增加法键必须登记，否则 apply-and-save 链路静默丢字段。"""
        assert "title_sanitized" in sec.SAVE_OUTLINE_ADDITIVE_KEYS
        assert sec.passthrough_additive({"title_sanitized": 2, "ok": True}) == {"title_sanitized": 2}

    def test_subtree_height_guard_present(self):
        assert "subtree_height = 1" in SECTIONS_SRC
        assert 'row["level"] + subtree_height > MAX_OUTLINE_DEPTH' in SECTIONS_SRC
        assert "_child_depth = _depth + 1" in SECTIONS_SRC

    def test_short_checkpoint_failure_degrades(self):
        """FIX-2：短方案与长方案同口径（degraded + checkpoint_failed）。"""
        assert "_short_ckpt_ok = True" in SSE_SRC
        assert '"degraded" if not _short_ckpt_ok else "completed"' in SSE_SRC
        assert '_short_payload["checkpoint_failed"] = True' in SSE_SRC
        # 长方案既有降级不得被回退
        assert 'final_status = "degraded"' in SSE_SRC

    def test_switch_to_stepwise_never_resets_last_p(self):
        """FIX-6：切档段内不得把 last_p 退回起点（可见进度回退）。

        connecting 阶段初始化 last_p=0.05 是合法的，故判据只锁切档段。
        """
        switch_block = SSE_SRC.split("if not _one_shot_ok:", 1)[1].split(
            "await wait_resume(task_id)", 1
        )[0]
        assert '_prog["last_p"]' not in switch_block
        assert "max(p, _prog.get(\"last_p\", 0.0))" in SSE_SRC

    def test_review_skip_branches_all_marked(self):
        """FIX-3：全部降级分支均须带 review_skipped / skip_reason。

        ✅ R188 重锚：新增「裸 issues 数组容忍」分支后 3 → 4，
        判据强度不降（仍逐处配对，且新分支的 skip_reason 单独锁）。
        """
        # ✅ R216 重锚：新增「熔断跳过审核」分支（quota_blocked）后 4 → 5；
        #    判据强度不降（仍逐处配对，且新分支的 skip_reason 单独锁）。
        assert REVIEW_SRC.count('"review_skipped": True') == 5
        assert REVIEW_SRC.count('"skip_reason":') == 5
        assert '"skip_reason": "bare_issues_array"' in REVIEW_SRC
        assert '"skip_reason": "quota_blocked"' in REVIEW_SRC
        assert 'if task_id and _is_quota_blocked(task_id):' in REVIEW_SRC
        # 旧日志的两条完整格式串不得回潮（注释里引用旧文案属说明，不算）
        assert "目录审核超时（%ds），跳过审核直接完成（scheme=%s）" not in REVIEW_SRC
        assert "目录审核失败（scheme=%s）: %s，跳过审核直接完成" not in REVIEW_SRC
        assert "本次目录未经 AI 审核，直接交付" in REVIEW_SRC

    def test_ancestor_guard_forces_utf8_subprocess(self):
        """FIX-8：子进程 stdout 编码与断言解码口径必须一致。

        两层都锁：只定义 ``_env`` 却不传给 ``subprocess.run`` 是无效修复
        （A/B M8 首跑 MISSED 正是这个盲区）。
        """
        assert '"PYTHONIOENCODING": "utf-8"' in ANCESTOR_TEST_SRC
        assert "timeout=8, env=_env)" in ANCESTOR_TEST_SRC


# ---------------------------------------------------------------- E FIX-1 行为实证
class TestMoveSubtreeDepth:
    @staticmethod
    async def _make_db():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = sqlite3.Row
        await db.execute(
            # ✅ R190 重锚：update_section 改名残留修复（FIX-9）需读旧标题列
            "CREATE TABLE sections (id TEXT PRIMARY KEY, scheme_id TEXT, parent_id TEXT, level INTEGER, title TEXT DEFAULT '')"
        )
        await db.executemany(
            "INSERT INTO sections VALUES (?,?,?,?,?)",
            [
                ("r1", "S", "", 1, "第一章"),
                ("c1", "S", "r1", 2, "1.1 带子树的章"),
                ("l1", "S", "c1", 3, "1.1.1 三级叶"),
                ("r2", "S", "", 1, "第二章"),
                ("c2", "S", "r2", 2, "2.1 目标父章"),
            ],
        )
        await db.commit()
        return db

    @pytest.mark.asyncio
    async def test_moving_chapter_with_subtree_is_rejected(self, monkeypatch):
        """旧逻辑 2+1=3 放行 → renumber 写 level=4 → 子树从三级界面消失。"""
        monkeypatch.setattr(sec, "outline_generation_in_progress", lambda _sid: False)
        db = await self._make_db()
        try:
            with pytest.raises(HTTPException) as ei:
                await sec.update_section("S", "c1", SectionUpdate(parent_id="c2"), db=db)
            assert ei.value.status_code == 400
            assert "层子目录" in str(ei.value.detail)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_legal_move_of_leaf_is_not_blocked_by_new_guard(self, monkeypatch):
        """无子树的叶子移动 subtree_height=1，判据等同旧行为，不得误伤。"""
        monkeypatch.setattr(sec, "outline_generation_in_progress", lambda _sid: False)
        db = await self._make_db()
        try:
            try:
                await sec.update_section("S", "l1", SectionUpdate(parent_id="c2"), db=db)
            except HTTPException as e:
                assert e.status_code != 400, f"合法移动被新深度护栏误拦: {e.detail}"
            except Exception as e:  # 成功路径缺列等与深度校验无关
                assert "目录最多支持" not in str(e)
        finally:
            await db.close()


# ---------------------------------------------------------------- F 自动确认
class TestAutoConfirm:
    """FIX-3 / FIX-4：硬前置不再恒 0，未审目录不得自动落库。"""

    def test_review_skipped_blocks_auto_confirm(self):
        result = oac.evaluate(_legal_tree(), {"passed": True, "review_skipped": True})
        assert result["ready"] is False
        assert any(b["code"] == "review_skipped" for b in result["blockers"])

    def test_legacy_event_without_skip_marker_keeps_contract(self):
        base = oac.evaluate(_legal_tree(), {"passed": True})
        assert base["ready"] is True
        assert not any(b["code"] == "review_skipped" for b in base["blockers"])

    def test_nested_issue_counts_drive_numbering_invalid(self):
        """核心定罪：_attach_quality_report 把 issue_counts 放在 quality.continuity 下。"""
        review = {
            "passed": True,
            "quality": {"continuity": {
                "issue_counts": {"numbering_mismatch": 2, "level_gaps": 1},
            }},
        }
        result = oac.evaluate(_legal_tree(), review)
        blockers = [b for b in result["blockers"] if b["code"] == "numbering_invalid"]
        assert len(blockers) == 1
        assert blockers[0]["count"] == 3

    def test_top_level_issue_counts_take_priority(self):
        """向后兼容：顶层 issue_counts 优先于嵌套回退。"""
        review = {
            "issue_counts": {"numbering_mismatch": 1},
            "quality": {"continuity": {"issue_counts": {"numbering_mismatch": 9}}},
        }
        result = oac.evaluate(_legal_tree(), review)
        blockers = [b for b in result["blockers"] if b["code"] == "numbering_invalid"]
        assert blockers and blockers[0]["count"] == 1

    def test_review_skipped_source_ratchet(self):
        src = (BACKEND / "app" / "services" / "outline_auto_confirm.py").read_text(encoding="utf-8")
        assert 'review.get("review_skipped")' in src
        assert 'continuity.get("issue_counts")' in src


# ---------------------------------------------------------------- G 提示词
class TestPromptNumberingNamespace:
    """OPT-1：L0/L1 与 L2/L3 同一编号命名空间口径。"""

    def test_l0_and_l1_declare_program_renumbering(self):
        assert PROMPT_SRC.count("【编号命名空间】") >= 2
        l0 = PROMPT_SRC.split('"outline_short_system"', 1)[1].split("_reg(", 1)[0]
        l1 = PROMPT_SRC.split('"outline_level1_system"', 1)[1].split("_reg(", 1)[0]
        assert "严禁自行添加" in l0 and "绝不可写入 title" in l0
        assert "严禁添加" in l1 and "无需输出任何编号" in l1

    def test_no_double_brace_escape_leftover(self):
        """R169 FIX-1 同族：本轮改动的两段提示词不得残留 .format 时代的转义。"""
        l0 = PROMPT_SRC.split('"outline_short_system"', 1)[1].split("_reg(", 1)[0]
        l1 = PROMPT_SRC.split('"outline_level1_system"', 1)[1].split("_reg(", 1)[0]
        assert "{{" not in l0 and "}}" not in l0
        assert "{{" not in l1 and "}}" not in l1
