/**
 * 「解析提取」Tab（Tab 1）面板（2026-11-04 自 SchemeWorkbenchPage 原样抽取，零行为变更）。
 *
 * 页面级状态 / handler / setter 全部显式注入；JSX / DOM / 文案 / 条件 / key /
 * 事件绑定逐字搬运，未做任何逻辑修改。
 */
import type { Dispatch, SetStateAction } from "react";
import { Button, Space, Tag, Tooltip, Typography } from "antd";
import { AppstoreOutlined, ReloadOutlined } from "@ant-design/icons";
import type { DocumentParseItem } from "../../../components/DocumentParseList";
import ParseResultCategoryPanel, {
  type ParsePanelSharedState,
} from "../../../components/ParseResultCategoryPanel";
import ImportSubTabBar, { type ImportSubTabKey } from "../../../components/ImportSubTabBar";
import BaItemFlatList from "../../../components/BaItemFlatList";
import UploadParseTab from "../../../components/UploadParseTab";
import BidAnalysisTab from "../../../components/BidAnalysisTab";
import type {
  BaItemDef,
  BaStoredItem,
  BaTextStats,
  BaGroup,
  BaSummary,
} from "../../../utils/bidAnalysis";
import type { DocRecord, DocStats, WorkflowTabKey } from "../../../utils/workflowDerived";
import type { BaEditItemLike, SchemeRecord } from "../workbenchTypes";

const { Text } = Typography;

/** 「解析提取」Tab（Tab 1）面板：页面级状态 / handler / setter 全部显式注入。 */
export interface ImportTabProps {
  // 子 Tab
  importSubTab: ImportSubTabKey;
  setImportSubTab: Dispatch<SetStateAction<ImportSubTabKey>>;
  // 项目资料文档
  docList: DocRecord[];
  docStats: DocStats;
  maxUploadBytes: number | undefined;
  categoryOptions: string[];
  generating: boolean;
  uploadingFacts: boolean;
  uploadedFiles: string[];
  parsingDocs: boolean;
  parsingDocId: string | null;
  handleUploadDocuments: (files: File[]) => void | Promise<void>;
  handleParseDocuments: (force?: boolean) => void | Promise<void>;
  loadDocuments: () => void | Promise<void>;
  handleParseDocument: (docId: string, fileName: string, force?: boolean) => void | Promise<void>;
  handlePreviewDocument: (doc: DocRecord) => void | Promise<void>;
  handleDeleteDocument: (docId: string, fileName: string) => void | Promise<void>;
  handleCategoryChange: (docId: string, category: string) => void | Promise<void>;
  setActiveTab: Dispatch<SetStateAction<string>>;
  // 解析 / 提取数据
  baSummary: BaSummary | null;
  baSummaryDerived: BaSummary | null;
  baDefs: BaItemDef[];
  baGroups: BaGroup[];
  baItems: BaStoredItem[];
  selectedBaItem: BaStoredItem | null;
  setSelectedBaItem: Dispatch<SetStateAction<BaStoredItem | null>>;
  setBaFullItem: Dispatch<SetStateAction<BaStoredItem | null>>;
  openBaEdit: (item: BaEditItemLike) => void;
  baLoading: boolean;
  baError: string | null;
  baDefsError: string | null;
  sharedParseState: ParsePanelSharedState;
  baRefreshResults: () => void;
  baActive: boolean;
  // 提取运行态 / 详情
  id: string | undefined;
  scheme: SchemeRecord | null;
  baShownProgress: number;
  baShownMsg: string;
  sectionChecking: boolean;
  sectionCheckResult: Record<string, unknown> | null;
  baTextStats: BaTextStats | null;
  loadBaMeta: () => void | Promise<void>;
  baOpenConfig: () => void;
  handleStopBa: () => void | Promise<void>;
  baStopping: boolean;
  handleCheckSections: () => void | Promise<void>;
  baRefreshAll: () => void;
  handleRerunItem: (def: BaItemDef) => void;
  baDismissSectionCheck: () => void;
  baGoImport: () => void;
  baGoOutline: () => void;
  baGoFacts: () => void;
}

/**
 * 「解析提取」左侧分类 / 提取清单面板。
 * 2026-11-04：从 ImportTab 内部上移到页面级左栏（与目录树、事实分类面板同级），
 * 使面板顶部与顶部步骤 Tab 顶部齐平，右侧步骤 Tab 栏自然右移并与子 Tab 左对齐。
 */
export interface ImportSidePanelProps {
  importSubTab: ImportSubTabKey;
  baGroups: BaGroup[];
  baItems: BaStoredItem[];
  baDefs: BaItemDef[];
  baDefsError: string | null;
  baLoading: boolean;
  baError: string | null;
  selectedBaItem: BaStoredItem | null;
  sharedParseState: ParsePanelSharedState;
  baRefreshResults: () => void;
  onSelectItem: Dispatch<SetStateAction<BaStoredItem | null>>;
  onEditItem: (item: BaEditItemLike) => void;
  onOpenFullView: Dispatch<SetStateAction<BaStoredItem | null>>;
}

export function ImportSidePanel({
  importSubTab,
  baGroups,
  baItems,
  baDefs,
  baDefsError,
  baLoading,
  baError,
  selectedBaItem,
  sharedParseState,
  baRefreshResults,
  onSelectItem,
  onEditItem,
  onOpenFullView,
}: ImportSidePanelProps) {
  return (
    <div
      style={{
        width: 300,
        flexShrink: 0,
        background: "#fafafa",
        border: "1px solid #f0f0f0",
        borderRadius: 8,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          padding: "10px 14px",
          background: "#fff",
          borderBottom: "1px solid #f0f0f0",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <Space size={6}>
          <AppstoreOutlined style={{ color: "#1677ff" }} />
          <Text strong style={{ fontSize: 13 }}>
            {importSubTab === "docs"
              ? "解析信息分类"
              : // ✅ 口径与右侧面板（baScopeText）同源：按已加载的提取项定义实报条数。
                baDefs.length
                ? `${baDefs.length} 项结构化提取`
                : "结构化提取"}
          </Text>
          <Tag color="blue" style={{ margin: 0 }}>
            {(baGroups || []).length} 分类
          </Tag>
        </Space>
        <Tooltip title="刷新解析结果">
          <Button size="small" type="text" icon={<ReloadOutlined />} onClick={baRefreshResults} />
        </Tooltip>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", padding: "8px 4px" }}>
        {importSubTab === "docs" ? (
          <ParseResultCategoryPanel
            variant="list-only"
            groups={baGroups}
            items={baItems}
            selectedItem={selectedBaItem ?? null}
            loading={baLoading}
            error={baError}
            sharedState={sharedParseState}
            onSelectItem={onSelectItem}
            onEditItem={onEditItem}
            onOpenFullView={onOpenFullView}
            onRefresh={baRefreshResults}
          />
        ) : (
          <BaItemFlatList
            defs={baDefs}
            items={baItems}
            loading={baLoading && baDefs.length === 0}
            defsError={baDefsError}
            selectedItemId={selectedBaItem?.item_id ?? null}
            onSelectItem={onSelectItem}
          />
        )}
      </div>
    </div>
  );
}

/** 「解析提取」Tab（Tab 1）面板：JSX 自页面 children 原样搬入。 */
export function ImportTab(props: ImportTabProps) {
  const {
    importSubTab,
    setImportSubTab,
    docList,
    docStats,
    maxUploadBytes,
    categoryOptions,
    generating,
    uploadingFacts,
    uploadedFiles,
    parsingDocs,
    parsingDocId,
    handleUploadDocuments,
    handleParseDocuments,
    loadDocuments,
    handleParseDocument,
    handlePreviewDocument,
    handleDeleteDocument,
    handleCategoryChange,
    setActiveTab,
    baSummary,
    baSummaryDerived,
    baDefs,
    baGroups,
    baItems,
    selectedBaItem,
    setSelectedBaItem,
    setBaFullItem,
    openBaEdit,
    baLoading,
    baError,
    baDefsError,
    sharedParseState,
    baRefreshResults,
    baActive,
    id,
    scheme,
    baShownProgress,
    baShownMsg,
    sectionChecking,
    sectionCheckResult,
    baTextStats,
    loadBaMeta,
    baOpenConfig,
    handleStopBa,
    baStopping,
    handleCheckSections,
    baRefreshAll,
    handleRerunItem,
    baDismissSectionCheck,
    baGoImport,
    baGoOutline,
    baGoFacts,
  } = props;

  return (
    /* 2026-11-04：左侧「解析信息分类」面板已上移到页面级左栏（ImportSidePanel，与
           目录树 / 事实分类面板同级），此处只渲染子 Tab 栏 + 子 Tab 主体，其左边缘与顶部
           步骤 Tab 栏（解析提取 / 目录生成 …）自然对齐。 */
    <div style={{ flex: 1, minHeight: 0, minWidth: 0, display: "flex", flexDirection: "column" }}>
      {/* ✅ 2026-09-24 T2：子 Tab 标签栏抽为受控组件 ImportSubTabBar（含提取徽标） */}
      <div style={{ flexShrink: 0, marginBottom: 8 }}>
        <ImportSubTabBar activeKey={importSubTab} onChange={setImportSubTab} summary={baSummary} />
      </div>
      <div style={{ flex: 1, minHeight: 0, minWidth: 0, display: "flex", flexDirection: "column" }}>
        {importSubTab === "docs" ? (
          <div
            className="scroll-area"
            style={{ height: "100%", minHeight: 0, overflowY: "auto", paddingRight: 4 }}
          >
            {/* 上传解析主体（含上传区、资料与解析列表、下一步等） */}
            <UploadParseTab
              docs={docList as DocumentParseItem[]}
              maxUploadBytes={maxUploadBytes}
              categoryOptions={categoryOptions}
              generating={generating}
              uploadingFacts={uploadingFacts}
              uploadedFiles={uploadedFiles}
              parsingDocs={parsingDocs}
              parsingDocId={parsingDocId}
              onUploadFiles={handleUploadDocuments}
              onParseAll={() => handleParseDocuments()}
              onReparseAll={() => handleParseDocuments(true)}
              onRefresh={loadDocuments}
              onParse={handleParseDocument}
              onPreview={handlePreviewDocument}
              onDelete={handleDeleteDocument}
              onCategoryChange={handleCategoryChange}
              onNavigate={(t: WorkflowTabKey) => setActiveTab(t)}
              // 2026-09-23 合并：上传解析「下一步」→ 切到同 Tab 的「项目提取」子页
              onSwitchToExtract={() => setImportSubTab("extract")}
              // ✅ 修复（2026-09-25）：把「解析信息分类显示栏」props 接入
              // UploadParseTab，使其内置的「信息显示窗口」真正渲染（此前页面
              // 在下方另写了一份 detail-only 面板导致重复渲染，且 UploadParseTab
              // 的解析项数「下一步」动态文案恒失真）。现由 UploadParseTab 统一
              // 渲染 detail-only 详情，与左栏 list-only 经同一 sharedParseState 联动。
              parseGroups={baGroups}
              parseItems={baItems}
              parseSelectedItem={selectedBaItem}
              parseLoading={baLoading}
              parseError={baError}
              sharedState={sharedParseState}
              onParseSelectItem={setSelectedBaItem}
              onParseEditItem={openBaEdit}
              onParseFullView={setBaFullItem}
              onParseRefresh={baRefreshResults}
              parseRunning={baActive}
            />
          </div>
        ) : (
          <BidAnalysisTab
            defs={baDefs}
            groups={baGroups}
            schemeId={id || undefined}
            projectId={scheme?.project_id}
            items={baItems}
            summary={baSummaryDerived}
            running={baActive}
            progress={baShownProgress}
            progressMsg={baShownMsg}
            parsedDocCount={docStats.parsedCount}
            selectedItem={selectedBaItem}
            sectionChecking={sectionChecking}
            sectionCheckResult={sectionCheckResult}
            textStats={baTextStats}
            defsError={baDefsError}
            // 2026-09-23：extract 子 Tab 下，「18 项结构化提取」清单已上移到左侧面板，
            // 右侧仅保留进度/操作栏 + 详情阅读区，避免与左面板重复展示同一份清单。
            contentOnly
            onRetryDefs={loadBaMeta}
            onStart={baOpenConfig}
            onStop={handleStopBa}
            stopping={baStopping}
            onOpenConfig={baOpenConfig}
            onCheckSections={handleCheckSections}
            onRefresh={baRefreshAll}
            onSelectItem={setSelectedBaItem}
            onRerunItem={handleRerunItem}
            onOpenFullView={setBaFullItem}
            onEditItem={openBaEdit}
            onDismissSectionResult={baDismissSectionCheck}
            // 2026-09-23 合并：BidAnalysisTab 内「前往上传解析」→ 切回同 Tab 的 docs 子页
            onGoImport={baGoImport}
            onGoOutline={baGoOutline}
            onGoFacts={baGoFacts}
          />
        )}
      </div>
    </div>
  );
}
