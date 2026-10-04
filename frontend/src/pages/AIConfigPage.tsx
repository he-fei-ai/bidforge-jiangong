import { useEffect, useRef, useState } from "react";
import {App, Card, Form, Input, Select, Button, message,
  Tag, Table, Typography, Space, InputNumber, Tabs, Tooltip,
  Statistic, Row, Col, Switch, Modal, Empty, List, Alert, Checkbox,} from "antd";
import {
  PlusOutlined, DeleteOutlined, ThunderboltOutlined,
  CheckCircleOutlined, CloseCircleOutlined, LinkOutlined,
  ApiOutlined, SettingOutlined, BarChartOutlined,
  SafetyCertificateOutlined, FireOutlined, GlobalOutlined,
  ArrowUpOutlined, ArrowDownOutlined, SortAscendingOutlined,
  ExportOutlined, ImportOutlined, ReloadOutlined, ClearOutlined,
  WarningOutlined, KeyOutlined, HistoryOutlined, NodeIndexOutlined,
} from "@ant-design/icons";

import { aiApi } from "../api";
import { useAntdMessageHub } from "../utils/activityCenter";
import { TtlCache } from "../utils/ttlCache";
import { useAiConfigGovernance } from "../hooks/useAiConfigGovernance";
// ✅ D7（2026-09-23）：页面直接引用后端契约类型（见 types/aiConfig.ts）
import type {
  AIAuditLogItem, AIConfigAuditChange, AIConfigAuditItem, AIConfigItem,
  AIConfigPrecheckResponse, AIConfigPresetRaw, AIDynamicModel, AIHealth,
  AINetworkProbe, AIProviderPlan, AIProviderPreset, AIRuntimeResponse,
  AISceneRouteItem, AIUsageStats,
} from "../types/aiConfig";

import { PageHero, StatCards, StatItem } from '../utils/ui';

const { Title, Text } = Typography;

// ✅ 性能优化（2026-09-24 · 遗留项 #2）：/ai/stats 后端一次聚合 9 条 SQL，
//    页面每次挂载/刷新/清理都会重跑。模块级 30s TTL 缓存：窗口内复用上次
//    响应，大幅降低后端聚合压力；清理审计日志成功后显式 clear() 强制重拉。
//    key 按 days 分桶（stats:1 / stats:30 / stats:365 …），切换周期自动隔离。
const statsCache = new TtlCache<AIUsageStats | null>(30_000);

/** 健康状态 → 展示颜色（no_key / key_invalid 都是「配置存在但没就绪」的警告态） */
const healthColor = (status?: string) =>
  status === "configured" ? "#52c41a"
    : (status === "key_invalid" || status === "no_key" || status === "env_corrupt") ? "#faad14"
      : "#ff4d4f";

/** /ai/health 的 status → 中文标签
 *  configured    = 已配置且 Key 可用
 *  key_invalid   = 有密文但解不开（换过 FERNET_KEY / 删过 secret_key.key）
 *  no_key        = ✅ 新增：压根没填 Key（此前被错报成 key_invalid，
 *                  前端提示「无法解密」把用户引向排查加密密钥这一错误方向）
 *  not_configured= 没有任何 is_active=1 的配置
 *  env_corrupt   = ✅ 2026-09-25 新增：「当前生效环境」的值损坏。
 *                  运行时拒绝按通用环境继续（否则会拿错环境的密钥/地址），
 *                  但**配置其实一条都没坏** —— 必须与「未配置」区分开，
 *                  否则用户会去重建模型配置，而真正要做的只是清空环境标签。
 */
const HEALTH_LABELS: Record<string, string> = {
  configured: "已配置",
  key_invalid: "密钥失效",
  no_key: "未填密钥",
  not_configured: "未配置",
  env_corrupt: "环境值异常",
};

// ✅ D7（2026-09-23）：供应商预设与动态模型列表直接复用后端契约类型，
//    不再各写一份「长得像但字段可选性不同」的本地接口（此前 tsc 因二者不兼容报错，
//    正是类型契约的价值体现）。
type PlanInfo = AIProviderPlan;
type ProviderInfo = AIProviderPreset;

export default function AIConfigPage() {
  const { message: _antdMsg, modal } = App.useApp();
  const msg = useAntdMessageHub(_antdMsg, "文本模型配置");
  // ✅ D7 收敛（2026-09-23）：全部改为后端契约类型（types/aiConfig.ts），
  //    细节字段拼错/读取不存在的字段从此在 tsc 阶段暴露。
  const [configs, setConfigs] = useState<AIConfigItem[]>([]);
  const [presets, setPresets] = useState<Record<string, AIConfigPresetRaw>>({});
  const [providers, setProviders] = useState<Record<string, ProviderInfo>>({});
  const [health, setHealth] = useState<AIHealth | null>(null);
  const [stats, setStats] = useState<AIUsageStats | null>(null);
  const [statsDays, setStatsDays] = useState<number>(30);
  const [auditLogs, setAuditLogs] = useState<AIAuditLogItem[]>([]);
  // ===== 用量日志：筛选 / 分页 / 刷新 / 清理 =====
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditPage, setAuditPage] = useState(1);
  const [auditPageSize, setAuditPageSize] = useState(20);
  const [auditFilter, setAuditFilter] = useState<{ provider_name?: string; action?: string; scene?: string; success?: string; days?: number }>({});
  const [auditProviders, setAuditProviders] = useState<string[]>([]);
  const [auditActions, setAuditActions] = useState<string[]>([]);
  // ✅ 2026-09-25：库里出现过的场景（后端随日志一起回传），供「场景」筛选下钻
  const [auditScenes, setAuditScenes] = useState<string[]>([]);
  /** 主 Tab 受控：让「按场景聚合」标签可以一键跳到「用量日志」并带上筛选 */
  const [mainTab, setMainTab] = useState<string>("configs");
  const [auditLoading, setAuditLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [form] = Form.useForm();
  const watchPlan = Form.useWatch("plan", form) as string | undefined;
  // ✅ 新增：请求方式（normal 普通请求 / stream 流式请求）
  const watchRequestMode = Form.useWatch("request_mode", form) as string | undefined;
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    ok: boolean;
    msg: string;
    label?: string;
    category?: string;
    suggestion?: string;
    // ✅ 探测成功但走了非流式回退时的提示（流式接口不可用）
    warning?: string;
    network?: AINetworkProbe;
  } | null>(null);
  const [editingId, setEditingId] = useState<string>("");
  const [addModalOpen, setAddModalOpen] = useState(false);
  const [selectedProvider, setSelectedProvider] = useState<string>("deepseek");
  const [customModels, setCustomModels] = useState<AIDynamicModel[]>([]);
  // ✅ 新增：平台返回的模型数超过后端上限时被截断，界面需明示（否则用户以为就这么多）
  const [customModelsTruncated, setCustomModelsTruncated] = useState(false);
  const [fetchingModels, setFetchingModels] = useState(false);
  const [customFetchError, setCustomFetchError] = useState<string>("");
  // ===== ✅ 接线 aiApi.updateFallbackChain：熔断降级链顺序配置 =====
  const [chain, setChain] = useState<AIConfigItem[]>([]);
  const [chainSaving, setChainSaving] = useState(false);
  // ===== ✅ 接线 aiApi.precheck：仅 DNS/TCP 连通性预检（不发认证请求，比「测试连接」更轻）=====
  const [prechecking, setPrechecking] = useState(false);
  const [precheckResult, setPrecheckResult] = useState<AIConfigPrecheckResponse | null>(null);
  // ===== ✅ 2026-09-23 新增：场景模型路由（多模型）/ 配置变更审计 =====
  //     状态、加载与操作收敛到 useAiConfigGovernance hook。
  const governance = useAiConfigGovernance(msg);
  const {
    sceneRoutes, sceneSaving, activeEnv, envOptions, envSaving,
    runtime, runtimeDraft, runtimeSaving, configAudits, rollingBack,
    sceneActiveEnv, envError,
    loadSceneRoutes, loadConfigAudits, loadEnv, loadRuntime,
    handleSceneRouteChange, handleSaveRuntime, handleEnvChange,
    setRuntimeDraft, setRollingBack, setRefresh,
  } = governance;

  // configs 变化时按 priority ASC 同步可排序的降级链（后端 priority 越小越先尝试）
  useEffect(() => {
    setChain([...configs].sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0)));
  }, [configs]);

  const moveChain = (index: number, dir: -1 | 1) => {
    const next = [...chain];
    const j = index + dir;
    if (j < 0 || j >= next.length) return;
    [next[index], next[j]] = [next[j], next[index]];
    setChain(next);
  };

  const handleSaveChain = async () => {
    setChainSaving(true);
    try {
      await aiApi.updateFallbackChain(chain.map((c) => ({ id: c.id })));
      msg.success("降级顺序已保存，AI 调用失败时会按此顺序依次尝试");
      load();
    } catch (e: any) {
      msg.error(e.message || "保存降级顺序失败");
    } finally {
      setChainSaving(false);
    }
  };

  const handlePrecheck = async () => {
    let baseUrl = "";
    try {
      baseUrl = form.getFieldValue("base_url") || "";
    } catch { /* form 未挂载 */ }
    if (!baseUrl && !editingId) {
      msg.warning("请先填写 Base URL");
      return;
    }
    setPrechecking(true);
    setPrecheckResult(null);
    try {
      // ✅ 修复：原实现只传表单里的 base_url —— 编辑一条「按量计费」配置时
      //    地址字段是只读回填的还好，但若地址为空/被清空就完全测不了。
      //    现补传 config_id，后端可回退用库里的地址（与「测试连接」口径一致）。
      const { data } = await aiApi.precheck({ base_url: baseUrl, config_id: editingId || "" });
      setPrecheckResult(data);
      if (data.ok) msg.success("连通性预检通过");
      else msg.warning(data.message || "连通性预检未通过");
    } catch (e: any) {
      setPrecheckResult({ ok: false, message: e.message || "预检请求失败" });
      msg.error(e.message || "预检失败");
    } finally {
      setPrechecking(false);
    }
  };

  // ✅ 用量日志独立加载：支持筛选 / 分页 / 刷新（原实现只在首次加载时拉 50 条，无法翻页或过滤）
  const loadAuditLogs = async (
    page = auditPage,
    pageSize = auditPageSize,
    filter = auditFilter
  ) => {
    setAuditLoading(true);
    try {
      const { data: logs } = await aiApi.auditLogs({
        limit: pageSize,
        offset: (page - 1) * pageSize,
        provider_name: filter.provider_name || "",
        action: filter.action || "",
        scene: filter.scene || "",
        success: filter.success ?? "",
        days: filter.days || 0,
      });
      setAuditLogs(logs.items || []);
      setAuditTotal(logs.total || 0);
      setAuditProviders(logs.providers || []);
      setAuditActions(logs.actions || []);
      setAuditScenes(logs.scenes || []);
    } catch (e: any) {
      msg.error(e.message || "加载用量日志失败");
    } finally {
      setAuditLoading(false);
    }
  };

  const loadStats = async (days = statsDays) => {
    const key = `stats:${days}`;
    const cached = statsCache.get(key);
    if (cached.hit) {
      setStats(cached.value);
      return;
    }
    try {
      const { data: s } = await aiApi.stats(days);
      statsCache.set(key, s);
      setStats(s);
    } catch {
      // 统计失败不影响主配置展示
    }
  };

  const load = async () => {
    try {
      const [{ data: cfg }, { data: models }, { data: h }] = await Promise.all([
        aiApi.getConfig(),
        aiApi.getModels(),
        aiApi.health(),
      ]);
      setConfigs(cfg.items || []);
      setPresets(cfg.presets || {});
      setProviders(models.providers || {});
      setHealth(h);
      // ✅ 修复：原实现仅在 status==="configured" 时加载统计与日志，
      //    而 key 解密失败（key_invalid）或历史日志存在时也要能看到用量。
      await Promise.all([
        loadStats(),
        loadAuditLogs(1, auditPageSize, auditFilter),
        loadSceneRoutes(),
        loadConfigAudits(),
        loadEnv(),
        loadRuntime(),
      ]);
    } catch (e: any) {
      msg.error(e.message || "加载失败");
    }
  };

  useEffect(() => { load(); setRefresh(load); }, []);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      if (editingId) values.id = editingId;
      const { data } = await aiApi.saveConfig(values);
      // ✅ 修复：后端为防「零主配置」（编辑当前使用配置并关掉开关会让 AI 全线不可用）
      //    可能会保留 is_active 并回传 warning，原实现只报「保存成功」，用户不知情。
      // ✅ 2026-09-23：后端会把「数值越界被自动收敛」的字段放在 warnings 里如实回传，
      //    必须提示 —— 否则界面填了并发 8、实际生效 5，用户以为已生效。
      const warns: string[] = Array.isArray(data?.warnings) ? data.warnings : [];
      if (data?.warning) msg.warning(data.warning);
      if (warns.length) msg.warning(`部分参数超出合法范围，已自动调整：${warns.join("；")}`);
      if (!data?.warning && warns.length === 0) msg.success("配置保存成功");
      setAddModalOpen(false);
      form.resetFields();
      setEditingId("");
      setTestResult(null);
      load();
    } catch (e: any) {
      if (e.errorFields) return;
      msg.error(e.response?.data?.detail || e.message || "保存失败");
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const values = await form.validateFields();
      const { data } = await aiApi.testConfig({
        config_id: editingId || "",       // 如果是编辑已有配置，把 id 传过去让后端从 DB 读 key
        provider_name: values.provider_name,
        plan: values.plan || "pay_as_you_go",
        api_key: values.api_key || "",   // 表单没填时就是空字符串，后端会用 config_id 去 DB 取
        base_url: values.base_url,
        model: values.model,
        // ✅ 探测请求沿用表单的 max_tokens：部分平台单次上限低于默认 8192，
        //    用默认值探测会被拒绝（400），而用户填的信息其实完全正确。
        max_tokens: values.max_tokens || 8192,
        // ✅ 新增：按表单所选「请求方式」优先探测（失败自动用另一种方式兜底），
        //    否则用户勾了流式、探测却按普通请求走，测出来的结论与真实链路不符。
        request_mode: values.request_mode || "normal",
      });
      setTestResult(data.ok
        ? { ok: true, msg: data.response || "", network: data.network, warning: data.warning }
        : {
            ok: false,
            msg: data.error || "",
            label: data.label,
            category: data.category,
            suggestion: data.suggestion,
            network: data.network,
          });
    } catch (e: any) {
      setTestResult({ ok: false, msg: e.message || "测试失败", category: "http" });
    }
    setTesting(false);
  };

  const handleToggle = async (id: string) => {
    try {
      const { data } = await aiApi.toggleConfig(id);
      // ✅ 修复：切到「没填 Key / 密钥失效」的配置时后端会回传 warning，
      //    原实现一律提示成功 —— 用户以为切好了，实际下一次生成必然失败。
      if (data?.warning) msg.warning(data.warning);
      else msg.success("已设为当前使用平台");
      load();
    } catch (e: any) {
      msg.error(e.message || "切换失败");
    }
  };

  const handleDelete = async (id: string) => {
    modal.confirm({
      title: "确认删除",
      content: "删除后无法恢复，确定要删除此配置吗？",
      okText: "删除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const { data } = await aiApi.deleteConfig(id);
          // ✅ 2026-09-25：删除会连带解除引用该配置的场景模型路由。
          //    此前只回「已删除」，用户不知道若干场景已经悄悄改回共用主配置。
          const cleared = data?.cleared_scene_routes || 0;
          if (cleared > 0) {
            msg.info(`已删除；同时解除 ${cleared} 条场景模型路由（这些场景改回共用「当前使用」配置）`);
          } else if (data?.warning) {
            msg.warning(data.warning);
          } else {
            msg.success("已删除");
          }
          load();
        } catch (e: any) {
          msg.error(e.message || "删除失败");
        }
      },
    });
  };

  // ✅ 2026-09-23 新增：清除已保存的 API Key（收回密钥）
  const handleClearKey = (record: any) => {
    modal.confirm({
      title: "确认清除 API Key",
      content: `将清除「${record.provider_name} / ${record.model}」已保存的 API Key；`
        + "清除后该配置无法发起 AI 调用，需要重新填写才能使用。",
      okText: "清除",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const { data } = await aiApi.clearConfigKey(record.id);
          if (data?.warning) msg.warning(data.warning);
          else if (data?.unchanged) msg.info("该配置本来就没有保存 API Key");
          else msg.success("已清除 API Key");
          load();
        } catch (e: any) {
          msg.error(e.response?.data?.detail || e.message || "清除失败");
        }
      },
    });
  };

  // ✅ 2026-09-23 新增：场景路由/运行时开关/多环境操作已收敛到 useAiConfigGovernance hook

  // ✅ 2026-09-23 新增（G6）：回滚配置到某次变更之前（密钥不参与回滚）
  const handleRollback = (record: any) => {
    const changes: AIConfigAuditChange[] = record.changes || [];
    // 「是否同时还原当前使用标记」用普通对象承载：确认框内容与 onOk 共享同一引用，
    // 无需为这个一次性开关引入额外的 React 状态。
    const opt = { includeActive: false };
    modal.confirm({
      title: "确认回滚该次变更",
      content: (
        <div style={{ fontSize: 12 }}>
          <div style={{ marginBottom: 8 }}>
            将把该配置恢复到 <b>{record.action_label || record.action}</b> 之前的状态：
          </div>
          <ul style={{ paddingLeft: 18, margin: 0 }}>
            {changes.map((c) => (
              <li key={c.field}>
                {c.label}：<Text delete>{String(c.before)}</Text> → <Text strong>{String(c.after)}</Text>
              </li>
            ))}
            {changes.length === 0 && <li>（该记录无结构化差异信息，将按快照恢复）</li>}
          </ul>
          <div style={{ marginTop: 8, color: "#888" }}>
            注：API Key <b>不参与回滚</b>（不会复活旧密钥，也不会清掉当前密钥）。
          </div>
          <Checkbox
            style={{ marginTop: 8, fontSize: 12 }}
            onChange={(e) => { opt.includeActive = e.target.checked; }}
          >
            同时还原「当前使用」标记（默认不还原；可能切换主配置，且不允许出现零主配置）
          </Checkbox>
        </div>
      ),
      okText: "回滚",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        setRollingBack(record.id);
        try {
          const { data } = await aiApi.rollbackConfig(
            record.config_id, record.id, opt.includeActive);
          if (data?.warning) msg.warning(data.warning);
          const n = (data.changes || []).length;
          msg.success(`已回滚${n ? `（${n} 个字段）` : ""}`);
          await load();
        } catch (e: any) {
          msg.error(e.response?.data?.detail || e.message || "回滚失败");
        } finally {
          setRollingBack("");
        }
      },
    });
  };

  const handleEdit = (record: any) => {
    setEditingId(record.id);
    setSelectedProvider(record.provider_name);
    setAddModalOpen(true);
    setTestResult(null);
    form.setFieldsValue({
      provider_name: record.provider_name,
      plan: record.plan || "pay_as_you_go",
      api_key: "",
      base_url: record.base_url,
      model: record.model,
      max_tokens: record.max_tokens,
      temperature: record.temperature,
      timeout: record.timeout,
      concurrency: record.concurrency,
      // ✅ 新增：请求方式回显（历史配置无该字段时按普通请求）
      request_mode: record.request_mode || "normal",
      // ✅ 2026-09-23（多环境）：环境标签回显（历史配置为空 = 通用）
      env: record.env || "",
      is_active: record.is_active,
      remark: record.remark || "",
    });
  };


  // ✅ 新增：批量连通性预检 —— 一眼看出哪些「备选配置」其实是假备胎
  const [precheckAllLoading, setPrecheckAllLoading] = useState(false);
  const [precheckAllResult, setPrecheckAllResult] = useState<Record<string, any>>({});
  const handlePrecheckAll = async () => {
    setPrecheckAllLoading(true);
    try {
      const { data } = await aiApi.precheckAll();
      const map: Record<string, any> = {};
      (data.items || []).forEach((it: any) => { map[it.id] = it; });
      setPrecheckAllResult(map);
      if (data.fail_count > 0) {
        msg.warning(`${data.ok_count}/${data.total} 个配置网络可达，${data.fail_count} 个不可达`);
      } else {
        msg.success(`全部 ${data.total} 个配置网络可达`);
      }
    } catch (e: any) {
      msg.error(e.message || "批量预检失败");
    } finally {
      setPrecheckAllLoading(false);
    }
  };

  // ✅ 新增：配置导出（不含 API Key），便于备份 / 迁移到其他机器
  const handleExport = async () => {
    try {
      const { data } = await aiApi.exportConfig();
      // ✅ 编码修复：导出 JSON 前置 UTF-8 BOM（\uFEFF），避免 Windows 记事本打开乱码；
      //    重新导入时（handleImport）会先剥离 BOM，不影响解析。
      const blob = new Blob(["\uFEFF" + JSON.stringify(data, null, 2)], {
        type: "application/json;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `ai-config-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      msg.success(`已导出 ${data.count} 条配置（不含 API Key，导入后需重新填写）`);
    } catch (e: any) {
      msg.error(e.message || "导出失败");
    }
  };

  // ✅ 新增：配置导入（覆盖同名或新增，Key 需重新填写）
  const handleImport = async (file: File) => {
    setImporting(true);
    try {
      // ✅ 编码修复：剥离文件可能携带的 UTF-8 BOM（\uFEFF），否则 JSON.parse 会报错，
      //    导致「在本工具导出后在记事本中打开过再导入」等场景直接失败。
      const text = (await file.text()).replace(/^\uFEFF/, "");
      const parsed = JSON.parse(text);
      const items = Array.isArray(parsed) ? parsed : parsed.items || [];
      if (!items.length) {
        msg.warning("文件中没有可导入的配置");
        return;
      }
      const { data } = await aiApi.importConfig(items, false, false);
      // ✅ 修复：导入后若系统仍无「当前使用」配置，后端回传 warning（原本只有成功提示，
      //    用户以为导入即可用，实际依旧一个都生成不了）。
      if (data.warning) msg.warning(data.warning);
      else msg.success(`导入完成：新增/更新 ${data.imported} 条，跳过 ${data.skipped} 条。${data.hint || ""}`);
      // ✅ 2026-09-23：导入文件里的越界数值会被后端静默收敛，如实提示条目数
      if (data.clamped_items > 0) {
        msg.warning(`其中 ${data.clamped_items} 条配置的参数超出合法范围（如并发数 > 5、温度 > 2），已按合法区间保存`);
      }
      load();
    } catch (e: any) {
      msg.error(e.message || "导入失败，请确认文件为本系统导出的配置 JSON");
    } finally {
      setImporting(false);
    }
  };

  // ✅ 新增：用量日志清理（原实现只增不删，日志无限增长）
  const handleCleanupLogs = () => {
    let keepDays = 30;
    let onlyFailed = false;
    modal.confirm({
      title: "清理历史用量日志",
      content: (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            仅保留最近若干天的调用日志，更早的记录会被删除，用于控制数据库体积。
          </Text>
          <Space>
            <span>保留最近</span>
            <InputNumber
              min={1}
              max={3650}
              defaultValue={30}
              onChange={(v) => { keepDays = Number(v) || 30; }}
            />
            <span>天</span>
          </Space>
          <Space>
            <Switch size="small" onChange={(v) => { onlyFailed = v; }} />
            <Text style={{ fontSize: 12 }}>仅清理失败记录（保留成功样本用于统计）</Text>
          </Space>
        </Space>
      ),
      okText: "清理",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const { data } = await aiApi.cleanupAuditLogs(keepDays, onlyFailed);
          msg.success(`已清理 ${data.deleted} 条历史日志`);
          // ✅ 审计日志已删除 → 统计必然变化，必须强制重拉而非命中 30s 缓存
          statsCache.clear();
          setAuditPage(1);
          loadAuditLogs(1, auditPageSize, auditFilter);
          loadStats();
        } catch (e: any) {
          msg.error(e.message || "清理失败");
        }
      },
    });
  };

  const handleProviderChange = (v: string) => {
    setSelectedProvider(v);
    setCustomModels([]);
    setCustomModelsTruncated(false);
    setCustomFetchError("");
    // 不同供应商的 Key 不可复用；否则切换后保存/测试可能把上一供应商密钥发到新地址。
    form.setFieldsValue({ api_key: "" });
    if (v !== "custom") {
      const plan = form.getFieldValue("plan") || "pay_as_you_go";
      applyPlanDefaults(v, plan);
    } else {
      form.setFieldsValue({ base_url: "", model: "" });
    }
    setTestResult(null);
  };

  // ✅ 修复竞态：请求序号守卫——慢的旧请求响应不再覆盖新结果
  const fetchModelsSeqRef = useRef(0);
  const tryFetchCustomModels = async (baseUrl: string, apiKey: string, configId?: string) => {
    if (!baseUrl) return;  // base_url 必须；apiKey 在有 configId 时后端会从 DB 读
    const seq = ++fetchModelsSeqRef.current;
    setFetchingModels(true);
    setCustomFetchError("");
    try {
      const { data } = await aiApi.fetchCustomModels(baseUrl, apiKey, configId);
      if (seq !== fetchModelsSeqRef.current) return; // 已有更新的请求发出，丢弃旧响应
      if (data.ok) {
        const fetched = data.models || [];
        setCustomModels(fetched);
        setCustomModelsTruncated(!!data.truncated);
        if (fetched.length > 0 && !form.getFieldValue("model")) {
          form.setFieldsValue({ model: fetched[0].value });
        }
      } else {
        setCustomFetchError(data.error || "获取模型列表失败");
        setCustomModels([]);
        setCustomModelsTruncated(false);
      }
    } catch (e: any) {
      if (seq !== fetchModelsSeqRef.current) return;
      setCustomFetchError(e.message || "获取模型列表失败");
      setCustomModels([]);
      setCustomModelsTruncated(false);
    } finally {
      if (seq === fetchModelsSeqRef.current) setFetchingModels(false);
    }
  };

  // ✅ 性能优化：输入框每敲一键就发一次网络请求（含 DNS 预检），加 500ms 防抖
  const fetchModelsTimerRef = useRef<number | null>(null);
  const debouncedFetchCustomModels = (baseUrl: string, apiKey: string, configId?: string) => {
    if (fetchModelsTimerRef.current) {
      window.clearTimeout(fetchModelsTimerRef.current);
    }
    fetchModelsTimerRef.current = window.setTimeout(() => {
      fetchModelsTimerRef.current = null;
      tryFetchCustomModels(baseUrl, apiKey, configId);
    }, 500);
  };
  // 组件卸载时清理未触发的定时器
  useEffect(() => {
    return () => {
      if (fetchModelsTimerRef.current) {
        window.clearTimeout(fetchModelsTimerRef.current);
      }
    };
  }, []);

  /**
   * 是否随地址/Key 输入自动拉取模型列表。
   *
   * ✅ 修复：原条件为 `custom || !editingId` —— 编辑一条**已知供应商**配置、
   *    并把计费方式切成「包月套餐」时，地址与 Key 全换了，模型列表却不会刷新，
   *    下拉里仍是按量计费的模型名（在包月接入点上不存在）→ 测试连接 400。
   *    现把「包月套餐（地址手填）」也纳入自动拉取范围。
   */
  const shouldAutoFetchModels = () =>
    selectedProvider === "custom" || !editingId || watchPlan === "coding_plan";

  const applyPlanDefaults = (provider: string, plan: string) => {
    if (provider === "custom") return;
    const preset = presets[provider];
    const planInfo = preset?.plans?.[plan];
    if (planInfo) {
      form.setFieldsValue({
        base_url: planInfo.base_url,
        model: planInfo.model,
      });
    }
  };

  const handlePlanChange = (plan: string) => {
    applyPlanDefaults(selectedProvider, plan);
    // 两种计费方式对应不同的 API Key 与地址，切换时重置 Key 与模型列表
    form.setFieldsValue({ api_key: "" });
    setTestResult(null);
    setCustomModels([]);
    setCustomModelsTruncated(false);
    setCustomFetchError("");
  };

  const handleAdd = () => {
    setEditingId("");
    setTestResult(null);
    setSelectedProvider("deepseek");
    setCustomModels([]);
    setCustomModelsTruncated(false);
    setCustomFetchError("");
    form.resetFields();
    const defaultPlan = "pay_as_you_go";
    form.setFieldsValue({
      provider_name: "deepseek",
      plan: defaultPlan,
      max_tokens: 8192,
      temperature: 0.7,
      timeout: 900,
      concurrency: 4,
      request_mode: "normal",
      is_active: true,
      remark: "",
    });
    applyPlanDefaults("deepseek", defaultPlan);
    setAddModalOpen(true);
  };

  const providerKeys = Object.keys(providers);
  const activeConfig = configs.find(c => c.is_active);

  const columns = [
    {
      title: "供应商",
      dataIndex: "provider_name",
      key: "provider_name",
      render: (v: string) => {
        const info = providers[v];
        return info ? (
          <Space>
            <Tag color="blue">{info.label}</Tag>
            {v === "agnes" && <Tag color="orange">内置</Tag>}
          </Space>
        ) : v;
      },
    },
    {
      title: "模型",
      dataIndex: "model",
      key: "model",
      render: (v: string) => <Text code>{v}</Text>,
    },
    {
      title: "计费方式",
      dataIndex: "plan",
      key: "plan",
      render: (v: string) => {
        const labels: Record<string, string> = {
          pay_as_you_go: "按量计费",
          coding_plan: "包月套餐",
        };
        return <Tag color={v === "coding_plan" ? "purple" : "blue"}>{labels[v] || v || "按量计费"}</Tag>;
      },
    },
    {
      title: "Base URL",
      dataIndex: "base_url",
      key: "base_url",
      width: 220,
      render: (v: string) =>
        v ? (
          <Tooltip title={v}>
            <Text code style={{ fontSize: 11 }}>{v.length > 38 ? v.slice(0, 38) + "…" : v}</Text>
          </Tooltip>
        ) : <Tag color="red">未设置</Tag>,
    },
    {
      title: "API Key",
      dataIndex: "api_key",
      key: "api_key",
      render: (v: string, r: any) => {
        if (r.key_broken) {
          return (
            <Tooltip title="密文无法解密（常见于更换过加密密钥或删除过 data/secret_key.key），请重新填写 API Key">
              <Tag color="warning" icon={<WarningOutlined />}>密钥失效</Tag>
            </Tooltip>
          );
        }
        return r.has_key
          ? <Tooltip title="为安全起见仅显示末 4 位"><Text type="secondary">{v}</Text></Tooltip>
          : <Tag color="red">未设置</Tag>;
      },
    },
    {
      title: "状态",
      dataIndex: "is_active",
      key: "is_active",
      render: (a: number, r: AIConfigItem) => {
        // ✅ 2026-09-23：被运行时开关禁用的厂商在列表里必须显式可见，
        //    否则「明明配了、却一直不用它」只能靠翻审计日志才发现。
        const disabled = (runtime?.disabled_providers || []).includes(r.provider_name);
        return (
          <Space size={4} wrap>
            {a
              ? <Tag color="success" icon={<CheckCircleOutlined />}>当前使用</Tag>
              : <Tag>未启用</Tag>}
            {disabled && (
              <Tooltip title="该厂商已被「运行时厂商开关」禁用：不参与任何 AI 调用（配置与密钥仍保留，恢复开关即可使用）">
                <Tag color="error">已禁用</Tag>
              </Tooltip>
            )}
          </Space>
        );
      },
    },
    {
      title: "并发",
      dataIndex: "concurrency",
      key: "concurrency",
      align: "center" as const,
    },
    {
      title: "请求方式",
      dataIndex: "request_mode",
      key: "request_mode",
      width: 110,
      render: (v: string) => (
        v === "stream" ? (
          <Tooltip title="后端以流式（stream）方式接收厂商响应，边收边拼成完整结果后再继续流程：首字节更快、长正文更不易被网关超时掐断">
            <Tag color="purple" icon={<ThunderboltOutlined />}>流式请求</Tag>
          </Tooltip>
        ) : (
          <Tooltip title="一次性请求并等待完整响应（默认）">
            <Tag>普通请求</Tag>
          </Tooltip>
        )
      ),
    },
    {
      title: "环境",
      dataIndex: "env",
      key: "env",
      width: 90,
      render: (v: string) =>
        v ? (
          <Tooltip title="多环境标签：仅在「当前生效环境」匹配时参与主配置/降级链选取">
            <Tag color="geekblue">{v}</Tag>
          </Tooltip>
        ) : (
          <Tooltip title="通用配置：任何环境下都可被使用（默认）">
            <Tag>通用</Tag>
          </Tooltip>
        ),
    },
    {
      title: "备注",
      dataIndex: "remark",
      key: "remark",
      render: (v: string) => v || "-",
    },
    {
      title: "操作",
      key: "actions",
      render: (_: any, r: any) => (
        <Space size="small">
          <Button size="small" type="link" onClick={() => handleEdit(r)}>编辑</Button>
          {r.is_active ? (
            <Tag color="success" style={{ margin: 0 }}>当前使用</Tag>
          ) : (
            <Button size="small" type="primary" onClick={() => handleToggle(r.id)}>
              设为当前使用
            </Button>
          )}
          {!r.is_active && (
            <Button size="small" type="link" danger onClick={() => handleDelete(r.id)}>
              <DeleteOutlined />
            </Button>
          )}
          {/* ✅ 2026-09-23：清除密钥（当前使用中的配置也可清除，后端会回传告警） */}
          {r.has_key && (
            <Tooltip title="清除已保存的 API Key（收回密钥；清除后需重新填写才能调用）">
              <Button size="small" type="link" onClick={() => handleClearKey(r)}>
                <KeyOutlined />
              </Button>
            </Tooltip>
          )}
        </Space>
      ),
    },
  ];

  // ✅ 2026-09-25：场景码 → 中文名（取自 /ai/scene-routes 的 known_scenes，
  //    与「场景模型路由」面板同一份口径；未知场景原样显示，绝不隐藏数据）
  const sceneLabel = (code: string) => {
    if (!code) return "";
    const hit = (sceneRoutes || []).find((s: any) => s.scene === code);
    return hit?.label || code;
  };

  /**
   * 从「按场景」聚合直接下钻到该场景的调用明细：
   * 设置筛选 → 切到「用量日志」Tab → 按新筛选重新拉取。
   * （此前 by_scene 只是死数字，看到失败率高也无从查起。）
   */
  const jumpToSceneAudit = (scene: string) => {
    const f = { ...auditFilter, scene: scene || undefined };
    setAuditFilter(f);
    setAuditPage(1);
    setMainTab("audit");
    loadAuditLogs(1, auditPageSize, f);
  };

  const auditColumns = [
    {
      title: "供应商",
      dataIndex: "provider_name",
      key: "provider_name",
      render: (v: string) => providers[v]?.label || v || "-",
    },
    {
      title: "模型",
      dataIndex: "model",
      key: "model",
      width: 200,
      render: (v: string) => v ? <Text code style={{ fontSize: 11 }}>{v}</Text> : "-",
    },
    { title: "操作", dataIndex: "action", key: "action", width: 90 },
    {
      title: "场景",
      dataIndex: "scene",
      key: "scene",
      width: 120,
      // 改造前的历史调用没有 scene（当时漏传）→ 显示「未标记」而不是空白，
      // 便于区分「真的没场景」与「老记录未埋点」
      render: (v: string) => v
        ? <Tag color="blue" style={{ fontSize: 11 }}>{sceneLabel(v)}</Tag>
        : <Text type="secondary" style={{ fontSize: 11 }}>未标记</Text>,
    },
    {
      title: "Token 用量",
      key: "tokens",
      render: (_: any, r: any) => {
        const total = (r.prompt_tokens || 0) + (r.completion_tokens || 0);
        if (!total) return <Text type="secondary" style={{ fontSize: 12 }}>—</Text>;
        return (
          <Space size="small">
            <Tag>输入 {r.prompt_tokens || 0}</Tag>
            <Tag>输出 {r.completion_tokens || 0}</Tag>
            {r.cached_tokens > 0 && <Tag color="green">缓存 {r.cached_tokens}</Tag>}
          </Space>
        );
      },
    },
    {
      title: "耗时",
      dataIndex: "duration",
      key: "duration",
      width: 90,
      render: (v: number) => v ? `${Number(v).toFixed(1)}s` : "-",
    },
    {
      title: "结果",
      dataIndex: "success",
      key: "success",
      width: 70,
      render: (s: number, r: any) => s
        ? <Tag color="success" icon={<CheckCircleOutlined />}>成功</Tag>
        : <Tooltip title={r.error || "（无错误详情：本次改造前的历史记录）"}>
            <Tag color="error" icon={<CloseCircleOutlined />}>失败</Tag>
          </Tooltip>,
    },
    {
      title: "时间",
      dataIndex: "created_at",
      key: "created_at",
      width: 160,
      render: (v: string) => v?.slice(5, 19) || "-",
    },
  ];

  return (
    <div className="scroll-area" style={{ overflowY: "auto", overflowX: "hidden" }}>
      {/* 顶部状态卡片 */}
      <Row gutter={16} style={{ marginBottom: 16 }}>
        <Col span={6}>
          <Card size="small" style={{ borderLeft: activeConfig ? "3px solid #52c41a" : "3px solid #ff4d4f" }}>
            <Statistic
              title="当前使用平台"
              value={activeConfig ? providers[activeConfig.provider_name]?.label || activeConfig.provider_name : "未配置"}
              prefix={<ApiOutlined />}
              valueStyle={{ fontSize: 18, color: activeConfig ? "#52c41a" : "#ff4d4f" }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small" style={{ borderLeft: activeConfig ? "3px solid #52c41a" : "3px solid #ff4d4f" }}>
            <Statistic
              title="当前模型"
              value={activeConfig?.model || "—"}
              prefix={<ThunderboltOutlined />}
              valueStyle={{ fontSize: 18 }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="连接状态"
              value={HEALTH_LABELS[health?.status || ""] || "未配置"}
              prefix={health?.status === "configured"
                ? <CheckCircleOutlined style={{ color: "#52c41a" }} />
                : <CloseCircleOutlined style={{ color: healthColor(health?.status) }} />}
              valueStyle={{
                fontSize: 18,
                color: healthColor(health?.status),
              }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card size="small">
            <Statistic
              title="总调用次数"
              value={stats?.summary?.total || 0}
              prefix={<BarChartOutlined />}
              valueStyle={{ fontSize: 18 }}
            />
          </Card>
        </Col>
      </Row>

      {/* ✅ 新增：运行时可观测信息（原实现看不到 Base URL / 降级候选 / 并发 / 熔断） */}
      {health?.status === "key_invalid" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前使用配置的 API Key 无法解密"
          description="常见于更换过加密密钥（FERNET_KEY）或删除了 data/secret_key.key。请重新填写并保存该配置的 API Key。"
        />
      )}
      {/* ✅ 新增：区分「压根没填 Key」与「密文解不开」——
          此前后端把前者也报成 key_invalid，用户会按上面的提示去排查加密密钥，南辕北辙 */}
      {health?.status === "no_key" && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="当前使用配置尚未填写 API Key"
          description={health?.hint
            || "该配置已启用但没有可用的 Key，所有 AI 生成能力不可用。请编辑该配置补填 API Key 并保存。"}
        />
      )}
      {health?.status === "not_configured" && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="尚未启用任何文本模型配置，所有 AI 生成能力均不可用"
          description="请在下方「模型配置」中添加供应商并点击「设为当前使用」。"
        />
      )}
      {/* ✅ 2026-09-25：环境值损坏（env_corrupt）—— 一条配置都没坏，只是
          「当前生效环境」的运行时值非法。此前该状态会让多个读端点直接 500，
          界面只剩「加载失败」，用户既看不到原因也找不到恢复入口。
          现在后端降级返回数据 + 如实回传原因，这里给出**一键恢复为通用环境**。 */}
      {(health?.status === "env_corrupt" || !!envError) && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="「当前生效环境」的值非法，AI 调用已停止（模型配置本身没有损坏）"
          description={
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Text style={{ fontSize: 12 }}>
                {health?.hint || envError
                  || "运行环境值未通过校验，后端拒绝按通用环境继续（避免误用其它环境的密钥与地址）。"}
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                下方配置清单已按「通用环境」展示，可正常查看。恢复方法：把当前生效环境
                清空（回到不做环境过滤的通用模式），或改选一个存在的环境标签。
              </Text>
              <Space>
                <Button
                  size="small"
                  type="primary"
                  loading={envSaving}
                  onClick={() => handleEnvChange("")}
                >
                  恢复为通用环境
                </Button>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  库中已有环境：{envOptions.length ? envOptions.join("、") : "（无）"}
                </Text>
              </Space>
            </Space>
          }
        />
      )}
      {/* ✅ 增强：`no_key / key_invalid` 也展示运行时信息 —— 之前只在 configured 时渲染，
          「Key 有问题」时恰好最需要看降级候选与熔断状态，却什么都看不到。 */}
      {!!health && ["configured", "no_key", "key_invalid"].includes(health.status) && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Row gutter={24}>
            <Col span={8}>
              <Text type="secondary" style={{ fontSize: 12 }}>当前接入地址：</Text>
              <Text code style={{ fontSize: 12 }}>{health.base_url || "—"}</Text>
            </Col>
            <Col span={4}>
              <Statistic title="降级候选" value={health.fallback_count ?? 0} suffix="个" valueStyle={{ fontSize: 16 }} />
            </Col>
            <Col span={4}>
              {/* ✅ 新增：当前使用配置的请求方式 —— 排查「同一份配置首字节慢 /
                  长正文易断」时必须能看到运行时真正生效的是哪一种 */}
              <Text type="secondary" style={{ fontSize: 12 }}>请求方式：</Text>
              <Tag color={health.request_mode === "stream" ? "purple" : "default"}>
                {health.request_mode_label || (health.request_mode === "stream" ? "流式请求" : "普通请求")}
              </Tag>
            </Col>
            <Col span={4}>
              {/* ✅ 增强：分别展示「配置并发」与「运行时并发」——
                  原实现标着「当前并发」却显示的是落库配置值；自适应控制器
                  会根据响应速度随时上下调整，两者经常不一致。 */}
              <Statistic
                title="并发（配置 / 运行）"
                value={health.concurrency ?? 0}
                suffix={`/ ${health.live_concurrency ?? "—"}`}
                valueStyle={{ fontSize: 16 }}
              />
            </Col>
            <Col span={6}>
              <Text type="secondary" style={{ fontSize: 12 }}>熔断状态：</Text>
              {Object.keys(health.degraded_providers || {}).length === 0
                ? <Tag color="green">正常</Tag>
                : Object.entries(health.degraded_providers || {}).map(([k, v]) => (
                    <Tag key={k} color={v.state === "OPEN" ? "red" : "default"}>
                      {k}: {v.state}{v.failures ? ` (连续失败${v.failures})` : ""}
                    </Tag>
                  ))}
            </Col>
          </Row>
        </Card>
      )}

      {/* 用量统计卡片 */}
      {stats && stats.summary?.total > 0 && (
        <Card
          size="small"
          style={{ marginBottom: 16 }}
          extra={
            <Space>
              <Text type="secondary" style={{ fontSize: 12 }}>统计范围</Text>
              <Select
                size="small"
                value={statsDays}
                style={{ width: 100 }}
                onChange={(v) => { setStatsDays(v); loadStats(v); }}
                options={[
                  { value: 1, label: "最近 1 天" },
                  { value: 7, label: "最近 7 天" },
                  { value: 30, label: "最近 30 天" },
                  { value: 90, label: "最近 90 天" },
                ]}
              />
            </Space>
          }
        >
          <Row gutter={24}>
            <Col span={4}>
              <Statistic title="总Token用量" value={stats.summary.total_tokens || 0} />
            </Col>
            <Col span={4}>
              {/* ✅ 口径说明：缓存命中 / 输入 token（输出 token 无缓存语义，不进分母） */}
              <Tooltip title="缓存命中 Token ÷ 输入 Token。提示词缓存可显著降低长文档重复解析的费用与延迟；命中率偏低说明各次请求的前缀差异大（正常现象），持续为 0 可检查厂商是否支持 prompt cache。">
                <div>
                  <Statistic
                    title="缓存命中率"
                    value={stats.summary.cache_hit_rate ?? 0}
                    suffix="%"
                  />
                </div>
              </Tooltip>
            </Col>
            <Col span={4}>
              <Tooltip title="仅统计成功调用（失败/熔断跳过的 0 秒记录不参与平均）">
                <div>
                  <Statistic title="平均耗时" value={Number(stats.summary.avg_duration || 0).toFixed(1)} suffix="s" />
                </div>
              </Tooltip>
            </Col>
            <Col span={3}>
              <Statistic
                title="成功率"
                value={stats.summary.success_rate == null ? "—" : stats.summary.success_rate}
                suffix={stats.summary.success_rate == null ? "" : "%"}
              />
            </Col>
            <Col span={3}>
              {/* ✅ 口径修正：只统计真实调用失败；熔断跳过单列，避免把「没发出去的请求」当失败 */}
              <Tooltip title="真实调用失败（不含被熔断器跳过的请求）">
                <div>
                  <Statistic title="失败次数" value={stats.summary.failed_count || 0} />
                </div>
              </Tooltip>
            </Col>
            <Col span={3}>
              <Tooltip title="请求被熔断器跳过、压根没发出去的次数。数值长期偏高说明候选链整体不健康或限流严重。">
                <div>
                  <Statistic title="熔断跳过" value={stats.summary.skipped_count || 0} />
                </div>
              </Tooltip>
            </Col>
            <Col span={2}>
              <div style={{ fontWeight: 600, marginBottom: 8, fontSize: 12 }}>按操作</div>
              <Space wrap size={4}>
                {stats.by_action?.map((a: any) => (
                  <Tag key={a.action} style={{ fontSize: 11 }}>
                    {a.action}: {a.calls}
                  </Tag>
                ))}
              </Space>
            </Col>
          </Row>
          <div style={{ marginTop: 12 }}>
            <div style={{ fontWeight: 600, marginBottom: 8 }}>各供应商调用分布</div>
            <Space wrap>
              {stats.by_provider?.map((p: any) => {
                // ✅ 2026-09-17 增强：成功率着色（<60% 红色预警，帮助一眼识别死配置）
                const sr = p.success_rate;
                const srColor = sr == null ? "default" : sr >= 90 ? "green" : sr >= 60 ? "blue" : "red";
                return (
                  <Tag
                    key={`${p.provider_name}-${p.model}-${p.config_id || p.base_url || "default"}`}
                    color={srColor}
                  >
                    {providers[p.provider_name]?.label || p.provider_name}
                    {p.model ? ` / ${p.model}` : ""}
                    {p.config_id ? ` / 配置 ${p.config_id.slice(0, 8)}` : ""}
                    {p.base_url && !p.config_id ? ` / ${p.base_url}` : ""}: {p.calls}次
                    {sr != null && ` / 成功率${sr}%`}
                    {(p.prompt_tokens || 0) + (p.completion_tokens || 0) > 0 &&
                      ` / ${(p.prompt_tokens || 0) + (p.completion_tokens || 0)} tokens`}
                    {p.cache_hit_rate != null && p.cache_hit_rate > 0 && ` / 缓存命中${p.cache_hit_rate}%`}
                    {(p.fail_count || 0) > 0 && ` / 失败${p.fail_count}次`}
                    {(p.skipped_count || 0) > 0 && ` / 熔断跳过${p.skipped_count}次`}
                  </Tag>
                );
              })}
            </Space>
          </div>
          {/* ✅ 2026-09-25：按场景聚合 —— 此前后端算了 by_scene 却没有任何展示，
              而明细又只能按供应商/操作筛：「某场景失败率高」这件事既看不见也查不到。
              场景码统一显示中文名（与场景模型路由同口径），点击可直接下钻到该场景明细。 */}
          {(stats.by_scene?.length || 0) > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>
                按业务场景
                <Text type="secondary" style={{ fontSize: 11, marginLeft: 8 }}>
                  点击场景可直接查看其调用明细
                </Text>
              </div>
              <Space wrap>
                {stats.by_scene.map((sc: any) => {
                  const rate = sc.calls
                    ? Math.round(((sc.success_count || 0) / sc.calls) * 100) : null;
                  const rateColor = rate == null ? "default"
                    : rate >= 90 ? "green" : rate >= 60 ? "blue" : "red";
                  return (
                    <Tag
                      key={sc.scene || "unknown"}
                      color={rateColor}
                      style={{ cursor: "pointer" }}
                      onClick={() => jumpToSceneAudit(sc.scene || "")}
                    >
                      {sceneLabel(sc.scene) || "未标记"}: {sc.calls}次
                      {rate != null && ` / 成功率${rate}%`}
                      {(sc.tokens || 0) > 0 && ` / ${sc.tokens} tokens`}
                    </Tag>
                  );
                })}
              </Space>
            </div>
          )}
          {/* ✅ 2026-09-17 新增：失败原因 TOP 分布（配合审计 error 列） */}
          {stats.by_error?.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <div style={{ fontWeight: 600, marginBottom: 8 }}>失败原因 TOP（按调用次数）</div>
              <Space wrap>
                {stats.by_error.map((e: any, i: number) => (
                  <Tooltip key={i} title={`${providers[e.provider_name]?.label || e.provider_name} / ${e.model || "—"}`}>
                    <Tag color="volcano">
                      {providers[e.provider_name]?.label || e.provider_name}: {e.err}: {e.calls}次
                    </Tag>
                  </Tooltip>
                ))}
              </Space>
            </div>
          )}
        </Card>
      )}

      <Tabs
        activeKey={mainTab}
        onChange={setMainTab}
        items={[
          {
            key: "configs",
            label: <span><SettingOutlined /> 模型配置</span>,
            children: (
              <div>
                {/* ===== ✅ 2026-09-23 新增：多环境切换器（无需重启即时生效）===== */}
                <Card size="small" style={{ marginBottom: 12 }}>
                  <Space wrap>
                    <Text strong>当前生效环境：</Text>
                    <Select
                      size="small"
                      style={{ width: 200 }}
                      value={activeEnv}
                      loading={envSaving}
                      onChange={handleEnvChange}
                      options={[
                        { value: "", label: "通用（不过滤）" },
                        ...envOptions.map((e) => ({ value: e, label: e })),
                      ]}
                    />
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      通用环境不过滤任何配置（与未启用多环境时一致）；选择具体环境后，
                      主配置优先取该环境专用配置，降级链只使用「通用 + 该环境」的候选，
                      不会跨环境降级。环境标签在每条配置的「环境」列维护。
                    </Text>
                    {envOptions.length === 0 && (
                      <Tag>尚未配置任何环境标签</Tag>
                    )}
                  </Space>
                </Card>
                <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
                  <Title level={5} style={{ margin: 0 }}>已配置的供应商（{configs.length}）</Title>
                  <Space wrap>
                    {/* ✅ 新增：配置备份 / 迁移（导出不含 API Key） */}
                    <Tooltip title="导出配置 JSON（不含 API Key，可用于备份或迁移到其他机器）">
                      <Button icon={<ExportOutlined />} onClick={handleExport} disabled={!configs.length}>
                        导出配置
                      </Button>
                    </Tooltip>
                    <Tooltip title="导入配置 JSON（导入后需重新填写 API Key）">
                      <Button
                        icon={<ImportOutlined />}
                        loading={importing}
                        onClick={() => {
                          const input = document.createElement("input");
                          input.type = "file";
                          input.accept = ".json,application/json";
                          input.onchange = () => {
                            const f = input.files?.[0];
                            if (f) handleImport(f);
                          };
                          input.click();
                        }}
                      >
                        导入配置
                      </Button>
                    </Tooltip>
                    <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
                    <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>
                      添加供应商
                    </Button>
                  </Space>
                </div>

                {configs.length === 0 ? (
                  <Empty description="尚未配置任何 AI 供应商，请点击「添加供应商」">
                    <Button type="primary" icon={<PlusOutlined />} onClick={handleAdd}>
                      添加供应商
                    </Button>
                  </Empty>
                ) : (
                  <Table
                    dataSource={configs}
                    columns={columns}
                    rowKey="id"
                    pagination={false}
                    size="small"
                  />
                )}

                {/* ===== ✅ 熔断降级链顺序（aiApi.updateFallbackChain）===== */}
                {configs.length > 0 && (
                  <Card
                    size="small"
                    title={<span><SortAscendingOutlined /> 熔断降级链顺序</span>}
                    style={{ marginTop: 16 }}
                    extra={
                      <Space>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          越靠上越先尝试（当前使用中的配置不受顺序影响，始终最先）
                        </Text>
                        <Tooltip title="对全部配置做 DNS/TCP 连通性预检，不发认证请求、不消耗额度，用于识别不可用的备选配置">
                          <Button
                            size="small"
                            icon={<LinkOutlined />}
                            loading={precheckAllLoading}
                            onClick={handlePrecheckAll}
                          >
                            一键预检
                          </Button>
                        </Tooltip>
                        <Button
                          size="small"
                          type="primary"
                          loading={chainSaving}
                          onClick={handleSaveChain}
                        >
                          保存顺序
                        </Button>
                      </Space>
                    }
                  >
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 8 }}
                      message={
                        <Text style={{ fontSize: 12 }}>
                          调用失败时按顺序依次降级尝试。<Text strong>当前使用中的配置始终最先尝试</Text>
                          （不受此顺序影响）；其余配置按下方顺序降级，越靠上越先尝试。
                          未填写 API Key 的配置会被自动跳过。
                        </Text>
                      }
                    />
                    <List
                      size="small"
                      dataSource={chain}
                      renderItem={(c: any, i: number) => (
                        <List.Item
                          actions={[
                            <Tooltip title="上移（更高优先级）" key="up">
                              <Button
                                size="small"
                                icon={<ArrowUpOutlined />}
                                disabled={i === 0}
                                onClick={() => moveChain(i, -1)}
                              />
                            </Tooltip>,
                            <Tooltip title="下移" key="down">
                              <Button
                                size="small"
                                icon={<ArrowDownOutlined />}
                                disabled={i === chain.length - 1}
                                onClick={() => moveChain(i, 1)}
                              />
                            </Tooltip>,
                          ]}
                        >
                          <Space wrap>
                            <Tag color="blue" style={{ minWidth: 28, textAlign: "center" }}>{i + 1}</Tag>
                            {c.is_active && <Tag color="green">当前使用</Tag>}
                            {!c.has_key && (
                              <Tooltip title="未填写 API Key，降级时会被自动跳过">
                                <Tag color="red">缺 Key</Tag>
                              </Tooltip>
                            )}
                            {(runtime?.disabled_providers || []).includes(c.provider_name) && (
                              <Tooltip title="该厂商已被「运行时厂商开关」禁用，降级时会被自动跳过">
                                <Tag color="error">已禁用</Tag>
                              </Tooltip>
                            )}
                            <Text strong>{providers[c.provider_name]?.label || c.provider_name}</Text>
                            <Text type="secondary" style={{ fontSize: 12 }}>{c.model || "-"}</Text>
                            {c.remark && (
                              <Text type="secondary" style={{ fontSize: 11 }}>{c.remark}</Text>
                            )}
                            {precheckAllResult[c.id] && (
                              <Tooltip title={precheckAllResult[c.id].message}>
                                <Tag color={precheckAllResult[c.id].ok ? "success" : "error"}>
                                  {precheckAllResult[c.id].ok
                                    ? `可达${precheckAllResult[c.id].ip ? ` (${precheckAllResult[c.id].ip})` : ""}`
                                    : "不可达"}
                                </Tag>
                              </Tooltip>
                            )}
                          </Space>
                        </List.Item>
                      )}
                    />
                  </Card>
                )}

                {/* ===== ✅ 2026-09-23 新增：运行时厂商开关 ===== */}
                {configs.length > 0 && runtime && (
                  <Card
                    size="small"
                    title={<span><FireOutlined /> 运行时厂商开关（临时禁用，不删配置）</span>}
                    style={{ marginTop: 16 }}
                    extra={
                      <Space>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          即时生效、无需重启；可用于「某厂商临时欠费 / 限流 / 故障 / 合规下线」
                        </Text>
                        <Button
                          size="small"
                          type="primary"
                          loading={runtimeSaving}
                          onClick={handleSaveRuntime}
                        >
                          保存开关
                        </Button>
                      </Space>
                    }
                  >
                    <Alert
                      type={runtime.all_configured_disabled ? "error" : "info"}
                      showIcon
                      style={{ marginBottom: 8 }}
                      message={
                        <Text style={{ fontSize: 12 }}>
                          被禁用的厂商<Text strong>不参与任何 AI 调用</Text>（连探测请求都不发），
                          调用会自动降级到其它候选，并在用量日志中留下 <Text code>provider_disabled</Text> 记录。
                          与「删除配置」的区别：配置与密钥原样保留，恢复开关即可继续使用。
                          {runtime.all_configured_disabled && (
                            <Text type="danger">
                              ⚠ 当前已禁用全部已配置厂商，所有 AI 生成能力不可用！
                            </Text>
                          )}
                        </Text>
                      }
                    />
                    <Space wrap>
                      <Text style={{ fontSize: 13 }}>禁用厂商：</Text>
                      <Select
                        mode="multiple"
                        size="small"
                        style={{ minWidth: 420 }}
                        placeholder="选择要临时禁用的厂商（留空 = 全部可用）"
                        value={runtimeDraft}
                        onChange={(v) => setRuntimeDraft(v as string[])}
                        options={runtime.providers.map((p) => ({
                          value: p,
                          label: runtime.configured_providers.includes(p)
                            ? `${providers[p]?.label || p}（已配置）`
                            : `${providers[p]?.label || p}（未配置）`,
                        }))}
                      />
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        已配置厂商仍可用：{runtime.effective_provider_count} / {runtime.configured_providers.length}
                      </Text>
                    </Space>
                  </Card>
                )}

                {/* ===== ✅ 2026-09-23 新增：场景模型路由（多模型）===== */}
                {configs.length > 0 && sceneRoutes.length > 0 && (
                  <Card
                    size="small"
                    title={<span><NodeIndexOutlined /> 场景模型路由（可选）</span>}
                    style={{ marginTop: 16 }}
                  >
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 8 }}
                      message={
                        <Text style={{ fontSize: 12 }}>
                          默认为<Text strong>跟随「当前使用」配置</Text> —— 不设置时行为与旧版完全一致。
                          可为特定业务场景指定专属配置（例如「正文生成」用长文模型、「事实提取」用快模型）。
                          指定的配置同样需要有可用的 API Key 才会生效；配置被删除后会自动回落主配置。
                          {activeEnv || sceneActiveEnv
                            ? <> 当前生效环境为 <Tag color="blue" style={{ fontSize: 11 }}>{activeEnv || sceneActiveEnv}</Tag>
                              指向<Text strong>其它环境</Text>的配置会被跳过（回落主配置），并标红提示。</>
                            : " 当前为通用环境，不做环境过滤。"}
                        </Text>
                      }
                    />
                    {/* 场景较多（20+），限高滚动避免把页面撑得过长 */}
                    <div style={{ maxHeight: 300, overflowY: "auto" }}>
                    <Row gutter={[8, 8]}>
                      {sceneRoutes.map((s: any) => (
                        <Col span={12} key={s.scene}>
                          <Space wrap>
                            <Text style={{ display: "inline-block", width: 132, fontSize: 13 }}>
                              {s.label}
                            </Text>
                            <Select
                              size="small"
                              style={{ width: 280 }}
                              value={s.config_id || ""}
                              loading={sceneSaving === s.scene}
                              onChange={(v) => handleSceneRouteChange(s.scene, v)}
                              options={[
                                { value: "", label: "跟随「当前使用」配置" },
                                ...configs.map((c: any) => ({
                                  value: c.id,
                                  label: `${providers[c.provider_name]?.label || c.provider_name} / ${c.model || "-"}${c.has_key ? "" : "（缺 Key）"}`,
                                })),
                              ]}
                            />
                            {s.missing && (
                              <Tooltip title="路由指向的配置已被删除，该场景会自动回落「当前使用」配置">
                                <Tag color="error">配置已删除</Tag>
                              </Tooltip>
                            )}
                            {/* ✅ 2026-09-25：跨环境路由此前只在后端日志里 WARNING，
                                界面显示「已指定」但运行时静默回落 —— 典型配了不生效 */}
                            {!s.missing && s.env_mismatch && (
                              <Tooltip title={`该配置属于环境「${s.config_env}」，与当前生效环境「${activeEnv || sceneActiveEnv || "通用"}」不同：运行时会跳过此路由并回落「当前使用」配置。请切换环境，或改选本环境的配置。`}>
                                <Tag color="warning">跨环境·暂不生效</Tag>
                              </Tooltip>
                            )}
                          </Space>
                        </Col>
                      ))}
                    </Row>
                    </div>
                  </Card>
                )}

                {/* ===== ✅ 2026-09-23 新增：配置变更记录（审计留痕）===== */}
                <Card
                  size="small"
                  title={<span><HistoryOutlined /> 配置变更记录</span>}
                  style={{ marginTop: 16 }}
                  extra={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      最近 30 天 · 最多 20 条（不含明文密钥）
                    </Text>
                  }
                >
                  {configAudits.length === 0 ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      暂无记录。新增/修改/删除配置、切换「当前使用」、调整降级链与密钥时都会在此留痕。
                    </Text>
                  ) : (
                    <List
                      size="small"
                      dataSource={configAudits}
                      renderItem={(r: any) => (
                        <List.Item
                          actions={r.rollbackable ? [
                            <Tooltip key="rb" title="把该配置恢复到这次变更之前的状态（API Key 不参与回滚）">
                              <Button
                                size="small"
                                type="link"
                                loading={rollingBack === r.id}
                                onClick={() => handleRollback(r)}
                              >
                                回滚
                              </Button>
                            </Tooltip>,
                          ] : []}
                        >
                          <Space direction="vertical" size={2} style={{ width: "100%" }}>
                            <Space wrap size="small">
                              <Tag color="blue">{r.action_label || r.action}</Tag>
                              {(r.provider_name || r.model) && (
                                <Text style={{ fontSize: 12 }}>
                                  {r.provider_name}{r.model ? ` / ${r.model}` : ""}
                                </Text>
                              )}
                              <Text type="secondary" style={{ fontSize: 12 }}>{r.detail}</Text>
                              <Text type="secondary" style={{ fontSize: 11 }}>
                                {r.created_at}{r.client_ip ? ` · ${r.client_ip}` : ""}
                              </Text>
                            </Space>
                            {/* ✅ 2026-09-23（G6）：结构化变更明细（历史记录无快照 → 不展示） */}
                            {(r.changes || []).length > 0 && (
                              <Space wrap size="small">
                                {(r.changes || []).map((c: any) => (
                                  <Tag key={c.field} color="default" style={{ fontSize: 11 }}>
                                    {c.label}：{String(c.before) || "（空）"} → {String(c.after) || "（空）"}
                                  </Tag>
                                ))}
                              </Space>
                            )}
                          </Space>
                        </List.Item>
                      )}
                    />
                  )}
                </Card>

                {/* 供应商预设信息展示 */}
                <Card
                  size="small"
                  title={<span><GlobalOutlined /> 支持的供应商</span>}
                  style={{ marginTop: 16 }}
                >
                  <Row gutter={[12, 12]}>
                    {providerKeys.map(key => {
                      const info = providers[key];
                      const configured = configs.some(c => c.provider_name === key);
                      return (
                        <Col span={8} key={key}>
                          <Card
                            size="small"
                            hoverable
                            style={{
                              borderLeft: configured ? "3px solid #52c41a" : "3px solid transparent",
                            }}
                            onClick={() => {
                              // ✅ 修复：原实现点击「已配置」的卡片没有任何反应，
                              //    用户以为按钮坏了、只能去表格里找「编辑」。
                              //    现在已配置 → 直接进入该配置的编辑弹窗。
                              if (configured) {
                                const target = configs.find(c => c.provider_name === key);
                                if (target) handleEdit(target);
                                return;
                              }
                              handleAdd();
                              setSelectedProvider(key);
                              form.setFieldsValue({
                                provider_name: key,
                                base_url: info.base_url,
                                model: info.default_model,
                              });
                            }}
                          >
                            <Space direction="vertical" size={4} style={{ width: "100%" }}>
                              <Space style={{ width: "100%", justifyContent: "space-between" }}>
                                <Text strong>{info.label}</Text>
                                {configured ? <Tag color="green">已配置 · 点击编辑</Tag> : <Tag>未配置</Tag>}
                              </Space>
                              <Text type="secondary" style={{ fontSize: 12 }}>{info.description}</Text>
                              <Space size={4} wrap>
                                {info.models.slice(0, 3).map((m: any) => (
                                  <Tag key={m.value} style={{ fontSize: 11 }}>
                                    {m.label.split(" ")[0]}
                                    {m.recommended && <FireOutlined style={{ color: "#fa8c16", marginLeft: 2 }} />}
                                  </Tag>
                                ))}
                                {info.models.length > 3 && <Tag>+{info.models.length - 3}</Tag>}
                              </Space>
                              <Text type="secondary" style={{ fontSize: 11 }}>{info.pricing}</Text>
                            </Space>
                          </Card>
                        </Col>
                      );
                    })}
                    {/* 自定义供应商卡片 */}
                    <Col span={8}>
                      <Card
                        size="small"
                        hoverable
                        style={{
                          borderLeft: "3px solid #fa8c16",
                          borderStyle: "dashed",
                        }}
                        onClick={() => {
                          handleAdd();
                          setSelectedProvider("custom");
                          form.setFieldsValue({
                            provider_name: "custom",
                            base_url: "",
                            model: "",
                          });
                        }}
                      >
                        <Space direction="vertical" size={4} style={{ width: "100%" }}>
                          <Space style={{ width: "100%", justifyContent: "space-between" }}>
                            <Text strong style={{ color: "#fa8c16" }}>自定义</Text>
                            <Tag color="orange">OpenAI 兼容</Tag>
                          </Space>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            适用于任何兼容 OpenAI API 格式的供应商平台
                          </Text>
                          <Space size={4} wrap>
                            <Tag style={{ fontSize: 11 }}>自动获取模型</Tag>
                            <Tag style={{ fontSize: 11 }}>自定义 Base URL</Tag>
                          </Space>
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            填写地址和 Key 后自动拉取可用模型
                          </Text>
                        </Space>
                      </Card>
                    </Col>
                  </Row>
                </Card>
              </div>
            ),
          },
          {
            key: "audit",
            label: <span><BarChartOutlined /> 用量日志{auditTotal ? ` (${auditTotal})` : ""}</span>,
            children: (
              <div>
                {/* ✅ 增强：筛选 + 刷新 + 清理（原实现只有一张写死 50 条的只读表格） */}
                <Space wrap style={{ marginBottom: 12 }}>
                  <Select
                    allowClear
                    size="small"
                    placeholder="全部供应商"
                    style={{ width: 160 }}
                    value={auditFilter.provider_name}
                    onChange={(v) => {
                      const f = { ...auditFilter, provider_name: v };
                      setAuditFilter(f);
                      setAuditPage(1);
                      loadAuditLogs(1, auditPageSize, f);
                    }}
                    options={auditProviders.map(p => ({
                      value: p, label: providers[p]?.label || p,
                    }))}
                  />
                  <Select
                    allowClear
                    size="small"
                    placeholder="全部操作"
                    style={{ width: 140 }}
                    value={auditFilter.action}
                    onChange={(v) => {
                      const f = { ...auditFilter, action: v };
                      setAuditFilter(f);
                      setAuditPage(1);
                      loadAuditLogs(1, auditPageSize, f);
                    }}
                    options={auditActions.map(a => ({ value: a, label: a }))}
                  />
                  {/* ✅ 2026-09-25：场景下钻 —— 用量统计里有「按场景」聚合，
                      但明细此前无法按场景过滤，看到某场景失败率高却查不到具体请求 */}
                  <Select
                    allowClear
                    size="small"
                    placeholder="全部场景"
                    style={{ width: 170 }}
                    value={auditFilter.scene}
                    onChange={(v) => {
                      const f = { ...auditFilter, scene: v };
                      setAuditFilter(f);
                      setAuditPage(1);
                      loadAuditLogs(1, auditPageSize, f);
                    }}
                    options={auditScenes.map(s => ({
                      value: s, label: `${sceneLabel(s)}（${s}）`,
                    }))}
                  />
                  <Select
                    allowClear
                    size="small"
                    placeholder="全部结果"
                    style={{ width: 120 }}
                    value={auditFilter.success}
                    onChange={(v) => {
                      const f = { ...auditFilter, success: v };
                      setAuditFilter(f);
                      setAuditPage(1);
                      loadAuditLogs(1, auditPageSize, f);
                    }}
                    options={[
                      { value: "1", label: "成功" },
                      { value: "0", label: "失败" },
                    ]}
                  />
                  <Select
                    allowClear
                    size="small"
                    placeholder="不限时间"
                    style={{ width: 130 }}
                    value={auditFilter.days}
                    onChange={(v) => {
                      const f = { ...auditFilter, days: v };
                      setAuditFilter(f);
                      setAuditPage(1);
                      loadAuditLogs(1, auditPageSize, f);
                    }}
                    options={[
                      { value: 1, label: "最近 1 天" },
                      { value: 7, label: "最近 7 天" },
                      { value: 30, label: "最近 30 天" },
                    ]}
                  />
                  <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    loading={auditLoading}
                    onClick={() => loadAuditLogs(auditPage, auditPageSize, auditFilter)}
                  >
                    刷新
                  </Button>
                  <Tooltip title="删除较早的历史日志，控制数据库体积">
                    <Button size="small" danger icon={<ClearOutlined />} onClick={handleCleanupLogs}>
                      清理日志
                    </Button>
                  </Tooltip>
                </Space>
                <Table
                  dataSource={auditLogs}
                  columns={auditColumns}
                  rowKey="id"
                  loading={auditLoading}
                  size="small"
                  scroll={{ x: 900 }}
                  pagination={{
                    current: auditPage,
                    pageSize: auditPageSize,
                    total: auditTotal,
                    showSizeChanger: true,
                    showTotal: (t) => `共 ${t} 条`,
                    onChange: (p, ps) => {
                      setAuditPage(p);
                      setAuditPageSize(ps);
                      loadAuditLogs(p, ps, auditFilter);
                    },
                  }}
                />
              </div>
            ),
          },
        ]}
      />

      {/* 添加/编辑供应商弹窗 */}
      <Modal
        title={editingId ? "编辑供应商配置" : "添加供应商配置"}
        open={addModalOpen}
        forceRender
        onOk={handleSave}
        onCancel={() => { setAddModalOpen(false); form.resetFields(); setEditingId(""); setTestResult(null); setCustomModels([]); setCustomModelsTruncated(false); setCustomFetchError(""); }}
        width={680}
        okText="保存配置"
        cancelText="取消"
      >
        <Form form={form} layout="vertical">
          {/* 供应商选择 */}
          <Form.Item name="provider_name" label="供应商" rules={[{ required: true }]}>
            <Select
              onChange={handleProviderChange}
              disabled={!!editingId}
            >
              {providerKeys.map(key => (
                <Select.Option key={key} value={key}>
                  {providers[key]?.label || key}
                  {providers[key]?.models?.some((m: any) => m.recommended) && " (推荐)"}
                </Select.Option>
              ))}
              <Select.Option key="custom" value="custom">
                <span style={{ fontWeight: 600 }}>自定义（OpenAI 兼容）</span>
              </Select.Option>
            </Select>
          </Form.Item>

          {/* 计费方式 */}
          {selectedProvider !== "custom" && (
            <Form.Item name="plan" label="计费方式" rules={[{ required: true }]}>
              <Select
                onChange={handlePlanChange}
              >
                <Select.Option value="pay_as_you_go">按量计费（使用平台预设地址）</Select.Option>
                <Select.Option value="coding_plan">包月套餐（Coding Plan，手动填写包月接入点地址）</Select.Option>
              </Select>
            </Form.Item>
          )}
          {/* 两种计费方式对比说明 */}
          {selectedProvider !== "custom" && (
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 16, padding: "6px 12px" }}
              message={
                <Text type="secondary" style={{ fontSize: 12 }}>
                  按量计费：使用平台预设地址（只读）；包月套餐：API 地址与按量计费不同，需手动填写厂商提供的包月接入点。两种计划对应各自的 API Key。
                </Text>
              }
            />
          )}

          {/* 供应商信息提示（预设供应商） */}
          {selectedProvider !== "custom" && providers[selectedProvider] && (
            <Card size="small" style={{ marginBottom: 16, background: "rgba(24,144,255,0.06)" }}>
              <Space direction="vertical" size={2} style={{ width: "100%" }}>
                <Space>
                  <Text strong>{providers[selectedProvider].label}</Text>
                  {providers[selectedProvider].website && (
                    <Tooltip title="点击访问供应商官网">
                      <Button
                        size="small"
                        type="link"
                        icon={<LinkOutlined />}
                        href={providers[selectedProvider].website}
                        target="_blank"
                      >
                        获取 API Key
                      </Button>
                    </Tooltip>
                  )}
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>{providers[selectedProvider].description}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>{providers[selectedProvider].pricing}</Text>
              </Space>
            </Card>
          )}

          {/* 自定义供应商提示 */}
          {selectedProvider === "custom" && (
            <Card size="small" style={{ marginBottom: 16, background: "rgba(250,140,22,0.08)" }}>
              <Space direction="vertical" size={2} style={{ width: "100%" }}>
                <Text strong style={{ color: "#fa8c16" }}>自定义供应商</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  适用于任何兼容 OpenAI API 格式的供应商。填写 Base URL 和 API Key 后，系统将自动获取可用模型列表。
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  Base URL 格式示例：https://api.example.com/v1
                </Text>
              </Space>
            </Card>
          )}

          {/* API Key */}
          <Form.Item
            name="api_key"
            label={
              <Space>
                <span>API Key</span>
                <Tooltip title="API Key 使用 AES-256 加密存储，不会明文保存">
                  <SafetyCertificateOutlined style={{ color: "#52c41a" }} />
                </Tooltip>
              </Space>
            }
            rules={editingId ? [] : [{ required: true, message: "请输入 API Key" }]}
          >
            <Input.Password
              placeholder={editingId ? "留空则保持原有 Key 不变" : "输入 API Key"}
              autoComplete="new-password"
              onChange={(e) => {
                if (shouldAutoFetchModels()) {
                  const baseUrl = form.getFieldValue("base_url") || "";
                  debouncedFetchCustomModels(baseUrl, e.target.value, editingId || undefined);
                }
              }}
            />
          </Form.Item>

          {/* Base URL */}
          <Form.Item
            name="base_url"
            label={
              <Space>
                <span>Base URL</span>
                {selectedProvider !== "custom" && watchPlan === "pay_as_you_go" && (
                  <Tag color="blue" style={{ fontSize: 11, margin: 0 }}>平台预设</Tag>
                )}
                {watchPlan === "coding_plan" && (
                  <Tag color="purple" style={{ fontSize: 11, margin: 0 }}>包月·手填</Tag>
                )}
              </Space>
            }
            dependencies={["plan", "provider_name"]}
            rules={[
              ({ getFieldValue }) => ({
                required:
                  getFieldValue("provider_name") === "custom" ||
                  getFieldValue("plan") === "coding_plan",
                message:
                  getFieldValue("plan") === "coding_plan"
                    ? "包月套餐必须填写 API 地址（包月接入点不同于按量计费）"
                    : "请输入 Base URL",
              }),
            ]}
          >
            <Input
              readOnly={selectedProvider !== "custom" && watchPlan === "pay_as_you_go"}
              placeholder={
                selectedProvider === "custom"
                  ? "https://api.example.com/v1"
                  : watchPlan === "coding_plan"
                    ? "请输入包月套餐对应的 API 地址"
                    : "使用平台预设地址"
              }
              onChange={(e) => {
                if (shouldAutoFetchModels()) {
                  const apiKey = form.getFieldValue("api_key") || "";
                  debouncedFetchCustomModels(e.target.value, apiKey, editingId || undefined);
                }
              }}
            />
          </Form.Item>
          {/* Base URL 来源说明 */}
          {selectedProvider !== "custom" && watchPlan === "pay_as_you_go" && presets[selectedProvider]?.base_url && (
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: -8, marginBottom: 12 }}>
              按量计费使用平台预设地址：<Text code>{presets[selectedProvider].base_url}</Text>（只读，如需自定义地址请选包月套餐或自定义供应商）
            </Text>
          )}
          {watchPlan === "coding_plan" && (
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: -8, marginBottom: 12 }}>
              包月套餐（Coding Plan）为包月计费，API 地址不同于按量计费，请填写厂商提供的包月接入点地址
            </Text>
          )}

          {/* 模型选择 */}
          <Form.Item
            name="model"
            label={
              <Space>
                <span>模型</span>
                <Button
                  size="small"
                  type="link"
                  icon={<ApiOutlined />}
                  loading={fetchingModels}
                  onClick={async () => {
                    const baseUrl = form.getFieldValue("base_url") || "";
                    const apiKey = form.getFieldValue("api_key") || "";
                    const cfgId = editingId || undefined;
                    // 编辑模式下如果没输入新 key，从已存配置拿
                    if (editingId && !apiKey) {
                      try {
                        const { data: cfg } = await aiApi.getConfig();
                        const item = cfg.items?.find((c: any) => c.id === editingId);
                        if (item?.has_key) {
                          // 传 config_id 让后端从 DB 读 key
                          await tryFetchCustomModels(baseUrl, "", editingId);
                          return;
                        }
                      } catch {
                        // 凭据探测失败：忽略，走下面带显式 apiKey 的常规拉取
                      }
                    }
                    await tryFetchCustomModels(baseUrl, apiKey, cfgId);
                  }}
                >
                  {fetchingModels ? "获取中..." : "🔄 获取最新模型"}
                </Button>
              </Space>
            }
            rules={[{ required: true }]}
          >
            <Select
              mode={"combobox" as any}
              showSearch
              allowClear
              placeholder={
                watchPlan === "coding_plan"
                  ? "包月套餐请填写该套餐对应的模型名"
                  : "从下拉选择，或直接输入/粘贴模型名（如 nvidia/llama-3.1-8b-instruct）"
              }
              filterOption={(input, option) =>
                (option?.label ?? "").toLowerCase().includes(input.toLowerCase())
              }
              loading={fetchingModels}
              options={(() => {
                // 优先用动态拉取的 customModels；否则 fallback 到静态 preset
                if (customModels.length > 0) {
                  return customModels.map((m: any) => ({
                    value: m.value,
                    label: `${m.label}${m.context ? `  (${m.context})` : ""}`,
                  }));
                }
                if (selectedProvider === "custom") return [];
                return providers[selectedProvider]?.models?.map((m: any) => ({
                  value: m.value,
                  label: `${m.label}${m.context ? `  (${m.context})` : ""}`,
                })) || [];
              })()}
            />
          </Form.Item>

          {/* 模型获取状态提示 */}
          {(
            <div style={{ marginBottom: 12 }}>
              {fetchingModels && <Tag color="processing">正在获取模型列表...</Tag>}
              {!fetchingModels && customModels.length > 0 && (
                <Tag color="success">已获取 {customModels.length} 个可用模型</Tag>
              )}
              {!fetchingModels && customModelsTruncated && (
                <Tooltip title="该平台返回的模型过多，仅展示上下文最大的前若干个；可直接在输入框中手动填写未列出的模型名">
                  <Tag color="orange">模型过多，已截断显示</Tag>
                </Tooltip>
              )}
              {!fetchingModels && customFetchError && (
                <Space direction="vertical" size={2}>
                  <Tag color="error">获取失败：{customFetchError}</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    可手动在上方输入模型名称，或检查 Base URL 和 API Key 是否正确
                  </Text>
                </Space>
              )}
              {!fetchingModels && customModels.length === 0 && !customFetchError && (
                <Text type="secondary" style={{ fontSize: 12 }}>
                  填写 Base URL 和 API Key 后自动获取可用模型列表。也可点击上方「🔄 获取最新模型」按钮手动拉取。
                </Text>
              )}
            </div>
          )}

          {/* ✅ 新增：请求方式 —— 流式只改变后端与厂商之间的调用方式，
              应用侧（正文生成/事实提取/图表生成）仍等待完整结果后继续流程。 */}
          <Form.Item
            name="request_mode"
            label={
              <Space>
                <span>请求方式</span>
                <Tooltip title="仅决定后端与厂商平台之间的调用方式；应用侧仍等待完整结果后继续流程，对外行为完全一致">
                  <ThunderboltOutlined style={{ color: "#722ed1" }} />
                </Tooltip>
              </Space>
            }
            initialValue="normal"
          >
            <Select
              options={[
                { value: "normal", label: "普通请求（一次性等待完整响应）" },
                { value: "stream", label: "流式请求（后端边收边拼，仍等完整结果）" },
              ]}
            />
          </Form.Item>
          <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: -8, marginBottom: 12 }}>
            {watchRequestMode === "stream"
              ? "流式请求：后端以分片方式接收厂商响应，拼接为完整结果后再继续后续流程。首字节到达更快、长正文更不易被网关超时掐断；应用侧行为不变。若该平台不支持 stream 接口，系统会自动回退为普通请求重试，不会导致生成失败。"
              : "普通请求：一次性发起请求并等待厂商返回完整响应（默认，兼容性最好）。"}
          </Text>

          {/* 参数调节
              ✅ 2026-09-23：区间与后端 provider_factory._RANGE 严格对齐 ——
              此前并发允许填到 8（后端上限 5）、Max Tokens 上限 32768（后端 200000），
              界面校验得住、提交后被静默收敛，用户以为填的值生效了。 */}
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item name="max_tokens" label="Max Tokens">
                <InputNumber min={256} max={200000} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="temperature" label="Temperature">
                <InputNumber min={0} max={2} step={0.1} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item name="concurrency" label="并发数">
                <InputNumber
                  min={1}
                  max={5}
                  style={{ width: "100%" }}
                  title="全局并发硬上限为 5（超过部分后端会自动收敛到 5）"
                />
              </Form.Item>
            </Col>
          </Row>

          {/* ✅ 2026-09-23（多环境）：环境标签。留空 = 通用，任何环境都可用。 */}
          <Form.Item
            name="env"
            label="环境标签（可选）"
            tooltip="用于 dev/test/prod 等多环境隔离：留空 = 通用（任何环境都可用）；填了具体环境后，只有在「当前生效环境」匹配时才参与主配置/降级链选取。只允许字母、数字、下划线、中划线。"
          >
            <Input placeholder="如 prod / test，留空 = 通用" allowClear />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="timeout" label="超时时间（秒）">
                <InputNumber min={10} max={3600} style={{ width: "100%" }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="remark" label="备注">
                <Input placeholder="如：主力模型/备用模型" />
              </Form.Item>
            </Col>
          </Row>

          {/* 活跃设置 */}
          <Form.Item name="is_active" label="设为当前使用厂商平台" valuePropName="checked">
            <Switch checkedChildren="当前使用" unCheckedChildren="不启用" />
          </Form.Item>

          {/* 测试连接 / 连通性预检 */}
          <Space style={{ width: "100%", justifyContent: "space-between" }} wrap>
            <Space>
              <Button onClick={handleTest} loading={testing} icon={<ThunderboltOutlined />}>
                测试连接
              </Button>
              <Tooltip title="只做 DNS + TCP 连通性检查，不发送任何需要认证的请求 —— 用于确认网络与地址是否通，比「测试连接」更快且不消耗额度">
                <Button
                  onClick={handlePrecheck}
                  loading={prechecking}
                  icon={<LinkOutlined />}
                >
                  连通性预检
                </Button>
              </Tooltip>
            </Space>
            {precheckResult && (
              <Tag
                icon={precheckResult.ok ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
                color={precheckResult.ok ? "success" : "error"}
              >
                {precheckResult.ok
                  ? `DNS/TCP 可达${precheckResult.ip ? `（${precheckResult.host} → ${precheckResult.ip}）` : ""}`
                  : (precheckResult.message || "预检未通过")}
              </Tag>
            )}
            {testResult && (
              <Space>
                {testResult.ok ? (
                  <Tag icon={<CheckCircleOutlined />} color="success">连接成功</Tag>
                ) : (
                  <Tag icon={<CloseCircleOutlined />} color="error">{testResult.label || "连接失败"}</Tag>
                )}
                <Text type={testResult.ok ? "success" : "danger"} style={{ fontSize: 12 }}>
                  {testResult.msg}
                </Text>
                {!testResult.ok && testResult.suggestion && (
                  <Text style={{ fontSize: 11, color: "#888" }}>💡 {testResult.suggestion}</Text>
                )}
                {testResult.ok && testResult.warning && (
                  <Text style={{ fontSize: 11, color: "#d46b08" }}>⚠ {testResult.warning}</Text>
                )}
                {testResult.network?.ok && <Tag color="blue">网络可达</Tag>}
              </Space>
            )}
          </Space>
        </Form>
      </Modal>
    </div>
  );
}
