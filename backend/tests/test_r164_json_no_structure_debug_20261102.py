# -*- coding: utf-8 -*-
"""R164（2026-11-02）：JSON 结构未检出时的 DEBUG 诊断埋点护栏。

背景（生产日志实证，logs/backend.log）：
  `json_response :: JSON 修复第 1/2 次仍未通过：输出中未找到 JSON 结构` 共 270 次
  （135 + 135）。旧实现在该分支只返回 issues、不落任何日志，排障时无法看到模型
  到底输出了什么 —— 只能靠猜。本轮在 ``parse_and_validate`` 的 ``extract_json``
  返回 None 分支补一条 DEBUG 级日志（raw 前 300 字），供排障时按 trace 定位。

本护栏锁定三件事（纯加法式，零行为变更）：
  A. 模型输出无 JSON 结构时，DEBUG 日志确实触发且含 raw_head 预览；
  B. 合法 JSON 路径不触发该 DEBUG 日志（不制造噪音）；
  C. 源码锚：``parse_and_validate`` 的 ``extract_json`` 返回 None 分支必须调用
     ``logger.debug`` 且格式串含 ``raw_head``（防后人误删诊断埋点）。
"""
import logging

import app.services.ai.json_response as jr


def test_debug_logged_when_no_json_structure(caplog):
    """A: 纯文本输出（无 { [ ）→ 触发 DEBUG + raw_head 预览。"""
    caplog.set_level(logging.DEBUG, logger="json_response")
    obj, issues = jr.parse_and_validate("这是一段纯文本，没有任何 JSON 结构。")
    assert obj is None
    assert issues == ["输出中未找到 JSON 结构"]
    recs = [r for r in caplog.records if r.levelno == logging.DEBUG
            and "JSON 结构未在模型输出中检出" in r.getMessage()]
    assert len(recs) == 1, "应恰好一条 DEBUG 诊断日志"
    assert "raw_head=" in recs[0].getMessage()
    assert "这是一段纯文本" in recs[0].getMessage()


def test_debug_not_logged_for_valid_json(caplog):
    """B: 合法 JSON → 不触发该 DEBUG 日志（不制造噪音）。"""
    caplog.set_level(logging.DEBUG, logger="json_response")
    obj, issues = jr.parse_and_validate('{"ok": true}')
    assert obj == {"ok": True}
    assert issues == []
    recs = [r for r in caplog.records if "JSON 结构未在模型输出中检出" in r.getMessage()]
    assert recs == [], "合法 JSON 路径不应触发未检出诊断"


def test_debug_preview_capped_at_300(caplog):
    """A 续：raw 超长时预览截断到 300 字（防日志膨胀）。"""
    caplog.set_level(logging.DEBUG, logger="json_response")
    long_text = "x" * 1000  # 纯文本，无 JSON 结构
    jr.parse_and_validate(long_text)
    recs = [r for r in caplog.records if "JSON 结构未在模型输出中检出" in r.getMessage()]
    assert len(recs) == 1
    msg = recs[0].getMessage()
    # repr() 会带引号；截断后 raw_head 内容长度不超过 300 + 引号包裹开销
    assert msg.count("x") <= 302


def test_source_anchor_debug_call_in_none_branch():
    """C: 源码锚 —— extract_json 返回 None 分支必须调 logger.debug 且含 raw_head。"""
    import inspect
    src = inspect.getsource(jr.parse_and_validate)
    # 锚定 None 分支
    assert 'if not js:' in src
    seg = src[src.index('if not js:'):]
    seg = seg[:seg.index('return None, [')]
    assert 'logger.debug(' in seg, "None 分支未落 DEBUG 诊断日志"
    assert 'raw_head' in seg, "诊断日志缺 raw_head 预览键"
