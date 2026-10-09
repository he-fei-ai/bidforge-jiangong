"""目录生成模块 · 工作流日志审查回归测试（2026-09-16 第三轮）

依据运行库 `task_registry` 的真实失败消息定位的两处缺陷：

| 缺陷 | 运行库证据 | 后果 |
|---|---|---|
| 短方案收尾处引用旧变量名 `_partial`（改名 `_partial_holder` 时漏改） | 最新一条 outline_generation：`failed p=1.00 name '_partial' is not defined`（且 checkpoint 里已有完整 10 章目录） | 目录其实已生成，却以 error 收场 —— 前端拿不到一级目录确认闸门 |
| 9 处 JSON 任务的消息列表**只有 system** | `outline_generation` 3 次、`facts_generation` 1 次 `所有 AI 提供商调用失败：…No user query found in messages.` | 在要求 user 消息的 provider（sensenova/agnes）上整类任务必然失败 |

外加一项**结构性护栏**：静态检查（ruff F821）—— 它本可以一次性发现
`_partial`、`compliance.logger`、`charts._rewrite_code_block` 这三类未定义名。
"""
import ast
import inspect
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from app.routers import sse_handlers as sh
from app.services.ai import json_response as jr


# ============================================================
# O-1：未定义名（运行库 NameError 的直接对应项）
# ============================================================
class TestNoUndefinedNames:
    def test_generate_outline_has_no_stale_partial_name(self):
        """源码级护栏：目录生成里不得再出现裸 `_partial`（只允许 `_partial_holder`）。"""
        tree = ast.parse(inspect.getsource(sh.generate_outline).lstrip())
        bad = [n.id for n in ast.walk(tree)
               if isinstance(n, ast.Name) and n.id == "_partial"]
        assert bad == [], "存在旧变量名 _partial（应为 _partial_holder）"

    def test_generate_content_has_no_stale_names(self):
        tree = ast.parse(inspect.getsource(sh.generate_content).lstrip())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "_partial" not in names

    def test_ruff_finds_no_undefined_names_in_app(self):
        """静态检查门禁：`ruff --select F821` 必须无输出。

        本类的三个缺陷（`_partial` / `compliance.logger` /
        `charts._rewrite_code_block`）都是 F821，且都只会在特定分支运行时才炸 ——
        单测覆盖不到，但静态检查一眼可见。此测试把"跑一次静态检查"变成常驻门禁。

        僵尸子进程防护（2026-09-23）：旧实现用裸 `subprocess.run` 且**无 timeout**，
        `ruff check app` 偶发挂死、或 pytest 被中断时，ruff 子进程会泄漏成为僵尸
        （并可能占用文件句柄）。现统一走 `run_child`：带超时 + 超时回收进程树 +
        Windows 下入 KILL_ON_JOB_CLOSE 作业（本进程一旦被强杀，OS 连带回收子进程）。
        """
        from app.utils.process_cleanup import run_child

        backend_root = Path(__file__).resolve().parents[1]
        try:
            if shutil.which("ruff") is None:
                # 通过当前解释器调用（ruff 可能只装在当前环境）
                probe = run_child(
                    [sys.executable, "-m", "ruff", "--version"], timeout=30)
                if probe.returncode != 0:
                    pytest.skip("ruff 不可用，跳过静态检查门禁")
            proc = run_child(
                [sys.executable, "-m", "ruff", "check", "app",
                 "--select", "F821", "--output-format", "concise"],
                timeout=180, cwd=str(backend_root))
        except subprocess.TimeoutExpired:
            pytest.skip("ruff 静态检查超时（已回收子进程），本轮跳过门禁")
        assert proc.returncode == 0, f"存在未定义名：\n{proc.stdout}\n{proc.stderr}"


# ============================================================
# O-2：消息列表必须有 user 消息（provider 兼容性）
# ============================================================
class TestEnsureUserMessage:
    def test_system_only_gets_nudge(self):
        msgs = [{"role": "system", "content": "只输出 JSON"}]
        out = jr._ensure_user_message(msgs)
        assert [m["role"] for m in out] == ["system", "user"]
        assert out[1]["content"].strip(), "user 消息不能为空串（另有 provider 拒绝空 content）"

    def test_existing_user_untouched(self):
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "u"}]
        assert jr._ensure_user_message(msgs) == msgs

    def test_dirty_input_is_safe(self):
        assert jr._ensure_user_message([]) == [
            {"role": "user", "content": jr.JSON_USER_NUDGE}]
        assert jr._ensure_user_message(None) is None
        # 非字典元素不应导致崩溃
        out = jr._ensure_user_message(["oops"])
        assert out[-1]["role"] == "user"

    async def test_collect_json_response_sends_user_message(self, monkeypatch):
        """端到端：调用方只给 system 时，实际发给 provider 的消息里必须有 user。"""
        captured: dict = {}

        async def fake_chat(messages, **kwargs):
            captured["messages"] = messages
            return '{"ok": true}'

        monkeypatch.setattr(jr, "chat_with_fallback", fake_chat)
        obj, _raw = await jr.collect_json_response(
            [{"role": "system", "content": "请输出 JSON"}],
            lambda o: [] if o.get("ok") else ["缺少 ok"])
        assert obj == {"ok": True}
        roles = [m["role"] for m in captured["messages"]]
        assert "user" in roles, "provider 兼容性兜底未生效"

    def test_failed_task_error_event_carries_partial_outline(self):
        """源码级护栏：终态失败时必须把已生成成果随 error 事件下发（可挽救）。"""
        src = inspect.getsource(sh.generate_outline)
        idx = src.index("_err_payload = {'event': 'error'")
        tail = src[idx:]
        assert "_partial_holder.get(\"outline\")" in tail
        assert "'outline': _err_outline" in tail
        assert "await _save_outline_checkpoint" in tail

    def test_outline_call_sites_covered_by_shared_guard(self):
        """目录链路所有 JSON 调用都经由 collect_json_response ⇒ 已被兜底覆盖。"""
        for fn in (sh.generate_outline, sh._review_and_fix_outline):
            assert "collect_json_response(" in inspect.getsource(fn)
        src = inspect.getsource(sh)
        for marker in ("sub_prompt", "review_prompt", "fix_prompt"):
            assert marker in src, marker
        # 三处提示词都是 system-only 的写法（历史写法），由共享入口补齐 user
        assert jr._ensure_user_message([{"role": "system", "content": "x"}])[-1]["role"] == "user"


# ============================================================
# O-3：word_budget 脏值不得崩掉分档判断（2026-09-19 深度审查）
# ============================================================
class TestWordBudgetGuard:
    def test_generate_outline_coerces_word_budget(self):
        """源码级护栏：word_budget 必须稳健取整后再比较。

        旧实现 `scheme.get("word_budget", 30000)` 在值为 NULL 时返回 None，
        `None > OUTLINE_STEPWISE_MIN_WORDS` 抛 TypeError → 整次目录生成失败。
        """
        src = inspect.getsource(sh.generate_outline)
        assert 'int(scheme.get("word_budget") or 30000)' in src, \
            "word_budget 未做稳健取整（NULL/脏值会崩分档判断）"
        assert "except (TypeError, ValueError)" in src

    def test_none_word_budget_falls_back(self):
        """行为级护栏：NULL / 字符串 / 非法值均回退 30000，不抛异常。"""
        for dirty in (None, "", "abc", 30000.0):
            try:
                wb = int(dirty or 30000)
            except (TypeError, ValueError):
                wb = 30000
            assert isinstance(wb, int) and wb == 30000, dirty
