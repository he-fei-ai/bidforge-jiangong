"""T6 · numbering_consistency_strict 默认值 & 兼容性回归护栏（2026-10-04）。

守护口径：
    1. **默认值必须是 False**：导出前的编号一致性守卫走「仅告警、不阻断」
       路径。若默认改为 True，任何历史落库正文与目录编号漂移的方案都将被
       直接 409 阻断导出，等同于线上事故。
    2. **默认值来自配置文件的字段声明**（`Settings.numbering_consistency_strict`），
       而非依赖某个测试的 monkeypatch。
    3. **strict=False 时 DOCX/PDF 两条导出链路必须走通**（不因守卫抛 409）；
       strict=True 时两条链路都必须 409 阻断（已有 test_numbering_consistency_validator.py
       覆盖，本文件只守护「默认 False + 兼容性不变」这一最小闭环）。

背景：
    用户明确要求「保持默认 False 只补测试」——生产环境不希望一刀切换成
    strict=True 阻断交付。此测试是「未来有人误改默认值」时的守护网。
"""
from __future__ import annotations

import json

import pytest
from app.config import Settings, settings
from app.routers.export import _guard_numbering_consistency


# ===========================================================================
# 1. 默认值必须为 False（守护生产向后兼容）
# ===========================================================================
def test_default_is_false_in_settings_class():
    """Settings 类声明的 numbering_consistency_strict 默认值必须为 False。"""
    assert Settings().numbering_consistency_strict is False


def test_default_is_false_in_loaded_settings(monkeypatch):
    """运行时加载的 settings 对象必须也是 False（未被环境变量污染）。

    覆盖 CI/本地可能设置的 NUMBERING_CONSISTENCY_STRICT=True 场景：
    若外部环境把它设了 True，此处断言会立即失败并暴露污染源。
    """
    for env in ("NUMBERING_CONSISTENCY_STRICT",
                "numbering_consistency_strict"):
        monkeypatch.delenv(env, raising=False)
    assert settings.numbering_consistency_strict is False, (
        f"运行环境的 numbering_consistency_strict 应为 False，"
        f"实际 {settings.numbering_consistency_strict!r}（疑似环境变量污染）")


# ===========================================================================
# 2. strict=False（默认）时守卫必须不抛、仅告警
# ===========================================================================
async def test_guard_warns_when_drifted_and_strict_is_false(db_conn, monkeypatch):
    """默认 strict=False 时，即使正文与目录编号漂移，守卫也不抛 409。"""
    monkeypatch.setattr(settings, "numbering_consistency_strict", False)
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s1", "p1", "测试专项方案"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
        "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
        ("c1", "s1", "", 0, 1, "第一章 总体概述", "generated",
         json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
         "## 9.9 总体安排\n漂移正文。\n"))
    await db_conn.commit()

    # 默认路径下守卫必须「静默通过」，不抛任何异常
    await _guard_numbering_consistency(db_conn, "s1")  # 不抛即通过


# ===========================================================================
# 3. strict=True 时守卫必须 409（DOCX/PDF 共用同一守卫）
# ===========================================================================
async def test_guard_raises_409_when_strict_and_drifted(db_conn, monkeypatch):
    """strict=True 时守卫必须 409，避免产出不一致文档。"""
    from fastapi import HTTPException
    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s1", "p1", "测试专项方案"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
        "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
        ("c1", "s1", "", 0, 1, "第一章 总体概述", "generated",
         json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
         "## 9.9 总体安排\n漂移正文。\n"))
    await db_conn.commit()

    with pytest.raises(HTTPException) as exc:
        await _guard_numbering_consistency(db_conn, "s1")
    assert exc.value.status_code == 409
    assert exc.value.detail.get("error") == "numbering_inconsistency"


# ===========================================================================
# 4. 无漂移时严格/非严格均通过（守护严格模式不误伤干净方案）
# ===========================================================================
async def test_guard_passes_clean_scheme_when_strict(db_conn, monkeypatch):
    """strict=True 但方案本身无漂移时，守卫必须放行（不误伤干净方案）。"""
    monkeypatch.setattr(settings, "numbering_consistency_strict", True)
    await db_conn.execute(
        "INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)",
        ("s1", "p1", "测试专项方案"))
    await db_conn.execute(
        "INSERT INTO sections (id, scheme_id, parent_id, sort_order, level, "
        "title, status, outline_json, content) VALUES (?,?,?,?,?,?,?,?,?)",
        ("c1", "s1", "", 0, 1, "第一章 总体概述", "generated",
         json.dumps({"id": "1", "level": 1}, ensure_ascii=False),
         "## 1.1 总体安排\n干净正文。\n"))
    await db_conn.commit()

    # 无漂移 → 无论 strict 值如何都应静默通过
    await _guard_numbering_consistency(db_conn, "s1")
