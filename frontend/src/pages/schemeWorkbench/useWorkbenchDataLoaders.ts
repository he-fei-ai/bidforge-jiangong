import { useCallback } from "react";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { factsApi, bidAnalysisApi } from "../../api";
import { normalizeBaGroups, recomputeBaSummary } from "../../utils/bidAnalysis";
import type { BaGroup, BaItemDef, BaStoredItem, BaSummary } from "../../utils/bidAnalysis";
import { isCurrentSchemeRequest } from "./factsShared.ts";
import type { DocRecord } from "../../utils/workflowDerived";

/**
 * ✅ 2026-11-04 拆分（G8 前置 · 低风险）：文档列表 / 提取项目元数据 / 提取结果
 * 三个只读加载器自 SchemeWorkbenchPage 原位搬迁。
 *
 * 纯结构重构，零行为变更：
 *   - 三个 useCallback 的函数体与依赖数组逐字搬运；
 *   - 返回值同名，页面引用零改动；不含任何副作用编排或状态机。
 */
export interface WorkbenchDataLoadersDeps {
  id: string | undefined;
  schemeProjectId: string | undefined;
  msg: { warning: (s: string) => void };
  currentSchemeIdRef: MutableRefObject<string>;
  factsDocsListAbortRef: MutableRefObject<AbortController | null>;
  setDocList: Dispatch<SetStateAction<DocRecord[]>>;
  setBaDefs: Dispatch<SetStateAction<BaItemDef[]>>;
  setBaGroups: Dispatch<SetStateAction<BaGroup[]>>;
  setBaDefsError: Dispatch<SetStateAction<string | null>>;
  setBaItems: Dispatch<SetStateAction<BaStoredItem[]>>;
  setBaSummary: Dispatch<SetStateAction<BaSummary | null>>;
  setBaLoading: (v: boolean) => void;
  setBaError: Dispatch<SetStateAction<string | null>>;
  baDefsRef: MutableRefObject<BaItemDef[]>;
}

export function useWorkbenchDataLoaders(deps: WorkbenchDataLoadersDeps) {
  const {
    id,
    schemeProjectId,
    msg,
    currentSchemeIdRef,
    factsDocsListAbortRef,
    setDocList,
    setBaDefs,
    setBaGroups,
    setBaDefsError,
    setBaItems,
    setBaSummary,
    setBaLoading,
    setBaError,
    baDefsRef,
  } = deps;

  // ✅ 加固：以 scheme id 为主查询（后端反查 project_id），
  //    不再依赖 scheme.project_id —— 该字段缺失时旧实现会静默 return，导致列表恒空
  const loadDocuments = useCallback(async () => {
    if (!id) return;
    const requestedSchemeId = id;
    factsDocsListAbortRef.current?.abort();
    const ac = new AbortController();
    factsDocsListAbortRef.current = ac;
    try {
      const { data } = await factsApi.listDocuments({
        schemeId: requestedSchemeId,
        projectId: schemeProjectId,
        signal: ac.signal,
      });
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current, ac.signal)) return;
      setDocList(data.documents || []);
    } catch (e: unknown) {
      const err = e as { name?: string; message?: string };
      if (err?.name === "AbortError" || ac.signal.aborted) return;
      if (!isCurrentSchemeRequest(requestedSchemeId, currentSchemeIdRef.current)) return;
      msg.warning(err?.message || "加载已上传资料列表失败");
    } finally {
      if (factsDocsListAbortRef.current === ac) factsDocsListAbortRef.current = null;
    }
  }, [id, schemeProjectId, msg]);

  /** 解析项定义 + 分组（后端 /bid-analysis/items 为唯一权威源） */
  const loadBaMeta = useCallback(async () => {
    try {
      const { data } = await bidAnalysisApi.items();
      const defs: BaItemDef[] = data.items || [];
      setBaDefs(defs);
      setBaGroups(normalizeBaGroups(data.groups || [], defs));
      setBaDefsError(null);
    } catch (e: unknown) {
      const err = e as {
        name?: string;
        message?: string;
        response?: { data?: { detail?: unknown } };
      };
      console.warn("加载解析项定义失败:", err?.message || e);
      // 不再只留 console.warn：旧实现失败后组件永久停在「定义加载中…」，
      // 用户既不知道出错也没有重试入口（2026-09-23 遗留项修复）。
      setBaDefsError(String(err?.response?.data?.detail || err?.message || "网络异常"));
    }
  }, []);

  /** 已存储的解析项结果 + 汇总（后端 /bid-analysis/results） */
  const loadBaResults = useCallback(async () => {
    if (!id) return;
    setBaLoading(true);
    setBaError(null);
    try {
      const { data } = await bidAnalysisApi.results(id, schemeProjectId);
      const items: BaStoredItem[] = data.items || [];
      setBaItems(items);
      setBaSummary(data.summary || recomputeBaSummary(items, baDefsRef.current));
    } catch (e: unknown) {
      const err = e as {
        name?: string;
        message?: string;
        response?: { data?: { detail?: string } };
      };
      console.warn("加载结构化提取结果失败:", err?.message || e);
      setBaError(err?.response?.data?.detail || err?.message || "加载解析结果失败");
    } finally {
      setBaLoading(false);
    }
  }, [id, schemeProjectId]);

  return { loadDocuments, loadBaMeta, loadBaResults };
}
