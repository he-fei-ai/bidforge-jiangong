/**
 * 全局 Header「运行状态 / 消息中心」提示区（所有页面共用）
 *
 * 展示两类信息（可同时出现）：
 * 1. liveTask：当前方案运行中的后台任务（目录/正文生成等）实时进度提示，
 *    由 useSchemeLiveTask（方案工作台）轮询并写入消息中心存储，任务完成后自动刷新数据；
 * 2. 最新一条活动消息：来自全局消息中心 activityCenter —— 目录生成、正文生成、
 *    审核与预检、导出文档等所有 msg.* 提示、后台消息都会同步到这里
 *    （原生 toast 已全面取消，统一在此展示），点击可打开完整历史浮层。
 *
 * 单条消息自动隐藏：success/info 8s、warning/error 15s（仅隐藏展示，历史保留）。
 */
import { useEffect, useState } from "react";
import { Button, Empty, Popover, Tooltip } from "antd";
import {
  BellOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DeleteOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  PauseCircleOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  clearActivity,
  getActivityItems,
  getActivityTime,
  getLiveTask,
  subscribeActivity,
  type ActivityItem,
  type ActivityKind,
} from "../utils/activityCenter";
import { liveTaskTypeLabel } from "../hooks/useSchemeLiveTask";

const KIND_META: Record<ActivityKind, { icon: React.ReactNode; color: string }> = {
  success: { icon: <CheckCircleOutlined />, color: "#52C41A" },
  error: { icon: <CloseCircleOutlined />, color: "#FF4D4F" },
  warning: { icon: <WarningOutlined />, color: "#FAAD14" },
  info: { icon: <InfoCircleOutlined />, color: "#1677FF" },
  loading: { icon: <LoadingOutlined />, color: "#1677FF" },
};

const AUTO_HIDE_MS: Record<string, number> = {
  success: 8000,
  info: 8000,
  loading: 8000,
  warning: 15000,
  error: 15000,
};

export default function ActivityHint() {
  const [, force] = useState(0);
  const [hiddenId, setHiddenId] = useState<string | null>(null);

  // 订阅消息中心：任何 pushActivity 都会触发重渲染
  useEffect(() => subscribeActivity(() => force((v) => v + 1)), []);

  const items = getActivityItems();
  const latest = items[0] as ActivityItem | undefined;
  const liveTask = getLiveTask();

  // 最新消息到达后定时收起展示（历史浮层仍保留）
  useEffect(() => {
    if (!latest) return;
    setHiddenId(null);
    const ttl = AUTO_HIDE_MS[latest.kind] ?? 8000;
    const timer = window.setTimeout(() => setHiddenId(latest.id), ttl);
    return () => window.clearTimeout(timer);
  }, [latest?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const chipVisible = !!latest && hiddenId !== latest.id;

  const history = (
    <div className="bp-activity-history">
      <div className="bp-activity-history-head">
        <span>消息记录（{items.length}）</span>
        {items.length > 0 && (
          <Button
            size="small"
            type="text"
            icon={<DeleteOutlined />}
            onClick={clearActivity}
          >
            清空
          </Button>
        )}
      </div>
      <div className="bp-activity-history-body">
        {items.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="暂无消息"
            style={{ margin: "16px 0" }}
          />
        ) : (
          items.map((it) => {
            const meta = KIND_META[it.kind] || KIND_META.info;
            return (
              <div key={it.id} className="bp-activity-row">
                <span style={{ color: meta.color }} className="bp-activity-row-icon">
                  {meta.icon}
                </span>
                <span className="bp-activity-row-src">{it.source}</span>
                <span className="bp-activity-row-text" title={it.text}>
                  {it.text}
                </span>
                <span className="bp-activity-row-time">{getActivityTime(it.ts)}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );

  const liveHint =
    liveTask ? (
      <span className="bp-live-task-hint" key={liveTask.id}>
        {liveTask.status === "paused" ? <PauseCircleOutlined /> : <LoadingOutlined />}
        检测到后台「{liveTaskTypeLabel(liveTask.task_type)}」任务
        {liveTask.status === "paused" ? "已暂停" : "仍在进行"}
        （{Math.round((liveTask.progress || 0) * 100)}%），完成后自动刷新
      </span>
    ) : null;

  const chip =
    chipVisible && latest ? (
      <Tooltip title="点击查看消息记录" mouseEnterDelay={0.4}>
        <span className="bp-activity-chip" style={{ color: KIND_META[latest.kind].color }}>
          {KIND_META[latest.kind].icon}
          <span className="bp-activity-chip-text">{latest.text}</span>
        </span>
      </Tooltip>
    ) : (
      <Tooltip title="消息记录（各功能提示与后台消息）">
        <span className="bp-activity-bell">
          <BellOutlined />
        </span>
      </Tooltip>
    );

  return (
    <>
      {liveHint}
      <Popover
        placement="bottomRight"
        trigger="click"
        content={history}
        overlayClassName="bp-activity-popover"
      >
        {chip}
      </Popover>
    </>
  );
}
