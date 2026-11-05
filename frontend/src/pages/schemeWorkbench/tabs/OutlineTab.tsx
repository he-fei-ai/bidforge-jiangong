/**
 * 「目录生成」Tab（Tab 2）面板（2026-11-04 自 SchemeWorkbenchPage 原样抽取，零行为变更）。
 *
 * 页面级状态 / handler / setter 全部显式注入；JSX / DOM / 文案 / 条件 / key /
 * 事件绑定逐字搬运，未做任何逻辑修改。
 */
import type { Dispatch, Key, RefObject, SetStateAction } from "react";
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
  UploadOutlined,
} from "@ant-design/icons";
import {
  collectAllKeys,
  findSectionById,
  hasUnsavedLocalNodes,
  type TreeNode,
} from "../outlineTreeModel";
import { NEXT_TAB } from "../../../utils/workflowDerived";
import {
  UPLOAD_FILE_ACCEPT,
  partitionUploadFiles,
  resolveMaxUploadBytes,
  formatUploadLimitMb,
} from "../../../utils/uploadAccept";
import type { MultiSectionHint, OutlineNodeInput, WorkbenchSection } from "../workbenchTypes";
import { OutlineTreeActions } from "../OutlineTreeActions";
import { OutlineNodeBudgetPanel } from "../OutlineNodeBudgetPanel";
import { TaskControlButtons } from "../TaskControlButtons";

const { Text } = Typography;

/** 「目录生成」Tab（Tab 2）面板：页面级状态 / handler / setter 全部显式注入。 */
export interface OutlineTabProps {
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
}

export function OutlineTab({
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
}: OutlineTabProps) {
  return (
    // ✅ 修复：目录生成内容（编制要求卡 + 多标段提示 + 编辑树 + 目录库/AI 生成区）较长，
    //    与其余 Tab 同一模式：根容器必须 flex:1 + minHeight:0 + overflowY:auto，
    //    否则超出视口的底部内容会被外层 overflow:hidden 裁掉，无法滚动查看/操作。
    <div
      className="scroll-area"
      style={{ flex: 1, minHeight: 0, overflowY: "auto", paddingRight: 4 }}
    >
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
          disabled={generating}
          loading={generating && genType === "outline"}
        >
          {tree.length > 0 ? "AI 重新生成目录" : "AI 生成目录"}
        </Button>
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
          <Button icon={<UploadOutlined />} disabled={generating}>
            导入目录（智能识别）
          </Button>
        </Upload>
        <Tooltip title="勾选后，识别结果先按标准章节骨架归位（保留用户细分内容，未归位内容进「补充章节」）再应用">
          <Checkbox
            checked={outlineImportReorganize}
            disabled={generating}
            onChange={(e) => setOutlineImportReorganize(e.target.checked)}
          >
            整理为标准结构
          </Checkbox>
        </Tooltip>
        <Button icon={<FolderOpenOutlined />} onClick={openLibraryPicker} disabled={generating}>
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
            disabled={generating || !lastUpload}
          >
            存为目录库
          </Button>
        </Tooltip>
        <Button icon={<CaretRightOutlined />} onClick={() => addChildNode()} disabled={generating}>
          新增一级章节
        </Button>
        <Button
          onClick={saveOutlineTree}
          type="primary"
          ghost
          disabled={generating || tree.length === 0}
        >
          💾 保存目录
        </Button>
        <Button
          onClick={clearAllOutline}
          danger
          type="primary"
          ghost
          disabled={generating || tree.length === 0}
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
        {selectedSection && tree.length > 0 && (
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
              draggable
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
