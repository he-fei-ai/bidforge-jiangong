/**
 * 「目录生成」Tab（Tab 2）面板（2026-11-04 自 SchemeWorkbenchPage 原样抽取，零行为变更）。
 *
 * 页面级状态 / handler / setter 全部显式注入；JSX / DOM / 文案 / 条件 / key /
 * 事件绑定逐字搬运，未做任何逻辑修改。
 */
import type { Dispatch, Key, ReactNode, RefObject, SetStateAction } from "react";
import type { MessageInstance } from "antd/es/message/interface";
import type { TreeDataNode, TreeProps } from "antd";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Input,
  Space,
  Tag,
  Tooltip,
  Tree,
  Typography,
  Upload,
} from "antd";
import {
  AimOutlined,
  CaretRightOutlined,
  FileTextOutlined,
  FolderOpenOutlined,
  PlayCircleOutlined,
  RobotOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import {
  collectAllKeys,
  findSectionById,
  hasUnsavedLocalNodes,
  type TreeNode,
} from "../outlineTreeModel";
import { ScoringCoveragePanel } from "./ScoringCoveragePanel";
import { NEXT_TAB } from "../../../utils/workflowDerived";
import {
  UPLOAD_FILE_ACCEPT,
  partitionUploadFiles,
  resolveMaxUploadBytes,
  formatUploadLimitMb,
} from "../../../utils/uploadAccept";
import type { MultiSectionHint, OutlineNodeInput, WorkbenchSection } from "../workbenchTypes";
import { OutlineTreeActions } from "../OutlineTreeActions";
import { OutlineVersionsPanel } from "../OutlineVersionsPanel";
import { OutlineNodeBudgetPanel } from "../OutlineNodeBudgetPanel";
import { TaskControlButtons } from "../TaskControlButtons";

const { Text } = Typography;

/** 「目录生成」Tab（Tab 2）面板：页面级状态 / handler / setter 全部显式注入。 */
export interface OutlineTabProps {
  /** ✅ R179 N1（2026-11-05）：自动确认风险提示条（开关开启时才有内容）。
   *  关闭（默认）时传 null，界面与旧版完全一致。 */
  autoConfirmNotice?: ReactNode;
  // 编制要求
  requirements: string;
  setRequirements: Dispatch<SetStateAction<string>>;
  saveRequirements: () => void | Promise<void>;
  // 多标段提示
  multiSectionHint: MultiSectionHint | null;
  // 目录生成运行态
  generating: boolean;
  genType: string;
  handleGenerateOutline: () => void | Promise<void>;
  // 导入目录（智能识别）
  maxUploadBytes: number | undefined;
  handleUploadParseOutline: (file: File) => void | Promise<void>;
  outlineImportReorganize: boolean;
  setOutlineImportReorganize: Dispatch<SetStateAction<boolean>>;
  // 目录库
  openLibraryPicker: () => void | Promise<void>;
  lastUpload: { id: string; name: string; outline: OutlineNodeInput[] } | null;
  handleSaveAsLibrary: () => void | Promise<void>;
  // 目录树操作
  tree: TreeNode[];
  addChildNode: (parentKey?: string) => void;
  saveOutlineTree: () => void | Promise<void>;
  clearAllOutline: () => void;
  taskPaused: boolean;
  taskStopping: boolean;
  handleControl: (action: "pause" | "resume" | "stop") => void | Promise<void>;
  // 选中章节 + 树编辑
  selectedSection: WorkbenchSection | null;
  setSelectedSection: Dispatch<SetStateAction<WorkbenchSection | null>>;
  moveFlags: Record<string, { up: boolean; down: boolean }>;
  moveNode: (key: string, dir: -1 | 1) => void;
  deleteNode: (key: string) => void;
  setActiveTab: Dispatch<SetStateAction<string>>;
  reorderHint: string | null;
  setReorderHint: Dispatch<SetStateAction<string | null>>;
  handleBudgetChange: (key: string, budget: number) => void;
  editTreeScrollRef: RefObject<HTMLDivElement>;
  outlineEditTreeData: TreeDataNode[];
  expandedKeys: Key[];
  setExpandedKeys: Dispatch<SetStateAction<Key[]>>;
  onOutlineTreeDrop: (info: Parameters<NonNullable<TreeProps["onDrop"]>>[0]) => void;
  msg: MessageInstance;
  // ✅ R179 M1（2026-11-05）：评分项覆盖校验面板所需的最小注入
  schemeId: string;
  projectId: string;
  onCreateSuggestedSection: (
    title: string,
    parentId: string | null,
    level: number,
  ) => Promise<void>;
  /** ✅ R181 N1：目录版本回退成功后重新加载目录/章节（不传则面板只刷新自身）。 */
  onOutlineRestored?: () => void | Promise<void>;
  // ✅ 阶段 2 旁路：omp 文件工作区 Agent 生成目录入口（受 omp_task_enabled 控制）
  ompTaskEnabled?: boolean;
  generatingOmp?: boolean;
  handleGenerateOutlineOmp?: () => void | Promise<void>;
}

export function OutlineTab({
  autoConfirmNotice,
  requirements,
  setRequirements,
  saveRequirements,
  multiSectionHint,
  generating,
  genType,
  handleGenerateOutline,
  maxUploadBytes,
  handleUploadParseOutline,
  outlineImportReorganize,
  setOutlineImportReorganize,
  openLibraryPicker,
  lastUpload,
  handleSaveAsLibrary,
  tree,
  addChildNode,
  saveOutlineTree,
  clearAllOutline,
  taskPaused,
  taskStopping,
  handleControl,
  selectedSection,
  setSelectedSection,
  moveFlags,
  moveNode,
  deleteNode,
  setActiveTab,
  reorderHint,
  setReorderHint,
  handleBudgetChange,
  editTreeScrollRef,
  outlineEditTreeData,
  expandedKeys,
  setExpandedKeys,
  onOutlineTreeDrop,
  msg,
  schemeId,
  projectId,
  onCreateSuggestedSection,
  onOutlineRestored,
  ompTaskEnabled,
  generatingOmp,
  handleGenerateOutlineOmp,
}: OutlineTabProps) {
  // ✅ 阶段 2 旁路：omp 生成期间后端会整表重建 sections，所有会写目录树的入口
  //    （主链生成 / 导入 / 套用 / 保存 / 清除 / 新增章节）必须一并禁用 ——
  //    后端的 content/outline 双 409 只认主链路任务表，感知不到 omp 旁路。
  const outlineBusy = generating || !!generatingOmp;
  // ✅ R179 M1（2026-11-05）：评分项缺失/多余章节的「定位」——复用既有
  //    findSectionById 与 selectedSection 选中态（不新增第二个选中状态），
  //    同时展开目标祖先节点保证节点可见。
  const handleLocateScoringSection = (sectionId: string) => {
    const sec = findSectionById(tree, sectionId);
    if (!sec) return;
    setSelectedSection(sec);
    const trail: Key[] = [];
    const walk = (nodes: TreeNode[], key: string, parents: Key[]): boolean => {
      for (const n of nodes) {
        if (n.key === key) {
          trail.push(...parents);
          return true;
        }
        if (n.children && n.children.length > 0 && walk(n.children, key, [...parents, n.key!])) {
          return true;
        }
      }
      return false;
    };
    walk(tree, sectionId, []);
    setExpandedKeys(
      (prev) => Array.from(new Set([...(prev as Key[]), ...trail, sec.key!])) as Key[],
    );
    const el = document.querySelector<HTMLElement>(`[data-section-id="${sectionId}"]`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  return (
    // ✅ 修复：目录生成内容（编制要求卡 + 多标段提示 + 编辑树 + 目录库/AI 生成区）较长，
    //    与其余 Tab 同一模式：根容器必须 flex:1 + minHeight:0 + overflowY:auto，
    //    否则超出视口的底部内容会被外层 overflow:hidden 裁掉，无法滚动查看/操作。
    <div
      className="scroll-area"
      style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}
    >
      {/* ✅ R179 N1：自动确认风险提示（开关关闭时 autoConfirmNotice 为 null，不渲染） */}
      {autoConfirmNotice}
      {/* 编制要求 / 评审要点（对齐 OpenBidKit score-planning：要求→目录必含映射） */}
      <Card
        size="small"
        style={{ marginBottom: 12, background: "#fafafa" }}
        styles={{ body: { padding: "8px 12px" } }}
        title={
          <Space size={8}>
            <AimOutlined style={{ color: "#1677ff" }} />
            <Text strong style={{ fontSize: 13 }}>
              编制要求 / 评审要点（目录必须逐条覆盖）
            </Text>
          </Space>
        }
        extra={
          <Button size="small" type="primary" onClick={saveRequirements}>
            保存
          </Button>
        }
      >
        <Input.TextArea
          value={requirements}
          onChange={(e) => setRequirements(e.target.value)}
          placeholder={
            "逐条填写编制要求或评审要点（每行一条），AI 生成目录时将强制逐条覆盖，审核阶段自动补齐缺失章节。示例：\n危大工程清单及管控措施\n监测方案与信息化施工要求\n应急预案与应急物资配置"
          }
          autoSize={{ minRows: 3, maxRows: 8 }}
        />
      </Card>
      {/* ✅ R179 M1（2026-11-05）：评分项 → 目录映射覆盖校验（只读校验，不自动生成正文） */}
      <ScoringCoveragePanel
        schemeId={schemeId}
        projectId={projectId}
        msg={msg}
        onLocate={handleLocateScoringSection}
        onCreateSuggested={onCreateSuggestedSection}
      />
      {/* ✅ 多标段提示（对齐 OpenBidKit bidSectionDetector）：导入的招标文件疑似多标段时给出拆分建议 */}
      {multiSectionHint?.has_multiple && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`检测到疑似多标段项目资料（约 ${multiSectionHint.total_declared || multiSectionHint.detected_count} 个标段）`}
          description="当前导入的项目资料可能包含多个标段 / 标包。建议按标段拆分后分别生成方案，避免目录与正文互相混淆。"
        />
      )}
      {/* 顶部操作区 */}
      <Space style={{ marginBottom: 12 }} wrap>
        <Button
          type="primary"
          icon={<PlayCircleOutlined />}
          onClick={handleGenerateOutline}
          disabled={outlineBusy}
          loading={generating && genType === "outline"}
        >
          {tree.length > 0 ? "AI 重新生成目录" : "AI 生成目录"}
        </Button>
        {/* ✅ 阶段 2 旁路：omp 文件工作区 Agent 生成目录（受 omp_task_enabled 控制）。
            与主链路 SSE 不同，这是普通同步请求：后端拉起 omp 多步 Agent 写 OUTLINE.md
            后整表回写入库。仅在开关开启时显隐；禁用由后端 409 兜底。 */}
        {ompTaskEnabled && (
          <Tooltip title="由 omp 文件工作区 AI Agent 独立生成目录（与主链路 SSE 互不干扰；多步生成可能需要数分钟）">
          <Button
            icon={<RobotOutlined />}
            onClick={handleGenerateOutlineOmp}
            disabled={outlineBusy}
            loading={generatingOmp}
          >
              🤖 Agent 生成目录
            </Button>
          </Tooltip>
        )}
        <Upload
          accept={UPLOAD_FILE_ACCEPT}
          beforeUpload={(file) => {
            // ✅ 2026-10-10（FIX-2 类型预检）：accept 只过滤文件选择框，拖拽可
            //    绕过。与链① UploadParseTab 同口径，先按扩展名白名单 + 大小
            //    上限拦截，避免非法文件整份传完才被后端 400 拒绝。
            const { accepted, rejected } = partitionUploadFiles(
              [file],
              resolveMaxUploadBytes(maxUploadBytes),
            );
            if (rejected.length > 0) {
              const byType = rejected.filter((r) => r.reason === "type");
              const bySize = rejected.filter((r) => r.reason === "size");
              if (byType.length > 0) {
                msg.error(`不支持的文件类型，已忽略：${byType.map((r) => r.file.name).join("、")}`);
              }
              if (bySize.length > 0) {
                msg.error(`文件过大，请上传 ${formatUploadLimitMb(maxUploadBytes)} 以内的文件`);
              }
              return false;
            }
            handleUploadParseOutline(accepted[0]);
            return false; // 阻止自动上传
          }}
          showUploadList={false}
        >
          <Button icon={<UploadOutlined />} disabled={outlineBusy}>
            导入目录（智能识别）
          </Button>
        </Upload>
        <Tooltip title="勾选后，识别结果先按标准章节骨架归位（保留用户细分内容，未归位内容进「补充章节」）再应用">
          <Checkbox
            checked={outlineImportReorganize}
            disabled={outlineBusy}
            onChange={(e) => setOutlineImportReorganize(e.target.checked)}
          >
            整理为标准结构
          </Checkbox>
        </Tooltip>
        <Button icon={<FolderOpenOutlined />} onClick={openLibraryPicker} disabled={outlineBusy}>
          从目录库套用
        </Button>
        <Tooltip
          title={
            lastUpload
              ? `把「${lastUpload.name}」的识别结果存为目录库（待审核）`
              : "请先「导入目录（智能识别）」上传文件"
          }
        >
          <Button
            icon={<FileTextOutlined />}
            onClick={handleSaveAsLibrary}
            disabled={outlineBusy || !lastUpload}
          >
            存为目录库
          </Button>
        </Tooltip>
        <Button icon={<CaretRightOutlined />} onClick={() => addChildNode()} disabled={outlineBusy}>
          新增一级章节
        </Button>
        <Button
          onClick={saveOutlineTree}
          type="primary"
          ghost
          disabled={outlineBusy || tree.length === 0}
        >
          💾 保存目录
        </Button>
        <Button
          onClick={clearAllOutline}
          danger
          type="primary"
          ghost
          disabled={outlineBusy || tree.length === 0}
        >
          🗑️ 清除所有目录
        </Button>
        {/* ✅ R106 FIX-2：目录生成 Tab 补停止入口（此前只能靠全局任务抽屉停止）*/}
        {generating && genType === "outline" && (
          <TaskControlButtons
            paused={taskPaused}
            stopping={taskStopping}
            onControl={handleControl}
          />
        )}
        <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
          💡 双击章节标题可改名；行内 ↑/↓ 或拖拽可调整顺序；选中后可一键上移/下移/删除
        </Text>
      </Space>

      {/* ✅ 目录生成进度已统一到页面顶部进度区（此处不再重复渲染第二个进度条） */}

      {/* ✅ R181 N1：目录版本列表 + 二次确认回退（后端已强制失效缓存） */}
      <OutlineVersionsPanel schemeId={schemeId} msg={msg} onRestored={onOutlineRestored} />

      {/* 目录树编辑区 */}
      <Card
        size="small"
        title={
          <Space>
            <Text strong>目录结构</Text>
            <Tag color="blue">{collectAllKeys(tree).length} 个章节</Tag>
            {hasUnsavedLocalNodes(tree) && <Tag color="orange">有未保存的修改</Tag>}
          </Space>
        }
        extra={
          <OutlineTreeActions
            selected={selectedSection as TreeNode | null}
            moveFlags={moveFlags}
            showNext={tree.length > 0}
            disabled={!!generatingOmp}
            onAddChild={() => selectedSection && addChildNode(selectedSection.key!)}
            onMove={(dir) => selectedSection && moveNode(selectedSection.key!, dir)}
            onDelete={() => selectedSection && deleteNode(selectedSection.key!)}
            onNextStep={() => setActiveTab(NEXT_TAB.outline)}
          />
        }
      >
        {reorderHint && (
          <Alert
            type="success"
            showIcon
            banner
            closable
            message={reorderHint}
            style={{ marginBottom: 8 }}
            onClose={() => setReorderHint(null)}
          />
        )}
        {/* ✅ 字数预算编辑入口（2026-09-21 补齐功能缺口）：
            此前预算全链路只读，用户无法在正文生成前规划每章篇幅 */}
        {/* ✅ R231（2026-11-13）：omp 生成期间整表重建 sections，选中章节可能被
            删除 ⇒ 预算 PATCH 落 0 行 409；omp 期隐藏面板（omp 结束后随 load()
            恢复）。主链路生成期间保持原行为（word_budget-only PATCH 豁免守卫）。 */}
        {selectedSection && tree.length > 0 && !generatingOmp && (
          <OutlineNodeBudgetPanel
            node={selectedSection as TreeNode}
            onBudgetChange={handleBudgetChange}
          />
        )}
        {tree.length === 0 ? (
          <Empty description="暂无目录，请点击上方 AI 生成目录 或 导入目录" />
        ) : (
          <div
            ref={editTreeScrollRef}
            className="scroll-area"
            style={{ minHeight: 200, maxHeight: "calc(100vh - 420px)", overflow: "auto" }}
          >
            <Tree
              treeData={outlineEditTreeData}
              /* ✅ R231（2026-11-13）：omp 目录生成期间禁用拖拽——拖拽落库
                 (reorder) 与 omp 结束后的整表重建互踩，后端双 409 感知不到
                 omp 旁路（可行性报告 §11.4 局限 B）；主链路生成期间保持原行为
                 （reorder 端点自带 outline_generation_in_progress 409）。 */
              draggable={!generatingOmp}
              expandedKeys={expandedKeys}
              onExpand={(keys) => setExpandedKeys(keys as Key[])}
              selectedKeys={selectedSection ? [selectedSection.key!] : []}
              onSelect={(keys) => {
                if (keys.length > 0) {
                  const sec = findSectionById(tree, keys[0] as string);
                  if (sec) setSelectedSection(sec);
                }
              }}
              onDrop={onOutlineTreeDrop}
              showLine={{ showLeafIcon: false }}
              blockNode
            />
          </div>
        )}
      </Card>
    </div>
  );
}
