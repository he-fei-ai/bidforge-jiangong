/**
 * 「上传解析」Tab · 主体（组件级抽取）。
 *
 * 原先整个 Tab 的内联 JSX（上传区 / 上传进度 / 资料概览 / 解析进行中 /
 * 解析完成提示 / 资料与解析卡片头部的批量按钮 / 下一步跳转）都写死在
 * 8000 行的 SchemeWorkbenchPage 里，无法单测。抽成受控组件后：
 *   - 数据与状态由页面通过 props 传入，组件只负责渲染与回调；
 *   - 「资料与解析」列表复用已抽出的 DocumentParseList；
 *   - 交互正确性由 tests/uploadParseTab.test.tsx 钉住。
 *
 * 统计口径（已解析 / 待解析 / 截断 / 全部就绪）复用 computeDocStats，避免页面
 * 与组件各算一套而漂移。
 */
import React from "react";
import { Alert, App, Button, Card, Progress, Space, Spin, Tag, Tooltip, Typography, Upload } from "antd";
import {
  AppstoreOutlined,
  ArrowRightOutlined,
  ExportOutlined,
  FileSearchOutlined,
  FolderOpenOutlined,
  GlobalOutlined,
  ReloadOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import DocumentParseList, { type DocumentParseItem } from "./DocumentParseList";
import ParseResultCategoryPanel, { type ParsePanelSharedState } from "./ParseResultCategoryPanel";
import { computeDocStats, type WorkflowTabKey } from "../utils/workflowDerived";
import {
  MAX_UPLOAD_BYTES,
  UPLOAD_FILE_ACCEPT,
  partitionUploadFiles,
} from "../utils/uploadAccept";
import type { BaGroup, BaStoredItem } from "../utils/bidAnalysis";

const { Text } = Typography;

function formatMb(bytes: number): number {
  return Math.round(bytes / (1024 * 1024));
}

export type UploadParseTabProps = {
  docs: DocumentParseItem[];
  categoryOptions: string[];
  /**
   * 单文件大小上限（字节），由宿主页面从后端 /system/upload-limits 取得后下发。
   * 不传时用内置兜底默认（30MB）；组件不自取网络配置，保持受控可测。
   */
  maxUploadBytes?: number;
  /** 目录/正文等生成任务进行中（用于禁用上传与解析入口） */
  generating: boolean;
  uploadingFacts: boolean;
  /** 本次上传的文件名（仅用于进度提示） */
  uploadedFiles: string[];
  parsingDocs: boolean;
  parsingDocId: string | null;
  onUploadFiles: (files: File[]) => void;
  onParseAll: () => void;
  onReparseAll: () => void;
  onRefresh: () => void;
  onParse: (docId: string, fileName: string, force?: boolean) => void;
  onPreview: (doc: DocumentParseItem) => void;
  onDelete: (docId: string, fileName: string) => void;
  onCategoryChange: (docId: string, category: string) => void;
  onNavigate: (tab: WorkflowTabKey) => void;
  // ===== 「解析信息分类显示栏」（2026-09-23 新增，取代原「目录树」位置）=====
  // 全部为可选：未传 onParseSelectItem 时整个分类栏不渲染（默认保持既有行为，
  // 既有页面/测试不受影响）。数据与「提取项目」Tab 同源，由页面统一持有。
  /** 13 分组（后端 /bid-analysis/items 的 groups，经 normalizeBaGroups 归一） */
  parseGroups?: BaGroup[];
  /** 已存储的解析结果（后端 /bid-analysis/results 的 items） */
  parseItems?: BaStoredItem[];
  /** 当前选中解析项（与「提取项目」Tab 共享） */
  parseSelectedItem?: any | null;
  parseLoading?: boolean;
  parseError?: string | null;
  /** 选中解析项（传入后分类栏才渲染） */
  onParseSelectItem?: (item: any) => void;
  /** 人工校正当前解析项（可选） */
  onParseEditItem?: (item: any) => void;
  /** 查看完整（可选） */
  onParseFullView?: (item: any) => void;
  /** 刷新解析结果（可选） */
  onParseRefresh?: () => void;
  /**
   * 左分类栏（list-only）与右侧详情（detail-only）的展开/激活状态共享对象。
   * 传入后，本组件内置的「信息显示窗口」与页面左栏通过同一对象联动，
   * 实现"点击左侧分类 → 右侧详情同步"的连贯浏览。
   */
  sharedState?: ParsePanelSharedState;
  /**
   * AI 提取进行中（对应页面 baActive）。传入后内置详情面板在跑批期间禁用
   * 「人工校正」，避免刚写入的 source='manual' 被 AI 落库覆盖（校正白费）。
   * 不传则视为未运行（编辑按钮常驻可用）。
   */
  parseRunning?: boolean;
  /**
   * 2026-09-23 合并：切换到同 Tab 内的「项目提取」子页。
   * 传入后，「下一步」主按钮改为切换子 Tab（而非跳到 outline 顶层 Tab），
   * 让「上传解析 → 项目提取」在同页内连贯完成。
   */
  onSwitchToExtract?: () => void;
};

export default function UploadParseTab({
  docs,
  categoryOptions,
  maxUploadBytes,
  generating,
  uploadingFacts,
  uploadedFiles,
  parsingDocs,
  parsingDocId,
  onUploadFiles,
  onParseAll,
  onReparseAll,
  onRefresh,
  onParse,
  onPreview,
  onDelete,
  onCategoryChange,
  onNavigate,
  parseGroups,
  parseItems,
  parseSelectedItem,
  parseLoading,
  parseError,
  onParseSelectItem,
  onParseEditItem,
  onParseFullView,
  onParseRefresh,
  onSwitchToExtract,
  sharedState,
  parseRunning,
}: UploadParseTabProps) {
  const { modal, message } = App.useApp();
  // 「下一步」按钮文案动态生成：解析项数取后端 /items 的分组定义（旧实现硬编码
  // 「18 项」，后端增减项后文案失真）；定义未加载时不报数，只说「全表」。
  const baNextLabel = (() => {
    const n = (parseGroups || []).reduce((acc, g) => acc + (g.items?.length || 0), 0);
    return n > 0 ? `下一步：提取项目（${n} 项全表）` : "下一步：提取项目（全表）";
  })();
  const {
    parsedCount: parsedDocCount,
    pendingCount: pendingDocCount,
    truncatedCount: truncatedDocCount,
    failedCount: failedDocCount,
    actionableCount: actionableDocCount,
    allParsed: allDocsParsed,
  } = computeDocStats(docs);
  // ✅ 2026-09-24 B4：批量按钮文案区分「有待解析」与「仅剩失败可重试」两种情形。
  //    后端 parse-all（force=false）会处理所有 parsed_markdown 为空的文档
  //    （含 failed），故按钮可用条件与计数统一用 actionableCount。
  const parseAllLabel =
    pendingDocCount > 0
      ? `解析全部待解析（${actionableDocCount}）`
      : failedDocCount > 0
        ? `重试失败文档（${failedDocCount}）`
        : "解析全部待解析";
  const busy =
    generating || uploadingFacts || parsingDocs || !!parsingDocId;
  // 实际生效上限：非法（非正数）下发值回落兜底默认，避免误关闭守卫。
  const effectiveMaxBytes =
    typeof maxUploadBytes === "number" && maxUploadBytes > 0
      ? maxUploadBytes
      : MAX_UPLOAD_BYTES;
  const maxMb = formatMb(effectiveMaxBytes);

  return (
    <div className="scroll-area" style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}>
      {/* ===== 上传区 ===== */}
      <Card size="small" style={{ marginBottom: 4 }} styles={{ body: { padding: 4 } }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <Upload
            multiple
            accept={UPLOAD_FILE_ACCEPT}
            showUploadList={false}
            disabled={busy}
            beforeUpload={(file, fileList) => {
              if (fileList[0] === file) {
                const files = fileList.map((f) => (f as any).originFileObj || f) as File[];
                // ✅ 硬校验：accept 只过滤文件选择框，拖拽可绕过，故这里再按
                //    扩展名白名单 + 后端下发的大小上限剔除非法文件；合法文件照常
                //    上传，避免「整批被后端拒绝」或「白等大文件上传」。
                const { accepted, rejected } = partitionUploadFiles(files, effectiveMaxBytes);
                const typeRejected = rejected.filter((r) => r.reason === "type");
                const sizeRejected = rejected.filter((r) => r.reason === "size");
                if (typeRejected.length > 0) {
                  message.error(
                    `不支持的文件类型，已忽略：${typeRejected.map((r) => r.file.name).join("、")}`
                  );
                }
                if (sizeRejected.length > 0) {
                  message.error(
                    `单个文件不能超过 ${maxMb}MB，已忽略：${sizeRejected.map((r) => r.file.name).join("、")}`
                  );
                }
                if (accepted.length > 0) {
                  setTimeout(() => onUploadFiles(accepted), 0);
                }
              }
              return false;
            }}
          >
            <Button
              type="primary"
              icon={<UploadOutlined />}
              disabled={busy}
              loading={uploadingFacts}
            >
              上传文件保存
            </Button>
          </Upload>
          <Text type="secondary" style={{ fontSize: 12, flex: 1, minWidth: 240 }}>
            支持 Word / PDF / Markdown / 文本 / Excel / 图片（扫描件走 OCR），单个文件不超过 {maxMb}MB。
            上传保存后在本页下方列表中<b>逐个或批量解析</b>为纯文本。
          </Text>
        </div>
      </Card>

      {/* ===== 上传进度 ===== */}
      {uploadingFacts && (
        <Card size="small" style={{ marginBottom: 4 }} styles={{ body: { padding: 4 } }}>
          <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
            <div style={{ flex: "0 0 320px" }}>
              {/* ✅ 修复：旧实现硬编码 percent={50}，与真实上传进度无关
                  （axios 单请求拿不到上传进度）。改为满值 + active 条纹
                  动画表示「进行中」，不再显示虚假百分比。 */}
              <Progress percent={100} status="active" showInfo={false} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <Text strong>🔄 正在保存文件（保存后可在下方列表解析）...</Text>
              {uploadedFiles.length > 0 && (
                <Space wrap style={{ marginTop: 4 }}>
                  {uploadedFiles.map((name, i) => (
                    <Tag key={i} color="processing">{name}</Tag>
                  ))}
                </Space>
              )}
            </div>
          </div>
        </Card>
      )}

      {/* ===== 导入概览 ===== */}
      {docs.length > 0 && (
        <Card size="small" style={{ marginBottom: 4 }} styles={{ body: { padding: "3px 5px" } }}>
          <Space size={14} wrap>
            <Text strong style={{ fontSize: 13 }}>📦 资料概览</Text>
            <span>共 <Text strong>{docs.length}</Text> 个文件</span>
            <Tag color="green">✓ 已解析 {parsedDocCount}</Tag>
            {pendingDocCount > 0 && <Tag color="orange">⏳ 待解析 {pendingDocCount}</Tag>}
            {failedDocCount > 0 && (
              <Tooltip title="这些文档解析失败（如格式不支持 / 扫描件无 OCR），可在下方列表点「解析」重试">
                <Tag color="red">⚠ 解析失败 {failedDocCount}</Tag>
              </Tooltip>
            )}
            {truncatedDocCount > 0 && (
              <Tooltip title="内容达到解析上限可能被截断，建议在下方列表点「重新解析」">
                <Tag color="volcano">⚠ 可能被截断 {truncatedDocCount}</Tag>
              </Tooltip>
            )}
          </Space>
        </Card>
      )}

      {/* ===== 解析进行中 ===== */}
      {parsingDocs && (
        <Card size="small" style={{ marginBottom: 4 }} styles={{ body: { padding: 4 } }}>
          <Space>
            <Spin size="small" />
            <Text strong>正在解析文档，扫描件 OCR 较慢，请勿关闭页面...</Text>
          </Space>
        </Card>
      )}

      {/* ===== 待解析引导（上传完成但尚未解析任何文档时指路） =====
          2026-09-23 下午：从完整 Alert（带 description，~119px）改为 banner 紧凑单行（~32px），
          省 ~87px 以满足"总高度减少 1/3"。说明性文字合并到 message 里一行讲清。 */}
      {docs.length > 0 && parsedDocCount === 0 && !parsingDocs && (
        <Alert
          type="info"
          banner
          showIcon
          style={{ marginBottom: 4 }}
          message={`${docs.length} 个文件已保存，点击下方「解析全部待解析」开始（扫描件自动走 OCR）`}
        />
      )}

      {/* ===== 解析完成提示 ===== */}
      {allDocsParsed && !parsingDocs && (
        <Alert
          type="success"
          banner
          showIcon
          style={{ marginBottom: 4 }}
          message={`全部 ${docs.length} 个文档已解析完成，可进入目录生成或提取全局事实`}
          action={
            <Space size={4}>
              <Button size="small" onClick={() => onNavigate("outline")}>去目录生成</Button>
              <Button size="small" type="primary" onClick={() => onNavigate("facts")}>提取事实</Button>
            </Space>
          }
        />
      )}

      {/* ===== 资料与解析（上传 / 解析 / 增删 一体化）===== */}
      <Card
        size="small"
        title={
          <Space>
            <FolderOpenOutlined />
            <Text strong>资料与解析</Text>
            <Tag color="blue">{docs.length} 个文件</Tag>
            <Tag color="green">✓ 已解析 {parsedDocCount}</Tag>
            {pendingDocCount > 0 && <Tag color="orange">⏳ 待解析 {pendingDocCount}</Tag>}
            {failedDocCount > 0 && (
              <Tooltip title="这些文档解析失败（如格式不支持 / 扫描件无 OCR），可点「解析」重试">
                <Tag color="red">⚠ 解析失败 {failedDocCount}</Tag>
              </Tooltip>
            )}
            {truncatedDocCount > 0 && (
              <Tooltip title="内容达到解析上限可能被截断，建议点「重新解析」获取完整内容">
                <Tag color="volcano">⚠ 可能被截断 {truncatedDocCount}</Tag>
              </Tooltip>
            )}
          </Space>
        }
        extra={
          <Space size={4}>
            <Button
              type="primary"
              size="small"
              icon={<FileSearchOutlined />}
              onClick={() => onParseAll()}
              disabled={busy || actionableDocCount === 0}
              loading={parsingDocs}
            >
              {parseAllLabel}
            </Button>
            <Tooltip title="对所有文档（含已解析）重新解析，用于补齐旧版截断内容或在启用 OCR 后重扫扫描件">
              <Button
                size="small"
                icon={<ReloadOutlined />}
                disabled={busy || docs.length === 0}
                onClick={() => {
                  modal.confirm({
                    title: "重新解析全部文档",
                    content: `将对 ${docs.length} 个文档重新解析（含已解析的）。解析完成后需在「全局事实」页重新执行「AI 提取事实」才能更新事实变量。`,
                    okText: "开始重解析",
                    cancelText: "取消",
                    onOk: () => onReparseAll(),
                  });
                }}
              >
                全部重解析
              </Button>
            </Tooltip>
            {/* ✅ 修复（2026-09-23）：与同组件「上传 / 全部解析 / 全部重解析」同口径用
                busy —— 旧条件遗漏 parsingDocId 与 uploadingFacts，单份文档解析中
                （OCR 可能数分钟）或上传中仍可点刷新，loadDocuments 覆盖进行中的
                列表，前端状态与后端瞬时不一致。 */}
            <Button size="small" type="text" onClick={() => onRefresh()} disabled={busy}>
              <ReloadOutlined /> 刷新
            </Button>
          </Space>
        }
        style={{ marginBottom: 4 }}
      >
        <DocumentParseList
          docs={docs}
          parsingDocs={parsingDocs}
          parsingDocId={parsingDocId}
          categoryOptions={categoryOptions}
          onParse={onParse}
          onPreview={onPreview}
          onDelete={onDelete}
          onCategoryChange={onCategoryChange}
          // 2026-09-23 晚：资料与解析保持折叠 + 高度再减 1/3（用户反向放大信息显示窗口后，
          // 资料与解析要让位）：默认只显示前 2 行（原 3），下方有「展开全部」link 按需展开。
          compact
          defaultVisibleCount={2}
        />
      </Card>

      {/* ===== 信息显示窗口（2026-09-23 更名：原「解析信息分类显示栏」）=====
          按解析结果类别（后端 13 分组）分组展示；点击分类查看其下解析项，
          右侧显示详情，支持 查看/编辑（人工校正）/复制。仅在页面注入
          onParseSelectItem 时渲染，未注入时保持既有行为不变。
          2026-09-23 同步改造：import 模块左侧不再渲染目录树（SchemeTreePanel
          仅在 outline/content/review 等有目录依赖的模块显示），此处成为
          解析提取模块的主要信息浏览入口。 */}
      {onParseSelectItem && (
        <Card
          size="small"
          title={
            <Space>
              <AppstoreOutlined />
              <Text strong>信息显示窗口</Text>
              <Tag color="blue">{(parseGroups || []).length} 个分类</Tag>
            </Space>
          }
          style={{ marginBottom: 4 }}
        >
          <ParseResultCategoryPanel
            variant="detail-only"
            groups={parseGroups || []}
            items={parseItems || []}
            selectedItem={parseSelectedItem ?? null}
            loading={parseLoading}
            error={parseError}
            sharedState={sharedState}
            onSelectItem={onParseSelectItem}
            onEditItem={onParseEditItem}
            onOpenFullView={onParseFullView}
            onRefresh={onParseRefresh}
            running={parseRunning}
          />
        </Card>
      )}

      {/* ===== 下一步 ===== */}
      <Space wrap>
        <Button
          type="primary"
          icon={<ArrowRightOutlined />}
          disabled={parsedDocCount === 0}
          // 2026-09-23 合并：优先切换到同 Tab 的「项目提取」子页（若宿主已接入内嵌子 Tab）；
          // 否则回退到 outline 顶层 Tab（旧调用链 / 单测保留行为）。
          onClick={() => {
            if (onSwitchToExtract) onSwitchToExtract();
            else onNavigate("outline");
          }}
        >
          {baNextLabel}
        </Button>
        <Button icon={<ExportOutlined />} disabled={parsedDocCount === 0} onClick={() => onNavigate("outline")}>
          直接去目录生成
        </Button>
        <Button icon={<GlobalOutlined />} disabled={parsedDocCount === 0} onClick={() => onNavigate("facts")}>
          提取全局事实
        </Button>
        {parsedDocCount === 0 && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {docs.length === 0 ? "先导入至少一份资料文件" : "至少解析一份文档后才能进入后续步骤"}
          </Text>
        )}
      </Space>
    </div>
  );
}
