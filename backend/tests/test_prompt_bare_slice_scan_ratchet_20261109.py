# -*- coding: utf-8 -*-
"""R202（2026-11-09）全仓「提示词裸切」扫描棘轮（G2）。

《提示词硬截断全仓扫描与清零》(docs/提示词硬截断全仓扫描与清零_20261108.md) §6.2
指出：既有 AST 棘轮（test_prompt_truncation_ratchet_20261107.py）只扫提示词命名空间，
全仓口径扫描是**临时脚本**、未入库 → 口径漂移、无法常态化。

本文件把该口径**入库并自动化**：

* 扫描器 = ``tools/prompt_bare_slice_scan.py``（仓库根 tools/），判定口径与
  20261108 报告一致（切片是否被裁剪原语包裹 / 邻近是否有留痕 / 是否有模型可见标记）；
* **L1 严格层**：模型可见路径上的零留痕裸切，**必须逐条登记**在
  :data:`MODEL_FACING_REGISTRY` 里并写明理由；未登记 → 红（新增裸切必须被审视）。
* **L1 反向**：登记表里的条目若已不存在（修掉了/代码变了）→ 红
  （僵尸豁免必须删掉，不许「留着不碍事」—— 这正是白名单会重新长回来的原因）。
* **L2 计数层**：非模型可见路径的零留痕裸切**数量不得增长**（防止新增静默截断）。

⚠️ 只跑本文件：
    cd backend && py -3.14 -m pytest tests/test_prompt_bare_slice_scan_ratchet_20261109.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
APP = BACKEND / "app"
SCANNER = REPO / "tools" / "prompt_bare_slice_scan.py"


def _load_scanner():
    """从仓库根加载扫描器（工具在仓库根，测试在 backend/tests/）。"""
    spec = importlib.util.spec_from_file_location("prompt_bare_slice_scan", SCANNER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


#: 模型可见路径上的零留痕裸切登记表：(文件相对路径, 归一化代码片段) → 理由。
#:
#: 登记纪律（同 ``_IRRELEVANT_LIMITS``）：
#: * 每条必须写明「为什么这不是模型可见的静默丢字」；
#: * 代码改了就删条目（不再匹配即僵尸 → 棘轮报错）；
#: * 新增裸切必须先登记理由，不能默默出现。
MODEL_FACING_REGISTRY: dict[tuple[str, str], str] = {
    (
        "routers/bid_analysis.py",
        'msg += f"（首个失败原因：{errors[0][:200]}）"',
    ): "失败原因**摘要**（给人看的提示语），不进模型提示词；错误全文在日志/审计里。",
    (
        "routers/global_facts.py",
        'clean.append({"op": "delete", "fact_id": fid, "reason": str(op.get("reason") or "")[:200]})',
    ): "调整操作的**原因字段**（落库审计），非模型输入。",
    (
        "routers/global_facts_docs.py",
        "head = text[:MAX_PARSED_CHARS]",
    ): "解析后**入库**的正文头部（函数同时返回 truncated 布尔标记，截断事实有留痕）。",
    (
        "routers/global_facts_docs.py",
        "files = files[:max_files]",
    ): "上传**文件个数**上限（每份文件完整保留），非文本截断。",
    (
        "routers/global_facts_docs.py",
        "name = name[:_MAX_SAFE_FILENAME_CHARS]",
    ): "**文件名**安全截断（落库文件系统路径），与模型输入无关。",
    (
        "services/ai/mermaid_service.py",
        "code=code[:200],",
    ): "Mermaid 渲染失败时的**错误载荷**（诊断用），不进提示词。",
    (
        "services/ai/mermaid_service.py",
        'f"Docker 服务返回 HTTP {resp.status_code}: {redact_sensitive_text(resp.text)[:200]}",',
    ): "HTTP 错误响应摘要（诊断用），不进提示词。",
    (
        "services/ai/mermaid_service.py",
        'f"Railway 服务返回 HTTP {resp.status_code}: {redact_sensitive_text(resp.text)[:200]}",',
    ): "HTTP 错误响应摘要（诊断用），不进提示词。",
    (
        "services/ai/provider_factory.py",
        "return redact_sensitive_text(msg)[:300]",
    ): "异常**摘要**（已脱敏，写入 ai_audit_logs / 返回前端），不进提示词。",
    (
        "services/bid_section_context.py",
        "kept = [item for item in cleaned if item][:_MAX_EVIDENCE]",
    ): "依据**条目数**上限（每条依据完整保留），非文本截断。",
    (
        "services/content_utils.py",
        "for s in siblings[:max_siblings]:",
    ): "同级章节**条目数**上限（每条完整保留），非文本截断。",
    (
        "services/facts_enrich.py",
        "for it in (items or [])[:_MAX_PROMPT_FACTS]:",
    ): "事实**条目数**上限（每条完整保留），非文本截断。",
    (
        "services/facts_enrich.py",
        'for raw in obj["facts"][:_MAX_FINALIZE_FACT]:',
    ): "事实**条目数**上限（每条完整保留），非文本截断。",
    (
        "services/facts_patches.py",
        'return first_line[:_TITLE_MAX_CHARS] or f"补充事实项{index + 1}"',
    ): "事实**标题**派生上限（标题本身是短名词短语，且下游按标题匹配，不承载正文语义）。",
    (
        "services/facts_patches.py",
        "return head[:_TITLE_MAX_CHARS]",
    ): "事实**标题**派生上限（同上）。",
    (
        "services/outline_integrity.py",
        "for r in items[:_SAMPLE_LIMIT]",
    ): "目录完整性诊断的**采样条目数**上限，诊断用不进提示词。",
    (
        "services/review_autofix.py",
        'for code in (finding.get("evidence") or [])[:MAX_TARGETS]:',
    ): "待定位的**标准编号条目数**上限（每条完整），非文本截断。",
    (
        "services/standards_registry.py",
        "hits = match_categories(scheme_name, scheme_type, section_title)[:max_categories]",
    ): "标准**分类条目数**上限（每类标准清单完整保留），非文本截断。",
    (
        "routers/sections.py",
        "brief = brief[:8000]",
    ): "omp 目录生成提示词的项目简介**安全上限**（注释已写明防超长撑爆 omp 上下文/提示词预算），防御性截断，非信息丢失。",
    (
        "services/ai/omp_agent_roles.py",
        '"项目简报（完整版见 CONTEXT.md）：" + brief[:2000],',
    ): "提示词内嵌的**项目简报摘要**（完整版见 CONTEXT.md，模型可从工作区读取全文），仅防提示词膨胀。",
    (
        "services/ai/omp_agent_roles.py",
        '"目录树摘要：" + outline_digest[:4000],',
    ): "提示词内嵌的**目录树摘要上限**（全文以 CONTEXT.md 为准），仅防提示词膨胀。",
    (
        "services/ai/omp_safety.py",
        "secret_files = secret_like_paths(sorted(os.listdir(workspace_root))[:200])",
    ): "密钥文件点名只取**前 200 个条目**（每项完整保留），是文件个数上限而非文本截断。",
    (
        "services/ai/omp_task_agent.py",
        'f"omp 最终回复（截断）：{raw_text[:200]}"',
    ): "**错误消息摘要**（诊断用，含截断标记），非模型输入内容。",
}

#: 非模型可见路径零留痕裸切的**数量基线**（L2 计数棘轮）。
#:
#: 口径：这些截断丢的是「文件名 / 错误摘要 / 条目个数 / 解析头部」等
#: 非模型输入内容，不影响模型看到的文本 —— 但**新增**仍需审视，故锁数量不增。
#: 确需新增时必须同时写明理由并更新本常量（棘轮会因数量变化报错）。
#:
#: R243（2026-11-14）审计：基线自 R202 的 51 处增至 60 处，逐条核对均为近期
#: omp / agent-engine / hifi / export 轮次（R230-R242）引入的**良性有界截断**：
#: 文件条目数上限（omp_workflow / omp_diagnostics / image_engine / mermaid_gantt /
#: export `[:_EXPORT_FINDING_SECTION_CAP]`）、错误/失败摘要（agent_engine executors
#: 与 gateway / omp_workflow manual_review / export `text[:limit]`）、安全文件名
#: 截断（export `name[:max_base]`）。全部为计数/诊断/预览类，不进入模型可见文本。
#: R243 的 +1 条：routers/sections.py `run_omp_workflow` 里 `brief[:8000]` 安全上限，
#: 与已登记模型可见的 `generate_outline_via_omp` 同模式；因作用域名不含模型可见词
#: （omp 未入词表）而分类为非模型可见，实为有界且发往模型可见的安全截断（同记账
#: 后提示词预算）。登记为基线增长并写明理由。
NON_MODEL_FACING_BASELINE = 61


@pytest.fixture(scope="module")
def scan_result():
    scanner = _load_scanner()
    return scanner, scanner.scan_tree(APP, 200)


class TestScannerTool:
    def test_scanner_is_committed_in_repo(self):
        assert SCANNER.exists(), f"扫描器未入库：{SCANNER}"
        assert "tools" in {p.name for p in REPO.iterdir() if p.is_dir()}

    def test_scanner_reports_both_layers(self, scan_result):
        scanner, result = scan_result
        assert result.files_scanned > 100, "扫描范围明显不对（应覆盖 backend/app 全量）"
        assert result.total > 0
        assert len(result.zero_ledger) >= len(result.model_facing_zero_ledger)

    def test_cli_exit_code_contract(self, scan_result):
        """CLI：无模型可见零留痕裸切 → 退出码 0；否则 1（供 CI 使用）。"""
        scanner, _result = scan_result
        assert scanner.main(["--root", str(APP)]) in (0, 1)


class TestModelFacingRatchet:
    def test_no_unregistered_model_facing_bare_slice(self, scan_result):
        """L1：模型可见路径的零留痕裸切必须逐条登记。"""
        scanner, result = scan_result
        offenders = []
        for hit in result.model_facing_zero_ledger:
            key = (hit.path, hit.code)
            if key not in MODEL_FACING_REGISTRY:
                offenders.append(f"{hit.path}:{hit.line}  {hit.code}")
        assert not offenders, "以下模型可见裸切未登记（新增截断必须先写理由）：\n" + "\n".join(offenders)

    def test_registry_has_no_zombie_entries(self, scan_result):
        """L1 反向：登记条目若已不存在 → 必须删除（白名单不许悄悄长回来）。"""
        scanner, result = scan_result
        actual = {(h.path, h.code) for h in result.model_facing_zero_ledger}
        zombies = sorted(f"{p} :: {c}" for (p, c) in MODEL_FACING_REGISTRY if (p, c) not in actual)
        assert not zombies, "登记表存在僵尸条目（对应代码已修掉或已改写，须删除）：\n" + "\n".join(zombies)

    def test_registry_entries_have_reasons(self):
        """每条登记都必须写明理由（空理由等于没登记）。"""
        for key, reason in MODEL_FACING_REGISTRY.items():
            assert reason and len(reason.strip()) >= 8, f"{key} 缺少理由"

    def test_non_model_facing_count_does_not_grow(self, scan_result):
        """L2：非模型可见零留痕裸切数量不得增长（新增静默截断要有人点头）。"""
        scanner, result = scan_result
        non_model = len(result.zero_ledger) - len(result.model_facing_zero_ledger)
        assert non_model <= NON_MODEL_FACING_BASELINE, (
            f"非模型可见零留痕裸切增至 {non_model} 处（基线 {NON_MODEL_FACING_BASELINE}）："
            "新增截断必须审视并登记理由"
        )

    def test_total_zero_ledger_does_not_grow(self, scan_result):
        """L2 总量：零留痕裸切总数不得增长（防止把模型可见的挪进非模型类）。"""
        scanner, result = scan_result
        total = len(result.zero_ledger)
        assert total <= NON_MODEL_FACING_BASELINE + len(
            MODEL_FACING_REGISTRY
        ), f"零留痕裸切总数增至 {total} 处（基线 {NON_MODEL_FACING_BASELINE + len(MODEL_FACING_REGISTRY)}）"


class TestKnownFixesAreRegisteredAsLedgered:
    """本轮修掉的三处「模型可见静默截断」必须被扫描器判定为已受控。"""

    def test_outline_context_truncation_is_ledgered(self, scan_result):
        scanner, result = scan_result
        hits = [h for h in result.hits if h.path.endswith("_sse_outline_review.py") and "text[:limit]" in h.code]
        assert hits, "未找到上下文截断切片（代码形态变化，需同步棘轮）"
        assert all(h.ledgered for h in hits), "上下文截断仍被判为零留痕（记账接线丢失）"

    def test_review_autofix_context_is_marked(self):
        """审核回显上下文的截断标记必须落在源码里。

        ⚠️ 该处是 ``content[start:end]``（**非**纯 ``x[:N]`` 切片），本就
        不在扫描器的判定面内（扫描器只认纯上界切片）→ 只能源码断言。
        """
        src = (APP / "services" / "review_autofix.py").read_text(encoding="utf-8")
        assert "上下文已截断" in src, "审核回显上下文缺少模型可见截断标记"

    def test_chart_title_is_ledgered(self, scan_result):
        scanner, result = scan_result
        hits = [h for h in result.hits if h.path.endswith("content_blocks.py") and "_clean_chart_title" not in h.code]
        hits = [h for h in hits if "[:limit]" in h.code]
        assert not hits or all(h.ledgered for h in hits), "图题清洗仍判为零留痕"
