/**
 * 目录版本面板（R181 · 2026-11-06）。
 *
 * 后端 `GET /schemes/{id}/sections/outline-versions` 与
 * `POST .../outline-versions/{snapshot_id}/restore` 在 R179 已就绪，但前端
 * 没有列表与回退入口，用户看不到「自动确认前的 5 条快照」也无法回退。
 * 本面板只做最小可用闭环：列出快照（时间 / 来源 / 章节数）→ 二次确认 →
 * 回退 → 刷新列表并通知页面重新加载目录与章节。
 *
 * 回退的缓存失效（导出缓存 / 一致性扫描缓存 / 图表重挂）由后端强制完成，
 * 前端**不重复造**失效逻辑。
 */
import { useCallback, useEffect, useState } from "react";
import type { MessageInstance } from "antd/es/message/interface";
import { Button, Card, Empty, Popconfirm, Space, Tag, Typography } from "antd";
import { HistoryOutlined, ReloadOutlined } from "@ant-design/icons";
import { sectionsApi } from "../../api";

const { Text } = Typography;

export interface OutlineSnapshot {
  snapshot_id: string;
  type?: string;
  created_at?: string;
  node_count?: number;
  has_content?: boolean;
}

const TYPE_LABEL: Record<string, string> = {
  outline_auto_confirm: "自动确认前",
  outline_rollback_undo: "回退前（可撤销）",
};

function toSnapshot(value: unknown): OutlineSnapshot | null {
  if (typeof value !== "object" || value === null) return null;
  const row = value as Record<string, unknown>;
  const snapshotId = typeof row.snapshot_id === "string" ? row.snapshot_id : "";
  if (!snapshotId) return null;
  return {
    snapshot_id: snapshotId,
    type: typeof row.type === "string" ? row.type : undefined,
    created_at: typeof row.created_at === "string" ? row.created_at : undefined,
    node_count: typeof row.node_count === "number" ? row.node_count : undefined,
    has_content: typeof row.has_content === "boolean" ? row.has_content : undefined,
  };
}

function formatTime(raw?: string): string {
  if (!raw) return "时间未知";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export interface OutlineVersionsPanelProps {
  schemeId: string;
  msg: MessageInstance;
  /** 回退成功后通知页面重新加载目录与章节（缓存失效由后端负责）。 */
  onRestored?: () => void | Promise<void>;
}

export function OutlineVersionsPanel({ schemeId, msg, onRestored }: OutlineVersionsPanelProps) {
  const [items, setItems] = useState<OutlineSnapshot[]>([]);
  const [loading, setLoading] = useState(false);
  const [restoring, setRestoring] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!schemeId) {
      setItems([]);
      return;
    }
    setLoading(true);
    try {
      const resp = await sectionsApi.outlineVersions(schemeId, 5);
      const raw: unknown = resp?.data?.snapshots;
      setItems(
        Array.isArray(raw)
          ? raw.map(toSnapshot).filter((v): v is OutlineSnapshot => v !== null)
          : [],
      );
    } catch (e: unknown) {
      setItems([]);
      msg.error(`目录版本加载失败：${e instanceof Error ? e.message : "未知错误"}`);
    } finally {
      setLoading(false);
    }
  }, [schemeId, msg]);

  useEffect(() => {
    void load();
  }, [load]);

  const restore = useCallback(
    async (snapshotId: string) => {
      setRestoring(snapshotId);
      try {
        const { data } = await sectionsApi.restoreOutlineVersion(schemeId, snapshotId);
        // ✅ R218（2026-11-11 · P3 · D-E）：快照回退响应含 `empty_top_chapters`
        //    （空壳一级章 = 只有标题无子目录），前端此前零消费 ⇒ 回退后目录里
        //    出现空壳章却无提示（用户误以为目录完整）。以 WARNING 如实告知
        //    （不阻断回退，回退本身成功）。
        const _emptyTop = Number((data as { empty_top_chapters?: unknown })?.empty_top_chapters ?? 0);
        if (_emptyTop > 0) {
          msg.warning(`已回退到所选目录版本，但检出 ${_emptyTop} 个空壳一级章（只有标题、无子目录），请检查并补充`);
        } else {
          msg.success("已回退到所选目录版本");
        }
        await load();
        await onRestored?.();
      } catch (e: unknown) {
        msg.error(`回退失败：${e instanceof Error ? e.message : "未知错误"}`);
      } finally {
        setRestoring(null);
      }
    },
    [schemeId, msg, load, onRestored],
  );

  return (
    <Card
      size="small"
      data-testid="outline-versions-panel"
      title={
        <Space>
          <HistoryOutlined />
          <Text strong>目录版本（最近 5 条快照）</Text>
        </Space>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() => void load()}
        >
          刷新
        </Button>
      }
      style={{ marginBottom: 12 }}
    >
      {items.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={loading ? "加载中…" : "暂无目录快照（自动确认或回退时会自动创建）"}
        />
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {items.map((item) => (
            <Space key={item.snapshot_id} wrap data-testid="outline-version-item">
              <Text>{formatTime(item.created_at)}</Text>
              <Tag color={item.type === "outline_rollback_undo" ? "orange" : "blue"}>
                {TYPE_LABEL[item.type || ""] || item.type || "快照"}
              </Tag>
              <Text type="secondary">{item.node_count ?? 0} 个章节</Text>
              {item.has_content && <Tag color="green">含正文</Tag>}
              <Popconfirm
                title="回退到该版本？"
                description="当前目录与正文将被该快照覆盖；回退前的状态会自动存为新快照，可再次回退撤销。"
                okText="确认回退"
                cancelText="取消"
                onConfirm={() => void restore(item.snapshot_id)}
              >
                <Button
                  size="small"
                  danger
                  loading={restoring === item.snapshot_id}
                  data-testid="outline-version-restore"
                >
                  回退
                </Button>
              </Popconfirm>
            </Space>
          ))}
        </Space>
      )}
    </Card>
  );
}
