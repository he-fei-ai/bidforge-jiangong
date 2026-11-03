# -*- coding: utf-8 -*-
"""R104 · 一致性审计 422 错误文案跨语言 parity 护栏（2026-10-18）

收口 R103 §6 遗留第三/第四项（判定：机制已到位、缺的是可执行断言）。

``POST /api/v1/compliance/consistency-audit/{id}``（``compliance.run_consistency_audit``）
的两条业务失败是 ``HTTPException(422, "中文消息")`` —— 响应体为
``{"detail": "<字符串>"}``，**不是** FastAPI 校验错误的数组形
``{"detail": [{loc, msg, type}]}``。

这条链路的三跳此前**全程无断言**：

  后端 422 字符串文案  →  api/index.ts 响应拦截器写入 error.message
  →  SchemeWorkbenchPage 的 handleConsistencyAudit 用 e.message 弹 toast

R55 只锁了**数组形**的单一出口（``test_review_autofix_closeout_r55_20261008``
::``test_api_detail_array_single_source`` 只数 ``detailArrayToText`` 相关符号），
于是字符串形那一跳只要被"顺手简化"，用户就退回 axios 默认的
``Request failed with status code 422``，而既有前后端契约测试都不会红
（前端契约测试只校验 URL/timeout；R55 源码锁只数数组形的符号）。

按本仓一贯纪律（R75b「不做 → 加锁」/ R92「加法式不造第二份判据」）：
**不改任何行为、不加第二份翻译实现**，只把已在场的链路做成可执行断言。
行为级断言在 ``frontend/src/tests/consistencyAuditErrorR104.test.ts``
（用真实拦截器回调，非字符串比对）；跨语言静态 parity 放这里 —— 前端测试
无法读 ``.py``（仓库未装 @types/node，同 R71/R75b 惯例）。
"""
import io
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent

API_TS = "frontend/src/api/index.ts"
PAGE_TSX = "frontend/src/pages/SchemeWorkbenchPage.tsx"
# 大文件拆分后：页面内 handler 迁入 pages/schemeWorkbench/ 子模块（如 useReviewAudit.tsx），
# 锚点读取需聚合 facade + 子模块，断言语义不变。
PAGE_WB_DIR = "frontend/src/pages/schemeWorkbench"
COMP_PY = "backend/app/routers/compliance.py"
FE_TEST = "frontend/src/tests/consistencyAuditErrorR104.test.ts"


def _read(rel: str) -> str:
    return io.open(_ROOT / rel, encoding="utf-8-sig").read()


def _page_sources() -> str:
    """facade 页面 + schemeWorkbench/ 子模块聚合源码（拆分后等价读取范围）。"""
    parts = [_read(PAGE_TSX)]
    for p in sorted(list((_ROOT / PAGE_WB_DIR).glob("*.ts"))
                    + list((_ROOT / PAGE_WB_DIR).glob("*.tsx"))):
        parts.append(io.open(p, encoding="utf-8-sig").read())
    return "\n".join(parts)


def _fn_body(src: str, name: str) -> str:
    """取出指定函数体的文本（缩进块），用于把断言限定在正确作用域内。"""
    m = re.search(r"^(\s*)async def %s\(.*?\n" % re.escape(name), src, re.M)
    if not m:
        m = re.search(r"^(\s*)def %s\(.*?\n" % re.escape(name), src, re.M)
    assert m, f"未找到函数 {name}"
    pad = len(m.group(1))
    out = []
    for line in src[m.end():].splitlines()[1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= pad:
            break
        out.append(line)
    return "\n".join(out)


def _handle_body(src: str, const_name: str) -> str:
    """取出 ``const X = async () => { ... };`` 箭头函数体。"""
    m = re.search(r"const %s = async \(\) => \{" % re.escape(const_name), src)
    assert m, f"未找到 {const_name}"
    depth, out = 1, []
    for line in src[m.end():].splitlines()[1:]:
        depth += line.count("{") - line.count("}")
        out.append(line)
        if depth <= 0:
            break
    return "\n".join(out)


class TestBackendConsistencyAudit422:
    """后端两条 422 必须是字符串形 detail，且都点明缺什么。"""

    def test_both_422s_live_in_the_audit_endpoint(self):
        src = _read(COMP_PY)
        body = _fn_body(src, "run_consistency_audit")
        assert body.count("HTTPException(422") == 2, (
            "一致性审计端点的业务失败数量漂移（应为 2 条：无正文 / 无事实）"
        )

    def test_422_detail_is_string_not_validation_array(self):
        # 业务失败必须走字符串 detail —— 只有字符串才会被前端拦截器逐字透传
        body = _fn_body(_read(COMP_PY), "run_consistency_audit")
        for m in re.finditer(r"raise HTTPException\(422,\s*(.+?)\)", body):
            arg = m.group(1).strip()
            assert arg.startswith('"') and arg.endswith('"'), (
                f"422 的 detail 必须是纯字符串文案，实际为 {arg!r}"
            )

    def test_no_content_422_names_the_missing_input(self):
        assert "方案尚无正文内容，无法审计" in _read(COMP_PY)

    def test_no_facts_422_names_the_remedy(self):
        # 已可行动的那条：点名「全局事实提取」，用户照着做即可
        txt = _read(COMP_PY)
        assert "尚未提取项目关键事实" in txt
        assert "全局事实提取" in txt


class TestFrontendStringDetailChain:
    """字符串形 detail → error.message 的单一出口不被回退、不分裂。"""

    def test_string_and_array_forms_share_one_ternary(self):
        s = _read(API_TS)
        # 两个形态在**同一行三元**里分流 —— 任何"字符串形只认 detail 字符串"的
        # 回退都会让这行消失（这是 R55 只锁数组形留下的洞）
        assert (
            "Array.isArray(rawDetail) ? detailArrayToText(rawDetail) : rawDetail"
        ) in s
        # any 类型整改后 data 收敛为类型化别名 body —— 二者语义等价（同一载荷），
        # 仍要求同一标识符在三元两侧成对出现，不允许分裂成两个来源。
        m = re.search(
            r"const rawDetail = (data|body)\?\.detail \?\? \1\?\.message", s)
        assert m, "未找到 rawDetail 单出口三元（data/body 别名形态均未命中）"

    def test_string_branch_writes_error_message_exactly_once(self):
        s = _read(API_TS)
        assert "if (detail && typeof detail === \"string\")" in s
        assert s.count("error.message = detail") == 1, (
            "字符串形 detail 的写入点必须是唯一出口；出现第二份 = 判据分叉"
        )

    def test_translation_has_a_single_implementation(self):
        s = _read(API_TS)
        assert s.count("function detailArrayToText") == 1
        assert s.count("const scopeCn:") == 1


class TestAuditHandlerConsumesInterceptorMessage:
    """页面的兜底文案只允许兜底 —— 主路径必须消费拦截器写入的 e.message。"""

    def test_consistency_audit_handler_reads_e_message(self):
        body = _handle_body(_page_sources(), "handleConsistencyAudit")
        # 大文件拆分后 handler 迁入 pages/schemeWorkbench/useReviewAudit.tsx，
        # 捕获参数由 e 更名 err，且 message 与兜底文案合并为同一表达式：
        #   msg.error(err.message || "一致性审计失败")
        # 语义等价且更严（message 必须在 || 之前 = 主路径优先消费拦截器文案）。
        merged = re.search(
            r"msg\.error\(\s*\w+\.message\s*\|\|\s*[\"']一致性审计失败[\"']\s*\)", body)
        legacy = "msg.error(e.message" in body and '"一致性审计失败"' in body
        assert merged or legacy, (
            "一致性审计失败提示必须消费拦截器写入的 message（主路径在前），"
            "并保留兜底文案；只写死自己的文案 = 后端 422 文案永远到不了用户"
        )
        # 兜底文案保留（拦截器未写入时的最后防线），但主路径在前
        assert '"一致性审计失败"' in body

    def test_audit_card_preempts_both_422_branches(self):
        # 静态提示与运行时 422 指向同一个补救动作（跨语言口径同源）
        s = _read(PAGE_TSX)
        for kw in ("全局事实", "正文", "全局事实提取"):
            assert kw in s, f"审计卡片静态提示缺少补救关键词 {kw!r}"


class TestFrontendBehaviorGuardrailPresent:
    """护栏自身不被静默移除（R55 ::test_component_tests_pin_new_behaviors 同族）。"""

    def test_vitest_file_locks_the_string_form(self):
        s = _read(FE_TEST)
        for kw in (
            "DETAIL_NO_CONTENT",
            "DETAIL_NO_FACTS",
            "Request failed with status code 422",
            "detailArrayToText",
            "ERR_CANCELED",
        ):
            assert kw in s, f"前端行为护栏缺少判据 {kw!r}"
        # 必须是真实回调断言，不得退化成纯字符串比对
        assert "h.hooks.err" in s

    def test_pytest_parity_covers_both_languages(self):
        # 本文件必须真的同时读了三处源码，否则 parity 就是空转
        src = _read(__file__)
        for rel in (API_TS, PAGE_TSX, COMP_PY):
            assert f'"{rel}"' in src, rel


class TestByteHealth:
    """R65 同族：新增文件不得引入 BOM / 孤立 CR / 替换字符。"""

    @pytest.mark.parametrize("rel", [API_TS, FE_TEST, COMP_PY])
    def test_no_lone_cr_or_bom(self, rel):
        b = (_ROOT / rel).read_bytes()
        assert not b.startswith(b"\xef\xbb\xbf"), f"{rel}: UTF-8 BOM"
        assert b.count(b"\xef\xbf\xbd") == 0, f"{rel}: U+FFFD 替换字符"
        for tok in (b"\x00", b"\x07", b"\x1b"):
            assert tok not in b, f"{rel}: 控制字节 {tok!r}"

    def test_no_lone_cr_in_touched_files(self):
        # 唯一可信的行尾不变式：不得出现孤立 CR（CR 未被 LF 跟随）。
        # 它同时覆盖 CRLF 文件与 LF 文件 —— 本仓前端两种行尾并存
        # （api/index.ts 为 LF 原生、SchemeWorkbenchPage.tsx 为 CRLF 原生），
        # 断言 CRLF==LF 会误伤原生 LF 文件。
        for rel in (API_TS, PAGE_TSX, FE_TEST):
            b = (_ROOT / rel).read_bytes()
            i, lone = 0, 0
            while True:
                i = b.find(b"\r", i)
                if i < 0:
                    break
                if b[i : i + 2] != b"\r\n":
                    lone += 1
                i += 1
            assert lone == 0, f"{rel}: {lone} 个孤立 CR（行尾被混改）"

