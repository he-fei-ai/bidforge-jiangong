"""KNOWN_SCENES 与代码中真实 ``scene="..."`` 字面量的 parity 护栏（R47 债-4）。

背景
----
``app/services/ai/provider_factory.py`` 里的 ``KNOWN_SCENES`` 是「场景模型路由」
配置界面的可选场景白名单，注释自承「必须与代码中实际 ``scene=`` 字面量保持一致」。
此前的漂移护栏 ``tests/test_ai_config_security_routing.py::TestKnownScenesDrift``
用**行级正则 + 手动剥离注释/三引号块**的方式扫描 —— AGENTS.md R46 教训④明确
指出「源码是否包含字符串 X」是假判据，注释/多行字符串里提到的 ``scene="content"``
会被误命中。本文件改用 **AST** 遍历 ``ast.Call`` 的 ``keywords``，只认
``keyword.arg == "scene"`` 且 ``keyword.value`` 是 ``ast.Constant`` 的真实调用点，
注释、docstring、字符串字面量天然不进 AST 调用树，从根上消除误判。

只加测试、不改生产代码。若扫描发现未登记的 scene，测试必须失败 ——
由维护者决定是补登记还是修护栏（**不得在本文件里悄悄补 KNOWN_SCENES**）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.ai import provider_factory as pf


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_APP_DIR = _BACKEND_DIR / "app"


def _collect_used_scenes() -> set[str]:
    """用 AST 扫描 ``backend/app/`` 下所有 .py，收集真实 ``scene="..."`` 关键字实参。

    只认 ``ast.Call`` 的 ``keywords`` 中 ``arg == "scene"`` 且
    ``value`` 是 ``ast.Constant``（字符串）的字面量 —— 这是唯一可能出现在
    可执行调用里的形态；注释 / docstring / 普通字符串变量天然被排除。
    """
    used: set[str] = set()
    for p in _APP_DIR.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            # 生产代码不允许有 SyntaxError；跳过会让坏掉的文件静默脱管，故直接失败。
            raise AssertionError(f"无法解析 {p.relative_to(_BACKEND_DIR)}：语法错误")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "scene" and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    used.add(kw.value.value)
    return used


def test_all_used_scenes_registered_in_known_scenes():
    """代码里出现的每个 ``scene="..."`` 必须登记在 ``KNOWN_SCENES``。

    反向：新调用点用了未登记 scene，配置界面就无法为它单独指定模型
    （配了也不生效，静默失效）—— 这正是 KNOWN_SCENES 注释里警告的漂移。
    """
    used = _collect_used_scenes()
    missing = sorted(used - set(pf.KNOWN_SCENES))
    assert not missing, (
        "以下 scene 未登记到 KNOWN_SCENES（场景路由将无法覆盖）：\n  "
        + "\n  ".join(missing)
        + "\n\n请在 provider_factory.KNOWN_SCENES 补登记，或确认该 scene 是否误加。"
    )


def test_ast_scanner_found_at_least_one_real_scene():
    """护栏自检：AST 扫描必须真的扫到了场景字面量（防止扫描逻辑被改空后假绿）。

    历史上本仓已知有 ~25 个真实 scene 调用点（outline_draft / content_draft /
    facts_extract / consistency_scan ...）。若扫描结果为空，说明 AST 遍历被
    改坏（例如 walks 了错误目录、或 ``kw.arg`` 判据被改），护栏形同虚设。
    """
    used = _collect_used_scenes()
    # 出厂基线至少应覆盖这 8 个核心场景（R46 之前就一直在用）。
    must_have = {
        "outline_draft", "outline_level1", "outline_sublevel",
        "content_draft", "content_continue", "content_shrink",
        "facts_extract", "consistency_scan",
    }
    missing = sorted(must_have - used)
    assert not missing, (
        f"AST 扫描只找到 {len(used)} 个 scene 字面量，缺少核心场景：{missing}。"
        "扫描逻辑可能被改坏。"
    )


def test_known_scenes_non_empty_and_labels_non_empty():
    """KNOWN_SCENES 自身健全性：字典非空、每个 key 的中文 label 非空。"""
    assert pf.KNOWN_SCENES, "KNOWN_SCENES 不应为空"
    for k, v in pf.KNOWN_SCENES.items():
        assert isinstance(k, str) and k, f"空 scene key: {k!r}"
        assert isinstance(v, str) and v.strip(), (
            f"scene {k!r} 的中文 label 为空：界面会显示空白选项"
        )
