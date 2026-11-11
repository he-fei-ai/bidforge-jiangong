/**
 * 「全局事实」Tab（Tab 3）面板（2026-11-04 自 SchemeWorkbenchPage 原样抽取，零行为变更）。
 *
 * 页面级状态 / handler / setter 全部显式注入；JSX / DOM / 文案 / 条件 / key /
 * 事件绑定逐字搬运，未做任何逻辑修改。
 */
import type { Dispatch, SetStateAction } from "react";
import type { ModalFuncProps } from "antd";
import {
  Alert,
  Button,
  Card,
  Empty,
  List,
  Pagination,
  Progress,
  Radio,
  Space,
  Tag,
  Tooltip,
  Typography,
  Upload,
} from "antd";
import {
  ArrowRightOutlined,
  CaretRightOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  FileTextOutlined,
  GlobalOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { factsApi } from "../../../api";
import { isDocFailed, isDocParsed, NEXT_TAB } from "../../../utils/workflowDerived";
import type { DocRecord, DocStats } from "../../../utils/workflowDerived";
import { UPLOAD_FILE_ACCEPT } from "../../../utils/uploadAccept";
import type {
  FactChaptersResponse,
  FactCrossConflict,
  FactDangerResponse,
  FactGroupLike,
  FactItemLike,
  FactPagination,
  FactsSummary,
} from "../../../types/facts";
import { FACTS_PAGE_SIZE, buildFactsPagination, factErrorText, safeFactText } from "../factsShared";
import type { FactsCategoryEntry } from "../factsShared";
import type { FactsLogItem } from "../logTypes";
import type { FactsSegmentStats } from "../workbenchTypes";
import { FactsAdjustPanel } from "../FactsAdjustPanel";
import { FactsChapterCoveragePanel } from "../FactsChapterCoveragePanel";
import { FactsDiagnosticsPanel } from "../FactsDiagnosticsPanel";
import { FactsExtractProgressCard } from "../FactsExtractProgressCard";
import { FactsGroupList } from "../FactsGroupList";
import { FactsSegmentFailuresAlert } from "../FactsSegmentFailuresAlert";
import { TaskControlButtons } from "../TaskControlButtons";
// ✅ R212（P3-2）：severity 色板单一事实源（types/audit.ts）
//    ⚠️ 用**相对路径**：本仓 vite/vitest 未配置 "@" 别名（仅 tsconfig 有 paths），
//    用 "@/types/audit" 会让 vitest 在导入期直接报「Failed to resolve import」。
import { SEVERITY_COLOR, type Severity } from "../../../types/audit";

const { Text } = Typography;

/** 「全局事实」Tab（Tab 3）面板：页面级状态 / handler / setter 全部显式注入。 */
export interface FactsTabProps {
  id: string | undefined;
  // 事实数据与分组
  facts: FactGroupLike[];
  factsSummary: FactsSummary | null;
  factsCrossConflicts: FactCrossConflict[];
  factsSegmentStats: FactsSegmentStats | null;
  factsFilter: string;
  setFactsFilter: Dispatch<SetStateAction<string>>;
  factChapterTitles: Record<string, string>;
  factAttrTitles: Record<string, string>;
  factSourceKindTitles: Record<string, string>;
  factCategoryEntries: FactsCategoryEntry[];
  activeFactCategory: string | null;
  activeFactGroups: FactGroupLike[];
  factsPagination: FactPagination | null;
  // 项目资料文档
  docList: DocRecord[];
  docStats: DocStats;
  uploadedFiles: string[];
  factsDocsCollapsed: boolean;
  setFactsDocsCollapsed: Dispatch<SetStateAction<boolean>>;
  // 提取 / 解析运行态
  generating: boolean;
  genType: string;
  progress: number;
  progressMsg: string;
  missingValueMode: "fabricate" | "omit" | "placeholder";
  setMissingValueMode: Dispatch<SetStateAction<"fabricate" | "omit" | "placeholder">>;
  factsLogs: FactsLogItem[];
  uploadingFacts: boolean;
  parsingDocs: boolean;
  /** 单份解析进行中的 docId（可选；R203 起纳入上传/批量解析按钮禁用口径） */
  parsingDocId?: string | null;
  taskPaused: boolean;
  taskStopping: boolean;
  // 诊断
  factsChapterReport: FactChaptersResponse | null;
  factsDangerReport: FactDangerResponse | null;
  factsDiagnosticsLoading: boolean;
  // 导航 / 反馈
  setActiveTab: Dispatch<SetStateAction<string>>;
  msg: { success: (content: string) => void; error: (content: string) => void };
  modal: { confirm: (cfg: ModalFuncProps) => void };
  // 事实 / 文档处理器
  handleAckStaleAll: () => void | Promise<void>;
  handleBatchResolve: () => void | Promise<void>;
  handleUploadDocuments: (files: File[]) => void | Promise<void>;
  handleParseDocuments: (force?: boolean) => void | Promise<void>;
  handleParseDocument: (docId: string, fileName: string, force?: boolean) => void | Promise<void>;
  handleDeleteDocument: (docId: string, fileName: string) => void | Promise<void>;
  handleGenerateFacts: () => void | Promise<void>;
  /** ✅ T-05（2026-11-06）：释放失败段退避并立即重跑提取。 */
  handleRetryFailedSegments: () => void | Promise<void>;
  openFactCreate: () => void;
  handleClearAllFacts: () => void;
  openFactEdit: (group: FactGroupLike) => void;
  handleDeleteFactGroup: (group: FactGroupLike) => void;
  openFactItemEdit: (group: FactGroupLike | null, item: FactItemLike) => void;
  handleResolveFactItem: (item: FactItemLike) => void | Promise<void>;
  handleResolveConflictItem: (item: FactItemLike, value: string) => void | Promise<void>;
  handleAckStaleOne: (itemId: string, title?: string) => void | Promise<void>;
  handleControl: (action: "pause" | "resume" | "stop") => void | Promise<void>;
  loadFactsDiagnostics: () => void | Promise<void>;
  loadFacts: (pageOverride?: number) => void | Promise<void>;
  loadDocuments: () => void | Promise<void>;
}

export function FactsTab({
  id,
  facts,
  factsSummary,
  factsCrossConflicts,
  factsSegmentStats,
  factsFilter,
  setFactsFilter,
  factChapterTitles,
  factAttrTitles,
  factSourceKindTitles,
  factCategoryEntries,
  activeFactCategory,
  activeFactGroups,
  factsPagination,
  docList,
  docStats,
  uploadedFiles,
  factsDocsCollapsed,
  setFactsDocsCollapsed,
  generating,
  genType,
  progress,
  progressMsg,
  missingValueMode,
  setMissingValueMode,
  factsLogs,
  uploadingFacts,
  parsingDocs,
  /** ✅ R203：单份解析进行中的 docId——与 UploadParseTab 的 busy 口径对齐
   *  （可选向后兼容：未传时按旧口径，既有调用方/测试零影响）。 */
  parsingDocId,
  taskPaused,
  taskStopping,
  factsChapterReport,
  factsDangerReport,
  factsDiagnosticsLoading,
  setActiveTab,
  msg,
  modal,
  handleAckStaleAll,
  handleBatchResolve,
  handleUploadDocuments,
  handleParseDocuments,
  handleParseDocument,
  handleDeleteDocument,
  handleGenerateFacts,
  handleRetryFailedSegments,
  openFactCreate,
  handleClearAllFacts,
  openFactEdit,
  handleDeleteFactGroup,
  openFactItemEdit,
  handleResolveFactItem,
  handleResolveConflictItem,
  handleAckStaleOne,
  handleControl,
  loadFactsDiagnostics,
  loadFacts,
  loadDocuments,
}: FactsTabProps) {
  // ✅ R208 修复（门禁口径分裂）：本 Tab 内联的文档行列表是
  //    ``DocumentParseList``（解析提取 Tab）那份列表的**副本**，但两者的
  //    按钮门禁口径不同——DocumentParseList 用三维
  //    ``parsingDocs || !!parsingDocId || uploadingFacts``（:188），
  //    本 Tab 此前只判 ``parsingDocs``。后果有二：
  //      ① 上传中点行内「解析」：同名 upsert 会换新主键，在飞解析持旧
  //         doc_id 写不回 → 解析结果丢失（R203 FE-5 只在 DocumentParseList
  //         一侧落地，本副本漏改）；
  //      ② 单份解析中点行内「解析」：handler 被防重入静默吞掉 → 点击
  //         无任何反应（假动作）。
  //    这已是 AGENTS.md 教训「教训只在同类一侧落实会复发」的第三次实证
  //    （R191 C-F3 → R203 FE-5 → 本轮），故两侧口径必须一致。
  const docParseDisabled = parsingDocs || !!parsingDocId || uploadingFacts;
  return (
    // ✅ 修复：全局事实内容可能很长（资料列表 + 分组事实 Collapse），
    //    容器必须可滚动，否则超出视口的内容被外层 overflow:hidden 裁剪，无法查看/点击
    <div
      className="scroll-area"
      style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}
    >
      {/* ===== 顶部：统计面板 + 告警 ===== */}
      {factsSummary && factsSummary.total > 0 && (
        <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: "8px 16px" } }}>
          <div style={{ display: "flex", alignItems: "center", gap: 24, flexWrap: "wrap" }}>
            <Text strong>📊 事实概览：</Text>
            <span>
              共 <Text strong>{factsSummary.total}</Text> 项
            </span>
            {factsSummary.simulated > 0 && (
              <Tooltip title="AI 模拟生成，需人工确认后才能注入正文">
                <Tag color="orange">⚠️ 模拟值 {factsSummary.simulated}</Tag>
              </Tooltip>
            )}
            {factsSummary.unresolved > 0 && (
              <Tag color="blue">📝 待审核 {factsSummary.unresolved}</Tag>
            )}
            {factsSummary.conflicts > 0 && (
              <Tooltip title="同一事实存在多个取值，需人工裁决">
                <Tag color="red">🔸 矛盾 {factsSummary.conflicts}</Tag>
              </Tooltip>
            )}
            {/* ✅ BUG 修复（2026-09-27）：过期事实此前既不计入 has_warnings
                    也不展示，用户无法得知「为什么事实没进正文」。资料变更/重新提取
                    会把旧值标记 is_stale=1 并被注入门控排除，必须显式可见。
                    ✅ G3（2026-10-04）：补「确认仍有效」入口 —— 此前一旦被打上
                    is_stale=1 就没有任何解除手段（改值/裁决之外），用户核对过
                    但值未变时只能重跑 AI 提取。 */}
            {factsSummary.stale > 0 && (
              <Tooltip title="来源资料已变化或已被重新提取取代，这些事实不会注入正文；请重新提取或人工核对">
                <Tag color="volcano">🕒 来源过期 {factsSummary.stale}</Tag>
              </Tooltip>
            )}
            {factsSummary.stale > 0 && (
              <Button size="small" onClick={handleAckStaleAll}>
                我已核对，仍有效
              </Button>
            )}
            {factsSummary.has_warnings && (
              <Button size="small" type="primary" danger onClick={handleBatchResolve}>
                一键确认全部
              </Button>
            )}
            {!factsSummary.has_warnings && factsSummary.total > 0 && (
              <>
                <Tag color="green">
                  <CheckCircleOutlined /> 全部就绪
                </Tag>
                <Button
                  size="small"
                  type="primary"
                  ghost
                  icon={<ArrowRightOutlined />}
                  onClick={() => setActiveTab(NEXT_TAB.facts)}
                >
                  下一步：正文生成
                </Button>
              </>
            )}
          </div>
          {/* ✅ 缺陷 C 修复：展示 stats.by_category 分类分布（旧前端未消费）*/}
          {Array.isArray(factsSummary.by_category) && factsSummary.by_category.length > 0 && (
            <div
              style={{
                marginTop: 8,
                display: "flex",
                flexWrap: "wrap",
                gap: 6,
                alignItems: "center",
              }}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                分类分布：
              </Text>
              {factsSummary.by_category.map((c) => (
                <Tooltip
                  key={c.category}
                  title={`${c.title || c.category}：${c.groups} 组 / ${c.items} 条·模拟 ${c.simulated}·矛盾 ${c.conflicts}·待审 ${c.unresolved}`}
                >
                  <Tag
                    color={
                      c.conflicts > 0
                        ? "red"
                        : c.unresolved > 0
                          ? "blue"
                          : c.simulated > 0
                            ? "orange"
                            : undefined
                    }
                  >
                    {c.title || c.category} {c.items}
                  </Tag>
                </Tooltip>
              ))}
            </div>
          )}
          {/* ✅ R89：`simulated_ratio` 缺失时必须判 0 —— 旧写法
                  `stats.simulated_ratio > 0.3` 在字段缺席（后端未下发/旧版本）
                  时得到 undefined>0.3 = false，虽不崩但也读不出「后端没给」；
                  归一后与 buildFactsSummary 对其它计数的处理同口径。 */}
          {Number(factsSummary.simulated_ratio) > 0.3 && (
            <Alert
              style={{ marginTop: 8 }}
              type="warning"
              showIcon
              message={`模拟值占比 ${(Number(factsSummary.simulated_ratio) * 100).toFixed(0)}% 超过 30% 阈值，建议补充更多项目资料以减少 AI 模拟`}
            />
          )}
          {/* ✅ 缺陷 B 修复：展示 SSE completed 事件的跨段矛盾清单（旧前端未消费）*/}
          {factsCrossConflicts.length > 0 && (
            <Alert
              style={{ marginTop: 8 }}
              type="error"
              showIcon
              message={`检测到 ${factsCrossConflicts.length} 处跨段矛盾，请在下方对应条目中手动裁决`}
              description={
                <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  {factsCrossConflicts.slice(0, 8).map((cf, idx: number) => {
                    // ✅ R212（P3-2）：severity 色板收敛到 types/audit.ts 单一事实源
                    //    （此前内联三元**不含 block** ⇒ 阻断级落 blue）。
                    const sevColor = SEVERITY_COLOR[(cf.severity || "low") as Severity] ?? "default";
                    // ✅ R214（2026-11-10 · R210 D-6）：React key 用**组合键**。
                    //    旧实现 ``cf.rule_id || idx`` —— 后端 ``rule_id`` **不唯一**
                    //    （XV-MAT-DESIGN / XV-MACH-SCHED 各 4 个独立产出点、DB 唯一键
                    //    含 conflict_type）⇒ 同 rule_id 的多处冲突 key 相同，React 报
                    //    duplicate key 警告并复用节点（渲染错乱）。现按
                    //    rule_id + 双侧名称/值 + idx 组合，保证同屏唯一。
                    const conflictKey = [
                      cf.rule_id || "",
                      safeFactText(cf.side_a?.name),
                      safeFactText(cf.side_a?.value),
                      safeFactText(cf.side_b?.value),
                      idx,
                    ].join("|");
                    return (
                      <span key={conflictKey} style={{ fontSize: 12 }}>
                        <Tag color={sevColor}>{cf.severity || "low"}</Tag>
                        {/* R189：`value` 运行时可能是对象/数字（旧数据或 AI 输出），
                            `as string` 只是编译期收窄，作 React child 会整块崩。 */}
                        <Text strong>{safeFactText(cf.side_a?.name)}</Text>：
                        <Text delete type="danger">
                          {safeFactText(cf.side_a?.value)}
                        </Text>
                        {" ↔ "}
                        <Text type="warning">{safeFactText(cf.side_b?.value)}</Text>
                        {cf.resolution_hint ? (
                          <Text type="secondary"> · {safeFactText(cf.resolution_hint)}</Text>
                        ) : null}
                      </span>
                    );
                  })}
                  {factsCrossConflicts.length > 8 && (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      …另有 {factsCrossConflicts.length - 8} 处矛盾
                    </Text>
                  )}
                </div>
              }
            />
          )}
        </Card>
      )}

      {/* ===== 操作按钮（分步工作流：① 上传保存 → ② 解析 → ③ AI 提取 → ④ 修改）===== */}
      <Space style={{ marginBottom: 12 }} wrap>
        <Upload
          multiple
          accept={UPLOAD_FILE_ACCEPT}
          showUploadList={false}
          disabled={generating || uploadingFacts || parsingDocs || !!parsingDocId}
          beforeUpload={(file, fileList) => {
            if (fileList[0] === file) {
              // antd Upload 元素的 originFileObj 为可选键（受控链路才有），结构断言读取
              const files = fileList.map(
                (f) => (f as { originFileObj?: File }).originFileObj || f,
              ) as File[];
              setTimeout(() => handleUploadDocuments(files), 0);
            }
            return false;
          }}
        >
          {/* ✅ R203：Upload/Button disabled 补 !!parsingDocId 维度（与
              UploadParseTab busy 口径对齐）——单份解析（OCR 分钟级）进行中
              上传同名文件 → upsert 整行换新（parsed_markdown=''、
              parse_status='pending'），在飞解析持旧 doc_id 完成后写不回，
              解析结果静默丢失。 */}
          <Button
            icon={<UploadOutlined />}
            disabled={generating || uploadingFacts || parsingDocs || !!parsingDocId}
            loading={uploadingFacts}
          >
            ① 上传文件保存
          </Button>
        </Upload>
        <Button
          icon={<FileTextOutlined />}
          onClick={() => handleParseDocuments()}
          disabled={
            generating ||
            uploadingFacts ||
            parsingDocs ||
            !!parsingDocId ||
            docStats.actionableCount === 0
          }
          loading={parsingDocs}
        >
          {/* ✅ 2026-09-24 B4：与 import Tab 按钮同口径（待解析 + 失败均可批量处理）
                  ✅ 2026-09-25：改用 useMemo 化的 docStats，消除每次渲染的重复计算与 IIFE
                  ✅ R203：disabled 补 !!parsingDocId 维度（与 UploadParseTab busy 口径对齐）*/}
          ② 解析文档
          {docStats.pendingCount > 0
            ? `（${docStats.actionableCount} 待解析）`
            : docStats.failedCount > 0
              ? `（${docStats.failedCount} 解析失败可重试）`
              : ""}
        </Button>
        <Button
          type="primary"
          icon={<GlobalOutlined />}
          onClick={handleGenerateFacts}
          disabled={generating || uploadingFacts || parsingDocs || !!parsingDocId}
          loading={generating && genType === "facts"}
        >
          ③ AI 提取事实
        </Button>
        <Tooltip title="资料未给出的确定值如何处理：智能补全（标记模拟值）· 不杜撰（剔除模拟值）· 留待填写（写【待填写】）">
          <Radio.Group
            value={missingValueMode}
            onChange={(e) => setMissingValueMode(e.target.value)}
            size="small"
            buttonStyle="solid"
            disabled={generating || uploadingFacts || parsingDocs || !!parsingDocId}
          >
            <Radio.Button value="fabricate">🧩 智能补全</Radio.Button>
            <Radio.Button value="omit">🚫 不杜撰</Radio.Button>
            <Radio.Button value="placeholder">📝 留待填写</Radio.Button>
          </Radio.Group>
        </Tooltip>
        {/* ✅ R45 · D5：AI 调整事实抽为独立子组件（FactsAdjustPanel）。
                状态机 / 竞态守卫 / 二次确认全部归位到组件内，交互行为由
                factsAdjust.test.tsx 钉住；页面只保留后端契约接线。 */}
        <FactsAdjustPanel
          schemeId={id}
          busy={generating || uploadingFacts || parsingDocs}
          disabled={(factsSummary?.total || 0) === 0}
          confirmDialog={(opts) => modal.confirm(opts)}
          preview={async (instruction, signal) => {
            const { data } = await factsApi.adjust(
              {
                instruction,
                scheme_id: id!,
                apply: false,
              },
              { signal },
            );
            return data;
          }}
          apply={async (operations, instruction, signal) => {
            // 应用用户刚确认的预览计划，禁止后端再次调用 AI 生成另一份计划
            const { data } = await factsApi.adjust(
              {
                instruction,
                scheme_id: id!,
                apply: true,
                operations,
              },
              { signal },
            );
            await loadFacts();
            // ✅ 2026-10-08（F3 前端接线）：被丢弃的操作要在成功提示里也说清楚，
            //    否则「改了 3 条只落 2 条」在 toast 上仍是一个「已应用」。
            const ignoredCount = Array.isArray(data?.ignored) ? data.ignored.length : 0;
            msg.success(
              ignoredCount > 0
                ? `事实调整已应用，${ignoredCount} 项未纳入（详见面板提示）`
                : "事实调整已应用",
            );
            // 回传给面板渲染丢弃明细（组件把 void 当「无丢弃」）
            return { ignored: data?.ignored };
          }}
          onError={(fallback, e) => {
            // ✅ R210：走事实模块统一错误出口 —— 写保护 409 的 detail 是
            // **对象**（{code, message}），按字符串消费会让整条提示在 React 18
            // 下抛 "Objects are not valid as a React child" 而根本不出现。
            msg.error(factErrorText(e, fallback));
          }}
        />
        <Button
          icon={<CaretRightOutlined />}
          onClick={openFactCreate}
          disabled={generating || uploadingFacts || parsingDocs || !!parsingDocId}
        >
          手动新增
        </Button>
        {/* ✅ 缺陷 A 修复：接线 factsApi.clearAll（一键清空全部事实）*/}
        {factsSummary && factsSummary.total > 0 && (
          <Button
            danger
            icon={<DeleteOutlined />}
            onClick={handleClearAllFacts}
            disabled={generating || uploadingFacts || parsingDocs || !!parsingDocId}
          >
            清空全部事实
          </Button>
        )}
      </Space>

      {/* ===== 全局事实提取进度（阶段 + 分段级实时日志）===== */}
      {/* ✅ 组件级可测（2026-09-21）：渲染抽到模块级 FactsExtractProgressCard，
               行为由 factsTab.test.tsx 钉住 */}
      {generating && genType === "facts" && (
        <FactsExtractProgressCard
          progress={progress}
          progressMsg={progressMsg}
          logs={factsLogs}
          controls={
            <TaskControlButtons
              paused={taskPaused}
              stopping={taskStopping}
              onControl={handleControl}
            />
          }
        />
      )}

      <FactsDiagnosticsPanel
        loading={factsDiagnosticsLoading}
        chapterReport={factsChapterReport}
        dangerReport={factsDangerReport}
        onRefresh={() => void loadFactsDiagnostics()}
      />

      {/* ✅ 2026-10-06：消费此前被整体丢弃的 stats.by_chapter。
              与上方 DiagnosticsPanel 口径不同（那个查必填字段覆盖率，
              这个查各章实际落了多少条事实 + 缺哪些字段）。 */}
      <FactsChapterCoveragePanel stats={factsSummary} titles={factChapterTitles} />

      {/* ===== 上次提取的分段失败详情（部分未完成时可见）===== */}
      {/* ✅ 组件级可测（2026-09-21）：渲染抽到模块级 FactsSegmentFailuresAlert */}
      {factsSegmentStats &&
        ((factsSegmentStats.failed ?? 0) > 0 || (factsSegmentStats.declared_failed ?? 0) > 0) && (
          <FactsSegmentFailuresAlert
            stats={factsSegmentStats}
            onRetry={handleRetryFailedSegments}
            retrying={generating && genType === "facts"}
          />
        )}

      {/* ===== 筛选器 ===== */}
      {factsSummary && factsSummary.total > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Radio.Group
            value={factsFilter}
            onChange={(e) => setFactsFilter(e.target.value)}
            size="small"
          >
            <Radio.Button value="all">全部 ({factsSummary.total})</Radio.Button>
            {factsSummary.simulated > 0 && (
              <Radio.Button value="simulated">
                <span style={{ color: "#fa8c16" }}>⚠️ 模拟 ({factsSummary.simulated})</span>
              </Radio.Button>
            )}
            {factsSummary.conflicts > 0 && (
              <Radio.Button value="conflict">
                <span style={{ color: "#ff4d4f" }}>🔸 矛盾 ({factsSummary.conflicts})</span>
              </Radio.Button>
            )}
            {factsSummary.unresolved > 0 && (
              <Radio.Button value="unresolved">
                <span style={{ color: "#1677ff" }}>📝 待审核 ({factsSummary.unresolved})</span>
              </Radio.Button>
            )}
          </Radio.Group>
        </div>
      )}

      {uploadingFacts && (
        <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: 12 } }}>
          <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
            <div style={{ flex: "0 0 320px" }}>
              <Progress percent={100} status="active" showInfo={false} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Text strong>🔄 正在保存文件（保存后可点击「② 解析文档」继续）...</Text>
              {uploadedFiles.length > 0 && (
                <Space wrap style={{ marginTop: 4 }}>
                  {uploadedFiles.map((name, i) => (
                    <Tag key={i} color="processing">
                      {name}
                    </Tag>
                  ))}
                </Space>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* 已上传的项目资料文档列表
              ✅ 2026-09-23 改版：默认折叠收起，点击标题展开/收起查看 ——
              全局事实页的主角是提取结果（左侧分类面板 + 右侧分类详情），
              资料列表降级为可展开的参考信息，避免长列表把事实内容挤到屏外 */}
      <Card
        size="small"
        title={
          <Space
            style={{ cursor: "pointer", userSelect: "none", width: "100%" }}
            onClick={() => setFactsDocsCollapsed((v) => !v)}
          >
            <span style={{ fontSize: 11, color: "#999" }}>{factsDocsCollapsed ? "▶" : "▼"}</span>
            <FileTextOutlined />
            <Text strong>已上传的项目资料</Text>
            <Tag color="blue">{docList.length} 个文件</Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {factsDocsCollapsed ? "（点击展开查看）" : "（点击收起）"}
            </Text>
          </Space>
        }
        extra={
          <Space size={4}>
            {/* ✅ 全部重解析：补齐旧版截断内容 / 启用 OCR 后重扫扫描件 */}
            {docList.length > 0 && (
              <Tooltip title="对所有文档（含已解析）重新解析，用于补齐旧版截断内容或在启用 OCR 后重扫扫描件">
                <Button
                  size="small"
                  type="link"
                  disabled={docParseDisabled}
                  onClick={() => {
                    modal.confirm({
                      title: "重新解析全部文档",
                      content: `将对 ${docList.length} 个文档重新解析（含已解析的）。解析完成后需重新执行「③ AI 提取事实」才能更新全局事实。`,
                      okText: "开始重解析",
                      cancelText: "取消",
                      onOk: () => handleParseDocuments(true),
                    });
                  }}
                >
                  <ReloadOutlined /> 全部重解析
                </Button>
              </Tooltip>
            )}
            <Button size="small" type="link" onClick={() => loadDocuments()} disabled={generating}>
              <ReloadOutlined /> 刷新
            </Button>
          </Space>
        }
        style={{ marginBottom: 12 }}
      >
        {docList.length === 0 ? (
          <div style={{ textAlign: "center", padding: 16, color: "#999", fontSize: 13 }}>
            还没有上传资料文件。工作流：① 上传文件保存 → ② 解析文档 → ③ AI 提取事实
          </div>
        ) : factsDocsCollapsed ? (
          // ✅ 折叠态：不渲染文件列表，仅保留一行摘要（避免长列表挤出事实内容）
          <div style={{ textAlign: "center", padding: 8, color: "#999", fontSize: 12 }}>
            已收起 {docList.length} 个文件（含 {docList.filter((d) => isDocParsed(d)).length}{" "}
            个已解析），点击标题展开查看
          </div>
        ) : (
          <List
            size="small"
            dataSource={docList}
            renderItem={(doc) => {
              const ftypeIcon: Record<string, string> = {
                docx: "📄",
                pdf: "📑",
                md: "📝",
                txt: "📃",
                xlsx: "📊",
                xls: "📈",
                csv: "📉",
                png: "🖼️",
                jpg: "🖼️",
                jpeg: "🖼️",
                bmp: "🖼️",
                tiff: "🖼️",
              };
              const icon = ftypeIcon[doc.file_type as string] || "📎";
              const isParsed = isDocParsed(doc);
              const isFailed = isDocFailed(doc);
              return (
                <List.Item
                  actions={[
                    ...(!isParsed
                      ? [
                          <Button
                            key="parse"
                            size="small"
                            type="link"
                            icon={<FileTextOutlined />}
                            disabled={docParseDisabled}
                            onClick={() =>
                              handleParseDocument(doc.id, doc.file_name as string, isFailed)
                            }
                          >
                            {isFailed ? "重试解析" : "解析"}
                          </Button>,
                        ]
                      : []),
                    ...(isParsed && doc.truncated
                      ? [
                          <Tooltip
                            key="reparse"
                            title="该文档内容可能被旧版解析上限截断，重新解析可获取完整内容"
                          >
                            <Button
                              size="small"
                              type="link"
                              icon={<ReloadOutlined />}
                              disabled={docParseDisabled}
                              onClick={() =>
                                handleParseDocument(doc.id, doc.file_name as string, true)
                              }
                            >
                              重新解析
                            </Button>
                          </Tooltip>,
                        ]
                      : []),
                    <Button
                      key="del"
                      size="small"
                      danger
                      type="link"
                      icon={<CloseCircleOutlined />}
                      // ✅ R210（2026-11-10 · P2）：门禁与 DocumentParseList 副本**逐字一致**
                      //   （``parsingDocs || uploadingFacts || parsingDocId === doc.id``）。
                      //   旧实现只判 uploadingFacts ⇒ 解析进行中可删文档：删除链会
                      //   purge 掉该文档的四层产物并标 stale，而**在飞解析**随后仍为
                      //   已删除的 doc_id 写产物（孤儿）并再标一次 stale ⇒ 事实列表
                      //   出现指向不存在文档的过期条目。
                      disabled={parsingDocs || uploadingFacts || parsingDocId === doc.id}
                      onClick={() => handleDeleteDocument(doc.id, doc.file_name as string)}
                    >
                      删除
                    </Button>,
                  ]}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}>
                    <span style={{ fontSize: 18 }}>{icon}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          fontWeight: 500,
                          fontSize: 14,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                        title={doc.file_name as string}
                      >
                        {doc.file_name as string}
                      </div>
                      <div style={{ fontSize: 12, color: "#999" }}>
                        <Tag color="default" style={{ margin: "0 4px 0 0" }}>
                          {(doc.file_type as string | undefined)?.toUpperCase() || "未知格式"}
                        </Tag>
                        {isFailed ? (
                          <Tag color="red" style={{ margin: "0 4px 0 0" }}>
                            解析失败，可重试
                          </Tag>
                        ) : isParsed ? (
                          <Tag color="green" style={{ margin: "0 4px 0 0" }}>
                            ✓ 已解析 {doc.text_len || 0} 字
                          </Tag>
                        ) : (
                          <Tag color="orange" style={{ margin: "0 4px 0 0" }}>
                            ⏳ 待解析
                          </Tag>
                        )}
                        {isParsed && doc.truncated && (
                          <Tooltip title="内容达到旧版上限可能被截断，建议点「重新解析」获取完整内容">
                            <Tag color="volcano" style={{ margin: "0 4px 0 0" }}>
                              ⚠ 可能被截断
                            </Tag>
                          </Tooltip>
                        )}
                        {doc.created_at as string | undefined}
                      </div>
                    </div>
                  </div>
                </List.Item>
              );
            }}
          />
        )}
      </Card>

      {/* ===== 全局事实分组列表（增强版：带模拟值/矛盾/置信度标记）=====
               ✅ 2026-09-23 改版：展示左侧分类面板选中的分类详情（项目资料下方），
               组合筛选器（模拟/矛盾/待审核）仍然生效 */}
      {facts.length > 0 && activeFactCategory ? (
        <>
          <div
            style={{
              marginBottom: 8,
              display: "flex",
              alignItems: "center",
              gap: 8,
              flexWrap: "wrap",
            }}
          >
            <Text strong>
              {factCategoryEntries.find((e) => e.category === activeFactCategory)?.title ||
                activeFactCategory}
            </Text>
            <Tag color="blue">
              {activeFactGroups.length} 组 /{" "}
              {activeFactGroups.reduce((n, g) => n + (g.items?.length || 0), 0)} 项
            </Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              来自左侧「事实分类」面板选中项
            </Text>
          </div>
          <FactsGroupList
            groups={activeFactGroups}
            filter={factsFilter}
            onEditGroup={openFactEdit}
            onDeleteGroup={handleDeleteFactGroup}
            onEditItem={openFactItemEdit}
            onResolveItem={handleResolveFactItem}
            onResolveConflict={handleResolveConflictItem}
            onAckStaleOne={handleAckStaleOne}
            chapterTitles={factChapterTitles}
            factAttrTitles={factAttrTitles}
            sourceKindTitles={factSourceKindTitles}
          />
        </>
      ) : facts.length > 0 ? (
        <>
          <FactsGroupList
            groups={facts}
            filter={factsFilter}
            onEditGroup={openFactEdit}
            onDeleteGroup={handleDeleteFactGroup}
            onEditItem={openFactItemEdit}
            onResolveItem={handleResolveFactItem}
            onResolveConflict={handleResolveConflictItem}
            onAckStaleOne={handleAckStaleOne}
            chapterTitles={factChapterTitles}
            factAttrTitles={factAttrTitles}
            sourceKindTitles={factSourceKindTitles}
          />
        </>
      ) : (
        // ✅ 优化：工具栏（AI 提取/上传/手动新增）始终渲染于列表上方，
        // 空状态内不再重复渲染同一组按钮
        <Empty
          description="暂无全局事实，可使用上方按钮提取或录入"
          image={Empty.PRESENTED_IMAGE_SIMPLE}
        />
      )}
      {/* ✅ R189（2026-11-08 · P1 功能不可达修复）：分页控件此前只写在
          第二个分支（无分类视图）里，而 `activeFactCategory` 由
          `resolveSelectedFactCategory` 保证「只要有分组就必非 null」→
          首个分类分支恒命中，分页控件**永远不渲染**。用户只能看到当前页
          20 组，第 2 页及以后既翻不到、左侧分类面板也只基于当前页构造。
          现把控件提到两分支之外：两种视图共用同一份后端 pagination，
          总数不足一页时仍按 buildFactsPagination 返回不渲染（零行为变更）。 */}
      {facts.length > 0 &&
        (() => {
          const pg = buildFactsPagination(factsPagination, FACTS_PAGE_SIZE);
          if (!pg.show) return null;
          return (
            <div style={{ marginTop: 12, textAlign: "center" }}>
              <Pagination
                current={pg.page}
                pageSize={pg.pageSize}
                total={pg.total}
                size="small"
                showSizeChanger={false}
                onChange={(p: number) => {
                  void loadFacts(p);
                }}
              />
            </div>
          );
        })()}
    </div>
  );
}
