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
  //
  // ✅ R210（2026-11-10 · P1）：补 ``skipAbortPrev`` 参数（轮询专用）。
  //    旧行为：每次调用都 abort 上一次在飞的列表请求（后发者胜），这与「刷新」
  //    按钮语义一致；但 R203 FE-9 的 3s 轮询也走同一入口 ⇒ **轮询会 abort
  //    用户自己点的「刷新」**，被 abort 的请求在下方 ``ac.signal.aborted`` 处
  //    **静默吞掉**（无 toast / 无日志）⇒ 用户点了刷新却什么都没发生，且解析
  //    结束后轮询停摆 ⇒ 列表停在旧状态直到用户再点一次（R208 FE-4 只解决了
  //    「轮询打轮询」，本轮补上「轮询打用户请求」这一面）。
  //    现在：轮询入口传 ``skipAbortPrev=true`` ⇒ **不打断**在飞的用户请求；
  //    用户手动刷新仍保持「后发者胜」（默认路径 abort 前一次）。
  const loadDocuments = useCallback(async (skipAbortPrev = false) => {
    if (!id) return;
    const requestedSchemeId = id;
    if (!skipAbortPrev) {
      factsDocsListAbortRef.current?.abort();
    }
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
