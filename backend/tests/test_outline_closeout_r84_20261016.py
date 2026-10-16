"""R84 · 目录模块遗留项收口护栏（2026-10-16）

对应审计报告 §9 的 4 个遗留项，每项都做成**可执行断言**而非文字建议：

C1 遗留① —— ``renumber_outline_nodes`` 多重章节前缀残留
    三档剥离契约的分工与幂等：单轮（锁定契约）/ tree（目录树标题）/ 全量剥净。
    树档位必须「多轮剥章节编号、到子项标记即停」，否则
    「第1章 1.1 工程概况」落库后导出再套一层 → 用户可见双重编号。

C2 遗留③ —— ``outline_reorganize`` 别名表
    跨组零冲突（否则会静默归错组）、组内零重复、长度降序排序成立、
    且**注册顺序不影响归属**（旧注释「后写的会被短的覆盖」不成立）。

C4 遗留④ —— 分步生成并发窗口
    窗口宽度是**实际并发数**而非只影响步进取整：窗口内单元数 == _window，
    峰值同时执行数 == _window，且信号量许可 == _window。
    功能面由 test_outline_window_step_bug.py 的参数网格锁定（每章恰好一次），
    此处补「并发度语义」这一层。

C3 遗留② —— 跨用例配置污染与 stale 断言
    由被修的两个测试用例自身回归验证（见对应文件），本文件不重复登记。
"""
import asyncio
import inspect
import json
import re
import uuid

import app.routers.sse_handlers as sh
import pytest
from app.services import outline_reorganize as orz
from app.services.numbering import (
    _SUB_ITEM_MARKER_RE,
    renumber_outline_nodes,
    strip_outline_numbering,
    strip_outline_numbering_all,
    strip_outline_numbering_tree,
)


# ===========================================================================
# C1 · 三档编号剥离契约（单轮 / tree / 全量剥净）
# ===========================================================================
# (输入, 单轮期望, tree 档期望, 剥净档期望)
CONTRACT_TABLE = [
    # —— 单轮即收敛：tree 档必须与单轮逐字一致（既有锁定契约不回归）——
    ("第一章 工程概况", "工程概况", "工程概况", "工程概况"),
    ("1.1 项目基本信息", "项目基本信息", "项目基本信息", "项目基本信息"),
    ("一、施工总平面", "施工总平面", "施工总平面", "施工总平面"),
    ("2023 年度计划", "2023 年度计划", "2023 年度计划", "2023 年度计划"),
    ("3.5 钢级混凝土", "钢级混凝土", "钢级混凝土", "钢级混凝土"),
    # —— 树标题里的子项标记：单轮剥主编号即停止，tree 档同样必须保留 ——
    ("1.2.3（1）细部构造", "（1）细部构造", "（1）细部构造", "细部构造"),
    ("1.1（三）安全措施", "（三）安全措施", "（三）安全措施", "安全措施"),
    ("1.2.3（1）（2）双标记", "（1）（2）双标记", "（1）（2）双标记", "双标记"),
    ("2）细部构造", "细部构造", "细部构造", "细部构造"),
    ("（1）细部构造", "细部构造", "细部构造", "细部构造"),
    # —— 多重章节前缀：单轮残留、tree 档剥净、剥净档同样剥净 ——
    ("第1章 1.1 工程概况", "1.1 工程概况", "工程概况", "工程概况"),
    ("第一章 1 基本情况", "1 基本情况", "基本情况", "基本情况"),
    ("第一章 第一章 工程概况", "第一章 工程概况", "工程概况", "工程概况"),
    ("第3章 2.4.1钢筋工程", "2.4.1钢筋工程", "钢筋工程", "钢筋工程"),
    ("第1章 2 施工准备 3 质量控制", "2 施工准备 3 质量控制",
     "施工准备 3 质量控制", "施工准备 3 质量控制"),
    #  ⚠️ 已知边界（刻意接受）：正则锚定在行首，故只有**连续前缀**会被剥掉
    #    （「第1章 2 」剥尽、「3 质量控制」因不在行首而原样保留）。这比「剥掉句中
    #    的子项序数」更安全，且树档与剥净档在该类输入上逐字一致。
    # —— 数量型 / 年份型标题：三档都不得碰 ——
    ("2层作业平台", "2层作业平台", "2层作业平台", "2层作业平台"),
    ("十个人", "十个人", "十个人", "十个人"),
    ("施工准备", "施工准备", "施工准备", "施工准备"),
    # —— 标题本身即编号：剥离后为空 → 原样返回（契约「绝不返回空串」）——
    ("第1章", "第1章", "第1章", "第1章"),
    ("1.1", "1.1", "1.1", "1.1"),
]


class TestStripNumberingTierContract:
    """三档剥离的行为契约：单轮逐字不变、tree 档只在未收敛时多剥且守标记闸门。"""

    @pytest.mark.parametrize(
        "title,single,tree,all_", CONTRACT_TABLE,
        ids=[c[0] for c in CONTRACT_TABLE])
    def test_three_tier_contract_table(self, title, single, tree, all_):
        assert strip_outline_numbering(title) == single, title
        assert strip_outline_numbering_tree(title) == tree, title
        assert strip_outline_numbering_all(title) == all_, title

    @pytest.mark.parametrize("title", [t for t, _s, _tr, _all in CONTRACT_TABLE])
    def test_tree_first_round_is_byte_identical_to_single_pass(self, title):
        """tree 档第一轮严格等价单轮：所有既有单轮锁定契约逐字不变。

        这是本档能安全上线的前提——首轮结果与单轮完全相同；只有单轮**未收敛**
        （说明标题带多重章节前缀）时，tree 档才进入额外轮次继续剥。
        """
        first = strip_outline_numbering(title)
        # 判别力：单轮未收敛 且 首轮不是子项标记（marker 闸门触发）⇔ tree 多剥；
        # 单轮已收敛，或首轮即被 marker 闸门拦下（"1.2.3（1）细部构造"），
        # 都必须逐字等于单轮结果。
        assert (strip_outline_numbering(first) != first
                and not _SUB_ITEM_MARKER_RE.match(first)) == (
            strip_outline_numbering_tree(title) != first), (
            f"多重章节前缀必须继续剥净、已收敛/子项标记必须逐字不动: {title!r}")

    @pytest.mark.parametrize("title", [
        "第1章 1.1 工程概况", "第一章 1 基本情况", "第1章 1.1 工程概况",
    ])
    def test_tree_is_idempotent(self, title):
        once = strip_outline_numbering_tree(title)
        assert strip_outline_numbering_tree(once) == once, title

    def test_renumber_outline_nodes_strips_multi_layer_prefix(self):
        """目录树编号重排的唯一入口必须剥净多重章节前缀（遗留①本体）。

        旧实现走单轮：「第1章 1.1 工程概况」→「1.1 工程概况」落库，导出层按位置
        再套一层 → 用户看到「第一章 1.1 工程概况」（双重编号）。
        """
        tree = [
            {"title": "第1章 1.1 工程概况", "children": []},
            {"title": "第一章 1 施工部署", "children": []},
            {"title": "第3章 2.4.1钢筋工程", "children": []},
        ]
        out = renumber_outline_nodes(tree)
        titles = [n["title"] for n in out]
        assert titles == ["工程概况", "施工部署", "钢筋工程"], titles
        # 剥离后编号必须由位置派生（编号唯一事实源），不再沿用标题里的残留编号
        assert [n["id"] for n in out] == ["1", "2", "3"]
        for n in out:
            assert not re.match(r"^(第\s*[一二三四五六七八九十0-9]+\s*[章节]|[0-9]+(\.[0-9]+)+\s)",
                                n["title"]), f"落库标题仍带章节编号: {n['title']}"

    def test_renumber_outline_nodes_keeps_subitem_marker(self):
        """反向锁：子项标记是内容不是章节编号，tree 档不得吃掉。

        若误用剥净档（strip_outline_numbering_all），「（1）」会被一起剥掉，
        目录树标题丢子项归属。
        """
        tree = [{"title": "1.2.3（1）细部构造", "children": []}]
        out = renumber_outline_nodes(tree)
        assert out[0]["title"] == "（1）细部构造", out[0]["title"]
        assert out[0]["id"] == "1"

    def test_strip_titles_false_keeps_title_verbatim(self):
        """strip_titles=False 时标题逐字不动（既有契约）。"""
        tree = [{"title": "第1章 1.1 工程概况", "children": []}]
        out = renumber_outline_nodes(tree, strip_titles=False)
        assert out[0]["title"] == "第1章 1.1 工程概况"
        assert out[0]["id"] == "1"


# ===========================================================================
# C2 · 别名表冲突与排序
# ===========================================================================
class TestChapterAliasTable:
    """别名表的卫生不变式：跨组唯一、组内唯一、长别名优先、注册顺序无关。"""

    def test_no_alias_registered_in_two_groups(self):
        """跨组复用同一别名会静默归错组（后写者赢），必须零冲突。"""
        seen: dict[str, str] = {}
        for grp, toks in orz.CHAPTER_ALIASES.items():
            for t in toks:
                assert t not in seen, (
                    f"别名 {t!r} 同时登记在 {seen[t]!r} 与 {grp!r}，"
                    "后写者会静默覆盖前者 → 目录被归错组")
                seen[t] = grp

    def test_no_duplicate_alias_within_a_group(self):
        """组内重复别名是无害的死重量，但会让「条目数 == 唯一键数」的直觉失效。"""
        for grp, toks in orz.CHAPTER_ALIASES.items():
            dups = sorted({t for t in toks if toks.count(t) > 1})
            assert not dups, f"分组 {grp!r} 组内重复别名: {dups}"

    def test_lookup_covers_every_registered_alias(self):
        assert len(orz._ALIAS_LOOKUP) == sum(
            len(v) for v in orz.CHAPTER_ALIASES.values()), (
            "组内去重后条目数应等于唯一键数")

    def test_sorted_by_length_descending(self):
        lens = [len(t) for t in orz._ALIAS_SORTED]
        assert lens == sorted(lens, reverse=True), "长别名必须最先被检查"

    def test_registration_order_does_not_change_grouping(self):
        """旧注释「后写的会被短的覆盖」的实证否证。

        把 CHAPTER_ALIASES 的分组顺序整体反转后重建反查表，每个别名的归属
        必须与原表逐键一致——注册顺序不影响结果，「长别名优先」由 _ALIAS_SORTED
        的遍历顺序保证，而不是由写入顺序保证。
        """
        reversed_lookup: dict[str, str] = {}
        for grp, toks in reversed(list(orz.CHAPTER_ALIASES.items())):
            for t in toks:
                reversed_lookup[t] = grp
        assert reversed_lookup == orz._ALIAS_LOOKUP

    def test_longest_alias_wins_in_matching(self):
        """命中即返回 ⇒ 最长命中必须先被检查（「监测」不得抢「监测监控」）。"""
        assert orz._group_of("监测监控方案") == orz._ALIAS_LOOKUP["监测监控"]
        assert orz._group_of("监测") == orz._ALIAS_LOOKUP["监测"]


# ===========================================================================
# C4 · 分步生成并发窗口：窗口宽度 == 实际并发数
# ===========================================================================
_WINDOW_SRC_RE = {
    "window_derived": re.compile(r"_window\s*=\s*max\(1,\s*OUTLINE_CHAPTER_CONCURRENCY\)"),
    "step_is_window_times_merge_k": re.compile(r"_round_step\s*=\s*max\(1,\s*_window\)\s*\*\s*_merge_k"),
    "loop_steps_by_round": re.compile(r"for\s+batch_start\s+in\s+range\(0,\s*_total,\s*_round_step\)"),
    "window_bounded_fill": re.compile(r"while\s+_u\s*<\s*_total\s+and\s+len\(_window_units\)\s*<\s*_window"),
    "order_preserving_gather": re.compile(r"results\s*=\s*await\s+asyncio\.gather\(\*\["),
    "semaphore_licensed_by_concurrency": re.compile(
        r"asyncio\.Semaphore\(OUTLINE_CHAPTER_CONCURRENCY\)"),
}


class TestConcurrencyWindowInvariant:
    """窗口宽度是实际并发数（不是只影响步进取整）。"""

    def test_window_invariants_present_in_source(self):
        src = inspect.getsource(sh.generate_outline)
        for name, pat in _WINDOW_SRC_RE.items():
            assert pat.search(src), f"并发窗口不变式缺失: {name}"

    def test_no_window_resizing_between_derivation_and_fill(self):
        """_window 从派生到窗口填充之间不得被重新赋值（改了宽度但没改步进取整
        是最阴的回归形态：步进取整和实际填充量会脱钩）。"""
        src = inspect.getsource(sh.generate_outline)
        reassigns = re.findall(r"(?m)^\s*_window\s*=\s*(?!\s*max\(1,\s*OUTLINE_CHAPTER_CONCURRENCY\))",
                               src)
        assert not reassigns, f"_window 被重复赋值: {reassigns}"

    @pytest.mark.parametrize("window,merge_k,total", [
        (4, 2, 9),    # 窗口内多个完整单元 + 末轮残余
        (1, 2, 5),    # 串行档：峰值必须恰好 1
    ], ids=["w4xk2_t9", "w1xk2_t5"])
    async def test_window_width_is_the_actual_concurrency(
            self, db_conn, monkeypatch, window, merge_k, total):
        """实测峰值同时执行数 == _window，且单元总数 == ⌈total / _merge_k⌉。

        旧实现只靠 asyncio.Semaphore 表达并发，而信号量在 _fetch_unit_children
        **内部**才被消费、每轮 gather 恒只含一个单元 ⇒ 配置形同虚设（并发度永远 1）。
        现由 _window 直接决定本轮同时排队的单元数；本用例把 _fetch_unit_children
        换成「进入即登记、sleep 让出事件循环、退出注销」的桩，直接量出峰值深度。
        """
        monkeypatch.setattr(sh, "OUTLINE_STEPWISE_MIN_WORDS", 100)
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_CONCURRENCY", window)
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_BATCH_SIZE", merge_k)

        depth = {"now": 0, "peak": 0}
        calls = []

        async def fake_fetch_unit(unit_chapters, *, batch_prompt=None,
                                  single_prompts=None, task_id=None, sem=None,
                                  timeout=60, push_stats=None, **kw):
            calls.extend(i for i, _ in unit_chapters)
            depth["now"] += 1
            depth["peak"] = max(depth["peak"], depth["now"])
            try:
                # 让出事件循环，使同轮 gather 的其它单元都有机会进入 ——
                # 峰值深度由此反映真实并发宽度而非调度巧合。
                await asyncio.sleep(0.05)
            finally:
                depth["now"] -= 1
            # per 与单元章数等长（真实契约：(unit_status, per) 且
            # per[i] = (st, children)）；每章给出 1 个小节，避免触发
            # 「占位/泛化标题」护栏（空 children 会被记 9 处占位标题）
            per = [("ok", [{"title": f"第{i + 1}章-小节{i + 1}.1",
                            "children": []}])
                   for i, _ in unit_chapters]
            return "ok", per

        async def fake_collect(messages, validate_fn=None, **kwargs):
            outline = [{"title": f"第{i}章", "description": "", "children": []}
                       for i in range(1, total + 1)]
            return ({"outline": outline},
                    json.dumps({"outline": outline}, ensure_ascii=False))

        monkeypatch.setattr(sh, "_fetch_unit_children", fake_fetch_unit)
        monkeypatch.setattr(sh, "collect_json_response", fake_collect)

        sid, pid = uuid.uuid4().hex, uuid.uuid4().hex
        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)", (pid, "测试项目"))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name, word_budget,"
            " config_json) VALUES (?,?,?,?,?)",
            (sid, pid, "长方案-并发桩", 10000, json.dumps({}, ensure_ascii=False)))
        await db_conn.commit()

        resp = await sh.generate_outline(sid, None, db_conn)
        out = []
        async for chunk in resp.body_iterator:
            for line in (chunk if isinstance(chunk, str)
                         else chunk.decode("utf-8")).splitlines():
                if line.startswith("data: "):
                    out.append(json.loads(line[len("data: "):]))

        assert [e["event"] for e in out][-1] == "completed", (
            f"长方案应以 completed 收尾: {[e['event'] for e in out]}")
        assert sorted(calls) == list(range(total)), (
            f"每章恰好一次单元调用（0 基章序号），实际 {sorted(calls)}")
        assert depth["peak"] == window, (
            f"窗口宽度 {window} 必须是实际并发数，实测峰值 {depth['peak']}")

    async def test_semaphore_licence_matches_window(self, db_conn, monkeypatch):
        """信号量许可数必须 == 配置并发度（与 _window 同口径），不得各自取值。"""
        monkeypatch.setattr(sh, "OUTLINE_CHAPTER_CONCURRENCY", 3)
        src = inspect.getsource(sh.generate_outline)
        m = re.search(r"asyncio\.Semaphore\(([^)]+)\)", src)
        assert m and m.group(1).strip() == "OUTLINE_CHAPTER_CONCURRENCY", (
            f"信号量许可必须直接由配置并发度派生，实测: {m.group(1) if m else None}")
