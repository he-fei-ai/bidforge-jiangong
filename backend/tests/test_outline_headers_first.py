"""目录生成「响应头先行」重构回归测试（2026-09-23 · 30s 连接超时根因修复）

背景：旧实现在返回 StreamingResponse 之前串行 await 约 8 次装配 DB 查询/写
（结构化摘要、目录库参考 + ref_count 写库、事实/知识库构建），DB 锁竞争下
（busy_timeout=15s × 最多 4 次重试）最坏远超前端 sseFetch 的 30s 建连超时 →
前端报「后端连接超时」，而任务注册/AI 调用尚未开始（症状：左侧显示后台
未运行、无 AI 调用）。

锁定的不变量：
1. 方案 404 校验保留在路由体（HTTP 状态语义，供既有前端/测试消费）；
2. 全部装配逻辑（结构化摘要/目录库参考+ref_count/事实/知识库/施工内容/
   差集审计）移入 _assemble_context，且只在 event_stream 内部调用；
3. event_stream 解包还原同名局部变量，下游 50+ 处引用零改动；
4. 功能级：装配在 SSE 流内真实执行 —— 短方案全链路产出 connecting +
   completed 事件，装配产物（编制要求）确实注入提示词。
"""
import inspect
import json
import uuid

import pytest
from fastapi import HTTPException

import app.routers.sse_handlers as sh


def _src_before_assemble() -> str:
    """generate_outline 源码中 _assemble_context 定义之前的部分（路由体前段）。"""
    return inspect.getsource(sh.generate_outline).split(
        "async def _assemble_context", 1)[0]


def _assemble_ctx_src() -> str:
    """generate_outline 源码中 _assemble_context 函数体部分。

    注意：_assemble_context 是路由函数内的嵌套函数，无法用
    inspect.getsource 直接获取，改用源码切片（其定义位于路由体与
    event_stream 之间）。
    """
    src = inspect.getsource(sh.generate_outline)
    return src.split("async def _assemble_context", 1)[1].split(
        "async def event_stream", 1)[0]


def _event_stream_src() -> str:
    return inspect.getsource(sh.generate_outline).split(
        "async def event_stream", 1)[1]


# ============================================================
# 1. 源码级回归锁：响应头先行，装配不得阻塞路由体
# ============================================================
class TestHeadersFirstSourceLock:
    def test_404_check_stays_in_route_body(self):
        body = _src_before_assemble()
        assert "HTTPException(404" in body, \
            "方案 404 校验必须保留在路由体（HTTP 状态语义，前端依赖）"

    def test_assembly_awaits_not_before_stream(self):
        """装配类 await 不得出现在 event_stream 之前（旧实现响应头被
        串行 DB 装配阻塞，DB 锁竞争下最坏远超前端 30s 建连超时）。"""
        body = _src_before_assemble()
        for fragment in ("_build_structured_brief(", "build_reference_outline(",
                         "bump_ref_count(", "load_parsed_texts(",
                         "_build_facts_text(", "_build_knowledge_text("):
            assert fragment not in body, \
                f"装配调用 {fragment} 不得留在 event_stream 之前的路由体"

    def test_assembly_in_assemble_context_and_called_in_stream(self):
        ctx = _assemble_ctx_src()
        for fragment in ("_build_structured_brief(", "build_reference_outline(",
                         "bump_ref_count(", "load_parsed_texts(",
                         "_build_facts_text(", "_build_knowledge_text("):
            assert fragment in ctx, f"装配调用 {fragment} 必须移入 _assemble_context"
        stream = _event_stream_src()
        assert "await _assemble_context()" in stream, \
            "event_stream 必须在流内调用 _assemble_context（响应头先行）"
        # 解包还原同名局部变量（下游引用零改动的契约）
        for name in ("project_brief", "reference_outline", "requirements_text",
                     "project_facts", "construction_scope", "_audit_outline_inputs"):
            assert name in stream.split("_set_phase", 1)[0], \
                f"event_stream 必须解包还原局部变量 {name}"


# ============================================================
# 2. 功能级：装配在流内真实执行，全链路产出完整事件
# ============================================================
class TestAssemblyRunsInsideStream:
    async def _seed(self, db_conn):
        sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
            (pid, "测试项目"))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name, config_json)"
            " VALUES (?,?,?,?)",
            (sid, pid, "深基坑支护方案",
             json.dumps({"requirements": "必须包含监测章节"})))
        await db_conn.commit()
        return sid

    async def test_short_scheme_full_path_emits_completed(
            self, db_conn, monkeypatch):
        """短方案全链路：connecting → draft progress → completed，
        装配产物（编制要求）真实注入提示词 —— 证明装配移入流内后
        行为与旧实现等价。"""
        sid = await self._seed(db_conn)
        seen_prompts = []

        async def fake_collect(messages, validate_fn=None, **kwargs):
            seen_prompts.append((messages, kwargs))
            outline = [{"title": "第一章 工程概况", "description": "d",
                        "children": []}]
            if kwargs.get("scene") == "outline_draft":
                return ({"outline": outline},
                        json.dumps({"outline": outline}, ensure_ascii=False))
            # 审核/修复轮：直接通过，不再追加调用
            return ({"passed": True, "suggestions": []},
                    json.dumps({"passed": True}, ensure_ascii=False))

        monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)

        resp = await sh.generate_outline(sid, None, db_conn)
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
        events = [json.loads(line[len("data: "):])
                  for line in "".join(chunks).splitlines()
                  if line.startswith("data: ")]
        names = [e.get("event") for e in events]
        assert "connecting" in names
        assert "completed" in names, f"短方案必须以 completed 收尾，实际事件序列: {names}"
        completed = next(e for e in events if e.get("event") == "completed")
        # 审核/修复链路会规范化章节标题（剥离「第X章」编号），故用包含断言
        assert any("工程概况" in n.get("title", "") for n in completed["outline"]), \
            f"completed 必须携带生成的目录，实际: {completed['outline']}"
        # 装配产物注入提示词（config.requirements 在流内装配进 user_prompt）
        prompts = json.dumps(seen_prompts, ensure_ascii=False)
        assert "编制要求" in prompts
        assert "必须包含监测章节" in prompts

    async def test_missing_scheme_raises_404(self, db_conn):
        """404 语义保留在路由体：不存在的方案直接抛 HTTP 404（非流式）。"""
        with pytest.raises(HTTPException) as ei:
            await sh.generate_outline("no-such-scheme", None, db_conn)
        assert ei.value.status_code == 404
