"""目录生成 · 长方案分步「并发窗口」端到端回归测试（2026-10-04 BUG 修复）

背景：旧编排在 batch 循环内每轮 `asyncio.gather` 恒只含**一个**
`_fetch_unit_children`，并发信号量在 `_fetch_chapter_children` 内部才被消费，
从未跨章竞争 —— `OUTLINE_CHAPTER_CONCURRENCY=2` 实际并发度永远为 1，配置形同虚设
（长方案 30 章仍跑满串行耗时，表现为「看起来卡死」）。

同时暴露两处配套缺陷：
- 窗口步进仍为 `_merge_k`（=1）而窗口补齐按 `_merge_k` 累加 → 相邻窗口重叠
  1 章，同一章被重复处理、末章被漏处理；
- 逐单元归位用赋值而非累加 → 多单元下节点计数与 stopped 信号被后续单元覆盖。

锁定的不变量（端到端：`generate_outline` → `resp.body_iterator`）：
1. 并发峰值 = 窗口宽度：同一时刻在飞的子目录 AI 调用数必须达到
   OUTLINE_CHAPTER_CONCURRENCY（而不是恒为 1）；
2. 每个一级章**恰好被处理一次**：章序严格递增且覆盖全部章节
   （防「步进=_merge_k 导致重复追加 / 漏章」回潮）；
3. 停止语义：停止在**批间闸门**处生效，已完成的前缀章节随 stopped 事件
   完整下发（成果不丢失），闸门之后的窗口不得再发起 AI 调用。
"""
import asyncio
import json
import uuid

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


def _parse_events(chunks: list[str]) -> list[dict]:
    return [json.loads(line[len("data: "):])
            for line in "".join(chunks).splitlines() if line.startswith("data: ")]


async def _nop(task_id=None):
    return None


def _patch_ai(monkeypatch, unit_delay: float, total_chapters: int = 6):
    """AI 调用短路为直出成功；按 scene 分流一级目录 / 逐章子目录。

    提示词标记化：render 对 outline_sublevel_system 返回章号字符串
    （单章单元的 sub_prompt 即可还原章号），批量单元返回 "CHAPTERS:1,2"。
    这样测试可直接断言「哪些章被处理、各处理一次」。

    返回 stats：记录子目录阶段在飞调用数与峰值（并发窗口有效性的唯一观测点），
    以及每个窗口内 AI 调用的先后序（unit_calls）。
    stats["unit_barrier"] 可被外部置为 asyncio.Barrier，用于让某一波的单元
    在 AI 调用发起后同步等待 —— 用来精确证明「同一时刻有 N 个单元在飞」。
    """
    stats = {"unit_active": 0, "unit_peak": 0, "unit_barrier": None,
             "unit_calls": []}

    def fake_render(name: str, **kwargs) -> str:
        if name == "outline_sublevel_system":
            return str(kwargs.get("chapter_id", "?"))
        if name == "outline_sublevel_batch_system":
            return "CHAPTERS:" + ",".join(
                str(c.get("chapter_id", "?")) for c in kwargs.get("chapters") or [])
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
        head = messages[0] if messages else ""
        if isinstance(head, list):
            ids = [int(x) for x in str(head[0].get("content", ""))
                   .removeprefix("CHAPTERS:").split(",") if x.strip()]
        elif isinstance(head, str) and head.strip().isdigit():
            ids = [int(head)]
        assert ids, f"无法从单元提示词还原章号: {messages!r:.200}"
        stats["unit_calls"].extend(ids)
        stats["unit_active"] += 1
        stats["unit_peak"] = max(stats["unit_peak"], stats["unit_active"])
        await asyncio.sleep(unit_delay)
        if stats["unit_barrier"] is not None:
            await stats["unit_barrier"].wait()
        stats["unit_active"] -= 1
        outline = [{"title": f"{cid}.1", "description": "子节", "children": []}
                   for cid in ids]
        return ({"outline": outline},
                json.dumps({"outline": outline}, ensure_ascii=False))

    monkeypatch.setattr(sh, "render", fake_render)
    monkeypatch.setattr(sh, "collect_json_response", fake_collect)
    return stats


async def _run_stream(scheme_id, db_conn):
    resp = await sh.generate_outline(scheme_id, None, db_conn)
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
    return _parse_events(chunks)


class TestStepwiseWindowConcurrency:
    async def test_window_width_is_reached_and_chapters_not_duplicated(
            self, db_conn, monkeypatch):
        """并发峰值必须达到窗口宽度，且每章恰好处理一次（章序严格递增）。"""
        # OUTLINE_STEPWISE_MIN_WORDS 是模块加载时固化的常量（sse_handlers L156），
        # monkeypatch settings 实例不生效；必须直接改模块常量才能走长方案分步链路。
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        monkeypatch.setattr(sh, "wait_resume", _nop)
        monkeypatch.setattr(sh, "is_stopped", lambda task_id: False)
        sid = await _seed_long_scheme(db_conn)
        stats = _patch_ai(monkeypatch, unit_delay=0.02)

        events = await _run_stream(sid, db_conn)
        names = [e["event"] for e in events]
        assert names[-1] == "completed", (
            f"长方案应以 completed 收尾: {names} / "
            f"{[e.get('message') for e in events if e['event'] == 'error']}")

        # 并发峰值必须达到窗口宽度（旧实现恒为 1，配置形同虚设）
        assert sh.OUTLINE_CHAPTER_CONCURRENCY == 2
        assert stats["unit_peak"] == sh.OUTLINE_CHAPTER_CONCURRENCY, \
            (f"并发峰值必须达到窗口宽度 {sh.OUTLINE_CHAPTER_CONCURRENCY}，"
             f"实际 {stats['unit_peak']}")
        # 每章恰好一次子目录调用：步进回退到 _merge_k 会产生重复/漏章
        assert sorted(stats["unit_calls"]) == [1, 2, 3, 4, 5, 6], \
            f"每章恰好一次子目录调用（无重复、无漏章），实际 {stats['unit_calls']}"

        # completed 事件的目录章序必须严格递增且覆盖全部 6 章
        done = [e for e in events if e["event"] == "completed"][0]
        chapters = done["outline"]
        assert [c["title"] for c in chapters] == [
            "第1章", "第2章", "第3章", "第4章", "第5章", "第6章"], \
            f"章序必须严格递增且无重复：{[c['title'] for c in chapters]}"
        assert done.get("failed_chapters", []) == []

        # 进度事件里的 done 必须单调递增、终值等于总章数
        progresses = [e for e in events if e["event"] == "progress" and "done" in e]
        assert progresses, "分步链路必须推送带 done/total 的进度事件"
        dones = [e["done"] for e in progresses]
        assert dones == sorted(dones), f"进度 done 必须单调递增：{dones}"
        assert progresses[-1]["total"] == 6
        assert progresses[-1]["done"] == 6

    async def test_stop_at_batch_gate_keeps_prefix_and_skips_rest(
            self, db_conn, monkeypatch):
        """停止在批间闸门生效：前缀章节完整下发，闸门后的窗口不得再发起 AI 调用。

        编排顺序为「闸门 → 停止检查 → 并发发起」（sse_handlers.py L4451-4456），
        因此第一个未过闸门的窗口（第 2 窗，章 3-4）不会启动任何 AI 调用；
        已完成的前缀（第 1 窗 2 章）随 stopped 事件完整下发。

        停止触发时机：用 Barrier(2) 让第 1 窗两章 AI 都完成后再置 stopped，
        这样窗口 1 的归位与进度快照已完整落定，第 2 窗被编排层批间闸门拦下，
        避免依赖并发调度里微妙的到达顺序（不同章谁先完成）。
        """
        # 同 test 1：模块常量固化，改 settings 实例无效
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        sid = await _seed_long_scheme(db_conn)

        state = {"stopped": False, "gate_calls": 0}
        stats = _patch_ai(monkeypatch, unit_delay=0.02)
        # 第 1 窗两章 AI 都完成时 Barrier 释放：两章同时出信号量做复检，
        # 此时编排层尚未进入第 2 窗闸门 → is_stopped 仍为 False → 两章都返回 ok。
        stats["unit_barrier"] = asyncio.Barrier(2)

        async def fake_wait_resume(task_id):
            state["gate_calls"] += 1
            # wait_resume 调用序：1=level1 生成 → 2=窗1 批间闸门 →
            #                  3=窗1 章1 单元入口 → 4=窗1 章2 单元入口 →
            #                  5=窗2 批间闸门（此时窗1 已归位、两章都 ok 落定）。
            # 在第 2 窗闸门置 stopped，让编排层 L4454 的 is_stopped 检查命中 break，
            # 避免依赖章 3/4 单元入口的复检（不同章谁先完成有调度顺序依赖）。
            if state["gate_calls"] == 5:
                state["stopped"] = True
            return None

        monkeypatch.setattr(sh, "wait_resume", fake_wait_resume)
        monkeypatch.setattr(sh, "is_stopped",
                            lambda task_id: bool(state["stopped"]))

        events = await _run_stream(sid, db_conn)
        names = [e["event"] for e in events]

        assert names[-1] == "stopped", f"停止后必须以 stopped 收尾: {names}"
        # 第 2 窗被批间闸门拦下 → 停止之后的章不得再发起子目录 AI 调用
        assert sorted(stats["unit_calls"]) == [1, 2], \
            (f"停止之后的章节不得再发起子目录 AI 调用，"
             f"实际 {stats['unit_calls']}")
        # 前缀章节完整保留在 stopped 事件里
        done = [e for e in events if e["event"] == "stopped"][0]
        titles = [c["title"] for c in done["outline"]]
        assert titles == ["第1章", "第2章"], \
            f"stopped 事件必须携带已完成前缀章节：{titles}"
        assert done.get("failed_chapters", []) == []
