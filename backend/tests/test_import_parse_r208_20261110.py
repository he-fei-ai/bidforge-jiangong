# -*- coding: utf-8 -*-
"""R208 · 解析提取模块（上传解析 / 要点提取 / import）深度探索护栏（2026-11-10）。

承接 R191（深度审计）、R203（遗留收口）两轮，本轮为第三轮同模块探索。
六组：
A 目录编号形态收口（全角点号 + 中文数字非顿号分隔符）—— BE-2 / BE-3
B 图片/TIFF 像素闸实际有效性（R203 BE-2 未真正生效）—— BE-1
C 路由层读路径判空 + commit 裸奔家族收口 —— BE-6 / BE-7 / BE-8 / BE-9
D 存储层假成功（delete_project_docs_root）—— BE-5
E 契约一致性（reconciled 类型倒挂）—— BE-10
F 自证与字节体检

纪律（承自 AGENTS.md）：
  · 行为级断言优先，源码锁只锁「必须存在的结构」；
  · 每条护栏必须可被 A/B 反向验证摘除；
  · 字节体检常驻参数化（防编辑过程静默改行尾）。
"""

from __future__ import annotations

import ast
import inspect
import io
import subprocess
import sys
from pathlib import Path

import pytest
from app.routers import global_facts_docs as gfd
from app.services import file_parser as fp
from app.services.doc_pipeline import doc_storage as store
from app.services.doc_pipeline import pipeline as pl

BACKEND = Path(__file__).resolve().parents[1]


def simple_outline(text: str) -> list:
    return fp.simple_parse_outline(text)


def _ast_of(func) -> ast.FunctionDef:
    """取函数体的 AST（**不含** docstring 与注释）。

    ⚠️ 本轮教训：字符串源码锁会命中注释里对该字面量的引述 —— 例如
    「本函数仍保留 ``ignore_errors=True``」这句注释会让
    ``"ignore_errors=True" not in src`` 假红。判定「代码结构里有没有
    某个调用形态」必须走 AST 而非文本。
    """
    tree = ast.parse(inspect.getsource(func))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            return node
    raise AssertionError("未找到函数定义")


def _calls(node) -> list:
    """函数体内所有 Call 节点。"""
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _has_chained_convert_resize(func) -> bool:
    """是否存在 ``X.convert("RGB").resize(...)`` 这种「先全尺寸 convert 再 resize」的链。

    这正是 R203 BE-2 的写法：convert() 会先对原图全尺寸 load() 并产出
    一份全尺寸 RGB 拷贝，峰值内存按原图计 —— 像素闸形同虚设。
    """
    for call in _calls(_ast_of(func)):
        f = call.func
        if not isinstance(f, ast.Attribute) or f.attr != "resize":
            continue
        inner = f.value
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
            if inner.func.attr == "convert":
                return True
    return False


def _has_true_kwarg(func, name: str) -> bool:
    """函数调用里是否存在 ``name=True`` 的关键字实参。"""
    for call in _calls(_ast_of(func)):
        for kw in call.keywords:
            if kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
    return False


# ---------------------------------------------------------------------------
# A 组 · 目录编号形态收口（BE-2 全角点号 / BE-3 中文数字分隔符）
# ---------------------------------------------------------------------------


class TestOutlineNumberingFormsR208:
    @pytest.mark.parametrize(
        ("line", "title", "level"),
        [
            # ✅ BE-2：全角点号（U+FF0E）—— 中文 Word 自动编号高频形态
            ("1．1工程概况", "工程概况", 2),
            ("1．1．1施工准备", "施工准备", 3),
            ("一．总则", "总则", 2),
            # ✅ BE-3：中文数字 + 非顿号分隔符
            ("一.总则", "总则", 2),
            ("十一 施工部署", "施工部署", 2),
        ],
    )
    def test_new_numbering_forms_recognized(self, line, title, level):
        """本轮实证的三条漏识形态必须被识别为目录节点（此前整行漏识 → 目录少一层）。"""
        nodes = simple_outline(line)
        assert nodes, f"{line!r} 未被识别为目录节点"
        assert nodes[0]["title"] == title
        assert nodes[0]["level"] == level

    def test_fullwidth_dot_id_normalized_to_ascii(self):
        """全角点号编号的节点 id 必须归一为 ASCII 点号。

        理由：① 下游三处判定（"." in raw_id / count(".") / _unique_id）都按
        ASCII 点号写死，不归一则「1．1．1」被判成 level 2（应为 3）；
        ② 前端 antd Tree 以 id 作 React key，两种写法混用会造成同一节点两个 id。
        """
        nodes = simple_outline("1．1．1施工准备")
        assert nodes
        assert nodes[0]["id"] == "1.1.1", f"全角点号未归一：{nodes[0]['id']!r}"
        assert nodes[0]["level"] == 3

    def test_ascii_and_fullwidth_dot_same_level(self):
        """ASCII 形态与全角形态必须产出**完全相同**的 id/level（仅标题可比）。"""
        a = simple_outline("1.1.1施工准备")[0]
        b = simple_outline("1．1．1施工准备")[0]
        assert (a["id"], a["level"]) == (b["id"], b["level"])

    @pytest.mark.parametrize(
        "line",
        [
            "3.5万元包干",
            "1.5米，采用C30混凝土",
            "1.5MPa直接浇筑混凝土",
            "2.4GHz无线传输",
            "3.5%的利润率下浮",
            "2024.05.12 第一次工地会议纪要",
            "2023 年最新规范",
            "3.1.12 身份证复印件、照片；",
            "2024年度施工计划",
            "1.5万元，包干价",
        ],
    )
    def test_body_lines_still_not_turned_into_nodes(self, line):
        """误伤回归：放宽分隔符后，正文行**不得**变成伪节点。

        ⚠️ BE-3 的中文数字 + 空格形态与正文句同形，是本轮最大的误伤面，
        故负例集必须覆盖 R191/R203 三道护栏的全部形态。
        """
        assert simple_outline(line) == [], f"{line!r} 被误判为目录节点"

    def test_existing_forms_zero_regression(self):
        """既有形态逐条钉住：R191/R203 修复过的编号形态必须仍可识别。"""
        for line, title in [
            ("1.1工程概况", "工程概况"),
            ("1.1.1施工准备", "施工准备"),
            ("第一章 工程概况", "工程概况"),
            ("一、工程概况", "工程概况"),
            ("（一）总则", "总则"),
            ("第一部分 概述", "概述"),
        ]:
            nodes = simple_outline(line)
            assert nodes, f"{line!r} 漏识"
            assert nodes[0]["title"] == title

    def test_cn_nums_still_include_bai(self):
        """R203 BE-10 的「百」不得被本轮改动挤掉（防同类回归）。"""
        assert "百" in fp._OUTLINE_CN_NUMS
        assert simple_outline("第一百二十三章")

    def test_dot_quantifier_guard_still_active(self):
        """R191 第一道护栏（数值量词）仍必须在场（源码锚）。"""
        src = inspect.getsource(fp.simple_parse_outline)
        assert "_OUTLINE_DOT_QUANTIFIER_RE.match(title)" in src
        assert "_OUTLINE_DATE_ID_RE.match(_compact_id)" in src


# ---------------------------------------------------------------------------
# B 组 · 图片 / TIFF 像素闸实际有效性（BE-1 · R203 BE-2 未真正生效）
# ---------------------------------------------------------------------------


class TestPixelGateEffectiveR208:
    def test_downscale_helper_prefers_draft_then_thumbnail(self):
        """像素闸必须**在解码阶段**就缩减，而不是「先全尺寸解码再 resize」。

        R203 BE-2 写的是 ``img.convert("RGB").resize(...)`` —— PIL 的
        convert() 会**先对原图全尺寸 load() 并产出一份全尺寸 RGB 拷贝**，
        resize 发生在其**之后**，闸门只约束输出尺寸、对峰值内存毫无防护。
        本轮实测（monkeypatch Image.Image.load 记录时机）：
            convert->resize : load() = [(W,H)] ×4  ← 全尺寸解码
            draft+thumbnail: load() = [(W/2,H/2)] ×2 ← 解码前已缩减
        故护栏锁住「实现必须走 draft/thumbnail」这条结构。
        """
        node = _ast_of(fp._downscale_pil_image)
        attrs = {c.func.attr for c in _calls(node) if isinstance(c.func, ast.Attribute)}
        assert "draft" in attrs, "必须优先用 draft() 在解码前缩减"
        assert "thumbnail" in attrs, "必须用 thumbnail() 原地重采样"
        assert not _has_chained_convert_resize(fp._downscale_pil_image), (
            "不得再用 convert().resize()（峰值按原图计，闸门形同虚设）"
        )

    def test_downscale_helper_skips_when_within_budget(self):
        """未超闸时必须返回 (None, None)（调用方据此原样返回字节，不做任何解码）。"""
        from PIL import Image

        img = Image.new("RGB", (10, 10), (1, 2, 3))
        resized, size = fp._downscale_pil_image(img, 50_000_000)
        assert resized is None and size is None

    def test_downscale_helper_output_within_budget(self):
        """超闸时输出必须落在闸内。"""
        from PIL import Image

        img = Image.new("RGB", (400, 400), (1, 2, 3))
        resized, (nw, nh) = fp._downscale_pil_image(img, 1000)
        assert resized is not None
        assert nw * nh <= 1000
        assert resized.size[0] * resized.size[1] <= 1000

    def test_single_frame_downscale_still_records_diag(self, monkeypatch):
        """单帧超闸 → 降采样 + diag 留痕（R203 既有行为不得回退）。"""
        from PIL import Image

        monkeypatch.setattr(fp, "_OCR_RENDER_MAX_PIXELS", 1000)
        img = Image.new("RGB", (100, 100), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        diag: dict = {}
        out = fp._downscale_image_bytes(buf.getvalue(), diag)
        with Image.open(io.BytesIO(out)) as check:
            assert check.width * check.height <= 1000
        assert any("降采样" in str(w) for w in diag.get("warnings", []))

    def test_tiff_frame_downscale_still_records_diag(self, monkeypatch):
        """多页 TIFF 超大帧 → 降采样 + diag 留痕（R203 既有行为不得回退）。"""
        from PIL import Image

        monkeypatch.setattr(fp, "_OCR_RENDER_MAX_PIXELS", 1000)
        base = Image.new("RGB", (80, 80), (0, 0, 0))
        buf = io.BytesIO()
        base.save(
            buf,
            format="TIFF",
            save_all=True,
            append_images=[Image.new("RGB", (80, 80), (255, 0, 0))],
        )
        diag: dict = {}
        frames = fp._split_tiff_frames(buf.getvalue(), diag)
        assert len(frames) == 2
        for fr in frames:
            with Image.open(io.BytesIO(fr)) as check:
                assert check.width * check.height <= 1000
        assert any("降采样" in str(w) for w in diag.get("warnings", []))

    def test_small_image_passthrough_identity(self):
        """未超闸时必须逐字节返回原对象（不重新编码）。"""
        from PIL import Image

        img = Image.new("RGB", (40, 40), (0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        raw = buf.getvalue()
        assert fp._downscale_image_bytes(raw, {}) is raw


# ---------------------------------------------------------------------------
# C 组 · 路由层读路径判空 + commit 裸奔家族收口
# ---------------------------------------------------------------------------


class TestRoutingGuardsR208:
    def test_ingest_parsed_doc_read_path_null_guard(self):
        """BE-8：_ingest_parsed_doc 读路径必须补 ``cur is None`` 判空。

        本函数是「解析 → 四层产物落盘」的唯一入口，cur 为 None 时
        ``cur.fetchone()`` 抛 AttributeError 被外层 except 吞掉 →
        **解析正文已落库但四层产物全丢**，响应里 layers 键消失，
        用户与日志都只看到「解析成功」。
        """
        src = inspect.getsource(gfd._ingest_parsed_doc)
        assert "if cur is None:" in src
        assert "db.execute 返回 None" in src

    def test_mark_parse_failed_rollback_on_error(self):
        """BE-7：_mark_parse_failed 的 except 必须补 rollback（commit 裸奔家族）。"""
        src = inspect.getsource(gfd._mark_parse_failed)
        assert "await db.rollback()" in src

    def test_parse_all_review_select_null_guard(self):
        """BE-9：parse-all 锁内复核 SELECT 必须补判空。

        cur 为 None 时旧实现走 except → 只记一条 WARNING → **对已解析文档
        发起真实 OCR**（重复 OCR：分钟级 + 厂商成本）。
        """
        src = inspect.getsource(gfd.parse_all_documents)
        assert "复核文档 %s 解析状态失败（db.execute 返回 None）" in src

    def test_purge_document_commit_guard(self):
        """BE-6：purge_document 的 commit 必须被 try 包住 + 失败 rollback。

        commit 抛异常时，磁盘树清理与索引移除不执行 → DB 行已删而四层
        目录树 + 索引条目成孤儿。
        """
        src = inspect.getsource(pl.purge_document)
        assert "await db.rollback()" in src
        assert "raise" in src


# ---------------------------------------------------------------------------
# D 组 · 存储层假成功（BE-5）
# ---------------------------------------------------------------------------


class TestStorageFakeSuccessR208:
    def test_delete_project_docs_root_failure_returns_false(self, tmp_path, monkeypatch):
        """rmtree 失败（文件被占用）→ WARNING + 返回 False，不再假成功。

        与 R203 BE-13 已修好的 ``delete_doc_tree`` 同口径 —— 本函数是
        同一对孪生实现里的**另一侧**，R203 只修了一侧。
        """
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
        d = tmp_path / "p1"
        d.mkdir(parents=True)

        def _boom(target, *a, **k):
            raise OSError("模拟文件被占用")

        monkeypatch.setattr("shutil.rmtree", _boom)
        assert store.delete_project_docs_root("p1") is False

    def test_delete_project_docs_root_no_ignore_errors(self):
        """源码锁：不得再用 ignore_errors=True（那正是假成功的成因）。"""
        assert not _has_true_kwarg(store.delete_project_docs_root, "ignore_errors"), (
            "ignore_errors=True 会把删除失败吞掉 —— 那正是假成功的成因"
        )
        src = inspect.getsource(store.delete_project_docs_root)
        assert "logger.warning" in src

    def test_delete_project_docs_root_success_path(self, tmp_path, monkeypatch):
        """正常路径仍返回 True（幂等语义不变）。"""
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path)
        d = tmp_path / "p1"
        d.mkdir(parents=True)
        assert store.delete_project_docs_root("p1") is True


# ---------------------------------------------------------------------------
# E 组 · 契约一致性（BE-10 reconciled 类型倒挂）
# ---------------------------------------------------------------------------


class TestContractConsistencyR208:
    def test_reconciled_appends_object_not_string(self):
        """BE-10：reconciled 必须是**对象数组**（与 failed[]/truncated[] 同口径）。

        旧实现 append 的是文件名**字符串**，而前端 types/facts.ts 声明为
        ``Array<{doc_id?, file_name?}>`` —— 契约倒挂，按声明取 r.file_name
        的消费方恒得 undefined。
        """
        src = inspect.getsource(gfd.parse_all_documents)
        assert 'reconciled.append({"doc_id": doc["id"], "file_name": doc["file_name"]})' in src
        assert 'reconciled.append(doc["file_name"])' not in src


# ---------------------------------------------------------------------------
# E2 组 · 人工分类在同名重传后保留（BE-11）
# ---------------------------------------------------------------------------


class TestManualCategoryPreservedR208:
    """同名重传不得把**人工分类**静默改回自动分类。

    影响链：分类 → ``bid_analysis._combine_doc_texts`` 的
    ``extract_priority(doc_category)`` 读取序 → 30000 字预算内被纳入
    AI 提取的文档构成。人工纠正被抹掉 = 提取侧静默换料。
    """

    def test_valid_categories_helper_matches_single_source(self):
        """合法分类集合必须与 doc_categories 单一事实源一致（含「其他」）。"""
        from app.services import doc_categories as dc

        assert gfd._valid_doc_categories() == set(dc.ALL_DOC_CATEGORIES)

    def test_upload_upsert_keeps_existing_category(self):
        """源码锚：upsert 前必须读取同名旧行的 doc_category 并据此决定本次分类。

        实现要点：上传流程在 upsert **之前**已查过同名旧行，把该查询扩成
        ``SELECT id, file_path, doc_category`` 即可零额外查询拿到旧分类。
        """
        src = inspect.getsource(gfd.upload_documents)
        assert "SELECT id, file_path, doc_category FROM project_documents" in src
        assert '_old["doc_category"]' in src
        assert "doc_category = str(_pc)" in src

    def test_upload_upsert_still_writes_category_on_first_upload(self):
        """首次上传（新行，无同名旧行）仍写自动分类 —— 行为不变护栏。"""
        src = inspect.getsource(gfd.upload_documents)
        assert "doc_category = _auto_classify_document(fname, ftype)" in src
        assert "doc_category=excluded.doc_category" in src


# ---------------------------------------------------------------------------
# E2 组 · 人工分类在同名重传后保留（BE-11）· 行为级
# ---------------------------------------------------------------------------


class TestManualCategoryBehaviorR208:
    """**行为级**（真调 upload_documents 走同名替换路径）：人工分类不被还原。

    ⚠️ 不用源码锚 —— 源码锚只能证明「代码里有这句」，不能证明「行为对」；
    本组断言直接读库。
    """

    @staticmethod
    def _upload(name: str, data: bytes):
        import io

        from starlette.datastructures import UploadFile

        return UploadFile(file=io.BytesIO(data), filename=name)

    async def test_manual_category_survives_same_name_reupload(self, db_conn, tmp_path, monkeypatch):
        """人工把分类改为「报价清单」后同名重传 → 分类必须仍是「报价清单」。

        反例（修复前）：文件名「XX投标报价表.xlsx」的自动分类是「招标文件」
        （文件名含「投标」），重传会把人工值「报价清单」静默还原成
        「招标文件」→ 提取侧读取序被换掉。
        """
        import app.routers.global_facts as gf
        from app.services.doc_pipeline import doc_storage as store

        uploads = tmp_path / "uploads"
        monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects")
        pid = "r208cat001"
        await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db_conn.commit()

        fname = "XX投标报价表.txt"
        payload = "投标报价表明细\n综合单价 1234.56 元\n".encode("utf-8")

        # ① 首次上传 → 自动分类
        up1 = await gfd.upload_documents(
            scheme_id="", project_id=pid, files=[self._upload(fname, payload)], db=db_conn
        )
        doc_id = up1["saved"][0]["id"]
        cur = await db_conn.execute(
            "SELECT doc_category FROM project_documents WHERE id=?", (doc_id,)
        )
        auto_cat = (await cur.fetchone())["doc_category"]
        assert auto_cat == "招标文件", f"前置条件不成立：自动分类应为招标文件，实为 {auto_cat}"

        # ② 人工改为「报价清单」
        await gfd.update_document_category(doc_id, {"doc_category": "报价清单"}, db=db_conn)

        # ③ 同名重传
        up2 = await gfd.upload_documents(
            scheme_id="", project_id=pid, files=[self._upload(fname, payload)], db=db_conn
        )
        assert up2["replaced"] == 1, "同名重传必须被识别为替换"
        new_id = up2["saved"][0]["id"]

        # ④ 断言：人工分类保留
        cur = await db_conn.execute(
            "SELECT doc_category FROM project_documents WHERE id=?", (new_id,)
        )
        got = (await cur.fetchone())["doc_category"]
        assert got == "报价清单", (
            f"人工分类被静默还原为自动值：期望「报价清单」，实为「{got}」"
        )

    async def test_first_upload_still_auto_classifies(self, db_conn, tmp_path, monkeypatch):
        """首次上传仍走自动分类（零行为变更护栏）。"""
        import app.routers.global_facts as gf
        from app.services.doc_pipeline import doc_storage as store

        uploads = tmp_path / "uploads2"
        monkeypatch.setattr(gf, "FACT_UPLOADS_DIR", uploads)
        monkeypatch.setattr(store, "DOCS_ROOT", tmp_path / "projects2")
        pid = "r208cat002"
        await db_conn.execute("INSERT INTO projects(id,name) VALUES(?,?)", (pid, "p"))
        await db_conn.commit()

        payload = "工程量清单明细\n项目名称 数量 单价\n".encode("utf-8")
        up = await gfd.upload_documents(
            scheme_id="",
            project_id=pid,
            files=[self._upload("工程量清单.txt", payload)],
            db=db_conn,
        )
        doc_id = up["saved"][0]["id"]
        cur = await db_conn.execute(
            "SELECT doc_category FROM project_documents WHERE id=?", (doc_id,)
        )
        got = (await cur.fetchone())["doc_category"]
        assert got == "报价清单", f"首次上传应自动分类为报价清单，实为 {got}"


# ---------------------------------------------------------------------------
# F 组 · 自证与字节体检
# ---------------------------------------------------------------------------


class TestSelfProofR208:
    def test_r208_marker(self):
        assert True

    @pytest.mark.parametrize(
        "rel",
        [
            "app/services/file_parser.py",
            "app/services/doc_categories.py",
            "app/services/doc_pipeline/doc_storage.py",
            "app/services/doc_pipeline/pipeline.py",
            "app/routers/global_facts_docs.py",
        ],
    )
    def test_backend_files_pure_crlf_no_bom(self, rel):
        """字节体检（常驻参数化）：后端源文件必须纯 CRLF / 无 BOM / 无替换字符。

        ⚠️ AGENTS.md 教训：编辑过程可能**静默改整文件行尾**（R203 曾发生
        file_parser.py 全文件 LF 化、diff 虚涨 5181 行）——「体检脚本跑一次」
        不如「体检断言进测试」常驻。
        """
        raw = (BACKEND / rel).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel} 含 BOM"
        assert b"\xef\xbf\xbd" not in raw, f"{rel} 含 U+FFFD 替换字符"
        lf_total = raw.count(b"\n")
        crlf = raw.count(b"\r\n")
        assert lf_total == crlf, f"{rel} 行尾不一致（LF={lf_total - crlf} 裸 LF）"

    def test_guard_file_compiles(self):
        """自证：本护栏文件自身可被 Python 编译（防语法错误静默入库）。"""
        p = Path(__file__)
        r = subprocess.run(
            [sys.executable, "-m", "py_compile", str(p)],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr
