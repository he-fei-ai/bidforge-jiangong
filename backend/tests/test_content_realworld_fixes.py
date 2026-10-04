"""正文生成模块 · 运行日志驱动的问题修复（2026-09-16 第三轮）

本轮修复依据**运行库真实记录**（`task_registry` / `sections` 视为运行日志）：

| 证据（真实数据） | 结论 | 修复 |
|---|---|---|
| 96 章中 69 章（72%）`word_status='over'`，均值 1.69X、最高 2.82X | 目标字数被模型当作"下限" | 提示词新增《字数控制》硬性章节 + 运行时给出「区间 + 硬上限」+ 续写消息给出总上限；完成消息回传本次超字数章节数 |
| `正文0字完成（55/55 章失败）` 却记为 **completed** | 全章失败仍报"完成" | 新增 all_failed 语义：终态 failed + error 事件（携带失败明细） |
| 完成消息恒为 `正文N字完成`，N = **方案合计**字数 | 重生成单章时字数口径误导 | 部分生成改为「本次生成 X 字（方案合计 Y 字）」 |
| `_diagnostics/*` 在 GBK 控制台 `UnicodeEncodeError` 中止（日志可见） | 检查器自身崩溃 | 补齐 UTF-8 输出引导 |

对照测试见 `tests/test_content_deep_audit.py`（上一轮：终态顺序 / checkpoint / 写锁 / 超时 / 自动压缩）。
"""
import inspect

import pytest
from app.routers import sse_handlers as sh
from app.services import content_runtime as crt
from app.services.ai.prompts._registry import render


# ============================================================
# L-1：目标字数区间与硬上限
# ============================================================
class TestWordBudgetHint:
    def test_hint_contains_range_and_hard_cap(self):
        hint = sh._word_budget_hint(1000)
        assert "1000字" in hint
        assert "900~1100" in hint          # 0.9X ~ 1.1X
        assert "1200" in hint              # 1.2X 硬上限
        assert "不合格" in hint            # 明确后果

    def test_hint_scales_with_budget(self):
        assert "2700~3300" in sh._word_budget_hint(3000)     # 小章节也一样
        assert "3600" in sh._word_budget_hint(3000)

    def test_dirty_input_is_safe(self):
        assert sh._word_budget_hint(0) == ""
        assert sh._word_budget_hint(None) == ""
        assert sh._word_budget_hint("bad") == ""

    def test_runtime_uses_the_hint(self):
        """源码级护栏：正文上下文与续写消息都必须带上区间/上限，不得只写"目标 X 字"。

        ✅ 2026-09-28（T-1 收口）：续写消息构造已下沉 content_runtime，
        故「补充后总字数上限」/「int(word_budget * 1.1)」的断言同时检查
        generate_content 调用点与 content_runtime 实现点两处宿主。
        """
        src = inspect.getsource(sh.generate_content)
        assert '_word_budget_hint(word_budget)' in src
        cr_src = inspect.getsource(crt)
        # 续写：给出「补充后总字数上限」
        assert "补充后总字数上限" in cr_src
        assert "int(word_budget * 1.1)" in cr_src


class TestPromptWordCountControl:
    """提示词默认值的回归护栏（运行库无 DB 覆盖，默认值即生效内容）。"""

    def test_generation_prompt_has_word_count_section(self):
        text = render("content_generation_system", section_number="1", standards_text="",
                      scheme_name="N", scheme_type="T")
        assert "字数控制" in text
        assert "1.2X" in text and "0.9X ~ 1.1X" in text
        # 明确禁止注水（否则模型会用套话把字数顶上去）
        assert "严禁为凑字数" in text

    def test_continue_prompt_has_upper_bound(self):
        text = render("content_continue_system", scheme_name="N", scheme_type="T",
                      standards_text="")
        assert "1.1 倍以内" in text


# ============================================================
# L-2/L-3：全章失败语义与字数口径
# ============================================================
class TestTerminalSemantics:
    def _src(self) -> str:
        return inspect.getsource(sh.generate_content)

    def test_all_failed_marks_task_failed_before_completed_payload(self):
        src = self._src()
        i_all = src.index("if total > 0 and not done_ids:")
        i_payload = src.index("completed_payload = {")
        assert i_all < i_payload, "全章失败分支必须早于 completed 载荷构建"
        tail = src[i_all:i_payload]
        assert 'await finish_task(task_id, "failed"' in tail
        assert "'event': 'error'" in tail
        assert "failed_sections" in tail      # 明细随 error 事件下发

    def test_completed_message_distinguishes_run_and_total(self):
        src = self._src()
        assert "_partial_run = mode in (\"section\", \"continue\") or len(done_ids) < total" in src
        assert "本次生成 {_run_words} 字（方案合计 {total_wc} 字）" in src
        assert "正文{total_wc}字完成" in src      # 全量生成仍保持原口径

    def test_over_count_is_reported(self):
        src = self._src()
        assert '_stat["over"] = _stat.get("over", 0) + 1' in src
        assert "'over_count': _over_n" in src
        assert "章超字数" in src

    def test_over_counter_only_counts_over(self):
        """计数只在 word_status=='over' 时累加（与落库口径同源）。"""
        src = self._src()
        idx = src.index('_stat["over"] = _stat.get("over", 0) + 1')
        assert 'if ws == "over":' in src[idx - 80:idx]
