r"""解析提取 / 目录生成模块 · 深度探查修复护栏（2026-09-27 第三轮）

本轮先复现、后修复的 3 个真实缺陷（每条都有「修复前必失败」的反向验证）：

1. **P1 · 标题损坏（前后端同源正则）** `services/numbering.py::strip_outline_numbering`
   点分编号分支写作 `[0-9]+(?:\.[0-9]+){0,7}[分隔符]+`，路径后**没有**分隔符时
   正则回退成「更短前缀 + 把点当分隔符」：
     - "1.2.3（1）细部构造" → "3（1）细部构造"（残句）
     - "2.4.1钢筋工程"      → "1钢筋工程"（残留数字；叠加位置编号后双重编号）
   修复：前瞻捕获最长路径 + 反向引用整条吃满（模拟原子组）。
   前端 `SchemeWorkbenchPage.tsx::stripOutlineNumbering` 是同一正则的副本，
   同步修复（否则「前端预览 ≠ 落库/导出标题」）。

2. **P1 · 解析产物整批丢失** `services/doc_pipeline/md_structured.py`
   图片标记显式 `page:N` 指向不存在的页时，旧实现 `_page_entry(pnum)` 凭空插入
   无 `text` 的页条目 → 收尾拼装 `KeyError('text')` → 四层存储（解析层四份产物
   + doc_chunks 分块）整批落盘失败（`_ingest_parsed_doc` 只记日志），
   用户侧表现为「解析成功但结构化/分块结果没有」；同时 page_count 被虚增到引用页号。
   修复：未知页归位到标记所在页（并给拼装加 `.get` 防御）。

3. **P2 · 契约不成立** `services/numbering.py::validate_section_content_numbering`
   docstring 承诺 `content_subheading_renumber=False` 时 `skipped=True`，
   实际 `skipped` 恒 False（消费方把「无法判定」误读成「校验通过」）。现显式回传。
"""
import json
import re
from pathlib import Path

import pytest

from app.services.ai.json_response import strip_outline_numbering as jr_strip
from app.services.doc_pipeline.doc_chunker import chunk_document
from app.services.doc_pipeline.md_structured import parse_markdown_structured
from app.services.numbering import (
    renumber_outline_nodes,
    strip_outline_numbering,
    validate_section_content_numbering,
)


# ============================================================
# 1. strip_outline_numbering：点分编号不得回退（标题损坏）
# ============================================================
class TestStripOutlineNumberingNumericPath:
    """点分编号 / 单段编号 / 年份的剥离边界（后端唯一实现）。"""

    @pytest.mark.parametrize("raw,want", [
        # —— 本次修复（修复前分别得到 "3（1）细部构造" / "1钢筋工程"）——
        ("1.2.3（1）细部构造", "（1）细部构造"),
        ("2.4.1钢筋工程", "钢筋工程"),
        ("1.2.3（1）", "（1）"),
        ("3.2.4.5钢筋", "钢筋"),
        # 路径后带分隔符：与旧行为一致
        ("5.1.1.1 深基坑", "深基坑"),
        ("2.1 相关法律法规", "相关法律法规"),
        ("1.0 零级", "零级"),
        # 「第 X 章」允许空格（AI/粘贴文本常见）
        ("第 3 章 施工计划", "施工计划"),
        ("第 一 章 工程概况", "工程概况"),
        # 单段编号：必须有分隔符
        ("1 施工准备", "施工准备"),
        ("3 资源配置", "资源配置"),
        ("1）施工工艺", "施工工艺"),
        ("一、安全保证", "安全保证"),
        ("十二、安全保证", "安全保证"),
        # 非编号：数值 + 单位 / 年份 / 编号前缀型词
        ("1.5m 深", "1.5m 深"),
        ("2023 年度安全生产计划", "2023 年度安全生产计划"),
        ("2024年施工计划", "2024年施工计划"),
        ("2023年规范", "2023年规范"),
        ("表2-1 材料表", "表2-1 材料表"),
        ("图3-2 布置图", "图3-2 布置图"),
        ("3D打印施工方案", "3D打印施工方案"),
        # 标题本身即编号 / 空标题 / 首尾空白
        ("1.1", "1.1"),
        ("1.2.3", "1.2.3"),
        ("1.2.3.4.5.6.7.8", "1.2.3.4.5.6.7.8"),
        ("第一章", "第一章"),
        ("", ""),
        (" 2.1 ", "2.1"),
    ])
    def test_strip_cases(self, raw, want):
        assert strip_outline_numbering(raw) == want

    def test_dotted_path_never_leaves_partial_digits(self):
        """不变量：剥离后不得留下「来自编号的孤立数字前缀」残句。

        旧实现会剥出 "3（1）细部构造" / "1钢筋工程"，这里直接钉住语义。
        """
        for raw in ("1.2.3（1）细部构造", "2.4.1钢筋工程", "3.2.4.5钢筋"):
            out = strip_outline_numbering(raw)
            assert not out[0].isdigit(), f"{raw!r} 剥离后仍以数字开头：{out!r}"

    def test_repeated_strip_is_stable(self):
        """幂等性：已剥离的编号前缀不会再被二次剥离（防止叠加位置编号后漂移）。"""
        for raw in ("2.4.1钢筋工程", "第 3 章 施工计划", "5.1.1.1 深基坑"):
            once = strip_outline_numbering(raw)
            assert strip_outline_numbering(once) == once

    def test_second_pass_strips_subitem_marker(self):
        """「1.2.3（1）细部构造」第一遍剥点分路径，第二遍再剥「（1）」。

        旧实现第一遍就会剥坏（→ "3（1）细部构造"），第二次更是无从下手；
        修复后两遍各剥一层、结果收敛，且不会残留来自编号的数字。
        """
        once = strip_outline_numbering("1.2.3（1）细部构造")
        assert once == "（1）细部构造"
        assert strip_outline_numbering(once) == "细部构造"

    def test_json_response_entry_point_same_behavior(self):
        """两个入口（numbering / json_response 再导出）必须同源同行为。"""
        for raw in ("1.2.3（1）细部构造", "2.4.1钢筋工程", "2023 年度安全生产计划"):
            assert jr_strip(raw) == strip_outline_numbering(raw)

    def test_renumber_outline_nodes_keeps_leaf_title_intact(self):
        """目录树重排编号时，标题剥离不得损坏（id 由位置推导，标题保持裸标题）。"""
        nodes = [{"title": "第一章 工程概况", "children": [
            {"title": "1.1 编制依据", "children": [
                {"title": "1.2.3（1）细部构造", "children": []}]}]}]
        renumber_outline_nodes(nodes)
        leaf = nodes[0]["children"][0]["children"][0]
        assert leaf["title"] == "（1）细部构造"
        assert leaf["id"] == "1.1.1"
        assert leaf["level"] == 3



# ============================================================
# 2. md_structured：未知页引用不得让解析产物整批丢失
# ============================================================
_MD_UNKNOWN_PAGE = "<!-- page:1 -->\n基坑深度 12.5 米\n\n[IMAGE: scan.png, page:99]\n"
_MD_KNOWN_PAGE = ("<!-- page:1 -->\n甲页内容\n\n[IMAGE: a.png, page:2]\n"
                  "<!-- page:2 -->\n乙页内容\n")


class TestMdStructuredUnknownPageRef:
    def test_unknown_page_ref_does_not_raise(self):
        """修复前：KeyError('text') → 四层存储整批落盘失败（本用例修复前必失败）。"""
        st = parse_markdown_structured(_MD_UNKNOWN_PAGE, doc_id="D1")
        assert st["page_count"] == 1
        assert [p["page_num"] for p in st["pages"]] == [1]
        assert st["image_count"] == 1

    def test_unknown_page_ref_falls_back_to_marker_page(self):
        """未知页的图片归位到标记所在页（既不丢图，也不凭空造页）。"""
        st = parse_markdown_structured(_MD_UNKNOWN_PAGE, doc_id="D1")
        assert st["images"][0]["page_num"] == 1
        assert st["pages"][0]["images"] == ["img_001"]

    def test_known_page_ref_still_honoured(self):
        """页号真实存在时必须保持原样（来源溯源能力不能被降级）。"""
        st = parse_markdown_structured(_MD_KNOWN_PAGE, doc_id="D2")
        assert st["page_count"] == 2
        assert st["images"][0]["page_num"] == 2
        page2 = next(p for p in st["pages"] if p["page_num"] == 2)
        assert page2["images"] == ["img_001"]

    def test_chunk_document_survives_unknown_page_ref(self):
        """下游分块链路（chunk_document）同样不得崩，且仍有产物。"""
        st = parse_markdown_structured(_MD_UNKNOWN_PAGE, doc_id="D3")
        chunks = chunk_document(_MD_UNKNOWN_PAGE, doc_id="D3", structured=st)
        assert chunks
        assert all(c["doc_id"] == "D3" for c in chunks)

    def test_page_count_not_inflated_by_ref(self):
        """page_count 不得被引用页号虚增（曾出现 3 页文档报 99 页）。"""
        md = ("<!-- page:1 -->\nA\n<!-- page:2 -->\nB\n<!-- page:3 -->\nC\n\n"
              "[IMAGE: x.png, page:500]\n")
        st = parse_markdown_structured(md, doc_id="D4")
        assert st["page_count"] == 3


# ============================================================
# 3. validate_section_content_numbering：skipped 契约
# ============================================================
class TestValidateNumberingSkippedContract:
    async def test_skipped_true_when_switch_disabled(self, db_conn, monkeypatch):
        """content_subheading_renumber=False 时校验被跳过 → skipped=True。"""
        from app.config import settings
        monkeypatch.setattr(settings, "content_subheading_renumber", False)
        rep = await validate_section_content_numbering(
            db_conn, "s1", "sec1", "## 1.1 子标题\n\n正文\n")
        assert rep["skipped"] is True
        # 与既有消费方口径一致：视为通过（不产生漂移清单）
        assert rep["consistent"] is True

    async def test_skipped_false_when_switch_enabled(self, db_conn, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "content_subheading_renumber", True)
        rep = await validate_section_content_numbering(db_conn, "s1", "sec1", "")
        assert rep["skipped"] is False
        assert rep["consistent"] is True


# ============================================================
# 4. 章节树兄弟排序确定性（编号漂移防护）
# ============================================================
class TestSectionTreeSiblingOrderDeterminism:
    """sort_order 等值时旧实现的兄弟顺序取决于 SQL 返回顺序 → 编号可能漂移。

    修复：`_build_tree` 统一按 (sort_order, id) 排序（roots 与 children 都排），
    id 是主键 → 等值组也有全序，同一份数据任意次调用得到同一编号。
    """

    async def _base(self, db, rows):
        await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                         ("p1", "项目"))
        await db.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s1", "p1", "方案"))
        for nid, parent, title, so in rows:
            level = 2 if parent else 1
            oj = {"id": "1.1"} if parent else {"id": "1"}
            await db.execute(
                "INSERT INTO sections (id, scheme_id, project_id, parent_id, title,"
                " level, sort_order, status, outline_json, word_budget)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, "s1", "p1", parent, title, level, so, "empty",
                 json.dumps(oj, ensure_ascii=False), 1500))
        await db.commit()

    async def test_tied_root_sort_order_ordered_by_id(self, db_conn):
        # 故意先插 id 大的：若只按 sort_order 排序，结果会随 SQL 顺序变化
        await self._base(db_conn, [("zz", "", "后插", 0), ("aa", "", "先插", 0)])
        from app.routers.sections import _build_tree
        first = [n["id"] for n in await _build_tree(db_conn, "s1")]
        second = [n["id"] for n in await _build_tree(db_conn, "s1")]
        assert first == ["aa", "zz"]
        assert second == first

    async def test_tied_children_sort_order_ordered_by_id(self, db_conn):
        await self._base(db_conn, [("r1", "", "章", 0),
                                   ("c2", "r1", "子二", 0),
                                   ("c1", "r1", "子一", 0)])
        from app.routers.sections import _build_tree
        tree = await _build_tree(db_conn, "s1")
        assert [c["id"] for c in tree[0]["children"]] == ["c1", "c2"]

    async def test_renumber_follows_same_order(self, db_conn):
        """编号重排必须与树顺序同口径：等值 sort_order 时按 id 决定 1/2 号。"""
        await self._base(db_conn, [("zz", "", "B", 0), ("aa", "", "A", 0)])
        from app.routers.sections import _build_tree
        from app.services.numbering import renumber_section_outline_ids
        updates = renumber_section_outline_ids(await _build_tree(db_conn, "s1"))
        # 元组顺序：(outline_json, level, section_id)
        assert [u[2] for u in updates] == ["aa", "zz"]
        assert json.loads(updates[0][0])["id"] == "1"
        assert json.loads(updates[1][0])["id"] == "2"
        assert [u[1] for u in updates] == [1, 1]


# ============================================================
# 5. 结构变更后的批量子标题重规范化：消除 N+1（性能护栏）
# ============================================================
def _count_executes(db):
    """给 aiosqlite 连接套 execute 计数器（确定性计数，不用时间断言避免 flaky）。"""
    state = {"n": 0}
    original = db.execute

    async def counted(*a, **k):
        state["n"] += 1
        return await original(*a, **k)

    db.execute = counted
    return state


_BODY = "\n".join(["### 1.1 施工准备", "本段为正文内容示例。" * 5,
                   "### 2.3 验收要求", "- 要点一", "- 要点二"])


class TestRenormalizeAllSectionContentsPerf:
    """`_renormalize_all_section_contents` 被 5 个结构变更入口调用，必须是 O(1) 查询。

    旧实现逐章调用 `normalize_section_content_subheadings`，每章再查两次库
    （outline_json/level/title + COUNT 子章节）→ 200 章 = 401 次 execute，
    拖拽排序等高频操作每次都付这笔钱。现预取 `load_scheme_section_index`。

    上限取值说明：一次全等量重排（renumber_sections_after_reorder 内
    `_build_tree` 1 次 + executemany 1 次 + `_renormalize_all_section_contents`
    的 2 次）= 5 次 execute + 事务内 UPDATE，故断言 ≤ 6，留 1 次余量。
    """

    async def _seed(self, db, n):
        await db.execute("INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
                         ("p1", "项目"))
        await db.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s1", "p1", "方案"))
        for i in range(n):
            await db.execute(
                "INSERT OR REPLACE INTO sections (id, scheme_id, project_id, title,"
                " level, sort_order, outline_json, content)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f"sec{i}", "s1", "p1", f"第{i + 1}章", 1, i,
                 json.dumps({"id": str(i + 1)}), _BODY))
        await db.commit()

    async def test_query_count_is_constant_not_linear(self, db_conn):
        """200 章的查询数与 10 章相同（修复前会是 2N+1）。"""
        from app.routers.sections import _renormalize_all_section_contents

        await self._seed(db_conn, 10)
        st_small = _count_executes(db_conn)
        await _renormalize_all_section_contents(db_conn, "s1")
        n_small = st_small["n"]

        await self._seed(db_conn, 200)
        st_big = _count_executes(db_conn)
        await _renormalize_all_section_contents(db_conn, "s1")
        n_big = st_big["n"]

        assert n_small <= 6, f"10 章查询数 {n_small} 偏高，疑似 N+1 回归"
        assert n_big <= 6, f"200 章查询数 {n_big} 偏高，疑似 N+1 回归"
        assert n_big == n_small, f"查询数随章数增长（{n_small} → {n_big}），应为 O(1)"

    async def test_behaviour_unchanged_with_index(self, db_conn):
        """批量路径与「不传 index」逐章路径产出逐字节一致（优化不改语义）。"""
        from app.services.numbering import (
            load_scheme_section_index, normalize_section_content_subheadings)

        await self._seed(db_conn, 3)
        a, ca = await normalize_section_content_subheadings(
            db_conn, "s1", "sec1", _BODY)
        b, cb = await normalize_section_content_subheadings(
            db_conn, "s1", "sec1", _BODY,
            index=await load_scheme_section_index(db_conn, "s1"))
        assert a == b and ca == cb

    async def test_still_renormalizes_content(self, db_conn):
        """优化后功能不能丢：子标题编号确实被规范化并落库。

        用二级章（存储编号 2.1）验证：正文里 AI 自写的「### 3.1 子标题」应被
        重算为导出口径「### 1 子标题」（三级去掉章号，只剩相对路径）。
        """
        from app.routers.sections import _renormalize_all_section_contents

        await db_conn.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?,?)",
            ("p1", "项目"))
        await db_conn.execute(
            "INSERT OR IGNORE INTO schemes (id, project_id, name) VALUES (?,?,?)",
            ("s1", "p1", "方案"))
        await db_conn.execute(
            "INSERT OR REPLACE INTO sections (id, scheme_id, project_id, title,"
            " level, sort_order, outline_json, content)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("x1", "s1", "p1", "现场布置", 2, 0,
             json.dumps({"id": "2.1"}), "### 3.1 子标题\n\n正文段落。\n"))
        await db_conn.commit()

        await _renormalize_all_section_contents(db_conn, "s1")
        cur = await db_conn.execute("SELECT content FROM sections WHERE id='x1'")
        row = await cur.fetchone()
        assert "### 3.1 子标题" not in (row["content"] or ""), \
            "子标题编号未被重算，N+1 优化把功能改坏了"
        assert "子标题" in (row["content"] or "")

    async def test_prefetch_failure_falls_back(self, db_conn, monkeypatch):
        """预取失败必须 fail-soft 回退逐章查询，不得抛错阻断结构变更。"""
        import app.services.numbering as num
        from app.routers.sections import _renormalize_all_section_contents

        await self._seed(db_conn, 2)

        async def boom(*a, **k):
            raise RuntimeError("模拟预取失败")

        monkeypatch.setattr(num, "load_scheme_section_index", boom)
        # 不得抛异常即视为 fail-soft 生效（预取失败被内部 except 吞掉后回退逐章）
        await _renormalize_all_section_contents(db_conn, "s1")


# ============================================================
# 6. 前后端剥离编号正则的可执行等价护栏（AGENTS.md §4.10 纪律落地）
# ============================================================
# 前端路径（相对本文件：backend/tests → parents[2] = 仓库根 → frontend/src/pages）
_FRONTEND_PAGE_TS = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
)

# JS 字符串字面量内的转义捕获（详见 TestLeadingNumberRegexFrontendParity._extract）
_U_ESC = re.compile(r"\\u([0-9a-fA-F]{4})")


class TestLeadingNumberRegexFrontendParity:
    """后端 `_STRIP_NUMBER_RE` 与前端 `LEADING_NUMBER_RE` 必须**逐字节等价**。

    背景：同一剥离逻辑在 Python（numbering.py）与 TypeScript
    （SchemeWorkbenchPage.tsx::stripOutlineNumbering）各有一份。只靠两边各自筛
    单测无法发现「改一边忘一边」——两边的新逻辑各自都过。本测试从 pytest 侧
    读取前端源文件、重建 JS 正则，断言：
      1. 分隔符 / CJK 字符类与后端 `_SEP_CLASS` / `_CJK_BOUNDARY` 逐字符相等；
      2. 前端完整正则（还原 JS 字符串转义后）与后端 `_STRIP_NUMBER_RE.pattern`
         字符串相等——任何字符类 / 分支顺序漂移都会在此失败；
      3. 一组高难标题上两端 `re.sub` 逐字节一致（锁定正则引擎行为）。
    """

    @staticmethod
    def _js_value(raw: str) -> str:
        """JS 字符串字面量内文 → 运行时值（本文件全部转义都是 `\\\\x`）。"""
        return raw.replace("\\\\", "\\")

    @staticmethod
    def _decode(pattern: str) -> str:
        """把正则源码里的 `\\uXXXX` 解码为实际字符（消除转写差异）。"""
        return _U_ESC.sub(lambda m: chr(int(m.group(1), 16)), pattern)

    def _extract(self, ts: str):
        def const(name: str) -> str:
            m = re.search(rf"const {re.escape(name)}\s*=\s*\"([^\"]*)\"", ts)
            assert m, f"TS 源文件找不到常量 {name}"
            return self._js_value(m.group(1))

        sep = const("LEADING_NUMBER_SEP")
        cjk = const("LEADING_NUMBER_CJK")
        consts = {"LEADING_NUMBER_SEP": sep, "LEADING_NUMBER_CJK": cjk}
        m = re.search(r"const LEADING_NUMBER_RE = new RegExp\((.*?)\);", ts, re.S)
        assert m, "TS 源文件找不到 LEADING_NUMBER_RE"
        # 先剔除块内行尾注释（注释里有 "1 施工准备" 这类带引号的示例文案，
        # 不剔除会被当成字符串字面量混入重建结果）
        block = re.sub(r"//[^\r\n]*", "", m.group(1))
        # 块内 token = 字符串字面量 | 直接引用的常量名；按源码顺序还原 JS 运行时串
        src = "".join(
            self._js_value(lit) if lit else consts.get(ident, "")
            for lit, ident in re.findall(
                r'"((?:\\.|[^"\\])*)"|([A-Za-z_][A-Za-z0-9_]*)', block))
        return sep, cjk, src

    def test_char_classes_match_backend_constants(self):
        if not _FRONTEND_PAGE_TS.exists():
            pytest.skip("前端源文件缺失，跳过跨语言 parity")
        ts = _FRONTEND_PAGE_TS.read_text(encoding="utf-8")
        sep, cjk, _ = self._extract(ts)
        from app.services.numbering import _SEP_CLASS, _CJK_BOUNDARY
        assert sep == _SEP_CLASS, f"前端 SEP={sep!r} ≠ 后端 _SEP_CLASS={_SEP_CLASS!r}"
        assert self._decode(cjk) == self._decode(_CJK_BOUNDARY), \
            f"前端 CJK={cjk!r} ≠ 后端 _CJK_BOUNDARY={_CJK_BOUNDARY!r}"

    def test_full_regex_matches_backend_source(self):
        if not _FRONTEND_PAGE_TS.exists():
            pytest.skip("前端源文件缺失，跳过跨语言 parity")
        ts = _FRONTEND_PAGE_TS.read_text(encoding="utf-8")
        _, _, src = self._extract(ts)
        from app.services.numbering import _STRIP_NUMBER_RE
        assert self._decode(src) == self._decode(_STRIP_NUMBER_RE.pattern), (
            "前端 LEADING_NUMBER_RE 与后端 _STRIP_NUMBER_RE 已漂移。"
            "规则：改 numbering.py::_STRIP_NUMBER_RE 必须同步改前端"
            " LEADING_NUMBER_RE（字符类与分支顺序逐字符一致）。")

    def test_strip_behavior_battery_parity(self):
        if not _FRONTEND_PAGE_TS.exists():
            pytest.skip("前端源文件缺失，跳过跨语言 parity")
        ts = _FRONTEND_PAGE_TS.read_text(encoding="utf-8")
        _, _, src = self._extract(ts)
        from app.services.numbering import _STRIP_NUMBER_RE
        js_re = re.compile(self._decode(src))
        battery = [
            "1.2.3（1）细部构造", "2.4.1钢筋工程", "3.2.4.5钢筋",
            "1.2.3（1）", "5.1.1.1 深基坑", "2.1 相关法律法规", "1.0 零级",
            "第 3 章 施工计划", "第 一 章 工程概况", "1 施工准备",
            "3 资源配置", "1）施工工艺", "一、安全保证", "十二、安全保证",
            "1.5m 深", "2023 年度安全生产计划", "2024年施工计划",
            "2023年规范", "表2-1 材料表", "图3-2 布置图", "3D打印施工方案",
            "1.1", "1.2.3", "1.2.3.4.5.6.7.8", "第一章", "",
            " 2.1 ", "9月施工计划", "0.5t 平台", "1、施工", "（三）施工组织",
            "第一章 第一章 工程概况", "ⅰ. 总则", "a. 施工部署", "第二节 材料",
        ]
        for t in battery:
            a = _STRIP_NUMBER_RE.sub("", t, count=1)
            b = js_re.sub("", t, count=1)
            assert a == b, f"标题 {t!r}：后端剥出 {a!r} ≠ 前端 {b!r}"
