import { Alert, Button, Space } from "antd";
import type { UnknownTaskInfo } from "./taskPolling";

/**
 * ✅ 2026-11-04 拆分（G10 收口）：未知态横幅自 SchemeWorkbenchPage 原位搬迁。
 * 纯展示组件：文案、testid、按钮行为与搬出前逐字一致；状态仍由
 * useUnknownTaskRecovery 持有。
 */
export interface UnknownTaskBannerProps {
  unknownTask: UnknownTaskInfo | null;
  generating: boolean;
  typeLabel: Record<string, string>;
  onReattach: () => void;
  onDismiss: () => void;
}

export function UnknownTaskBanner({
  unknownTask,
  generating,
  typeLabel,
  onReattach,
  onDismiss,
}: UnknownTaskBannerProps) {
  if (!unknownTask) return null;
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 8 }}
      data-testid="unknown-task-banner"
      message={`连接已断开，后台「${typeLabel[unknownTask.genType] || unknownTask.genType}」任务可能仍在运行`}
      description={`${unknownTask.reason}。该任务可能仍在上游运行（AI 仍在计费）；为避免并发 / 重复计费，请勿直接重新生成，可重新接管监听或在重新生成时按指引先查询任务状态。`}
      action={
        <Space direction="vertical" size={4}>
          <Button
            size="small"
            type="primary"
            data-testid="unknown-task-reattach"
            loading={generating}
            onClick={onReattach}
          >
            重新接管任务
          </Button>
          <Button size="small" data-testid="unknown-task-dismiss" onClick={onDismiss}>
            暂时隐藏
          </Button>
        </Space>
      }
    />
  );
}
