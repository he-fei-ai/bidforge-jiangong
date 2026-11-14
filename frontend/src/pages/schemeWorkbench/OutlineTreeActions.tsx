import { sectionsApi } from "../../api";
import { type TreeNode } from "./outlineTreeModel.ts";
import {
  ArrowDownOutlined,
  ArrowRightOutlined,
  ArrowUpOutlined,
  CaretRightOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { Button, Space, Tooltip } from "antd";

export function OutlineTreeActions({
  selected,
  moveFlags,
  showNext,
  onAddChild,
  onMove,
  onDelete,
  onNextStep,
  disabled = false,
}: {
  selected: TreeNode | null;
  moveFlags: Record<string, { up: boolean; down: boolean }>;
  showNext: boolean;
  onAddChild: () => void;
  onMove: (dir: -1 | 1) => void;
  onDelete: () => void;
  onNextStep: () => void;
  /** ✅ R231（2026-11-13）：omp 目录生成期间整表重建 sections，树行内结构操作
   *  （新增/上移/下移/删除）必须一并禁用——后端双 409 只认主链路任务表，
   *  感知不到 omp 旁路（可行性报告 §11.4 局限 B）；缺省 false，既有调用
   *  不传时行为逐字不变。 */
  disabled?: boolean;
}) {
  return (
    <Space size={4}>
      {showNext && (
        <Button size="small" type="link" icon={<ArrowRightOutlined />} onClick={onNextStep}>
          下一步：全局事实
        </Button>
      )}
      <Tooltip title="新增子章节到选中节点">
        <Button
          size="small"
          icon={<CaretRightOutlined />}
          disabled={disabled || !selected}
          onClick={onAddChild}
        >
          新增子章节
        </Button>
      </Tooltip>
      <Tooltip title="上移选中章节">
        <Button
          size="small"
          icon={<ArrowUpOutlined />}
          disabled={disabled || !selected || !moveFlags[selected.key]?.up}
          onClick={() => onMove(-1)}
        >
          上移
        </Button>
      </Tooltip>
      <Tooltip title="下移选中章节">
        <Button
          size="small"
          icon={<ArrowDownOutlined />}
          disabled={disabled || !selected || !moveFlags[selected.key]?.down}
          onClick={() => onMove(1)}
        >
          下移
        </Button>
      </Tooltip>
      <Tooltip title="删除选中节点">
        <Button
          size="small"
          danger
          icon={<StopOutlined />}
          disabled={disabled || !selected}
          onClick={onDelete}
        >
          删除
        </Button>
      </Tooltip>
    </Space>
  );
}

/** 字数预算的合法区间（与后端 sectionsApi.update 的整数语义一致，超限会被服务端拒绝） */
