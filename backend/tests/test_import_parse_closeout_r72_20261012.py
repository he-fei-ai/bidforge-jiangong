"""解析提取模块 · R72 收口护栏（2026-10-12）

本轮修复的四个真实缺口，全部为「加法式 · 零判定行为变更」，用源码/对象
身份锁钉住接线，防止后续重构再次静默丢弃：

A. **【P1 断链】目录生成链截断告知**
   ``generate_outline`` 的装配阶段（``_assemble_context``）早已把「用了被截断
   的文档」采集进 ``_truncated_docs``，注释承诺「清单带到事件流」，但装配逻辑
   移入闭包时把 yield 点丢了 —— 清单只落日志、从不进 SSE。现补进 ``_assembled``
   产物，由 ``event_stream`` 解包后以 warning 事件显式告知。

B. **【P2 口径不对称】正文生成链截断告知**
   目录生成链传 ``truncated_out``，正文生成链（``generate_content``）根本不传；
   事实提取链（``generate_facts``）早已发 warning。现三链对齐。

C. **【P2 前端交互】目录库弹窗导入空结果覆盖树** —— 见前端
   ``outlineLibraryImport.test.ts``（本文件不覆盖）。

D. **【P2 假成功】写路径 rowcount 校验缺口**
   ``delete_document`` / ``update_document_category`` 只判游标 None（503），
   未校验影响行数；并发删除下 0 行仍回 ok=True，与 ``_r13_write`` 已确立的
   「行数不足 → 404」口径不一致。

T-A. **【技术债】扩展名集合双份字面量** —— 收敛为对象身份同一的单一事实源。
"""
from __future__ import annotations

import inspect

from app.routers import global_facts_docs as gfd
from app.routers import sse_handlers as sh
from app.services import file_parser as fp


# =========================================================================
# A：目录生成链截断告知接线（断链修复）
# =========================================================================
class TestOutlineTruncationNotice:
    def test_assemble_collects_into_assembled(self):
        """_assemble_context 必须把 _truncated_docs 写进 _assembled 产物。"""
        src = inspect.getsource(sh.generate_outline)
        assert "truncated_docs" in src, "装配产物缺 truncated_docs 键 → 清单未落到 _assembled"
        assert "list(_truncated_docs)" in src, "必须以 _truncated_docs 采集结果填充"

    def test_event_stream_unpacked(self):
        """event_stream 必须从 _assembled 解包出截断清单局部变量。"""
        src = inspect.getsource(sh.generate_outline)
        assert '_assembled.get("truncated_docs"' in src, "解包处缺 _assembled.get('truncated_docs')"

    def test_warning_event_emitted(self):
        """解包后必须以 warning 事件显式告知（不能只落日志）。"""
        src = inspect.getsource(sh.generate_outline)
        assert "if _trunc_docs:" in src, "缺截断清单非空守卫"
        assert "'event':'warning'" in src, "缺 warning 事件 yield"
        assert "解析不完整" in src, "缺与事实提取链同口径的告警文案"


# =========================================================================
# B：正文生成链截断告知接线（口径对齐）
# =========================================================================
class TestContentTruncationNotice:
    def test_passes_truncated_out(self):
        """正文链 load_parsed_texts 必须传 truncated_out 采集清单。"""
        src = inspect.getsource(sh.generate_content)
        assert "truncated_out=_content_trunc_docs" in src, (
            "正文生成链未采集截断清单（与目录链不对称）")

    def test_warning_event_emitted(self):
        """采集到的清单必须以 warning 事件进事件流。"""
        src = inspect.getsource(sh.generate_content)
        assert "if _content_trunc_docs:" in src, "缺正文链截断清单非空守卫"
        assert "'event':'warning'" in src, "缺正文链 warning 事件 yield"


# =========================================================================
# D：写路径 rowcount 校验（假成功护栏）
# =========================================================================
class TestWritePathRowcountGuard:
    def test_delete_document_checks_rowcount(self):
        """DELETE 必须在 None 判定之外再校验影响行数，0 行 → 404。"""
        src = inspect.getsource(gfd.delete_document)
        assert "safe_rowcount(del_doc_cur" in src, "删除文档未走 safe_rowcount"
        assert "HTTPException(404" in src, "0 行删除必须回 404 而非谎报 ok"

    def test_update_category_checks_rowcount(self):
        """UPDATE 分类必须校验影响行数，0 行 → 404。"""
        src = inspect.getsource(gfd.update_document_category)
        assert "safe_rowcount(upd_cur" in src, "改分类未走 safe_rowcount"
        assert "HTTPException(404" in src, "0 行改分类必须回 404"


# =========================================================================
# T-A：扩展名集合单一事实源（对象身份，不是内容相等）
# =========================================================================
class TestExtensionSingleSource:
    def test_dispatch_is_declared(self):
        """_SUPPORTED_EXTENSIONS 必须是 SUPPORTED_EXTENSIONS 同一对象（alias）。"""
        assert fp._SUPPORTED_EXTENSIONS is fp.SUPPORTED_EXTENSIONS, (
            "解析分派清单未收敛为对外声明的别名 → 仍是手抄两份，可能漂移")
