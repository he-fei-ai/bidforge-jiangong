/**
 * 「重置布局」按钮（2026-11-16 新增）。
 *
 * 作用：清空全部被记忆的拖拽布局（localStorage），并广播复位事件，
 * 各 ResizableGroup 收到后把面板比例恢复到默认值。
 *
 * 为什么做成「广播」而不是「逐组回调」：
 *   按钮不需要知道仓里有多少个分隔组；将来新增模块的分组也不必改这个按钮
 *   （避免 AGENTS.md 反复记账的「同一判据两份副本 / 漏改一处」反模式）。
 */
import { Button, Tooltip } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { clearAllLayouts, emitLayoutReset } from "./layoutStore";

export interface ResetLayoutButtonProps {
  /** 按钮尺寸（默认 small）。 */
  size?: "small" | "middle";
  /** 按钮类型（默认 text，用于放在工具栏）。 */
  type?: "default" | "text" | "link" | "primary";
  /** 是否显示文字（默认 false = 只显示图标）。 */
  showText?: boolean;
}

export function ResetLayoutButton({
  size = "small",
  type = "text",
  showText = false,
}: ResetLayoutButtonProps) {
  const onClick = () => {
    clearAllLayouts();
    emitLayoutReset();
  };
  return (
    <Tooltip title="重置布局：恢复各面板默认大小（双击分隔条可单独重置）">
      <Button size={size} type={type} icon={<ReloadOutlined />} onClick={onClick}>
        {showText ? "重置布局" : null}
      </Button>
    </Tooltip>
  );
}

export default ResetLayoutButton;
