"""目录生成「按类别自动匹配目录库」参考注入回归测试（2026-09-25 BUG 修复）

背景：generate_outline 路由体内 _assemble_context 曾把 scheme_auto_match_outline
命中的类别参考文本先追加进 ctx["reference_outline"]，随后又被末行
`ctx["reference_outline"] = reference_outline`（用户选库结果）无条件覆盖 ——
且追加时 ctx 尚未赋值（ctx.get 恒为空串）。净效果：开关开启后类别参考
从未进入提示词，而 ref_count 却照常累加（统计虚增），此前无任何测试覆盖该链路。

锁定不变量：
1. 开关开启 + 方案有 hazard_category：draft 提示词收到的 reference_outline
   同时包含「用户选库文本」与「类别匹配文本」（增量追加、互不覆盖）；
2. 类别命中的库 id 被 bump_ref_count 如实计数（与参考文本真正注入配套）；
3. 开关关闭（默认 False）：类别匹配函数完全不参与，reference_outline
   与用户选库文本逐字一致（旧行为不变，向后兼容）。
"""
import json
import uuid

import app.routers.sse_handlers as sh
import app.services.outline_reference as oref


async def _seed_scheme(db_conn, hazard_category: str) -> str:
    """落一个短方案（带 hazard_category），返回 scheme_id。"""
    sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
        (pid, "测试项目"))
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name, config_json,"
        " hazard_category) VALUES (?,?,?,?,?)",
        (sid, pid, "深基坑支护方案", json.dumps({}), hazard_category))
    await db_conn.commit()
    return sid


def _patch_reference_sources(monkeypatch, cat_ref: str, cat_hits: list[str]):
    """替换 outline_reference 三个入口：用户选库恒回 USER_REF_TEXT，
    类别匹配回指定文本/命中列表，bump_ref_count 记录每次计数的库 id。"""
    bumps: list[list[str]] = []
    cat_calls: list[list[str]] = []

    async def fake_build_ref(db, library_ids, only_approved=True):
        return "USER_REF_TEXT", []

    async def fake_cat_ref(db, cat_ids):
        cat_calls.append(list(cat_ids))
        return cat_ref, list(cat_hits)

    async def fake_bump(db, ids):
        bumps.append(list(ids))

    monkeypatch.setattr(oref, "build_reference_outline", fake_build_ref)
    monkeypatch.setattr(oref, "build_category_reference_outline", fake_cat_ref)
    monkeypatch.setattr(oref, "bump_ref_count", fake_bump)
    return bumps, cat_calls


def _patch_generation(monkeypatch):
    """AI 调用短路为直出成功，并捕获全部提示词消息。

    注意短方案链路 reference_outline 走 user_prompt 拼接
    （【目录库参考】：…），长方案走 render kwargs —— 统一从
    collect_json_response 收到的 messages 里断言，两条链路都覆盖。
    """
    seen_prompts: list[list[dict]] = []

    monkeypatch.setattr(sh, "render", lambda *a, **k: "PROMPT")

    async def fake_collect(messages, validate_fn=None, **kwargs):
        seen_prompts.append(messages)
        outline = [{"title": "工程概况", "description": "d", "children": []}]
        if kwargs.get("scene") == "outline_draft":
            return ({"outline": outline},
                    json.dumps({"outline": outline}, ensure_ascii=False))
        return ({"passed": True, "suggestions": []},
                json.dumps({"passed": True}, ensure_ascii=False))

    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    return seen_prompts


async def _run_stream(scheme_id, db_conn) -> list[str]:
    resp = await sh.generate_outline(scheme_id, None, db_conn)
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    events = [json.loads(line[len("data: "):])
              for line in "".join(chunks).splitlines()
              if line.startswith("data: ")]
    return [e.get("event") for e in events]


class TestAutoMatchReferenceInjection:
    async def test_enabled_appends_category_ref_to_prompt(
            self, db_conn, monkeypatch):
        """开关开启：类别参考必须与用户选库参考一同进入 draft 提示词，
        不允许被用户选库结果覆盖（旧 BUG 的回归锁）。"""
        monkeypatch.setattr(sh.settings, "scheme_auto_match_outline", True)
        sid = await _seed_scheme(db_conn, "基坑工程")
        bumps, cat_calls = _patch_reference_sources(
            monkeypatch, "CATEGORY_REF_TEXT", ["lib-cat-1"])
        seen_prompts = _patch_generation(monkeypatch)

        names = await _run_stream(sid, db_conn)
        assert "completed" in names, f"短方案必须以 completed 收尾: {names}"

        # 类别匹配确实按 hazard_category 拆分调用
        assert cat_calls == [["基坑工程"]]
        # draft 提示词（messages）同时含两段参考
        prompts = json.dumps(seen_prompts, ensure_ascii=False)
        assert "USER_REF_TEXT" in prompts and "CATEGORY_REF_TEXT" in prompts, \
            f"类别参考必须增量注入 draft 提示词，实际: {prompts[:400]}"
        # 命中的类别库如实计数
        assert ["lib-cat-1"] in bumps

    async def test_category_ref_missing_keeps_user_ref_only(
            self, db_conn, monkeypatch):
        """开关开启但类别库未命中（空文本）：不追加、不计数，
        reference_outline 保持用户选库文本原样。"""
        monkeypatch.setattr(sh.settings, "scheme_auto_match_outline", True)
        sid = await _seed_scheme(db_conn, "模板支架工程")
        bumps, _ = _patch_reference_sources(monkeypatch, "", [])
        seen_prompts = _patch_generation(monkeypatch)

        names = await _run_stream(sid, db_conn)
        assert "completed" in names
        prompts = json.dumps(seen_prompts, ensure_ascii=False)
        assert "USER_REF_TEXT" in prompts, "用户选库参考必须照常注入"
        assert "CATEGORY_REF_TEXT" not in prompts
        assert not bumps, "类别库未命中（空文本）时不得计数"

    async def test_disabled_skips_category_matching(
            self, db_conn, monkeypatch):
        """开关关闭（默认）：类别匹配完全不参与，行为与旧实现逐字一致。"""
        monkeypatch.setattr(sh.settings, "scheme_auto_match_outline", False)
        sid = await _seed_scheme(db_conn, "基坑工程")
        _patch_reference_sources(monkeypatch, "CATEGORY_REF_TEXT", ["lib-x"])
        seen_prompts = _patch_generation(monkeypatch)

        names = await _run_stream(sid, db_conn)
        assert "completed" in names
        prompts = json.dumps(seen_prompts, ensure_ascii=False)
        assert "USER_REF_TEXT" in prompts, "用户选库参考必须注入（旧行为）"
        assert "CATEGORY_REF_TEXT" not in prompts, \
            "开关关闭时类别参考绝不得进入提示词（向后兼容锁）"
