"""解析提取模块缺陷收口护栏（2026-10-05）

本轮逐条核实并修复/判定的四个前端问题（均落在 `SchemeWorkbenchPage.tsx`，
即「解析提取」模块的承载页）：

| # | 缺陷 | 修法 |
|---|---|---|
| B5 | 卸载 cleanup 缺 `factsListAbortRef` / `factsDocsListAbortRef` | 卸载清单补两路 abort |
| B6 | 切换方案时 `selectedBaItem` / `sectionCheckResult` 不重置 | `[id]` effect 补两行 reset |
| B7 | 左面板标题写死「18 项结构化提取」 | 按 `baDefs.length` 动态，未加载则不报数 |
| B8 | 无累计体积前端预检（200MB 才在后端被扣） | `splitByUploadTotalQuota` + 接线 |
| B10 | 「① 上传文件保存」入口绕过单文件硬校验（accept 可被拖拽绕过） | `partitionUploadFiles` 收敛到 `handleUploadDocuments` |

B9（启动按钮双击）经代码核实为**非缺陷**，不在此护栏内（记录以免重复排查：
`BidAnalysisTab.tsx` 只在 `!running` 时渲染启动按钮，且 `runBaSse` 同步设置
`baSseAbortRef` 守卫，`finally` 成对清理）。

⚠️ 前端测试不能用 `fs`（本仓未装 @types/node，tsc 会 TS2307），故跨语言的
**源码接线护栏**放 pytest 侧（与 `test_upload_format_contract.py`、
`TestExportGateHighSeverityParity` 同构）；纯函数行为测试在
`frontend/src/tests/uploadAccept.test.ts`。
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_PAGE = _REPO / "frontend" / "src" / "pages" / "SchemeWorkbenchPage.tsx"
_UPLOAD_ACCEPT_TS = _REPO / "frontend" / "src" / "utils" / "uploadAccept.ts"


def _page_src() -> str:
    assert _PAGE.exists(), f"缺少 {_PAGE}"
    return _PAGE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _upload_src() -> str:
    assert _UPLOAD_ACCEPT_TS.exists(), f"缺少 {_UPLOAD_ACCEPT_TS}"
    return _UPLOAD_ACCEPT_TS.read_text(encoding="utf-8").replace("\r\n", "\n")


def _strip_comments(src: str) -> str:
    """去行注释与块注释（判「是否还有硬编码文案」时必须先剥离注释示例）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    lines = [re.sub(r"//.*$", "", ln) for ln in src.split("\n")]
    return "\n".join(lines)


def _slice_after(src: str, anchor: str, span: int) -> str:
    """取 anchor 之后 span 个字符（行号会随编辑漂移，故按锚点定位）。"""
    i = src.find(anchor)
    assert i >= 0, f"锚点未命中：{anchor!r}（文件被改动或锚点失效，先复核源码）"
    return src[i:i + span]


# --------------------------------------------------------------------------- #
# B5 · 卸载 cleanup 必须 abort 全部事实列表请求
# --------------------------------------------------------------------------- #
def test_b5_unmount_cleanup_aborts_facts_list_refs():
    src = _page_src()
    # 注释即锚点：卸载 effect 的独有说明（[id] effect 里不会出现这段话）。
    # 注意源码该行是 `// ✅ 组件卸载时…`，故锚点**不带 `// ` 前缀**。
    block = _slice_after(src, "\u7ec4\u4ef6\u5378\u8f7d\u65f6\u53d6\u6d88\u6240\u6709\u8fdb\u884c\u4e2d\u7684 SSE", 4000)
    # 只截到本 effect 结束（依赖数组 []），防止断言命中的是别处代码。
    end = block.find("}, []);")
    assert end > 0, "未定位到卸载 effect 的结束（}, []);）"
    body = block[:end]
    assert "factsListAbortRef.current?.abort();" in body, (
        "卸载 cleanup 缺 factsListAbortRef.abort —— 全局事实列表请求泄漏，"
        "组件卸载后仍会 setState（B5 回归）")
    assert "factsDocsListAbortRef.current?.abort();" in body, (
        "卸载 cleanup 缺 factsDocsListAbortRef.abort（B5 回归）")


def test_b5_both_refs_are_declared():
    """两路 refs 必须真实存在（否则上面的断言只是在测注释里的字样）。"""
    src = _page_src()
    assert re.search(r"factsListAbortRef\s*=\s*useRef", src), "factsListAbortRef 未声明"
    assert re.search(r"factsDocsListAbortRef\s*=\s*useRef", src), "factsDocsListAbortRef 未声明"
    # 两路请求确实会写入各自的 ref（中止才有意义）
    assert src.count("factsListAbortRef.current = ac") >= 1
    assert src.count("factsDocsListAbortRef.current = ac") >= 1


def test_b5_scheme_switch_effect_still_aborts_them():
    """[id] effect 的 abort 清单不得因本轮改动而回退（新旧两条路径都要有）。"""
    src = _page_src()
    block = _slice_after(src, "const previousSchemeId = mountedSchemeIdRef.current", 2600)
    assert "factsListAbortRef.current?.abort();" in block
    assert "factsDocsListAbortRef.current?.abort();" in block


# --------------------------------------------------------------------------- #
# B6 · 切换方案重置提取项目选中态
# --------------------------------------------------------------------------- #
def test_b6_scheme_switch_resets_ba_selection():
    src = _page_src()
    block = _slice_after(src, "const previousSchemeId = mountedSchemeIdRef.current", 4500)
    end = block.find("}, [id]);")
    assert end > 0, "未定位到 [id] effect 的结束"
    body = block[:end]
    assert "setSelectedBaItem(null);" in body, (
        "切换方案未清空 selectedBaItem —— 右侧面板残留上一个方案的提取项详情（B6 回归）")
    assert "setSectionCheckResult(null);" in body, (
        "切换方案未清空 sectionCheckResult —— 多标段检测结果残留上一个方案（B6 回归）")


def test_b6_both_states_are_declared_and_written():
    src = _page_src()
    assert re.search(r"const \[selectedBaItem, setSelectedBaItem\]", src), "selectedBaItem 未声明"
    assert re.search(r"const \[sectionCheckResult, setSectionCheckResult\]", src), (
        "sectionCheckResult 未声明")


# --------------------------------------------------------------------------- #
# B7 · 左面板标题按定义条数动态
# --------------------------------------------------------------------------- #
def test_b7_extract_panel_title_is_dynamic():
    src = _page_src()
    # 动态口径：`${baDefs.length} 项结构化提取`
    assert "`${baDefs.length} \u9879\u7ed3\u6784\u5316\u63d0\u53d6`" in src, (
        "左面板标题未按 baDefs.length 动态渲染（B7 回归）")
    # 剥离注释后不得残留写死的「18 项结构化提取」（注释里的说明文字不算）
    code = _strip_comments(src)
    assert "18 \u9879\u7ed3\u6784\u5316\u63d0\u53d6" not in code, (
        "源码（非注释）仍写死「18 项结构化提取」—— 后端增减项后标题即失真（B7 回归）")
    # 必须有兜底文案：定义未加载时不报数
    assert "\u7ed3\u6784\u5316\u63d0\u53d6}" in code or "\u7ed3\u6784\u5316\u63d0\u53d6\"" in code, (
        "缺少「定义未加载则不报数」的兜底分支")


def test_b7_defs_state_exists():
    """标题数据源必须是已有的 baDefs（不是另起一套计数）。"""
    src = _page_src()
    assert re.search(r"const \[baDefs, setBaDefs\]", src), "baDefs 状态缺失"
    assert "setBaDefs(" in src


# --------------------------------------------------------------------------- #
# B8 · 上传累计体积预检接线
# --------------------------------------------------------------------------- #
def test_b8_quota_helper_is_wired_into_upload():
    src = _page_src()
    assert "splitByUploadTotalQuota" in src, "页面未引入累计体积配额预检（B8 未接线）"
    block = _slice_after(src, "const handleUploadDocuments = async", 4000)
    assert "splitByUploadTotalQuota(" in block, (
        "handleUploadDocuments 内未调用配额预检 —— 两条上传入口都不做预检（B8 回归）")
    assert "msg.warning(" in block, "配额命中时未提示用户（B8 回归：静默扣文件）"
    # 预检必须先于「开始上传」，否则已经 setUploadingFacts 才提示会让按钮态跳变
    i_helper = block.find("splitByUploadTotalQuota(")
    i_start = block.find("setUploadingFacts(true)")
    assert 0 <= i_helper < i_start, "配额预检必须发生在 setUploadingFacts 之前（B8 回归）"
    # 实际上传用的是分流后的文件，不是原批次
    assert "factsApi.uploadDocuments(toUpload" in block, (
        "预检分流结果未被使用 —— 超限文件仍会被发往后端（B8 回归）")


def test_b8_reads_max_total_bytes_from_limits_endpoint():
    src = _page_src()
    block = _slice_after(src, "max_upload_bytes", 900)
    assert "max_total_bytes" in block, (
        "页面未读取 /system/upload-limits 的 max_total_bytes（B8 回归：上限恒为兜底值）")
    assert re.search(r"const \[maxUploadTotalBytes, setMaxUploadTotalBytes\]", src), (
        "maxUploadTotalBytes 状态缺失")


def test_b8_helper_exported_with_sane_contract():
    src = _upload_src()
    assert re.search(r"export function splitByUploadTotalQuota", src), (
        "uploadAccept.ts 未导出 splitByUploadTotalQuota")
    m = re.search(r"export const MAX_UPLOAD_TOTAL_FALLBACK:\s*number\s*=\s*(.+?);", src)
    assert m, "缺少 MAX_UPLOAD_TOTAL_FALLBACK 常量导出"
    # 200 * 1024 * 1024 形态（与后端三处默认值对齐）
    assert m.group(1).replace(" ", "").startswith("200*1024*1024"), (
        f"兜底上限不是 200MB：{m.group(1)!r}")


# --------------------------------------------------------------------------- #
# 累计体积上限的「四处同值」parity（config / system / global_facts / 前端）
# --------------------------------------------------------------------------- #
def test_upload_total_limit_default_is_consistent_across_four_sources():
    from app.config import Settings
    from app.routers import global_facts, system

    cfg_default = Settings.model_fields["upload_max_total_bytes"].default
    assert cfg_default == 200 * 1024 * 1024, f"config 默认值漂移：{cfg_default}"
    assert system._UPLOAD_LIMIT_DEFAULTS["max_total_bytes"] == cfg_default, (
        "system.py 的 limits 兜底与 config 默认值不一致 → 前端拿到的上限与"
        "后端实际执行的上限可能不同")
    assert global_facts._DEFAULT_UPLOAD_TOTAL_BYTES == cfg_default, (
        "global_facts 的兜底与 config 默认值不一致")

    front = _upload_src()
    m = re.search(r"MAX_UPLOAD_TOTAL_FALLBACK:\s*number\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024", front)
    assert m, "前端兜底上限未能解析"
    assert int(m.group(1)) * 1024 * 1024 == cfg_default, (
        "前端 MAX_UPLOAD_TOTAL_FALLBACK 与后端默认值不一致")


def test_upload_limits_endpoint_exposes_max_total_bytes():
    """/system/upload-limits 必须回传 max_total_bytes（B8 的数据源）。"""
    import inspect

    from app.routers import system

    src = inspect.getsource(system)
    assert '"max_total_bytes"' in src, "system.py 未回传 max_total_bytes"


# --------------------------------------------------------------------------- #
# B9 · 记录为「已核实非缺陷」的锁（防止后人误改）
# --------------------------------------------------------------------------- #
def test_b9_start_button_guard_recorded_as_non_defect():
    """启动按钮在 running 时整体不渲染（非缺陷核实的依据，防回退）。

    双击窗口由 `runBaSse` 同步设置的 `baSseAbortRef` 兜住：第二次进入会因
    `baSseAbortRef.current` 非空而直接 return。这里只锁**渲染条件**这一半，
    避免有人把按钮改成 `disabled={running}` 却去掉条件渲染后以为等价。
    """
    p = _REPO / "frontend" / "src" / "components" / "BidAnalysisTab.tsx"
    src = p.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert re.search(r"\{\s*!running\s*\?\s*\(", src), (
        "启动按钮不再受 !running 条件渲染保护 —— B9 从「非缺陷」变成真缺陷，"
        "需重新评估双击守卫")


# --------------------------------------------------------------------------- #
# 静态扫仓：解析提取页面不得再引入新的「裸请求不上 abort 清单」
# --------------------------------------------------------------------------- #
def test_page_abort_refs_all_covered_by_unmount_cleanup():
    """声明了 AbortController ref 并在请求中赋值的，卸载清单必须逐个 abort。

    判据（同构于 R37「登记表按符号而非行号」教训）：
      - 找到形如 `xxxAbortRef.current = ac` 的**写入点**
      - 该 ref 名必须出现在卸载 cleanup 的 abort 列表里
    这样新增一路请求却忘登记时会当场失败，而不是等线上出现「卸载后 setState」。
    """
    src = _page_src()
    written = set(re.findall(r"(\w+AbortRef)\.current\s*=\s*ac", src))
    assert written, "未找到任何 AbortController 写入点（判据失效，先复核源码）"
    block = _slice_after(src, "\u7ec4\u4ef6\u5378\u8f7d\u65f6\u53d6\u6d88\u6240\u6709\u8fdb\u884c\u4e2d\u7684 SSE", 4000)
    end = block.find("}, []);")
    assert end > 0
    cleanup = block[:end]
    missing = sorted(r for r in written if f"{r}.current?.abort();" not in cleanup)
    # 外部库/别处管理的 ref 允许豁免（登记即白名单，按符号名登记、不按行号）
    allowlist: set[str] = set()
    missing = [r for r in missing if r not in allowlist]
    assert not missing, (
        f"以下 AbortController 在请求中被赋值，但卸载 cleanup 未 abort：{missing}")


# --------------------------------------------------------------------------- #
# B10 · 单文件硬校验收敛到唯一入口（两条上传入口一次覆盖）
# --------------------------------------------------------------------------- #
def _upload_block(span: int = 5000) -> str:
    src = _page_src()
    block = _slice_after(src, "const handleUploadDocuments = async", span)
    end = block.find("const handleParseDocuments")
    return block[:end] if end > 0 else block


def test_b10_partition_helper_is_wired_into_upload_handler():
    """入口 2「① 上传文件保存」此前把原始文件直传后端 → 超限/非法类型整批后端拒。

    `accept` 属性只过滤文件选择框，拖拽可绕过（`UploadParseTab` 早有硬校验，
    页面按钮入口没有），故硬校验必须收敛到两条入口的**共同上游**
    `handleUploadDocuments`，与 B8 的配额预检同构。
    """
    block = _upload_block()
    assert "partitionUploadFiles(" in block, (
        "handleUploadDocuments 未做单文件硬校验 —— 「① 上传文件保存」入口仍会"
        "把超限/非法扩展名文件发给后端（B10 回归）")
    assert "msg.error(" in block, "被拒文件未提示用户（B10 回归：静默丢弃）"
    # 顺序：单文件过滤 → 累计配额 → 开始上传
    i_part = block.find("partitionUploadFiles(")
    i_quota = block.find("splitByUploadTotalQuota(")
    i_start = block.find("setUploadingFacts(true)")
    assert 0 <= i_part < i_quota < i_start, (
        "预检顺序错位：应为 单文件过滤 → 累计配额 → setUploadingFacts")
    # 实际上传用的是过滤后的文件
    assert "factsApi.uploadDocuments(toUpload" in block, "过滤结果未被使用（B10 回归）"


def test_b10_single_choke_point_for_upload_requests():
    """页面内 `factsApi.uploadDocuments` 只能有 1 处调用（唯一出口）。"""
    src = _page_src()
    assert src.count("factsApi.uploadDocuments(") == 1, (
        "页面出现多处 factsApi.uploadDocuments —— 预检必然绕过其中一条入口")
    # 两条入口都汇入同一 handler
    assert "onUploadFiles={handleUploadDocuments}" in src, "import Tab 入口未接 handler"
    assert "handleUploadDocuments(files)" in src, "「① 上传文件保存」入口未接 handler"


def test_b10_import_tab_keeps_its_own_preflight():
    """入口 1 的先验过滤不得被摘除（否则两处提示文案分叉、且整批先发后端）。"""
    tab = _REPO / "frontend" / "src" / "components" / "UploadParseTab.tsx"
    src = tab.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "partitionUploadFiles(files, effectiveMaxBytes)" in src, (
        "UploadParseTab 摘掉了自身预检 —— 拖拽入口的「先拦后发」退化为先发后端")


def test_b10_reject_wording_is_consistent_across_both_entries():
    """两条入口的拒绝文案必须同口径（同一判据两处措辞 → 用户看到两套说法）。"""
    page = _page_src()
    tab = (_REPO / "frontend" / "src" / "components" / "UploadParseTab.tsx").read_text(
        encoding="utf-8").replace("\r\n", "\n")
    for phrase in ("\u4e0d\u652f\u6301\u7684\u6587\u4ef6\u7c7b\u578b\uff0c\u5df2\u5ffd\u7565\uff1a",
                   "\uff0c\u5df2\u5ffd\u7565\uff1a"):
        assert phrase in page and phrase in tab, f"两条入口的拒绝文案不一致：{phrase!r}"


def test_b10_helper_default_matches_import_tab_default():
    """硬校验上限必须回落到同一个 30MB 常量（否则两条入口判定不同）。"""
    page = _page_src()
    assert "MAX_UPLOAD_BYTES" in page, "页面未回落 MAX_UPLOAD_BYTES"
    up = _upload_src()
    m = re.search(r"MAX_UPLOAD_BYTES:\s*number\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024", up)
    assert m and int(m.group(1)) == 30, "MAX_UPLOAD_BYTES 不是 30MB"
    assert re.search(r"export function partitionUploadFiles", up), "partitionUploadFiles 未导出"


# --------------------------------------------------------------------------- #
# D1/D2 · 上传上限文案与文件数预检（2026-10-05 第二轮：前后端口径漂移）
# --------------------------------------------------------------------------- #
_WORKFLOW_DERIVED_TS = _REPO / "frontend" / "src" / "utils" / "workflowDerived.ts"


def _workflow_src() -> str:
    assert _WORKFLOW_DERIVED_TS.exists(), f"缺少 {_WORKFLOW_DERIVED_TS}"
    return _WORKFLOW_DERIVED_TS.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_d1_notice_text_is_parameterized_not_hardcoded():
    """拒绝文案里的 30MB / 200MB / 20 必须来自下发上限，不得写死。

    D1（前后端接口不一致）：后端 `upload_max_bytes` / `upload_max_total_bytes` /
    `upload_max_files_per_request` 均可配置并经 /system/upload-limits 下发；若文案
    写死，管理员改成 100MB 后用户仍被提示"超过 30MB"，据此调整文件却继续失败。
    """
    src = _workflow_src()
    code = _strip_comments(src)
    # 逐字硬编码旧文案不得再出现在**代码**（注释里的历史说明不算）
    for literal in ("\u4e2a\u8d85\u8fc7 30MB", "\u5355\u6b21 20 \u4e2a\u4e0a\u9650",
                    "200MB \u4e0a\u9650"):
        assert literal not in code, (
            f"workflowDerived.ts 仍硬编码上限文案 {literal!r} —— 与后端可配置上限漂移（D1 回归）")
    # 必须存在按入参渲染的形参（limits）与 MB 格式化辅助
    assert re.search(r"export function summarizeUploadResult\(\s*[\s\S]{0,120}?limits\?:\s*UploadLimitHints", src), (
        "summarizeUploadResult 未接收 limits 入参（D1 回归）")
    assert re.search(r"export function buildUploadNotice\(\s*[\s\S]{0,120}?limits\?:\s*UploadLimitHints", src), (
        "buildUploadNotice 未接收 limits 入参（D1 回归）")
    assert "export type UploadLimitHints" in src, "缺少 UploadLimitHints 类型导出"


def test_d1_page_passes_delivered_limits_into_notice():
    """页面必须把 /system/upload-limits 的三个上限透传给 buildUploadNotice。"""
    src = _page_src()
    assert "buildUploadNotice(data, {" in src, (
        "页面未向上传提示透传 limits —— 文案又退回硬编码默认值（D1 回归）")
    block = _slice_after(src, "buildUploadNotice(data, {", 300)
    for field in ("maxBytes:", "maxTotalBytes:", "maxFiles:"):
        assert field in block, f"buildUploadNotice 缺少上限字段 {field}（D1 回归）"


def test_d2_file_count_helper_exported_and_default_matches_backend():
    up = _upload_src()
    assert re.search(r"export function splitByUploadFileCount", up), (
        "uploadAccept.ts 未导出 splitByUploadFileCount（D2 未实现）")
    m = re.search(r"MAX_UPLOAD_FILES_FALLBACK:\s*number\s*=\s*(\d+)", up)
    assert m, "缺少 MAX_UPLOAD_FILES_FALLBACK 常量导出"
    # 与后端三处默认值同值（config 默认 = global_facts 兜底 = 前端兜底）
    from app.config import Settings
    from app.routers import global_facts
    cfg_default = Settings.model_fields["upload_max_files_per_request"].default
    assert int(m.group(1)) == cfg_default == global_facts._DEFAULT_UPLOAD_FILES, (
        "前端兜底文件数上限与后端默认值不一致 → 预检口径漂移")


def test_d2_file_count_preflight_wired_into_upload_handler():
    block = _upload_block()
    assert "splitByUploadFileCount(" in block, (
        "handleUploadDocuments 未做单次文件数预检 —— 超量文件要等整批传完才被发现（D2 回归）")
    # 顺序：单文件过滤 → 累计体积 → 文件数 → 开始上传
    i_part = block.find("partitionUploadFiles(")
    i_quota = block.find("splitByUploadTotalQuota(")
    i_count = block.find("splitByUploadFileCount(")
    i_start = block.find("setUploadingFacts(true)")
    assert 0 <= i_part < i_quota < i_count < i_start, (
        "预检顺序错位：应为 单文件过滤 → 累计体积 → 文件数 → setUploadingFacts")
    assert "factsApi.uploadDocuments(toUpload" in block, "文件数分流结果未被使用（D2 回归）"


def test_d2_page_reads_max_files_per_request_from_limits():
    src = _page_src()
    assert re.search(r"const \[maxUploadFiles, setMaxUploadFiles\]", src), (
        "maxUploadFiles 状态缺失（D2 回归）")
    block = _slice_after(src, "max_upload_bytes", 900)
    assert "max_files_per_request" in block, (
        "页面未读取 /system/upload-limits 的 max_files_per_request（D2 回归：上限恒为兜底值）")
    assert "setMaxUploadFiles" in block, "读取到 max_files_per_request 后未写入状态（D2 回归）"


# --------------------------------------------------------------------------- #
# D3 · 目录「导入目录」入口收敛到动态上限（2026-10-05）
# --------------------------------------------------------------------------- #
_MODAL_TS = _REPO / "frontend" / "src" / "components" / "OutlineLibraryEditModal.tsx"
_OUTLINE_PAGE_TS = _REPO / "frontend" / "src" / "pages" / "OutlineLibraryPage.tsx"


def test_d3_outline_import_entries_use_dynamic_upload_limit():
    """D3：两处「导入目录」入口不得再硬编码 30MB，须走动态上限（缺省回落兜底）。

    D3（前后端接口不一致）：方案工作台与目录库编辑弹窗的「导入目录（智能识别）」
    入口此前各自写死 `30 * 1024 * 1024` 阈值与"30MB"文案，与后端可配置的
    `upload_max_bytes`（经 /system/upload-limits 下发）漂移 —— 管理员调上限后
    入口仍在 30MB 处拦截并误报"30MB"。
    """
    page = _page_src()
    modal = _MODAL_TS.read_text(encoding="utf-8").replace("\r\n", "\n")
    outline_page = _OUTLINE_PAGE_TS.read_text(encoding="utf-8").replace("\r\n", "\n")

    for name, src in (("SchemeWorkbenchPage.tsx", page),
                      ("OutlineLibraryEditModal.tsx", modal)):
        code = _strip_comments(src)
        assert "30 * 1024 * 1024" not in code, (
            f"{name} 仍硬编码 30MB 上传上限 —— 与后端可配置上限漂移（D3 回归）")
        assert "resolveMaxUploadBytes(" in code, f"{name} 未用动态上限解析（D3 回归）"
        assert "formatUploadLimitMb(" in code, f"{name} 未用动态上限文案（D3 回归）"

    # 受控组件约定：弹窗不自取网络配置，由宿主页下传。
    assert re.search(r"maxUploadBytes\?:\s*number", modal), (
        "OutlineLibraryEditModal 未接收 maxUploadBytes 受控入参（D3 回归）")
    assert "systemApi.uploadLimits()" in outline_page, (
        "目录库页未从 /system/upload-limits 读取上限（D3 回归）")
    assert re.search(r"const \[maxUploadBytes, setMaxUploadBytes\]", outline_page), (
        "目录库页未持有 maxUploadBytes 状态（D3 回归）")
    assert "maxUploadBytes={maxUploadBytes}" in outline_page, (
        "目录库页未把 maxUploadBytes 下传编辑弹窗（D3 回归）")
