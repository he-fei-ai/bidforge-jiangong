"""目录写路径「落库前自动重写占位标题」的**单一实现 + 上传入口补接线**（R101）。

背景（本轮探针核对）：
  · R89 的自动外科式重写**只**内联在 `sections._save_outline_to_db`，
    而 `upload_outline.save_as_outline` 自建 DELETE/INSERT、**不走**该函数
    ⇒ 上传识别入口零闸门。这与 `config.outline_autofix_generic_on_save`
    注释声明的「目录库套用 / 上传识别 / AI 调整 / 手工编辑后保存」四类写入口
    全覆盖**不符**（BUG-1）。
  · `generic_autofixed` 在 `frontend/src` 零命中 ⇒ 自动改写**静默生效**，
    用户以为目录原样保存，实际章节名已变（BUG-2，前端侧）。

本文件锁定修复后的四条契约：
  ① 上传路径真的接上了自动重写（标题被改写并落库、`generic_autofixed` 回传）；
  ② **加法式**：开关置 False / 无残留时，响应一个键都不多（向后兼容红线）；
  ③ **fail-soft**：重写抛异常时保存照常成功、标题原样、只回传 residual；
  ④ 两条写路径共用**同一实现**（源码级锁，防再次分叉出第二份内联逻辑）。
"""

from __future__ import annotations

import copy
import inspect
import uuid

import pytest
from app.config import settings
from app.services import outline_title_fix as O

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

_RAW_TREE = [
    {
        "title": "第一章 工程概况",
        "children": [
            {"title": "专项6", "children": []},
        ],
    },
]


async def _seed(db) -> tuple[str, str]:
    pid = "proj_" + uuid.uuid4().hex[:8]
    sid = uuid.uuid4().hex[:8]
    rid = uuid.uuid4().hex[:8]
    await db.execute("INSERT INTO projects (id, name) VALUES (?,?)", (pid, "p"))
    await db.execute("INSERT INTO schemes (id, project_id, name) VALUES (?,?,?)", (sid, pid, "s"))
    await db.execute(
        "INSERT INTO uploaded_outlines (id, project_id, file_name, status)" " VALUES (?,?,?,?)",
        (rid, pid, "x.txt", "parsed"),
    )
    await db.commit()
    return pid, sid + "|" + rid


def _rename(tree: list, old: str, new: str) -> None:
    for n in tree:
        if not isinstance(n, dict):
            continue
        if str(n.get("title") or "").strip() == old:
            n["title"] = new
        _rename(n.get("children") or [], old, new)


@pytest.fixture
def _autofix_on(monkeypatch):
    monkeypatch.setattr(settings, "outline_autofix_generic_on_save", True, raising=False)
    monkeypatch.setattr(settings, "outline_check_generic_titles", True, raising=False)


@pytest.fixture
def _autofix_off(monkeypatch):
    monkeypatch.setattr(settings, "outline_autofix_generic_on_save", False, raising=False)


class _FixedTracker:
    """假 `_try_outline_title_fix`：把占位标题改成具体名，并记录调用次数。"""

    def __init__(self, old="专项6", new="基坑支护施工", exc=None):
        self.calls = 0
        self.old, self.new, self.exc = old, new, exc

    async def __call__(self, outline, hits, **_kw):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        fixed = copy.deepcopy(outline)
        _rename(fixed, self.old, self.new)
        return fixed


async def _section_titles(db, sid: str) -> list[str]:
    cur = await db.execute("SELECT title FROM sections WHERE scheme_id=?", (sid,))
    return [str(r["title"]) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# ① 上传路径接线
# ---------------------------------------------------------------------------


class TestUploadPathWiresAutofix:
    async def test_autofix_applied_and_reported(self, db_conn, _autofix_on, monkeypatch):
        from app.routers import upload_outline as uo

        tracker = _FixedTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        __pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": copy.deepcopy(_RAW_TREE)}, db=db_conn)

        assert tracker.calls == 1, "上传路径必须真的调用自动重写（修复前为 0）"
        assert res["ok"] is True
        assert res.get("generic_autofixed") == 1, "改写成功后必须加法式回传条数（否则自动改写对用户静默）"
        assert "generic_residual" not in res, "残留已清零 ⇒ 不得再多回该键"
        assert "基坑支护施工" in await _section_titles(db_conn, sid), "改写后的标题必须真的落库"

    async def test_no_residual_means_no_extra_key(self, db_conn, _autofix_on, monkeypatch):
        """干净目录：零 AI 调用、响应不多任何键（成本与兼容红线）。"""
        from app.routers import upload_outline as uo

        tracker = _FixedTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        __pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")
        clean = [{"title": "工程概况", "children": [{"title": "周边环境与施工条件", "children": []}]}]

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": clean}, db=db_conn)

        assert tracker.calls == 0, "检出为空 ⇒ 必须零 AI 调用"
        assert "generic_autofixed" not in res
        assert "generic_residual" not in res


# ---------------------------------------------------------------------------
# ② 加法式 / 开关回退
# ---------------------------------------------------------------------------


class TestUploadPathAdditive:
    async def test_flag_off_keeps_original_and_residual_only(self, db_conn, _autofix_off, monkeypatch):
        from app.routers import upload_outline as uo

        tracker = _FixedTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        __pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": copy.deepcopy(_RAW_TREE)}, db=db_conn)

        assert tracker.calls == 0, "开关关闭即逐字回到 R89 之前：不发起重写"
        assert res["ok"] is True
        assert "generic_autofixed" not in res, "开关关闭时不得多回键（加法式向后兼容）"
        assert res.get("generic_residual"), "只检出、只回传的旧口径必须保留"
        assert "专项6" in await _section_titles(db_conn, sid), "标题必须原样落库"


# ---------------------------------------------------------------------------
# ③ fail-soft
# ---------------------------------------------------------------------------


class TestUploadPathFailSoft:
    async def test_exception_does_not_block_save(self, db_conn, _autofix_on, monkeypatch):
        from app.routers import upload_outline as uo

        tracker = _FixedTracker(exc=RuntimeError("模型超时"))
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        __pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": copy.deepcopy(_RAW_TREE)}, db=db_conn)

        assert res["ok"] is True, "自动修复失败绝不阻断保存"
        assert "generic_autofixed" not in res
        assert res.get("generic_residual"), "残留照旧回传供人工核对"
        assert "专项6" in await _section_titles(db_conn, sid), "标题原样落库"


# ---------------------------------------------------------------------------
# ④ 单一实现 + 共用锁
# ---------------------------------------------------------------------------


class TestSingleImplementation:
    async def test_shared_fn_skips_empty_hits(self, monkeypatch):
        tracker = _FixedTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        tree = [{"title": "工程概况", "children": []}]
        out, hits, n = await O.autofix_generic_titles_on_save(tree, [])
        assert (out, hits, n) == (tree, [], 0)
        assert tracker.calls == 0

    async def test_shared_fn_respects_flag(self, _autofix_off, monkeypatch):
        tracker = _FixedTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        tree = [{"title": "专项6", "children": []}]
        hits = [{"path": "1", "title": "专项6", "kind": "generic"}]
        out, out_hits, n = await O.autofix_generic_titles_on_save(tree, hits)
        assert (out, out_hits, n) == (tree, hits, 0)
        assert tracker.calls == 0

    async def test_both_write_paths_share_the_single_impl(self):
        """源码级锁：两条写路径都委派同一实现，防再次分叉出第二份内联逻辑。"""
        from app.routers import sections
        from app.routers import upload_outline as uo

        save_src = inspect.getsource(sections._save_outline_to_db)
        assert "autofix_generic_titles_on_save" in save_src
        up_src = inspect.getsource(uo.save_as_outline)
        assert "autofix_generic_titles_on_save" in up_src, "上传路径必须与 save-outline 共用同一实现（R101 修复的核心）"
        assert "detect_generic_titles_for_write" in up_src

    async def test_structure_mismatch_discards_fix(self, _autofix_on, monkeypatch):
        """结构指纹守卫：改写结果增删了节点 ⇒ 整条丢弃。

        否则 upload 路径「按子节点索引路径还原改写前标题」会整体错位，
        把正文张冠李戴到别的章节上 —— 比丢失更糟。
        """

        class _BadTracker:
            calls = 0

            async def __call__(self, outline, hits, **_kw):
                _BadTracker.calls += 1
                bad = copy.deepcopy(outline)
                for n in bad:
                    if isinstance(n, dict) and (n.get("children") or []):
                        n["children"].append({"title": "凭空多出的节点", "children": []})
                        break
                return bad

        blob = _BadTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", blob)
        tree = [{"title": "工程概况", "children": [{"title": "专项6", "children": []}]}]
        hits = [{"path": "1.1", "title": "专项6", "kind": "generic"}]

        out, out_hits, n = await O.autofix_generic_titles_on_save(tree, hits)

        assert blob.calls == 1
        assert (out, out_hits, n) == (tree, hits, 0), "结构错位的改写结果必须整条丢弃、按原样保存"


# ---------------------------------------------------------------------------
# ⑤ 遗留问题 1：自动改写标题**不得**再导致正文丢失
# ---------------------------------------------------------------------------


class TestContentPreservedAcrossAutofix:
    """修复前：自动改写把「专项6」改成具体名后，新标题在旧库找不到同名行，
    匹配落空 ⇒ 旧章节进 to_delete 被级联删除、已生成正文丢失（只计入
    cleared_content_sections 如实回报，救不回来）。

    修复后：正文匹配身份改用「**改写前的原标题**」（按子节点索引路径快照），
    与展示/落库标题解耦 ⇒ 标题换成具体名、正文原样保留。
    """

    async def test_content_survives_rename(self, db_conn, _autofix_on, monkeypatch):
        from app.routers import upload_outline as uo
        from app.services.outline_utils import normalize_outline

        # 以**归一化后**的真实标题为基准，避免测试依赖「剥不剥编号」的实现细节
        norm = normalize_outline(copy.deepcopy(_RAW_TREE))
        old_title = str(norm[0]["children"][0]["title"])
        tracker = _FixedTracker(old=old_title, new="基坑支护施工")
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)

        _pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('s1',?,?,?,?,'2',0)",
            (sid, _pid, old_title, "已生成正文"),
        )
        await db_conn.commit()

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": copy.deepcopy(_RAW_TREE)}, db=db_conn)

        assert tracker.calls == 1
        assert res.get("generic_autofixed") == 1
        assert res.get("preserved_content") == 1, "改写标题后必须仍按「改写前标题」命中旧章节（遗留问题 1 的核心）"
        assert "cleared_content_sections" not in res, "不得再丢正文"
        cur = await db_conn.execute("SELECT title, content FROM sections WHERE id='s1'")
        row = await cur.fetchone()
        assert row["title"] == "基坑支护施工", "落库标题应为改写后的具体名"
        assert row["content"] == "已生成正文", "正文必须完整保留"

    async def test_unmatched_placeholder_still_inserted(self, db_conn, _autofix_on, monkeypatch):
        """反向：本次新引入的占位标题在旧库无同名行 ⇒ 不得被误保留。"""
        from app.routers import upload_outline as uo

        tracker = _FixedTracker()
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)
        _pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": copy.deepcopy(_RAW_TREE)}, db=db_conn)

        assert res.get("preserved_content") in (None, 0), "旧库无同名章节可保留"
        assert res.get("generic_autofixed") == 1

    async def test_no_rewrite_keeps_legacy_title_matching(self, db_conn, _autofix_off, monkeypatch):
        """开关关闭（未改写）时，匹配身份与标题同源 ⇒ 旧行为逐字不变。"""
        from app.routers import upload_outline as uo
        from app.services.outline_utils import normalize_outline

        norm = normalize_outline(copy.deepcopy(_RAW_TREE))
        old_title = str(norm[0]["children"][0]["title"])
        tracker = _FixedTracker(old=old_title)
        monkeypatch.setattr(O, "_try_outline_title_fix", tracker)

        _pid, pair = await _seed(db_conn)
        sid, rid = pair.split("|")
        await db_conn.execute(
            "INSERT INTO sections (id, scheme_id, project_id, title, content,"
            " level, sort_order) VALUES ('s1',?,?,?,?,'2',0)",
            (sid, _pid, old_title, "旧正文"),
        )
        await db_conn.commit()

        res = await uo.save_as_outline(rid, {"scheme_id": sid, "outline": copy.deepcopy(_RAW_TREE)}, db=db_conn)

        assert tracker.calls == 0
        assert res.get("preserved_content") == 1
        cur = await db_conn.execute("SELECT title, content FROM sections WHERE id='s1'")
        row = await cur.fetchone()
        assert row["title"] == old_title, "未改写 ⇒ 标题原样"
        assert row["content"] == "旧正文"
