import { FactsExtractProgressCard } from "./FactsExtractProgressCard.tsx";
import { PauseOutlined, PlayCircleOutlined, StopOutlined } from "@ant-design/icons";
import { Button, Tag } from "antd";

export function TaskControlButtons({
  paused,
  stopping,
  onControl,
}: {
  paused: boolean;
  stopping: boolean;
  onControl: (action: "pause" | "resume" | "stop") => void;
}) {
  return (
    <>
      <Button
        icon={<PauseOutlined />}
        onClick={() => onControl("pause")}
        disabled={paused || stopping}
      >
        暂停
      </Button>
      <Button
        icon={<PlayCircleOutlined />}
        onClick={() => onControl("resume")}
        disabled={!paused || stopping}
      >
        恢复
      </Button>
      <Button
        danger
        icon={<StopOutlined />}
        onClick={() => onControl("stop")}
        disabled={stopping}
        loading={stopping}
      >
        {stopping ? "停止中" : "停止"}
      </Button>
      {paused && !stopping && (
        <Tag color="warning" style={{ margin: 0 }}>
          ⏸ 已暂停（恢复后继续生成）
        </Tag>
      )}
    </>
  );
}

/**
 * 全局事实 · AI 调整面板（R45 · D5 · 组件级可测）
 *
 * 把「自然语言描述 → 预览待确认计划 → 二次确认 → 一次性应用」这条状态机
 * 从页面里抽成独立子组件，对齐 FactsExtractProgressCard 的抽法。
 *
 * 设计要点
 * --------
 * 1. **纯接线、不持有后端契约**：预览/应用分别注入 `preview` / `apply`
 *    两个异步函数，组件只负责收集输入、渲染计划、发起二次确认。
 *    页面保留「请求 payload 怎么拼」的单一出口，避免两处漂移。
 * 2. **切方案即丢弃**：方案切换时清空计划与弹层 —— 否则方案 A 的计划
 *    会被应用到方案 B（页面此前的 `setFactsAdjustPlan(null)` 已在此归位）。
 * 3. **空输入不收**：OK 按钮与 Enter 提交都做 `trim()` 判空，
 *    空白指令不下发（后端会当「无要求」返回空计划，浪费一次 AI 调用）。
 * 4. **计划最多列 20 条**：与确认弹窗同口径，超长时截断展示。
 *
 * ⚠️ 历史（R45 · G4）：调整要求原先用 `window.prompt` 收集 —— 阻塞式原生
 * 对话框会卡住整个渲染线程、无法排版说明，且在部分容器/隐私模式下被直接
 * 禁用（用户点了按钮「什么也没发生」）。全仓其余 57 处交互均走 antd
 * `modal.confirm`，本组件即那次收口的形态，现进一步抽成可测组件。
 */
