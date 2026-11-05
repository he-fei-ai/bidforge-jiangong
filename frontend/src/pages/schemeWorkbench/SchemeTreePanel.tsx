import {
  collectAllKeys,
  hasUnsavedLocalNodes,
  scrollToNodeInContainer,
  type TreeNode,
} from "./outlineTreeModel.ts";
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  CaretRightOutlined,
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import { Alert, Button, Empty, Input, Space, Tag, Tooltip, Tree } from "antd";
// ✅ any 清理专项（2026-11-02）：treeData 与 titleRender 节点契约复用 antd TreeDataNode
import type { TreeDataNode } from "antd";
import { memo } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

export type SchemeTreePanelProps = {
  tree: TreeNode[];
  treeData: TreeDataNode[];
  expandedKeys: React.Key[];
  selectedSectionKey?: React.Key;
  loading: boolean;
  generating: boolean;
  onRefresh: () => void;
  onToggleExpand: () => void;
  /** ✅ 导航职责：目录"生成/管理"统一在「目录生成」Tab，左侧面板只负责跳转，避免两处重复入口 */
  onGoToOutline: () => void;
  onSelectSection: (key: React.Key) => void;
  onExpand: (keys: React.Key[]) => void;
  /** ✅ 编辑增强：左侧树也支持上移/下移/重命名/删除，复用与右侧编辑树同一套逻辑 */
  moveFlags: Record<string, { up: boolean; down: boolean }>;
  onMoveNode: (key: string, dir: -1 | 1) => void;
  onRenameNode: (key: string, title: string) => void;
  onDeleteNode: (key: string) => void;
  onAddChildNode: (key: string) => void;
  onAddSiblingNode: (key: string) => void;
  /** ✅ 编辑增强：左侧树顶部"有未保存修改"提示中的「保存目录」动作 */
  onSaveOutline: () => void;
  /** 行内重命名编辑态（与右侧编辑树共用同一份状态） */
  renamingKey: string | null;
  renamingValue: string;
  setRenamingKey: (k: string | null) => void;
  setRenamingValue: (v: string) => void;
  /** 左侧树滚动容器 ref（由父组件持有，编辑树移动时同步定位用） */
  scrollRef: React.RefObject<HTMLDivElement>;
};

export const SchemeTreePanel = memo(function SchemeTreePanel(p: SchemeTreePanelProps) {
  const {
    tree,
    treeData,
    expandedKeys,
    selectedSectionKey,
    loading,
    generating,
    onRefresh,
    onToggleExpand,
    onGoToOutline,
    onSelectSection,
    onExpand,
    moveFlags,
    onMoveNode,
    onRenameNode,
    onDeleteNode,
    onAddChildNode,
    onAddSiblingNode,
    onSaveOutline,
    renamingKey,
    renamingValue,
    setRenamingKey,
    setRenamingValue,
    scrollRef,
  } = p;
  // ✅ 编辑增强：左侧树滚动容器（ref 由父组件持有，编辑树移动时同步定位）
  const handleMoveNode = (key: string, dir: -1 | 1) => {
    onMoveNode(key, dir);
    scrollToNodeInContainer(scrollRef.current, key);
  };
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
        }}
      >
        <Space size={6}>
          <FolderOpenOutlined style={{ color: "#1677ff" }} />
          <Text strong style={{ fontSize: 13 }}>
            目录树
          </Text>
          {tree.length > 0 && (
            <Tag color="blue" style={{ margin: 0 }}>
              {collectAllKeys(tree).length}
            </Tag>
          )}
        </Space>
        <Space size={4}>
          <Tooltip title="刷新">
            <Button size="small" type="text" icon={<ReloadOutlined />} onClick={onRefresh} />
          </Tooltip>
          <Tooltip title="展开/收起">
            <Button size="small" type="text" icon={<SwapOutlined />} onClick={onToggleExpand} />
          </Tooltip>
        </Space>
      </div>

      {/* ✅ 编辑增强：左侧树新增 local_ 临时节点后，顶部高亮提醒需「保存目录」，
          右侧工具栏已有橙色 Tag，这里在导航面板内也给出可一键保存的入口 */}
      {hasUnsavedLocalNodes(tree) && (
        <Alert
          type="warning"
          showIcon
          banner
          message="有未保存的新增章节"
          description="新增的章节尚未同步到服务端，请点击「保存目录」。"
          action={
            <Button size="small" type="primary" onClick={onSaveOutline}>
              保存目录
            </Button>
          }
          style={{ borderRadius: 0 }}
        />
      )}

      {/* 树（本面板只做"章节导航"，目录的生成/编辑统一在「目录生成」Tab；
          但为编辑便利，行内提供上移/下移按钮，复用与右侧编辑树同一套 moveNode 逻辑） */}
      <div
        ref={scrollRef}
        className="scroll-area"
        style={{ flex: 1, overflow: "auto", padding: "8px 4px", minHeight: 0 }}
      >
        {/* ✅ 编辑增强：左侧树行内操作按钮默认隐藏，悬浮行时淡入，
            小屏/窄面板下显著省空间（悬浮即"折叠/展开"） */}
        <style>{`
          .swb-tree-row .swb-act { opacity: 0; transition: opacity .15s ease; }
          .swb-tree-row:hover .swb-act { opacity: 1; }
          .swb-tree-row:hover { background: rgba(0,0,0,.03); }
        `}</style>
        {loading ? (
          <div style={{ textAlign: "center", padding: 40, color: "#999" }}>加载中...</div>
        ) : tree.length > 0 ? (
          <Tree
            treeData={treeData}
            expandedKeys={expandedKeys}
            onExpand={onExpand}
            selectedKeys={selectedSectionKey ? [selectedSectionKey] : []}
            onSelect={(keys) => {
              if (keys.length > 0) onSelectSection(keys[0]);
            }}
            titleRender={(node) => {
              // ✅ 编辑增强：行内重命名（与右侧编辑树共用 renamingKey 状态）
              if (renamingKey === node.key) {
                return (
                  <Input
                    size="small"
                    autoFocus
                    value={renamingValue}
                    style={{ width: "100%" }}
                    onChange={(e) => setRenamingValue(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onBlur={() => {
                      onRenameNode(node.key as string, renamingValue);
                      setRenamingKey(null);
                    }}
                    onPressEnter={() => {
                      onRenameNode(node.key as string, renamingValue);
                      setRenamingKey(null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setRenamingKey(null);
                      e.stopPropagation();
                    }}
                  />
                );
              }
              return (
                <div
                  className="swb-tree-row"
                  style={{ display: "flex", alignItems: "center", gap: 4, width: "100%" }}
                >
                  <span
                    style={{
                      flex: 1,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {node.title as React.ReactNode}
                  </span>
                  <Space
                    className="swb-act"
                    size={2}
                    align="center"
                    style={{ flexShrink: 0, marginLeft: "auto" }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Tooltip title="新增子章节">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        icon={<CaretRightOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          onAddChildNode(node.key as string);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="新增同级章节">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        icon={<PlusOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          onAddSiblingNode(node.key as string);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="重命名">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        icon={<EditOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          setRenamingValue(typeof node.title === "string" ? node.title : "");
                          setRenamingKey(node.key as string);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title={!moveFlags[node.key as string]?.up ? "已是最顶部" : "上移"}>
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        disabled={!moveFlags[node.key as string]?.up}
                        icon={<ArrowUpOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleMoveNode(node.key as string, -1);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title={!moveFlags[node.key as string]?.down ? "已是最底部" : "下移"}>
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        disabled={!moveFlags[node.key as string]?.down}
                        icon={<ArrowDownOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleMoveNode(node.key as string, 1);
                        }}
                      />
                    </Tooltip>
                    <Tooltip title="删除（含所有子章节）">
                      <Button
                        type="text"
                        size="small"
                        tabIndex={-1}
                        danger
                        icon={<DeleteOutlined />}
                        onClick={(e) => {
                          e.stopPropagation();
                          onDeleteNode(node.key as string);
                        }}
                      />
                    </Tooltip>
                  </Space>
                </div>
              );
            }}
            showLine={{ showLeafIcon: false }}
            blockNode
          />
        ) : (
          <Empty description="暂无目录" image={Empty.PRESENTED_IMAGE_SIMPLE}>
            <Button
              size="small"
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={generating}
              onClick={onGoToOutline}
            >
              前往「目录生成」
            </Button>
          </Empty>
        )}
      </div>
    </div>
  );
});
