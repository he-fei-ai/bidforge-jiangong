import { useCallback, useEffect } from "react";
import type { FormInstance } from "antd";
import type { ModalFuncProps } from "antd";
import { Input } from "antd";
import { exportApi } from "../../api";
import { findDefaultExportPreset } from "../../utils/workflowDerived";
import type { ChartRenderReadiness } from "../../utils/exportResponse";
import type { MutableRefObject } from "react";
import type { ExportFormatConfig } from "../../types/api";

// =========================================================================
// ✅ any 清理专项（2026-11-02）：本域消费的后端 REST 响应契约 / 页面 state 形状。
//    字段按 hook 内实际写入 + 页面（SchemeWorkbenchPage）实际渲染字段推导，
//    未知字段一律 ? 修饰，不虚构。
// =========================================================================

/** 导出格式预设单条（GET /export/presets 响应 presets[]；页面下拉读 id/name/is_default，回填读 config） */
export interface ExportPresetItem {
  id: string;
  name?: string;
  is_default?: boolean;
  config?: ExportFormatConfig;
}

/** 导出预检问题单条（exportIssues state；页面经 describeExportIssue 读 title/detail/chart_type） */
export interface ExportIssueItem {
  title?: string;
  detail?: string;
  chart_type?: string;
  /** ✅ B-16（R182 · 2026-11-06）：来源标记；`content_audit` = 内容体检并入项。 */
  source?: string;
  /** ✅ B-16：内容体检级别（error / warn），仅用于展示着色，不参与阻断判定。 */
  audit_level?: string;
  /** ✅ R212（2026-11-10 · P3-1）：聚合类问题**涉及处数**（20 章待审核 = 1 条
   *  issue + count=20）。此前类型未声明 ⇒ 前端只能按数组长度显示「1 个问题」，
   *  与同项目「共 151 处」量级差 20 倍（用户误以为只有一个问题）。 */
  count?: number;
  /** ✅ R212（P3-1）：聚合类问题涉及的章节 id（定位链）。 */
  section_ids?: string[];
  /** ✅ R212（P3-4）：导出阻断明细的**可区分标题**（R207 A5 后端已产出，
   *  前端结构化解析此前把它丢了，只因后端把标题拼进了 detail 字符串才偶然可见）。 */
  display_title?: string;
}

/** ✅ B-17（R182 · 2026-11-06）：交叉引用检查三态（无图表集合时为「无法判定」）。 */
export interface CrossrefCheckState {
  undetermined?: boolean;
  reason?: string;
  fig_count?: number;
  tbl_count?: number;
}

/** 导出统计（exportStats state；页面读 total_words/generated_count/section_count/empty_ratio/chart_*） */
export interface ExportStats {
  total_words?: number;
  generated_count?: number;
  section_count?: number;
  empty_ratio?: number;
  chart_total?: number;
  chart_done?: number;
  /** ✅ B-17：交叉引用三态；`undetermined=true` 时前端必须显示「无法判定」而非通过。 */
  crossref_check?: CrossrefCheckState;
  /** ✅ R221：图表渲染就绪度（`/export/check` 加法字段，缺省时页面不展示提示）。 */
  chart_render_readiness?: ChartRenderReadiness;
}

/** 导出预检结论（exportPreflight state；页面读 released/total/stale/grade/verdict/rule_version） */
export interface ExportPreflight {
  released?: boolean;
  total?: number;
  stale?: boolean;
  grade?: string;
  verdict?: string;
  rule_version?: string;
}

/** 重跑计划（rerunPlan state；页面读 rerunnable_sections/rerunnable_count） */
export interface ExportRerunPlan {
  rerunnable_sections?: string[];
  rerunnable_count?: number;
}

/** 导出缓存单条（GET /export/cache-status 响应 items[]；页面表格读 exists/created_at/content_fingerprint/result_path） */
export interface ExportCacheStatusItem {
  exists?: boolean;
  created_at?: string;
  content_fingerprint?: string;
  result_path?: string;
}

/** 导出缓存状态（GET /export/cache-status 响应；cacheStatus state，页面读 total/stale/items） */
export interface ExportCacheStatus {
  items: ExportCacheStatusItem[];
  total: number;
  stale: number;
}

/**
 * R-P1 拆分（纯结构重构）：导出格式预设域处理器，自 SchemeWorkbenchPage 原位搬迁。
 * 行为零变更：所有状态仍由页面持有，本 hook 仅承载处理器与预设初始化 effect；
 * 页面在原代码位置调用本 hook（调用点不动 ⇒ effect 注册顺序不变）。
 */
export interface ExportPresetsDeps {
  id: string | undefined;
  exportForm: FormInstance;
  presetOpLoading: string | null;
  setPresetOpLoading: (v: string | null) => void;
  cacheAbortRef: MutableRefObject<AbortController | null>;
  setCacheLoading: (v: boolean) => void;
  selectedPresetId: string;
  currentSchemeIdRef: MutableRefObject<string>;
  presetListAbortRef: MutableRefObject<AbortController | null>;
  setExportPresets: (v: ExportPresetItem[]) => void;
  setSelectedPresetId: (v: string) => void;
  setExportIssues: (v: ExportIssueItem[]) => void;
  setExportStats: (v: ExportStats | null) => void;
  setExportPreflight: (v: ExportPreflight | null) => void;
  // 本 hook 仅重置（只写 null）：占位符报告由页面导出预检域写入，元素契约随页面域收敛
  setPlaceholderReport: (v: null) => void;
  setRerunPlan: (v: ExportRerunPlan | null) => void;
  setCacheStatus: (v: ExportCacheStatus | null) => void;
  msg: {
    success: (s: string) => void;
    error: (s: string) => void;
    warning: (s: string) => void;
    info: (s: string) => void;
  };
  modal: {
    confirm: (cfg: ModalFuncProps) => void;
  };
}

export function useExportPresets(deps: ExportPresetsDeps) {
  const {
    id,
    exportForm,
    presetOpLoading,
    setPresetOpLoading,
    selectedPresetId,
    cacheAbortRef,
    setCacheLoading,
    currentSchemeIdRef,
    presetListAbortRef,
    setExportPresets,
    setSelectedPresetId,
    setExportIssues,
    setExportStats,
    setExportPreflight,
    setPlaceholderReport,
    setRerunPlan,
    setCacheStatus,
    msg,
    modal,
  } = deps;
  const fetchExportPresets = useCallback(
    async (sid: string) => {
      presetListAbortRef.current?.abort();
      const controller = new AbortController();
      presetListAbortRef.current = controller;
      try {
        const { data } = await exportApi.presets.list(sid, controller.signal);
        if (
          controller.signal.aborted ||
          currentSchemeIdRef.current !== sid ||
          presetListAbortRef.current !== controller
        )
          return;
        const presets: ExportPresetItem[] = data.presets || [];
        setExportPresets(presets);
        const defaultPreset = findDefaultExportPreset(presets);
        if (defaultPreset) {
          setSelectedPresetId(defaultPreset.id);
          exportForm.setFieldsValue(defaultPreset.config || {});
        } else {
          setSelectedPresetId("");
        }
      } catch {
        // 非致命：预设缺失不影响导出；切方案产生的取消不提示错误。
      } finally {
        if (presetListAbortRef.current === controller) presetListAbortRef.current = null;
      }
    },
    [exportForm],
  );
  // ✅ R-1：手动刷新预设（点"刷新"按钮）— 与保存/删除/设默认后的自动刷新区分，
  //    否则自动刷新会把 presetOpLoading 从 "save" 覆盖成 "refresh" 造成按钮状态跳变。
  const handleRefreshPresets = async () => {
    if (!id || presetOpLoading) return;
    const sid = id;
    setPresetOpLoading("refresh");
    try {
      await fetchExportPresets(sid);
    } finally {
      setPresetOpLoading(null);
    }
  };
  useEffect(() => {
    exportForm.resetFields();
    setExportPresets([]);
    setSelectedPresetId("");
    setExportIssues([]);
    setExportStats(null);
    setExportPreflight(null);
    setPlaceholderReport(null);
    setRerunPlan(null);
    setCacheStatus(null);
    if (id) fetchExportPresets(id);
  }, [id, fetchExportPresets]);
  const handleSavePreset = async () => {
    // ✅ BUG 修复：id 来自 useParams（string | undefined），旧实现未判空，
    //    直接把 undefined 传给 API（类型报错 + 请求 /schemes/undefined/...）
    if (!id || presetOpLoading) return;
    const sid = id;
    const config = exportForm.getFieldsValue(true);
    // ✅ R-2：弃用 window.prompt —— 浏览器原生弹窗既不受 AntD 主题影响、
    //    也不能通过 onOk 受控取消，且测试环境里 jsdom 无 prompt API 会失败。
    //    改用 Modal.confirm + 内联 Input，与项目其他确认弹窗风格一致。
    //    okButtonProps.disabled 在 modal.confirm 中是静态快照（不会因 Input 变化重渲染），
    //    所以按钮保持启用、在 onOk 内做空名称校验：拒绝提交并让弹窗保持打开。
    //    注意：modal.confirm 返回值在部分 AntD 版本不是 Promise，
    //    因此 loading 清理放在 onOk / onCancel 的 finally 中（不依赖返回值）。
    // 只改 nameRef.current，绑定本身从不重新赋值 → const（eslint prefer-const）
    const nameRef: { current: string } = { current: "" };
    setPresetOpLoading("save");
    modal.confirm({
      title: "保存为格式预设",
      content: (
        <Input
          size="small"
          autoFocus
          placeholder="请输入格式预设名称"
          maxLength={40}
          style={{ marginTop: 8 }}
          onChange={(e) => {
            nameRef.current = (e.target.value || "").trim();
          }}
          onPressEnter={() => {
            // Enter 键直接确认；onOk 内会做空名称校验
            const box = document.querySelector(".ant-modal-confirm-btns .ant-btn-primary");
            (box as HTMLButtonElement | null)?.click();
          }}
        />
      ),
      okText: "保存",
      cancelText: "取消",
      onOk: async () => {
        try {
          const name = nameRef.current;
          if (!name) {
            // 空名称：throw 让 modal.confirm 保持打开（AntD 特性）
            msg.warning("请输入格式预设名称");
            return Promise.reject(new Error("empty name"));
          }
          await exportApi.presets.create(sid, name, config);
          if (currentSchemeIdRef.current === sid) {
            msg.success("已保存为格式预设");
            await fetchExportPresets(sid);
          }
        } catch (e: unknown) {
          // 结构化窄断言：仅读取错误对象上真实存在的 message
          const err = e as { message?: string };
          if (currentSchemeIdRef.current === sid) msg.error(err?.message || "保存预设失败");
          // 让弹窗保持打开，方便用户重试
          return Promise.reject(e);
        } finally {
          setPresetOpLoading(null);
        }
      },
      onCancel: () => setPresetOpLoading(null),
    });
  };
  const handleSetDefaultPreset = async (pid: string) => {
    if (!id || presetOpLoading) return;
    const sid = id;
    setPresetOpLoading("setDefault");
    try {
      await exportApi.presets.setDefault(sid, pid);
      if (currentSchemeIdRef.current !== sid) return;
      msg.success("已设为项目默认预设");
      await fetchExportPresets(sid);
    } catch (e: unknown) {
      const err = e as { message?: string };
      if (currentSchemeIdRef.current === sid) msg.error(err?.message || "操作失败");
    } finally {
      setPresetOpLoading(null);
    }
  };
  const handleDeletePreset = (pid: string) => {
    if (!id || presetOpLoading) return;
    const sid = id;
    setPresetOpLoading("delete");
    // ✅ BUG 修复（2026-10-04）：AntD v5 的 modal.confirm() 不返回 Promise，
    //   不能在返回值上 .finally()（会 TypeError: ... is not a function）。
    //   loading 清理放 onOk / onCancel 的 finally，失败重试时也能清掉 loading
    //   —— 否则接口 reject 后弹窗留着、按钮却永远转圈。
    modal.confirm({
      title: "确认删除该格式预设？",
      okText: "删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: async () => {
        try {
          try {
            await exportApi.presets.remove(sid, pid);
            if (currentSchemeIdRef.current !== sid) return;
            msg.success("已删除预设");
            if (selectedPresetId === pid) setSelectedPresetId("");
            await fetchExportPresets(sid);
          } catch (e: unknown) {
            const err = e as { message?: string };
            if (currentSchemeIdRef.current === sid) msg.error(err?.message || "删除失败");
            // 让弹窗保持打开，方便用户重试
            return Promise.reject(e);
          }
        } finally {
          setPresetOpLoading(null);
        }
      },
      onCancel: () => setPresetOpLoading(null),
    });
  };
  // ===== ✅ 接线 exportApi.cacheStatus：查询仍有效的导出缓存（后端已过滤僵尸行）=====
  // R-P1 拆分：loadCacheStatus 自页面 4519 行原位搬迁（导出缓存域归口本 hook）。
  const loadCacheStatus = async () => {
    if (!id) return;
    const sid = id;
    cacheAbortRef.current?.abort();
    const controller = new AbortController();
    cacheAbortRef.current = controller;
    setCacheLoading(true);
    try {
      const { data } = await exportApi.cacheStatus(sid, controller.signal);
      if (
        controller.signal.aborted ||
        currentSchemeIdRef.current !== sid ||
        cacheAbortRef.current !== controller
      )
        return;
      setCacheStatus(data);
    } catch (e: unknown) {
      // 结构化窄断言：仅读取错误对象上真实存在的 name / code / message
      const err = e as { name?: string; code?: string; message?: string };
      const canceled = err?.name === "AbortError" || err?.code === "ERR_CANCELED";
      if (!canceled && currentSchemeIdRef.current === sid) {
        msg.error(err.message || "读取导出缓存状态失败");
      }
    } finally {
      if (cacheAbortRef.current === controller) {
        cacheAbortRef.current = null;
        setCacheLoading(false);
      }
    }
  };
  return {
    fetchExportPresets,
    handleRefreshPresets,
    handleSavePreset,
    handleSetDefaultPreset,
    handleDeletePreset,
    loadCacheStatus,
  };
}
