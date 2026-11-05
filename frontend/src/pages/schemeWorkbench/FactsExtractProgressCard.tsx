import { TaskControlButtons } from "./TaskControlButtons.tsx";
import { Card, Progress, Space } from "antd";
import { Typography } from "antd";
const { Title, Text } = Typography;

export function FactsExtractProgressCard({
  progress,
  progressMsg,
  logs,
  controls,
}: {
  progress: number;
  progressMsg: string;
  logs: Array<{ progress: number; message: string; time?: number; [k: string]: unknown }>;
  /** ✅ R106 FIX-2：可选控制区（暂停/恢复/停止），由页面注入 TaskControlButtons */
  controls?: React.ReactNode;
}) {
  const list = (logs || []).slice().reverse();
  return (
    <Card size="small" style={{ marginBottom: 12 }}>
      <Space direction="vertical" style={{ width: "100%" }} size={6}>
        <Text strong>📚 正在提取全局事实</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {progressMsg}
        </Text>
        <Progress percent={Math.round((progress || 0) * 100)} size="small" />
        {controls}
        <div style={{ maxHeight: 180, overflow: "auto" }}>
          {list.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              正在连接 AI，准备提取...
            </Text>
          ) : (
            list.map((l, i) => (
              <div key={i} style={{ fontSize: 12, lineHeight: "20px" }}>
                <Text type="secondary">{Math.round((l.progress || 0) * 100)}%</Text>{" "}
                <span>{l.message}</span>
              </div>
            ))
          )}
        </div>
        {list.length > 0 && (
          <Text type="secondary" style={{ fontSize: 12 }}>
            共 {list.length} 条进度记录
          </Text>
        )}
      </Space>
    </Card>
  );
}

/**
 * 任务控制按钮组（R106 FIX-2 · 组件级可测）
 *
 * 目录生成 / 全局事实两 Tab 此前没有停止入口（进度卡纯展示，只能靠全局任务
 * 抽屉停止）。与 ContentGenerationTab 内的暂停/恢复/停止同构，共用页面
 * handleControl + activeTaskIdRef（两条链的 SSE 均已写 task_id，后端 pause/
 * resume/stop 检查点早已生效，本次仅补 UI）：
 * - 停止发出指令后置灰显示「停止中」，任务落终态（generating 复位）才恢复
 *   —— 协作式停止「指令 accepted ≠ 已停止」，不给乐观回执；
 * - 暂停/恢复按 paused 互斥，停止中两者一并禁用（避免 stop 与 resume 竞态）。
 */
