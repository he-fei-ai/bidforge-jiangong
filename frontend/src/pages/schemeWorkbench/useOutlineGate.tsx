import type { MutableRefObject } from "react";
import type { ModalFuncProps } from "antd";
import { schemesApi, sectionsApi } from "../../api";
import {
  genericAutofixFailedNotice,
  genericAutofixNotice,
  genericResidualNotice,
} from "../../utils/genericResidual";
import { emptyTopChaptersNotice } from "../../utils/emptyTopChapters";
import { OutlineGateList } from "./OutlineGateList.tsx";
import { hasAnyContent, type TreeNode } from "./outlineTreeModel.ts";
import type { OutlineNodeInput, SchemeRecord } from "./workbenchTypes";

/**
 * ✅ 2026-11-04 拆分（G4 续）：目录写入提示 + 一级目录确认闸门自
 * SchemeWorkbenchPage 原位搬迁。纯结构重构，零行为变更：
 *   - saveRequirements / notifyClearedContent / notifyGenericResidual /
 *     openOutlineGate / doSaveGate 函数体逐字搬运；
 *   - 生成链（红线）仍留在页面，仅通过 deps 调用这些同名函数。
 */
export interface OutlineGateDeps {
  id: string | undefined;
  scheme: SchemeRecord | null;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  modal: { confirm: (cfg: ModalFuncProps) => void };
  requirements: string;
  treeRef: MutableRefObject<TreeNode[]>;
  hasContentRef: MutableRefObject<boolean>;
  load: () => Promise<unknown>;
}

export function useOutlineGate(deps: OutlineGateDeps) {
  const { id, scheme, msg, modal, requirements, treeRef, hasContentRef, load } = deps;

  /** 保存编制要求到 schemes.config_json.requirements */
  async function saveRequirements() {
    if (!id || !scheme?.project_id) {
      msg.error("缺少项目信息，无法保存");
      return;
    }
    try {
      const { data: s } = await schemesApi.get(scheme.project_id, id);
      let cfg: Record<string, unknown> = {};
      try {
        cfg = JSON.parse(s.config_json || "{}");
      } catch {
        cfg = {};
      }
      cfg.requirements = requirements.trim();
      await schemesApi.update(scheme.project_id, id, {
        config_json: JSON.stringify(cfg),
      });
      msg.success("编制要求已保存：生成目录时将逐条注入并强制覆盖，审核阶段自动补齐缺失章节");
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "保存编制要求失败");
    }
  }

  /** 整表重建可能清除未匹配章节的已生成正文（后端 2026-09-26 起量化回传）。
   * 此前「重新生成目录 → 确认闸门保存」是无提示地清空正文，用户不可恢复。 */
  function notifyClearedContent(
    data: { cleared_content_sections?: number } | null | undefined,
    prefix = "",
  ) {
    const n = Number(data?.cleared_content_sections || 0);
    if (n > 0) {
      msg.warning(
        `${prefix}${n} 个章节的已生成正文因未匹配到新目录而被清除（不可恢复）` +
          `——如需保留，请在生成目录前先备份或用「AI 调整目录」定向修改`,
      );
    }
  }

  /** 目录写入检出占位/泛化标题（后端 R85/R86 加法式回传 `generic_residual` 明细）。
   *
   * 背景：用户在 TOC 里看到的「专项6」「总体实施策划（2）」有两批来源 ——
   * 一批来自 AI 生成（有占位闸门 + 修复链路），另一批来自**写入路径**
   * （手工新增/改名、上传目录识别、目录库套用）。后端已在 create/update/
   * save-outline/upload 四条入口统一加法式回传明细（绝不阻断、绝不静默改写），
   * 但前端此前 `generic_residual` **零消费** ⇒ 用户完全无感知。
   *
   * 文案由纯函数 `genericResidualNotice` 产出（utils 层，vitest 可直接覆盖）。
   * 契约：仅当确有残留才回传该键（数组，元素 `{path,title,kind}`），
   * 旧后端 / 干净目录都不带此键 ⇒ 老版本一字不变、无提示。
   *
   * ✅ 本轮补：后端落库前可能已**自动改写**部分占位标题（外科式重写，只改标题
   * 不增删节点），并以加法式键 `generic_autofixed`（整数，仅 >0 才回传）告知
   * 改写条数。此前前端零消费 ⇒ 自动改写**静默生效**，用户以为目录原样保存。
   * 现一并提示（同一函数出口，7 个调用点零改动即生效）。
   *
   * ✅ R175 G-3（2026-11-03）再折入：空壳一级章通知（`empty_top_chapters`
   * 加法式回传，仅「AI 部分成果放行」与「逃生舱 allow_empty_chapters 放行」
   * 两条链路携带）。此前零消费 ⇒ 空章落库静默生效，用户不知道目录里留着
   * 无子章节的一级章（导出时只保留章标题）。仍走同一函数出口，全部调用点
   * 零改动即生效。
   */
  function notifyGenericResidual(data: unknown) {
    const text = genericResidualNotice(data);
    if (text) msg.warning(text);
    const fixed = genericAutofixNotice(data);
    if (fixed) msg.info(fixed);
    // ✅ D-09：自动改写**未执行**（fail-soft 原样落库）同样不得静默。
    const fixFailed = genericAutofixFailedNotice(data);
    if (fixFailed) msg.warning(fixFailed);
    const emptyTops = emptyTopChaptersNotice(data);
    if (emptyTops) msg.info(emptyTops);
  }

  /** 一级目录确认闸门（对齐 OpenBidKit outline-selection）：
   * 生成完成后不自动入库，由用户勾选确认一级章节后保存并锁定（locked=1） */
  function openOutlineGate(outline: OutlineNodeInput[]) {
    if (!id) return;
    const roots = (outline || []).filter((n) => n && typeof n === "object");
    if (!roots.length) {
      msg.warning("AI 返回空目录，未保存");
      return;
    }
    const selectedRef: { current: number[] } = { current: roots.map((_, i) => i) };
    modal.confirm({
      title: "🔍 一级目录确认（生成后入库前的最后一道闸门）",
      width: 660,
      content: <OutlineGateList outline={roots} selectedRef={selectedRef} />,
      okText: "确认并保存目录",
      cancelText: "暂不入库（手动编辑）",
      onOk: async () => {
        const filtered = roots.filter((_, i) => selectedRef.current.includes(i));
        if (!filtered.length) {
          msg.error("至少保留一个一级章节");
          return Promise.reject(new Error("empty"));
        }
        // ✅ 已有正文时二次确认：AI 新目录与旧章节无法按主键匹配，整表重建会清空正文
        if (hasAnyContent(treeRef.current) && !hasContentRef.current) {
          modal.confirm({
            title: "确认覆盖已有目录？",
            okButtonProps: { danger: true },
            content:
              "当前目录已有章节生成过正文。AI 新目录与旧章节无法按主键匹配，" +
              "确认保存将按新目录重建章节结构，**未匹配上的章节正文会被清除且不可恢复**。" +
              "如需保留原文，请点「取消」并改用「AI 调整目录」定向修改。",
            okText: "仍要保存（可能清除正文）",
            cancelText: "取消",
            onOk: () => {
              // 仅在用户**确认后**置位：取消不应"消耗"掉这次风险提示，
              // 否则用户取消一次后再点保存就静默清空正文（比原缺陷更隐蔽）。
              hasContentRef.current = true;
              return doSaveGate(filtered);
            },
          });
          return;
        }
        await doSaveGate(filtered);
      },
      onCancel: () => {
        // 目录已显示在目录树预览中，未入库 —— 用户可手动编辑后点「保存目录」
        msg.info("目录未入库：可在目录树中手动调整后点击「保存目录」");
      },
    });
  }

  async function doSaveGate(filtered: OutlineNodeInput[]) {
    if (!id) return;
    try {
      const { data } = await sectionsApi.saveOutline(id, {
        outline: filtered,
        source: "ai",
        lock_roots: true,
      });
      msg.success(`目录已确认保存（${filtered.length} 个一级章节已锁定）`);
      notifyClearedContent(data);
      notifyGenericResidual(data);
    } catch (saveErr: unknown) {
      const err = saveErr as { message?: string };
      msg.error(`目录已生成但保存失败：${err?.message || "未知错误"}，请点击「保存目录」重试`);
    }
    load();
  }

  return {
    saveRequirements,
    notifyClearedContent,
    notifyGenericResidual,
    openOutlineGate,
    doSaveGate,
  };
}
