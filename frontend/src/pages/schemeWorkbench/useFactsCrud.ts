/**
 * 全局事实手工增删改查回调簇（G5 · 2026-11-04 自 SchemeWorkbenchPage 原样搬迁）。
 *
 * 覆盖：分组新增/编辑、单条事实编辑、分组删除、单条确认、矛盾裁决、一键清空。
 * 保持搬迁前的函数形态（未包 useCallback）、异常处理与文案逐字不变；
 * 仅把页面级外部标识符改为经 deps 解构，依赖来源与搬迁前一致。
 */
import type { Dispatch, SetStateAction } from "react";
import type { FormInstance, ModalFuncProps } from "antd";
import type { MessageInstance } from "antd/es/message/interface";
import { factsApi } from "../../api";
import type { FactCrossConflict, FactGroupLike, FactItemLike } from "../../types/facts";
import type { FactsSegmentStats } from "./workbenchTypes";

/** 单条事实编辑态（页面 state 同构：条目本体 + 所属分组信息） */
export type FactItemEditingState = FactItemLike & { groupId?: string; groupTitle?: string };

export interface FactsCrudDeps {
  id: string | undefined;
  msg: MessageInstance;
  modal: { confirm: (cfg: ModalFuncProps) => void };
  loadFacts: (pageOverride?: number) => Promise<void>;
  factEditingId: string | null;
  setFactEditingId: Dispatch<SetStateAction<string | null>>;
  factEditingCategory: string;
  setFactEditingCategory: Dispatch<SetStateAction<string>>;
  factForm: FormInstance;
  setFactModalOpen: Dispatch<SetStateAction<boolean>>;
  setFactSubmitting: Dispatch<SetStateAction<boolean>>;
  factItemEditing: FactItemEditingState | null;
  setFactItemEditing: Dispatch<SetStateAction<FactItemEditingState | null>>;
  factItemForm: FormInstance;
  setFactItemModalOpen: Dispatch<SetStateAction<boolean>>;
  setFactItemSubmitting: Dispatch<SetStateAction<boolean>>;
  setFactsFilter: Dispatch<SetStateAction<string>>;
  setFactsSegmentStats: Dispatch<SetStateAction<FactsSegmentStats | null>>;
  setFactsCrossConflicts: Dispatch<SetStateAction<FactCrossConflict[]>>;
}

export interface FactsCrudHandlers {
  openFactCreate: () => void;
  openFactEdit: (group: FactGroupLike) => void;
  submitFact: () => Promise<void>;
  openFactItemEdit: (group: FactGroupLike | null, item: FactItemLike) => void;
  submitFactItem: () => Promise<void>;
  handleDeleteFactGroup: (group: FactGroupLike) => void;
  handleResolveFactItem: (item: FactItemLike) => Promise<void>;
  handleResolveConflictItem: (item: FactItemLike, value: string) => Promise<void>;
  handleClearAllFacts: () => void;
}

export function useFactsCrud(deps: FactsCrudDeps): FactsCrudHandlers {
  const {
    id,
    msg,
    modal,
    loadFacts,
    factEditingId,
    setFactEditingId,
    factEditingCategory,
    setFactEditingCategory,
    factForm,
    setFactModalOpen,
    setFactSubmitting,
    factItemEditing,
    setFactItemEditing,
    factItemForm,
    setFactItemModalOpen,
    setFactItemSubmitting,
    setFactsFilter,
    setFactsSegmentStats,
    setFactsCrossConflicts,
  } = deps;

  // ===== ✅ 全局事实：手工新增 / 修改（factsApi.create / factsApi.update）=====
  function openFactCreate() {
    setFactEditingId(null);
    setFactEditingCategory("");
    factForm.resetFields();
    setFactModalOpen(true);
  }

  function openFactEdit(group: FactGroupLike) {
    setFactEditingId(group.id!);
    setFactEditingCategory(group.category || "");
    // ✅ 回传 category：否则后端重建分组时会因缺省把归类降级为 other
    factForm.setFieldsValue({
      title: group.title,
      content: group.content,
      category: group.category || undefined,
    });
    setFactModalOpen(true);
  }

  async function submitFact() {
    if (!id) return;
    try {
      const values = await factForm.validateFields();
      setFactSubmitting(true);
      const category = values.category || factEditingCategory || undefined;
      if (factEditingId) {
        // FactGroupUpdate 模型要求 id 必填，故一并提交
        await factsApi.update(
          factEditingId,
          {
            id: factEditingId,
            title: values.title,
            content: values.content,
            category,
          },
          id,
        );
        msg.success("事实条目已更新");
      } else {
        await factsApi.create({ title: values.title, content: values.content, category }, id);
        msg.success("事实条目已新增");
      }
      setFactModalOpen(false);
      setFactEditingId(null);
      setFactEditingCategory("");
      factForm.resetFields();
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string; errorFields?: unknown };
      if (err.errorFields) return;
      msg.error(err.message || "保存失败");
    } finally {
      setFactSubmitting(false);
    }
  }

  // ===== ✅ 单条事实编辑（走 PATCH item_updates，不重建整个分组）=====
  function openFactItemEdit(group: FactGroupLike | null, item: FactItemLike) {
    setFactItemEditing({ ...item, groupId: group?.id, groupTitle: group?.title });
    factItemForm.setFieldsValue({
      name: item.name,
      value: item.value,
      category: item.category || group?.category || undefined,
      is_simulated: !!item.is_simulated,
      // ✅ R81：要求 vs 事实口径回填（缺省未区分 → 空串）
      value_kind: item.value_kind || "",
    });
    setFactItemModalOpen(true);
  }

  async function submitFactItem() {
    if (!factItemEditing?.fact_id) return;
    try {
      const values = await factItemForm.validateFields();
      setFactItemSubmitting(true);
      await factsApi.update(
        factItemEditing.fact_id,
        {
          id: factItemEditing.fact_id,
          item_updates: [
            {
              fact_id: factItemEditing.fact_id,
              name: values.name,
              value: values.value,
              category: values.category || undefined,
              is_simulated: !!values.is_simulated,
              // ✅ R81：口径随保存透传（后端 _apply_item_updates 归一；
              //    undefined 归一为 '' = 未区分，与旧行为等价）
              value_kind: values.value_kind ?? "",
            },
          ],
        },
        id,
      );
      msg.success("事实已更新");
      setFactItemModalOpen(false);
      setFactItemEditing(null);
      factItemForm.resetFields();
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string; errorFields?: unknown };
      if (err.errorFields) return;
      msg.error(err.message || "保存失败");
    } finally {
      setFactItemSubmitting(false);
    }
  }

  // ===== ✅ 断链修复：分组删除 / 单条确认 / 矛盾裁决 / 一键清空 =====
  //    此前 factsApi.delete / clearAll 已封装但前端无任何调用点，
  //    openFactEdit 为死代码；分组级删不掉、也不能一键清空。
  const handleDeleteFactGroup = (group: FactGroupLike) => {
    modal.confirm({
      title: "删除事实分组",
      content: `将删除「${group.title}」及其全部 ${group.items?.length ?? 0} 条事实（含已确认与手动录入），不可撤销。`,
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      // ✅ 避免 antd 命令式 confirm 在 onOk 抛错时产生 unhandled rejection：
      //    异常在内部 try/catch 消化，不向外抛出
      onOk: async () => {
        try {
          await factsApi.delete(group.id!, id);
          msg.success("事实分组已删除");
          await loadFacts();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err?.message || "删除失败");
        }
      },
    });
  };

  const handleResolveFactItem = async (item: FactItemLike) => {
    if (!item?.fact_id) return;
    try {
      await factsApi.resolve(item.fact_id, id);
      msg.success("已确认");
      // ✅ 等待刷新完成（2026-09-21）：旧实现未 await，用户紧接着点「一键清空」/
      //    切 Tab 时会读到旧 stats，徽标 /「全部就绪」入口短暂错乱。
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "确认失败");
    }
  };

  const handleResolveConflictItem = async (item: FactItemLike, value: string) => {
    if (!item?.fact_id) return;
    try {
      await factsApi.resolveConflict(item.fact_id, value, id);
      msg.success("矛盾已裁决");
      await loadFacts();
    } catch (e: unknown) {
      const err = e as { message?: string };
      msg.error(err?.message || "裁决失败");
    }
  };

  const handleClearAllFacts = () => {
    if (!id) return;
    modal.confirm({
      title: "一键清空全部事实",
      content:
        "将删除本方案可见的全部全局事实（包含项目共享事实，会影响同项目其它方案），同时重置 AI 增量提取进度并清空相关导出缓存。已上传的资料文档保留，可重新执行「③ AI 提取事实」重建。",
      okText: "全部清空",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          const { data } = await factsApi.clearAll(id);
          setFactsFilter("all");
          setFactsSegmentStats(null);
          setFactsCrossConflicts([]);
          msg.success(`已清空 ${data?.deleted ?? 0} 条事实，提取进度已重置`);
          await loadFacts();
        } catch (e: unknown) {
          const err = e as { message?: string };
          msg.error(err?.message || "清空失败");
        }
      },
    });
  };

  return {
    openFactCreate,
    openFactEdit,
    submitFact,
    openFactItemEdit,
    submitFactItem,
    handleDeleteFactGroup,
    handleResolveFactItem,
    handleResolveConflictItem,
    handleClearAllFacts,
  };
}
