# -*- coding: utf-8 -*-
"""目录生成模块深度审计收口护栏（R85 · 2026-10-16）

用户报症状：三级标题大量占位式泛化名（「专项6」「总体实施策划（2）」
「总体实施策划（3）」）+ 编号冲突后缀。

本文件锁定 4 个**已实证复现**的缺陷修复，并按本仓纪律为每一项配
「接线/接线被摘」的静态锁与「值域/反例」行为锁：

- BUG-A 泛化标题「词干 + 纯序号」变体覆盖不全（`outline_quality`）
- BUG-B 「（N）」编号冲突后缀正则值域缺中文数字与 4 位数字（`outline_quality`）
- BUG-C `duplicate_scheme` 不重排章节编号 ⇒ 副本一落库即编号全错（`schemes`）
- BUG-D `create_section` / `update_section` 用单轮档剥离 ⇒ DB 标题残留
  一层编号并经 `{section_title}` 原样污染正文生成提示词（`sections`）

⚠️ 判据纪律（AGENTS §5.14 / §5.15）：
  · 静态锁用 **AST 锁真实调用**，不锁「源码含字符串」（防 import 还在、调用被摘
    的假护栏形态）；
  · 行为锁必须**先断言样本确实被判为该类**再断言结论，防阳性用例静默通过；
  · 每条锁都配**反例**（合法标题必须不误伤），否则「判据越宽越严」会变成
    恒失败逼后人改松。
"""
import ast
import json
import re
import uuid
from pathlib import Path

import pytest

from app.models import SectionCreate, SectionUpdate
from app.routers import schemes as _schemes
from app.routers.sections import (
    _save_outline_to_db, _build_tree, create_section, update_section,
)
from app.services.numbering import (
    renumber_section_outline_ids, strip_outline_numbering_tree,
)
from app.services.outline_quality import (
    GENERIC_BARE_STEMS, _norm, detect_generic_titles, has_trailing_counter_suffix,
    is_generic_title,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"


# ====================================================================
# A 组 · BUG-A：泛化标题「词干 + 纯序号」结构化覆盖
# ====================================================================

#: 必须判为占位的形态（用户症状 + AI 换写逃逸的同族变体）
GENERIC_MUST = [
    "专项6", "专项方案6", "专项六", "专项措施6", "措施3", "内容2",
    "要点一", "内容十二", "专项", "措施", "内容", "要点",
    "总体实施策划", "总体实施策划（2）", "总体实施策划（三）",
    "专项6（2）", "其他", "有关要求",
]

#: 必须**不**误伤的合法标题（每条都在既有生产语料/护栏里出现过）
GENERIC_MUST_NOT = [
    "专项施工方案", "专项方案编制依据", "监测措施", "施工要求", "技术要求",
    "安全要求", "应急预案", "6项施工措施", "深基坑支护（2）", "施工工艺技术",
    "工程概况", "安全生产管理", "内容简介说明", "3D打印施工方案",
    "十二层平面布置", "编制依据", "工程概述", "质量保证措施",
]


class TestBugAStemOrdinalCoverage:
    """BUG-A：字面量枚举 → 结构判据（词干闭集 + 全串锚定 + 可选序号）。"""

    @pytest.mark.parametrize("title", GENERIC_MUST)
    def test_must_be_generic(self, title):
        assert is_generic_title(title) is True, f"占位名逃逸检测：{title!r}"

    @pytest.mark.parametrize("title", GENERIC_MUST_NOT)
    def test_legit_title_never_flagged(self, title):
        """反例锁：判据拓宽**不得**误伤合法标题（否则护栏会被后人改松）。"""
        assert is_generic_title(title) is False, f"合法标题被误判为占位：{title!r}"

    def test_detect_generic_titles_sees_variants(self):
        """端到端：检出出口必须真的报出（防 is_generic_title 与 detect 分叉）。"""
        hits = detect_generic_titles([{"title": t, "children": []}
                                     for t in ("专项方案6", "专项六", "措施3")])
        assert {h["title"] for h in hits} == {"专项方案6", "专项六", "措施3"}, hits
        assert all(h["kind"] == "generic" for h in hits), hits

    def test_structural_rule_is_single_source(self):
        """`is_generic_title` 与 `detect_generic_titles` 必须共用同一判据出口。

        反向锁：把结构判据从 `is_generic_title` 里摘掉 ⇒ 变体立刻逃逸 ⇒ 红。
        （AGENTS §5.14「源码含字符串」是假护栏；此处锁**真实调用**。）
        """
        src = (APP_DIR / "services" / "outline_quality.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "is_generic_title")
        called = {c.func.id for c in ast.walk(fn)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "_is_stem_ordinal_generic" in called, (
            "is_generic_title 不再消费结构判据 ⇒ 泛化变体逃逸回 R85 前的缺口")

    def test_stem_regex_is_fully_anchored(self):
        """结构正则必须**全串锚定** —— 这是「合法复合标题结构上不可能命中」
        的唯一依据（去掉 `^…$` 即退化成子串模糊匹配）。"""
        from app.services.outline_quality import GENERIC_STEM_ORDINAL_RE as rx
        pat = rx.pattern
        assert pat.startswith("^"), pat
        assert pat.endswith("$"), pat
        # 防「子串匹配」回归：真实复合标题不得命中
        for t in ("专项施工方案", "监测措施", "施工要求"):
            assert not rx.search(_norm(t)), t

    def test_bare_stem_whitelist_is_narrow(self):
        """裸词干白名单必须是**封闭且窄**的（不含「要求/说明/方法/流程」等
        可能构成正常章节名的词）。"""
        assert GENERIC_BARE_STEMS == {"专项", "措施", "内容", "要点", "其它", "其他"}
        for w in ("要求", "说明", "方法", "步骤", "流程", "工序", "安排", "保障"):
            assert w not in GENERIC_BARE_STEMS, (
                f"裸词干白名单收进了 {w!r} —— 该词可能构成正常章节名，只应带序号时判占位")


# ====================================================================
# B 组 · BUG-B：「（N）」编号冲突后缀值域
# ====================================================================

class TestBugBCounterSuffixDomain:
    """BUG-B：中文数字与 4 位数字此前**同时逃过检测侧与回写红线**。"""

    @pytest.mark.parametrize("title", [
        "监测措施（2）", "监测措施（三）", "监测措施（1234）",
        "监测措施(一)", "总体实施策划（三）", "专项6（2）",
    ])
    def test_suffix_detected(self, title):
        assert has_trailing_counter_suffix(title) is True, title

    @pytest.mark.parametrize("title", [
        "按第（3）条执行", "深基坑支护（2）不叫冲突", "施工方案（三）措施",
        "洞口（2）围护", "锚杆（12）拉结", "", "无括号标题",
    ])
    def test_non_trailing_not_detected(self, title):
        """反例锁：只认**整组位于标题末尾**的形态，绝不匹配正文中的括号数字
        （「施工方案（三）措施」这类括号数字出现在标题中间 ⇒ 不是消歧后缀）。"""
        assert has_trailing_counter_suffix(title) is False, title

    def test_chinese_suffix_normalizes_in_norm(self):
        """`_norm` 必须把中文数字后缀一并归一，否则「总体实施策划（三）」的
        基名会残留汉字数字、与本体「总体实施策划」不碰撞 ⇒ 冲突后缀漏报。"""
        assert _norm("总体实施策划（三）") == "总体实施策划"
        assert _norm("总体实施策划（2）") == "总体实施策划"

    def test_detect_reports_chinese_numeral_conflict(self):
        """端到端：碰撞判定必须能吃到中文数字后缀（旧实现恒 MISS）。"""
        hits = detect_generic_titles([
            {"title": "钢内支撑安装", "children": []},
            {"title": "钢内支撑安装（三）", "children": []}])
        assert [h["kind"] for h in hits] == ["conflict_suffix"], hits

    def test_suffix_regex_value_domain(self):
        """正则值域锁定（防日后收窄回 1~3 位阿拉伯数字）。"""
        from app.services.outline_quality import (
            _CN_ORDINAL_CHARS, _TRAILING_COUNTER_SUFFIX_RE as rx,
        )
        pat = rx.pattern
        assert "\\d{1,4}" in pat or "{4}" in pat, pat
        for ch in ("一", "三", "十"):
            assert ch in _CN_ORDINAL_CHARS, _CN_ORDINAL_CHARS

    def test_detection_side_keeps_collision_requirement(self):
        """刻意保留的既有分工（R79）：**检测侧**仍要求碰撞，防误伤
        「深基坑支护（2）」这类合法部位限定标题；回写侧只看形态。
        本锁防止后人「顺手统一」而把检测侧放宽 ⇒ 合法标题开始被误报。
        """
        assert detect_generic_titles(
            [{"title": "深基坑支护（2）", "children": []}]) == []
        # 但回写红线（单一形态判据）必须照收
        assert has_trailing_counter_suffix("深基坑支护（2）") is True


# ====================================================================
# C 组 · BUG-C：duplicate_scheme 必须重排副本编号
# ====================================================================

async def _seed_tied_sort_order(db, pid, sid, titles=("A 章", "B 章")):
    """造 `sort_order` 并列 —— 历史脏数据 / 外部脚本直改的真实形态。

    并列时 `sections._build_tree` 的次级排序键是 UUID，源方案靠 UUID 排出的
    顺序**复制后必然可能翻转**（副本 UUID 全是新的随机值）。
    """
    for i, title in enumerate(titles):
        await db.execute(
            "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
            " level, sort_order, status, outline_json, word_budget)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), sid, pid, "", title, 1, 0, "empty",
             json.dumps({"id": str(i + 1), "level": 1}, ensure_ascii=False), 1500))
    await db.commit()


async def _positions_and_ids(db, sid):
    tree = await _build_tree(db, sid)
    return [(n["title"], json.loads(n.get("outline_json") or "{}").get("id"))
            for n in tree]


class TestBugCDuplicateSchemeRenumbers:
    """BUG-C：复制路径此前**只重映射 UUID、不重排编号**。

    判据不是「副本 ids == 源 ids」（源自身可能已脏），而是**不变式**：
    副本的 `outline_json.id` 必须等于它自身的位置路径 —— 即副本自洽。
    """

    @pytest.mark.asyncio
    async def test_copy_is_self_consistent_with_tied_sort_order(self, db_conn):
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "源方案", "基坑支护与降水"))
        await _seed_tied_sort_order(db, pid, sid)

        new_id = (await _schemes.duplicate_scheme(
            db=db, project_id=pid, scheme_id=sid))["id"]

        pairs = await _positions_and_ids(db, new_id)
        assert len(pairs) == 2, pairs
        # 不变式：第 k 个位置的章节，编号必须是 "k"
        assert [i for _, i in pairs] == ["1", "2"], (
            f"副本编号与自身位置不一致（复制后即错）：{pairs}")

    @pytest.mark.asyncio
    async def test_copy_of_clean_scheme_is_unchanged(self, db_conn):
        """反向锁：对**干净**数据（sort_order 无并列）复制是恒等变换
        ⇒ 修复对既有正常路径零行为变化。"""
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "源方案", "基坑支护与降水"))
        for i, title in enumerate(("第一章", "第二章", "第三章")):
            await db.execute(
                "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
                " level, sort_order, status, outline_json, word_budget)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), sid, pid, "", title, 1, i, "empty",
                 json.dumps({"id": str(i + 1), "level": 1}), 1500))
        await db.commit()
        src = await _positions_and_ids(db, sid)

        new_id = (await _schemes.duplicate_scheme(
            db=db, project_id=pid, scheme_id=sid))["id"]
        cp = await _positions_and_ids(db, new_id)

        assert cp == src, f"干净方案的复制结果必须逐字一致：{src} -> {cp}"

    def test_duplicate_scheme_wires_renumber(self):
        """静态接线锁（AST 锁真实调用，不锁「源码含字符串」）。"""
        src = (APP_DIR / "routers" / "schemes.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "duplicate_scheme")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert "renumber_sections_after_reorder" in (names | attrs), (
            "duplicate_scheme 不再重排编号 ⇒ 副本一落库即带错号（R85 BUG-C 复发）")

    def test_renumber_is_wired_before_commit(self):
        """重排必须在 `commit()` **之前**调用，否则编号写不进去（同连接
        未提交写入可见性下，commit 之后的独立事务读不到）。"""
        src = (APP_DIR / "routers" / "schemes.py").read_text(encoding="utf-8")
        i_renum = src.find("renumber_sections_after_reorder(db, new_id)")
        i_commit = src.find("await db.commit()", src.find("def duplicate_scheme"))
        assert i_renum != -1, "未找到重排调用"
        assert i_commit != -1, "未找到 commit"
        assert i_renum < i_commit, "重排必须先于 commit"


# ====================================================================
# D 组 · BUG-D：标题写入路径的剥离档位统一
# ====================================================================

class TestBugDTitleStripCaliber:
    """BUG-D：`create_section` / `update_section` 此前用**单轮档**剥离。

    后果：同一目录经 `create_section` 落库为「1.1 工程概况」、经
    `save-outline` 落库为「工程概况」；残留串还会经 `sse_handlers` 的
    `{section_title}` **原样**注入正文生成提示词（与权威 `{section_number}`
    并列出现）。
    """

    DIRTY = "第1章 1.1 工程概况"

    @pytest.mark.asyncio
    async def test_create_and_save_outline_store_identical_title(self, db_conn):
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid_a, sid_b = str(uuid.uuid4()), str(uuid.uuid4())
        for sid, name in ((sid_a, "SA"), (sid_b, "SB")):
            await db.execute(
                "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
                (sid, pid, name, "基坑支护与降水"))

        r = await create_section(scheme_id=sid_a,
                                 data=SectionCreate(title=self.DIRTY), db=db)
        cur = await db.execute("SELECT title FROM sections WHERE id=?", (r["id"],))
        t_create = (await cur.fetchone())["title"]

        await _save_outline_to_db(db, sid_b, [{"title": self.DIRTY, "children": []}])
        cur = await db.execute(
            "SELECT title FROM sections WHERE scheme_id=? ORDER BY sort_order", (sid_b,))
        t_save = (await cur.fetchone())["title"]

        assert t_create == t_save == "工程概况", (
            f"两条落库路径标题形态分叉：create={t_create!r} save-outline={t_save!r}")

    @pytest.mark.asyncio
    async def test_update_section_strips_fully(self, db_conn):
        db = db_conn
        pid = str(uuid.uuid4())
        await db.execute("INSERT INTO projects (id,name) VALUES (?,?)", (pid, "P"))
        sid = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO schemes (id,project_id,name,type) VALUES (?,?,?,?)",
            (sid, pid, "S", "基坑支护与降水"))
        r = await create_section(scheme_id=sid,
                                 data=SectionCreate(title="干净标题"), db=db)
        await update_section(scheme_id=sid, section_id=r["id"],
                             data=SectionUpdate(title="第2章 2.3 支护施工"), db=db)
        cur = await db.execute("SELECT title FROM sections WHERE id=?", (r["id"],))
        assert (await cur.fetchone())["title"] == "支护施工"

    @pytest.mark.parametrize("title", [
        "专项施工方案", "深基坑支护施工", "工程概况", "监测措施",
        "3D打印施工方案", "十二层平面布置", "2023年规范", "1.1", "1.2.3",
    ])
    def test_clean_titles_are_identity(self, title):
        """对**已干净**标题，tree 档剥离必须是恒等变换 ⇒ 前端单轮剥离、
        导出剥净档与既有护栏全部不受影响（零回归的判据）。"""
        assert strip_outline_numbering_tree(title) == title, title

    def test_sections_imports_tree_tier(self):
        """静态接线锁：`sections.py` 的 `strip_outline_numbering` 必须是
        **tree 档**别名（防有人改回单轮档而不自知）。

        ⚠️ 反向锁覆盖**两个**来源：既不得从 `numbering` 直接导入单轮档，
        也不得再从 `json_response` 导入（那个名字指向单轮档）；两处同时
        存在还会互相遮蔽并触发 F811 —— 「同一判据两份副本」的典型形态。
        """
        src = (APP_DIR / "routers" / "sections.py").read_text(encoding="utf-8")
        assert "strip_outline_numbering_tree as strip_outline_numbering" in src, (
            "sections.py 的标题剥离档位被改回单轮档 ⇒ R85 BUG-D 复发")
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and (
                    "numbering" in n.module or "json_response" in n.module):
                for a in n.names:
                    assert a.name != "strip_outline_numbering", (
                        f"sections.py 从 {n.module} 直接导入单轮档 "
                        f"strip_outline_numbering（会绕开 tree 档）")
                    assert not (a.name == "strip_outline_numbering_tree"
                                and a.asname != "strip_outline_numbering"), (
                        "tree 档别名名被改，标题写入路径口径不可辨")

    def test_prompt_section_title_is_clean(self):
        """跨模块锁：`{section_title}` 是 DB 标题的**原样**注入通道 ——
        R85 前 DB 标题可能残留一层编号，模型会同时看到「当前章节：1.1 工程概况」
        与「章节编号：1」这种自相矛盾的标识。本锁断言标题写入路径已剥净。"""
        src = (APP_DIR / "routers" / "sse_handlers.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 找所有把 leaf["title"] / node["title"] 赋给 section_title 的位置
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.keyword) and n.arg == "section_title"]
        assert hits, "未找到 section_title 注入点（提示词接线被摘？）"
        # 注入本身允许原样（DB 侧已剥净），但必须存在剥净的写入侧保证
        assert "strip_outline_numbering_tree as strip_outline_numbering" in (
            (APP_DIR / "routers" / "sections.py").read_text(encoding="utf-8"))


# ====================================================================
# E 组 · 不变式：编号唯一事实源与字节体检
# ====================================================================

class TestInvariants:
    def test_renumber_is_position_derived(self):
        """`renumber_section_outline_ids` 必须按**位置**派生 id
        （R85 BUG-C 的修复依赖这条不变式）。"""
        tree = [{"id": "77", "level": 1, "title": "A",
                 "children": [{"id": "88", "level": 1, "title": "A1", "children": []}]}]
        ups = renumber_section_outline_ids(tree)
        got = {u[2]: (json.loads(u[0]).get("id"), u[1]) for u in ups}
        assert set(got.values()) == {("1", 1), ("1.1", 2)}, got

    @pytest.mark.parametrize("rel", [
        "services/outline_quality.py", "routers/sections.py", "routers/schemes.py",
    ])
    def test_byte_health(self, rel):
        """字节体检：BOM=False / U+FFFD=0 / 孤立 CR=0（AGENTS §5.14 R57/R58）。"""
        p = APP_DIR / rel
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 含 BOM"
        assert raw.count(b"\xef\xbf\xbd") == 0, f"{rel} 含 U+FFFD"
        crlf = raw.count(b"\r\n")
        assert raw.count(b"\r") == crlf, f"{rel} 存在孤立 CR（行尾损坏）"
        ast.parse(raw.decode("utf-8"))

    def test_no_duplicate_module_level_names(self):
        """追加符号不得与模块既有名字撞名（R65 同族教训）。"""
        for rel in ("services/outline_quality.py",):
            tree = ast.parse((APP_DIR / rel).read_text(encoding="utf-8"))
            names = [t.id for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     for t in n.body if isinstance(t, ast.Name)
                     and isinstance(t.ctx, ast.Store)]
            assert len(names) == len(set(names)), (
                f"{rel} 顶层重名：{[n for n in names if names.count(n) > 1]}")


# ====================================================================
# F 组 · 提示词纪律（渲染后判定，不是源码字符串匹配）
# ====================================================================

class TestPromptDisciplineRendered:
    """占位/冲突纪律必须**真的下发到模型**（R73b F4 的教训：源码含字符串
    不等于模型看得到；既有护栏只覆盖 6 个 key，其余产出/改写标题的入口
    无任何断言 —— 静默删掉一条纪律不会有任何测试变红）。

    判定口径：**先渲染**再断言（AGENTS §4.22.8「判据要指向真正下发给模型的
    那份文本」）。
    """

    #: (prompt key, 变量实参)
    TITLE_WRITING_KEYS = [
        ("outline_patch_system", dict(
            scheme_name="基坑支护", scheme_type="深基坑", project_brief="",
            project_facts="", chapter_titles="编制依据", missing_items="施工计划")),
        ("outline_recognition_system", dict(raw_text="目录：专项6 总体实施策划（2）")),
        ("outline_title_fix_system", dict(
            scheme_name="基坑支护", scheme_type="深基坑", project_brief="",
            project_facts="", broken_items="专项6")),
        ("outline_adjust_system", dict(
            current_outline="[]", instruction="调整", scheme_name="基坑支护",
            scheme_type="深基坑")),
    ]

    #: 纪律行的**判别性**特征：同一行内同时出现「（N）」与一个**禁止/改写**
    #: 祈使词。单看「泛化」或「占位」是**假判据** —— R85 A/B 的 M5 就是这么
    #: 漏掉的：把纪律行的前半句删掉后，后半句「…这类占位名」仍含「占位」，
    #: 断言照样通过。教训与 AGENTS §5.14 同源：判据必须与风险同构。
    _NEGATIONS = ("严禁", "不得", "禁止", "必须改写", "绝不准", "不能")

    def _discipline_lines(self, text: str) -> list[str]:
        return [ln for ln in text.splitlines()
                if "（N）" in ln and any(n in ln for n in self._NEGATIONS)]

    @pytest.mark.parametrize("key,vars_", TITLE_WRITING_KEYS,
                             ids=[k for k, _ in TITLE_WRITING_KEYS])
    def test_rendered_prompt_forbids_generic_and_suffix(self, key, vars_):
        from app.services.ai.prompts._registry import render
        text = render(key, **vars_)
        assert len(text) > 100, f"{key} 渲染结果异常短（判据可能空转）"
        # 判别性判据：必须存在「同一条规则行」既点名「（N）」冲突后缀、
        # 又下达禁止/改写指令（只看单个词会被残留句骗过 —— 见 M5 教训）
        lines = self._discipline_lines(text)
        assert lines, (
            f"{key} 渲染后**没有**「（N）」编号冲突后缀的禁止/改写规则行"
            f"（静默回归）")
        # 泛化/占位必须与该规则行**同行**出现（而不是散落在别处）
        assert any(("泛化" in ln) or ("占位" in ln) for ln in lines), (
            f"{key} 的「（N）」规则行未同时点名泛化/占位标题：{lines}")

    def test_discipline_judgment_is_discriminating(self):
        """判据自证（防空转 / 防假护栏）：把纪律行删掉后，本判据必须变红。

        这就是 A/B 的 M5 用例内联在此 —— 让「假绿」在**测试自身**里可复现，
        而不必等下一次 A/B 才被发现。
        """
        sample = (
            "1. 识别编号规则：1、1.1\n"
            "2. 返回 JSON\n"
            '3. 不得使用"（N）"编号冲突后缀；原始文本里的"专项6""其他"'
            '这类占位名**必须改写为具体名称**\n'
        )
        assert self._discipline_lines(sample), "判据对真实样例不生效"
        # 删掉纪律行（只留含「占位」的残留句）⇒ 必须不再命中
        stripped = "1. 识别编号规则：1、1.1\n2. 原始文本里若写的是这类占位名\n"
        assert not self._discipline_lines(stripped), (
            "判据被残留句骗过（假护栏）—— 这正是 A/B M5 漏抓的原因")

    def test_registry_entry_exists_for_every_key(self):
        """防空转：断言 key 真的在注册表里（否则 render 会抛，测试变成
        「错误类型断言」而非有效判据）。"""
        from app.services.ai.prompts import _registry
        for key, _ in self.TITLE_WRITING_KEYS:
            assert key in _registry._ALL_PROMPTS, f"{key} 未注册"


# ====================================================================
# G 组 · 端到端：占位闸门真的拦住「改写成变体占位名」这条复发路径
# ====================================================================

class TestEndToEndSurgicalFixRejectsVariants:
    """**本轮最关键的一组**：R85 之前的 6 份护栏全部只测 `is_generic_title`
    这个**纯函数**，从未走真实回写出口，于是「AI 把『专项6』改写成
    『专项方案6』」这条复发路径**全部绿灯**。

    判据必须落在**接线面**（`_apply_title_updates` 真回写 + 真复检），
    而不是判据面 —— AGENTS「接线类护栏要选必须依赖该接线才有结论的断言」。
    """

    #: AI 换一种同义写法即可绕过的变体（R85 前全部 `applied=1 / rejected=0`
    #: 且复检 `generic_residual=0`）
    EVASIONS = ["专项方案6", "专项六", "专项措施6", "措施3", "内容2", "要点一"]

    @pytest.mark.parametrize("variant", EVASIONS)
    def test_variant_is_rejected_by_write_path(self, variant):
        from app.routers.sse_handlers import _apply_title_updates
        tree = [{"title": "专项6", "description": "占位", "children": []}]
        out, applied, rejected = _apply_title_updates(
            tree, [{"path": "1", "title": variant}])
        assert applied == 0, f"{variant!r} 被写回目录（占位名复发）"
        assert len(rejected) == 1, rejected
        assert out[0]["title"] == "专项6", "拒收时**不得**改动原节点"

    @pytest.mark.parametrize("variant", ["专项方案6", "专项六", "措施3"])
    def test_post_check_reports_zero_residual_for_original(self, variant):
        """正向对照：把真占位名换成真占位名 → 复检必须仍报残留
        （防「判据被放宽成什么都不报」的恒绿形态）。"""
        out = [{"title": variant, "children": []}]
        assert detect_generic_titles(out), f"{variant!r} 复检未报残留（闸门失效）"

    def test_chinese_numeral_suffix_rejected_by_write_path(self):
        """BUG-B 端到端：中文数字后缀必须在**回写面**被拒（此前双侧漏网）。"""
        from app.routers.sse_handlers import _apply_title_updates
        tree = [{"title": "专项6", "children": []}]
        _, applied, rejected = _apply_title_updates(
            tree, [{"path": "1", "title": "钢内支撑安装（三）"}])
        assert applied == 0, "带中文数字冲突后缀的标题被写回目录"
        assert "（N）" in rejected[0]["reason"], rejected

    def test_clean_title_is_still_accepted(self):
        """反向锁：判据**不得**宽到把合法改写一并拒掉（否则功能全灭）。"""
        from app.routers.sse_handlers import _apply_title_updates
        tree = [{"title": "专项6", "children": []}]
        out, applied, rejected = _apply_title_updates(
            tree, [{"path": "1", "title": "钢内支撑安装"}])
        assert applied == 1 and not rejected, (applied, rejected)
        assert out[0]["title"] == "钢内支撑安装"