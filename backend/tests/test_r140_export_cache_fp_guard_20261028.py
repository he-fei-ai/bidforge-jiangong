"""R140 导出缓存「content 指纹」护栏（承接 R139 发现 H · 2026-10-28）

R139 深度探查把「export 缓存键是否真的含 content 指纹」定性为**设计假设、
无测试锁**（发现 H）。本轮补三处护栏，把该假设从「文档留痕」升级为「可验证
契约」：

- B1 行为锁：正文 content 变化 ⇒ ``_content_fingerprint(prep)[1]`` 必须变化
  （否则改正文后仍命中旧缓存，``invalidate_export_cache`` 形同空转 —— 与
  2026-09-17 事实变更不失效同一类缺陷）。既有 TestContentFingerprint 只锁了
  title/sort_order/parent/config/fe_codes/chart_fp/global_fact，**独漏 content**。
- B2 源码锁：``_content_fingerprint`` 的指纹 dict 必须真的包含每节的
  ``s.get("content")``（防止后人把正文从指纹里摘出去）。
- B3 源码锁：``export_docx`` 的缓存查询 SELECT 必须同时用 ``config_hash`` 与
  ``content_fingerprint`` 作 WHERE 键（否则内容变化虽改变指纹、查询却按
  旧键命中 —— 与 R139 发现 H「键未锁」同义；``config_hash`` 只排排版，
  ``content_fingerprint`` 才排内容，缺一即缓存口径分叉）。
- B4 行为锁：缓存查询参数取自已计算出的 ``content_hash``（而非固定字符串），
  保证「指纹变了 ⇒ 查询键变了 ⇒ 不命中旧行」这条因果链成立。
"""
import inspect

from app.routers import export as E


def _prep(sections, **kw):
    base = {"sections": sections, "chart_fp": [],
            "config": {}, "fe_codes": [], "global_facts": []}
    base.update(kw)
    return base


def _sec(content="正文", title="第一章 概述"):
    return {"id": "c1", "parent_id": "", "sort_order": 0, "level": 1,
            "title": title, "content": content}


class TestContentFingerprintGuard:
    def test_content_change_changes_fingerprint(self):
        """B1：正文变更必须改变 content 指纹（旧缓存不再命中）。"""
        before = E._content_fingerprint(_prep([_sec("旧正文")]))[1]
        after = E._content_fingerprint(_prep([_sec("新正文")]))[1]
        assert before != after, \
            "正文变更未改变 content 指纹 ⇒ 改正文后命中旧导出缓存"

    def test_content_inside_fingerprint_dict(self):
        """B2：指纹 dict 必须包含每节 content（源码锁，防摘除）。"""
        src = inspect.getsource(E._content_fingerprint)
        assert 's.get("content")' in src, \
            "content 指纹未包含节正文（s.get('content')），正文变更将被漏计"

    def test_cache_lookup_uses_both_keys_in_where(self):
        """B3：缓存查询 WHERE 必须同时含 config_hash 与 content_fingerprint。"""
        src = inspect.getsource(E.export_docx)
        assert "WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?" in src, \
            "export_docx 缓存查询未按 (config_hash, content_fingerprint) 双键查找"

    def test_cache_lookup_binds_computed_content_hash(self):
        """B4：缓存查询参数绑定已计算的 content_hash（非固定串）。"""
        src = inspect.getsource(E.export_docx)
        i = src.index("WHERE scheme_id=? AND config_hash=? AND content_fingerprint=?")
        # 查询调用紧跟的绑定元组必须引用 content_hash 变量
        tail = src[i:i + 240]
        assert "content_hash" in tail and "config_hash" in tail, \
            "缓存查询未把 (config_hash, content_hash) 绑定为参数"
