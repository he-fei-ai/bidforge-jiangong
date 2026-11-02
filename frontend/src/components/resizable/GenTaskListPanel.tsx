/**
 * 后台任务列表面板（工作台顶部监视区右栏 · 2026-10-30 R145）
 *
 * 数据源与侧栏 TaskStatusBar 同一接口 GET /api/v1/system/activity
 * （任务注册表 + 内存实时态合并），本面板只做 5s 轮询、**不另开 SSE 连接**，
 * 避免侧栏任务栏 + 本面板双常驻连接加重后端压力。
 * 状态文案 / 任务类型中文映射复用 TaskStatusBar 导出的单一事实源
 * （statusMeta / taskTypeLabel），两处展示永不漂移。
 *
 * fail-soft：请求失败保留上次数据（常驻 UI 不打断用户），页面隐藏时暂停轮询。
 */
import { useEffect, useRef, useState } from "react";
import { Card, Empty, Progress, Tag, Typography } from "antd";
import { systemApi } from "../../api";
import { statusMeta, taskTypeLabel } from "../TaskStatusBar";

const { Text } = Typography;

type TaskItem = {
  id: string;
  task_type: string;
  status: string;
  progress: number;
  message?: string;
  scheme_name?: string;
};

type ActivityData = {
  tasks?: { running?: TaskItem[]; recent?: TaskItem[] };
};

const POLL_MS = 5000;

export function GenTaskListPanel({ style }: { style?: React.CSSProperties }) {
  const [data, setData] = useState<ActivityData | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    let stopped = false;

    const load = async () => {
      if (stopped || inFlightRef.current || document.hidden) return;
      inFlightRef.current = true;
      try {
        const { data: d } = await systemApi.activity(8);
        if (!stopped) setData((d || {}) as ActivityData);
      } catch {
        // fail-soft：保留上次数据
      } finally {
        inFlightRef.current = false;
      }
    };

    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    // 切回页面立即补偿拉取（与 TaskStatusBar 同一口径）
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  const running = data?.tasks?.running ?? [];
  const recent = data?.tasks?.recent ?? [];

  return (
    <div style={{ height: "100%", minHeight: 0, minWidth: 0, display: "flex", ...style }}>
      <Card
        size="small"
        className="scroll-area"
        style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", overflow: "hidden" }}
        // ✅ R150：Card 真正滚动的是 body，滚动条必须挂在 body 上（挂 root 不生效）
        classNames={{ body: "bp-pane-scroll" }}
        styles={{ body: { flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 12px" } }}
        title={
          <Text strong style={{ fontSize: 13 }}>
            后台任务
            {running.length > 0 && (
              <Tag color="processing" style={{ marginLeft: 8 }}>
                {running.length} 个运行中
              </Tag>
            )}
          </Text>
        }
      >
        {running.length === 0 && recent.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<Text type="secondary" style={{ fontSize: 12 }}>暂无任务数据</Text>}
            style={{ marginTop: 16 }}
          />
        ) : (
          <>
            {running.length > 0 && (
              <>
                <Text type="secondary" style={{ fontSize: 11, letterSpacing: 1 }}>运行中</Text>
                {running.map((t) => {
                  const meta = statusMeta(t.status);
                  return (
                    <div key={t.id} style={{ padding: "6px 0", borderBottom: "1px dashed #f0f2f5" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                        <Text strong style={{ fontSize: 12, flexShrink: 0 }}>
                          {taskTypeLabel(t.task_type)}
                        </Text>
                        <Text
                          type="secondary"
                          style={{ fontSize: 11, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                        >
                          {t.scheme_name || t.message || ""}
                        </Text>
                        <Tag color={meta.color} style={{ marginRight: 0, flexShrink: 0 }}>
                          {meta.label}
                        </Tag>
                      </div>
                      <Progress
                        percent={Math.round((t.progress || 0) * 100)}
                        size="small"
                        status={t.status === "paused" ? "normal" : "active"}
                        style={{ marginTop: 2, marginBottom: 0 }}
                      />
                    </div>
                  );
                })}
              </>
            )}
            {recent.length > 0 && (
              <>
                <Text type="secondary" style={{ fontSize: 11, letterSpacing: 1, display: "block", marginTop: 8 }}>
                  最近完成
                </Text>
                {recent.slice(0, 5).map((t) => {
                  const meta = statusMeta(t.status);
                  return (
                    <div
                      key={t.id}
                      style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 0", minWidth: 0 }}
                    >
                      <Text strong style={{ fontSize: 12, flexShrink: 0 }}>
                        {taskTypeLabel(t.task_type)}
                      </Text>
                      <Text
                        type="secondary"
                        style={{ fontSize: 11, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {t.scheme_name || t.message || ""}
                      </Text>
                      <Tag color={meta.color} style={{ marginRight: 0, flexShrink: 0 }}>
                        {meta.label}
                      </Tag>
                    </div>
                  );
                })}
              </>
            )}
          </>
        )}
      </Card>
    </div>
  );
}

export default GenTaskListPanel;
