"""目录生成 · 分步「并发窗口 × 单元合并」步进错误回归测试（2026-10-06）

背景：分步生成编排（sse_handlers.generate_outline 的 event_stream）用
「并发窗口」提升长方案生成速度：

    _window  = OUTLINE_CHAPTER_CONCURRENCY   （本轮并行发起的单元调用数）
    _merge_k = OUTLINE_CHAPTER_BATCH_SIZE    （每个单元合并的章数）

每轮实际覆盖的章数 = _window × _merge_k，但窗口外层循环的步进写成
``range(0, _total, _window)`` —— 步进只用 _window。当 ``_merge_k == 1``（出厂
默认）时两者恰好相等，问题不可见（既有 test_outline_stepwise_window.py 只覆盖
_merge_k=1 这一档）；一旦用户把 ``outline_chapter_batch_size`` 调大（配置注释
明确要求「升批」由用户自行设置），**相邻两轮窗口就会重叠 _merge_k - 1 章**：

    _window=2, _merge_k=2, _total=6
      轮1 batch_start=0 → 单元 [0,1]、[2,3]  → 章 1,2,3,4
      轮2 batch_start=2 → 单元 [2,3]、[4,5]  → 章 3,4,5,6   ← 3、4 重复

后果（均为用户可见）：
  1. 重复章被 ``_merge_unit_results`` 二次 append 到 ``full_outline``，
     目录里出现同名重复章节，编号 1..8 而非 1..6；
  2. 重复章的 AI 子目录调用被重复烧掉（成本翻倍）；
  3. 若第二次调用返回不同的 children，第一次的成果被静默覆盖。

本文件锁定的不变量：**无论 _window 与 _merge_k 取何值，每章恰好被处理一次**，
且 completed 事件的章序严格递增并覆盖全部章节。

护栏形态（2026-10-06 加固）：
  1. **参数网格**：8 组 (window, merge_k, total) 端到端跑真实 SSE 流，覆盖
     ① 出厂默认档 ② merge_k > window ③ window > merge_k ④ 章数非整轮倍数
     四类风险形态。A/B 反向验证：把步进还原成旧形态后 **8 例中 7 例网格 + 1 例
     静态锁定向失败**，唯一通过的是 (1,1,5) 出厂默认档 —— 正是「旧步进恰好
     正确」的那一档，证明它无法靠默认档发现该缺陷。
  2. **静态锁**：直接断言步进由 _window 与 _merge_k 共同导出。功能断言的失败
     信息是「章被处理两次」，静态锚点能直接指到根因。

⚠️ 本轮踩坑（护栏自身，记以免重复排查）：首版 AI 桩对 `collect_json_response`
的 `messages` 一律取 `messages[0]`。但**单章单元的 messages 是裸字符串**
（`_build_unit_prompts` 的 single_prompts 存的是 `render(...)` 的返回 str，
`_fetch_chapter_children` 的 sub_prompt 原样透传，而
`json_response._ensure_user_message` 对非 list 直接原样返回），于是 `"11"`
被截成首字符 `"1"` —— 表面上看是「第 11 章没被处理、第 1 章被重复处理」，
像一条真实的生产缺陷。定位手法：逐层 monkeypatch 打点
（render → _fetch_unit_children → _fetch_chapter_children），在
`_fetch_chapter_children` 入口看到的是 `'11'`、到 `collect_json_response`
就成了 `'1'`，才确认是桩而非源码。教训：**桩必须锚定真实契约（两种形态都要
覆盖），而不是只覆盖自己预期的那一种**；一旦断言失败，先证伪桩再指控源码。
"""
import asyncio
import json
import re
import uuid

import pytest

import app.routers.sse_handlers as sh


async def _seed_long_scheme(db_conn, name="长方案-桩", word_budget=10000) -> str:
    """落一个长方案（word_budget > outline_stepwise_min_words → 走分步链路）。"""
    sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
    await db_conn.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "测试项目"))
    await db_conn.execute(
        "INSERT OR IGNORE INTO schemes (id, project_id, name, word_budget,"
        " config_json) VALUES (?,?,?,?,?)",
        (sid, pid, name, word_budget, json.dumps({}, ensure_ascii=False)))
    await db_conn.commit()
    return sid


def _patch_ai(monkeypatch, unit_delay: float = 0.01, total_chapters: int = 6):
    """AI 调用短路为直出成功；提示词标记化以便还原「哪些章被处理」。

    批量单元（messages 为 list）返回 ``{"chapters": [...]}``（按 chapter_id
    归位），单章单元返回 ``{"outline": [...]}`` —— 与两条消费路径的真实契约一致，
    否则批量响应会被判为「缺章」而整章回退单章，掩盖被锁定的步进问题。
    """
    stats = {"unit_calls": []}

    def fake_render(name: str, **kwargs) -> str:
        if name == "outline_sublevel_system":
            return str(kwargs.get("chapter_id", "?"))
        if name == "outline_sublevel_batch_system":
            # 生产代码把各章信息拼进 chapters_text（形如
            # "- chapter_id=3｜标题：…｜说明：…"），按它还原章号
            ids = re.findall(r"chapter_id=(\d+)", str(kwargs.get("chapters_text", "")))
            return "CHAPTERS:" + ",".join(ids)
        return "PROMPT"

    async def fake_collect(messages, validate_fn=None, **kwargs):
        scene = kwargs.get("scene")
        if scene == "outline_level1":
            outline = [{"title": f"第{i}章", "description": f"desc{i}",
                        "children": []} for i in range(1, total_chapters + 1)]
            return ({"outline": outline},
                    json.dumps({"outline": outline}, ensure_ascii=False))
        if scene != "outline_sublevel":
            return ({"passed": True, "suggestions": []},
                    json.dumps({"passed": True}, ensure_ascii=False))

        ids: list[int] = []
        # ⚠️ messages 有两种真实形态（与生产契约一致，二者都必须正确识别，
        #    否则会把「章号被截成首字符」当成生产缺陷 —— 本次首版桩即踩此坑）：
        #      - 批量单元：[{"role": "system", "content": "CHAPTERS:9,10"}]（list）
        #      - 单章单元：render(...) 的直接返回值「11」（**裸字符串**，见
        #        _build_unit_prompts 的 single_prompts 与 _fetch_chapter_children
        #        的 sub_prompt 传参；collect_json_response._ensure_user_message
        #        对非 list 原样透传）
        #    裸字符串必须整串使用，绝不能取 messages[0]（那会拿到首字符 "1"）。
        if isinstance(messages, str):
            content = messages
        elif isinstance(messages, list) and messages:
            head = messages[0]
            if isinstance(head, dict):
                content = str(head.get("content", ""))
            elif isinstance(head, list) and head:
                content = str(head[0].get("content", ""))
            else:
                content = str(head)
        else:
            content = ""
        if content.startswith("CHAPTERS:"):
            ids = [int(x) for x in content.removeprefix("CHAPTERS:").split(",")
                   if x.strip()]
        elif content.strip().isdigit():
            ids = [int(content)]
        assert ids, f"无法从单元提示词还原章号: {messages!r:.200}"
        stats["unit_calls"].extend(ids)
        await asyncio.sleep(unit_delay)

        return_obj = None
        if content.startswith("CHAPTERS:"):
            # 批量单元 → 按 chapter_id 归位的 {"chapters": [...]} 契约
            return_obj = {"chapters": [
                {"chapter_id": str(cid),
                 "outline": [{"title": f"第{cid}章-小节{cid}.1", "children": []}]}
                for cid in ids]}
        else:
            return_obj = {"outline": [
                {"title": f"第{cid}章-小节{cid}.1", "children": []}
                for cid in ids]}
        return (return_obj, json.dumps(return_obj, ensure_ascii=False))

    monkeypatch.setattr(sh, "render", fake_render)
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    return stats


async def _run_stream(scheme_id, db_conn):
    resp = await sh.generate_outline(scheme_id, None, db_conn)
    out = []
    async for chunk in resp.body_iterator:
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
        for line in text.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


# 参数网格：(窗口宽度 _window, 单元章数 _merge_k, 章数 _total)
#
# 覆盖四类风险形态（旧实现任一回退都会在这里失败）：
#   ① 两者相等 + 整轮倍数      —— 出厂默认档（_merge_k=1）时旧步进恰好正确，
#                                 问题不可见，既有护栏只测这一档；
#   ② _merge_k > _window       —— 相邻窗口重叠最严重（配置注释明确鼓励用户升批）；
#   ③ _window > _merge_k       —— 验证不是「只要 _merge_k <= _window 就安全」；
#   ④ 章数非整轮倍数           —— 末轮残余单元不得被上一轮重复覆盖、也不得漏掉。
GRID = [
    (1, 1, 5),    # 出厂默认档：两者皆 1（回归基线，旧步进恰好正确）
    (2, 2, 6),    # 两者相等 + 整轮倍数
    (2, 3, 7),    # _merge_k > _window + 非整轮倍数
    (3, 2, 9),    # _window > _merge_k + 整轮倍数
    (2, 5, 12),   # _merge_k 远大于 _window + 整轮倍数
    (3, 3, 8),    # 两者相等 + 非整轮倍数
    (1, 4, 10),   # _window=1 串行 + 大 _merge_k
    (4, 2, 11),   # _window > _merge_k + 非整轮倍数
]


class TestWindowStepWithBatchMerge:
    """窗口 × 单元合并下的步进不变量（每章恰好处理一次）。"""

    @pytest.mark.parametrize(
        "_window,_merge_k,total", GRID,
        ids=[f"w{w}xk{k}_t{t}" for w, k, t in GRID])
    async def test_each_chapter_processed_exactly_once(
            self, db_conn, monkeypatch, _window, _merge_k, total):
        """每轮实际覆盖 _window × _merge_k 章，外层步进必须取整轮覆盖量。

        旧实现写成 ``range(0, _total, _window)``：当 ``_merge_k == 1`` 时步进
        恰好等于覆盖量（问题不可见）；一旦升批，相邻两轮窗口重叠 ``_merge_k - 1``
        章 → 重复章被 ``_merge_unit_results`` 二次 append 到目录、子目录 AI 被
        重复调用（成本翻倍）、后一次成果静默覆盖前一次。
        """
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_CONCURRENCY", _window)
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_BATCH_SIZE", _merge_k)

        sid = await _seed_long_scheme(db_conn)
        stats = _patch_ai(monkeypatch, total_chapters=total)

        events = await _run_stream(sid, db_conn)
        names = [e["event"] for e in events]
        assert names[-1] == "completed", (
            f"长方案应以 completed 收尾: {names} / "
            f"{[e.get('message') for e in events if e['event'] == 'error']}")

        # 核心断言：每章恰好一次子目录调用（无重复、无漏章）
        assert sorted(stats["unit_calls"]) == list(range(1, total + 1)), (
            f"每章恰好一次子目录调用（无重复、无漏章），实际 {stats['unit_calls']}")

        # 交付态：completed 事件的章序必须严格递增且无重复
        done = [e for e in events if e["event"] == "completed"][0]
        titles = [c["title"] for c in done["outline"]]
        assert titles == [f"第{i}章" for i in range(1, total + 1)], (
            f"章序必须严格递增且无重复：{titles}")
        assert len(titles) == len(set(titles)), f"目录出现重复章节：{titles}"
        assert done.get("failed_chapters", []) == []

    def test_round_step_is_window_times_merge_k(self):
        """静态锁：外层步进必须由 _window 与 _merge_k 共同导出。

        集成断言（每章恰好一次）已在功能层锁定行为；此断言防止有人把
        ``_round_step = max(1, _window) * _merge_k`` 「简化」回 ``_window`` ——
        那会让 ②③④ 三类网格用例全部变红，但失败信息是「章被处理两次」而非
        「步进错了」，静态锚点能直接指到根因。
        """
        import inspect

        src = inspect.getsource(sh.generate_outline)
        assert "_round_step = max(1, _window) * _merge_k" in src, (
            "窗口外层步进必须取整轮覆盖量 _window × _merge_k")
        assert "range(0, _total, _round_step)" in src, (
            "窗口外层循环必须按 _round_step 步进")
