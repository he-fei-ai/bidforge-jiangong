/**
 * ✅ R179 M1（2026-11-05）：评分项 → 目录覆盖校验面板（目录生成 Tab · 编制要求卡片之后）。
 *
 * 设计约束：
 *  1) 只读校验，不自动生成正文、不自动调用 AI（「解析评分项」仅触发评分项抽取）；
 *  2) 「缺失项」v1 只做「新增建议章节 + 定位」，新增章节交给父级既有章节创建链路落库；
 *  3) undetermined（解析不到评分项）时只显示「无法判定」，**不得显示 0%**；
 *  4) 请求失败不得沿用旧结论（先清空再拉取）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Collapse, Empty, Space, Tag, Typography } from "antd";
import { AimOutlined, PlusOutlined, ReloadOutlined, ThunderboltOutlined } from "@ant-design/icons";
import type { MessageInstance } from "antd/es/message/interface";
import { scoringApi } from "../../../api";
import type {
  ScoringCoverageResponse,
  ScoringExtraNode,
  FailedSource,
} from "../../../types/scoring";

const { Text } = Typography;

interface Props {
  schemeId: string;
  projectId: string;
  msg: MessageInstance;
  onLocate: (sectionId: string) => void;
  onCreateSuggested: (title: string, parentId: string | null, level: number) => Promise<void>;
}

const STATUS_LABEL: Record<string, string> = {
  ok: "覆盖完整",
  partial: "部分来源未解析",
  undetermined: "无法判定",
};

const UNDETERMINED_TEXT: Record<string, string> = {
  no_extraction: "尚未解析评分项（当前项目还没有可用的评分项快照）",
  extraction_failed: "评分项解析失败（未能从投标文件中提取到评分项）",
  empty_items: "评分项解析成功但结果为空（投标文件中未出现评分项）",
};

function pct(rate: number | null): string {
  if (rate === null) return "—";
  return `${Math.round(rate * 1000) / 10}%`;
}

export function ScoringCoveragePanel({
  schemeId,
  projectId,
  msg,
  onLocate,
  onCreateSuggested,
}: Props) {
  const [data, setData] = useState<ScoringCoverageResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [error, setError] = useState<string>("");
  const [creatingKey, setCreatingKey] = useState<string>("");
  const seqRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (!schemeId) return;
    const seq = ++seqRef.current;
    abortRef.current?.abort();
    abortRef.current = null;
    // 先清空旧结论：请求失败时不得展示上一次的覆盖率
    setData(null);
    setError("");
    setLoading(true);
    try {
      const { data: res } = await scoringApi.coverage(schemeId);
      if (seq !== seqRef.current) return;
      setData(res);
    } catch (e: unknown) {
      if (seq !== seqRef.current) return;
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      setError(err?.response?.data?.detail || err?.message || "加载评分项覆盖度失败");
      setData(null);
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, [schemeId]);

  useEffect(() => {
    void load();
    return () => {
      seqRef.current += 1;
      abortRef.current?.abort();
    };
  }, [load]);

  const handleExtract = useCallback(async () => {
    if (!projectId) {
      msg.warning("当前方案未关联项目，无法解析评分项");
      return;
    }
    setExtracting(true);
    try {
      const { data: res } = await scoringApi.extract(projectId);
      if (res.status === "failed") {
        msg.warning(res.error || "评分项解析失败（未定位到评标办法/评分标准）");
      } else if (res.status === "empty") {
        // 0 条结果但已保留旧快照：必须提示，不得报成「解析完成」
        msg.warning(res.error || "评分项解析返回 0 条，已保留上一次快照");
      } else if (res.status === "partial") {
        msg.warning(`评分项已解析（部分来源失败）：成功 ${res.docs_ok}/${res.docs_total} 个文档`);
      } else {
        msg.success(`评分项解析完成：共 ${res.items_count} 条`);
      }
      await load();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } }; message?: string };
      msg.error(err?.response?.data?.detail || err?.message || "评分项解析失败");
    } finally {
      setExtracting(false);
    }
  }, [projectId, load, msg]);

  const handleCreateSuggested = useCallback(
    async (itemId: string, title: string, parentId: string | null, level: number) => {
      if (!title.trim()) return;
      setCreatingKey(itemId);
      try {
        await onCreateSuggested(title.trim(), parentId, level);
        msg.success(`已新增建议章节「${title.trim()}」，请补充正文内容`);
        await load();
      } catch (e: unknown) {
        const err = e as { response?: { data?: { detail?: string } }; message?: string };
        msg.error(err?.response?.data?.detail || err?.message || "新增建议章节失败");
      } finally {
        setCreatingKey("");
      }
    },
    [onCreateSuggested, load, msg],
  );

  const status = data?.status || "";
  const summary = data?.summary;
  const undetermined = status === "undetermined";
  // ✅ 方案 §1.7：已覆盖（mapped）默认折叠为绿色组，展开可见明细；不占用主视野
  const mappedItems = (data?.items || []).filter((m) => m.status === "mapped");

  return (
    <Card
      size="small"
      data-testid="scoring-coverage-panel"
      style={{ marginBottom: 12, background: "var(--bp-panel-bg)" }}
      styles={{ body: { padding: "8px 12px" } }}
      title={
        <Space size={8}>
          <AimOutlined style={{ color: "#13c2c2" }} />
          <Text strong style={{ fontSize: 13 }}>
            评分项覆盖校验（评分项 → 目录映射）
          </Text>
          {status && (
            <Tag
              data-testid="scoring-status-tag"
              color={
                status === "ok"
                  ? "green"
                  : status === "partial"
                    ? "orange"
                    : status === "undetermined"
                      ? "gold"
                      : "default"
              }
            >
              {data?.extraction?.status === "empty"
                ? "沿用旧快照（未更新）"
                : STATUS_LABEL[status] || status}
            </Tag>
          )}
          {!undetermined && summary && (
            <Tag color="blue" data-testid="scoring-coverage-rate">
              覆盖率 {pct(summary.coverage_rate)}
            </Tag>
          )}
        </Space>
      }
      extra={
        <Space size={6}>
          <Button
            size="small"
            icon={<ReloadOutlined />}
            onClick={() => void load()}
            loading={loading}
            data-testid="scoring-refresh-btn"
          >
            重新校验
          </Button>
          <Button
            size="small"
            type="primary"
            ghost
            icon={<ThunderboltOutlined />}
            onClick={() => void handleExtract()}
            loading={extracting}
            data-testid="scoring-extract-btn"
          >
            解析评分项
          </Button>
        </Space>
      }
    >
      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="评分项覆盖校验加载失败（数据不可用）"
          description={error}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      )}

      {!error && undetermined && (
        <Alert
          type="warning"
          showIcon
          data-testid="scoring-undetermined"
          message="无法判定（未解析到评分项）"
          description={
            `${UNDETERMINED_TEXT[data?.undetermined_reason || ""] || "评分项数据不可用"}。` +
            "请先点击「解析评分项」，从投标文件的评标办法/评分标准中提取评分项。"
          }
        />
      )}

      {!error && !undetermined && data?.extraction?.status === "empty" && (
        <Alert
          type="warning"
          showIcon
          data-testid="scoring-empty-run"
          style={{ marginBottom: 8 }}
          message="本次解析返回 0 条评分项，已保留上一次快照"
          description={
            `当前覆盖结论基于旧快照（共 ${summary?.total_items ?? 0} 项），` +
            "请复核投标文件的评标办法/评分标准是否变更后再采信。"
          }
        />
      )}

      {!error && !undetermined && summary && data && (
        <>
          <Space wrap size={[6, 6]} style={{ marginBottom: 8 }}>
            <Tag>评分项 {summary.total_items}</Tag>
            <Tag color="green">已覆盖 {summary.mapped}</Tag>
            <Tag color={summary.missing > 0 ? "red" : "default"}>缺失 {summary.missing}</Tag>
            <Tag color={summary.extra > 0 ? "orange" : "default"}>多余章节 {summary.extra}</Tag>
            <Tag color={summary.unmatched > 0 ? "volcano" : "default"}>
              未解析来源 {summary.unmatched}
            </Tag>
            {data.extraction && (
              <Tag>
                来源 {data.extraction.docs_ok}/{data.extraction.docs_total} 个文档
              </Tag>
            )}
          </Space>

          {mappedItems.length > 0 && (
            <Collapse
              ghost
              size="small"
              data-testid="scoring-mapped-collapse"
              style={{ marginBottom: 8 }}
              items={[
                {
                  key: "mapped",
                  label: (
                    <Text strong style={{ fontSize: 12, color: "var(--bp-success-text)" }}>
                      已覆盖 {mappedItems.length} 项（点击展开明细）
                    </Text>
                  ),
                  children: (
                    <div>
                      {mappedItems.map((m) => {
                        const sid = m.section_id;
                        return (
                          <div
                            key={m.id}
                            style={{
                              display: "flex",
                              gap: 6,
                              alignItems: "center",
                              padding: "2px 0",
                            }}
                          >
                            <Tag color="green">
                              {m.score === null ? m.score_raw || "?" : `${m.score} 分`}
                            </Tag>
                            <span style={{ flex: 1 }}>
                              {m.item_no ? `${m.item_no} ` : ""}
                              {m.item_name}
                            </span>
                            {m.section_title && (
                              <Text type="secondary" style={{ fontSize: 11 }}>
                                → {m.section_title}
                              </Text>
                            )}
                            {sid && (
                              <Button
                                type="link"
                                size="small"
                                style={{ padding: 0 }}
                                data-testid={`scoring-locate-${sid}`}
                                onClick={() => onLocate(sid)}
                              >
                                定位
                              </Button>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  ),
                },
              ]}
            />
          )}

          {data.missing.length > 0 && (
            <div data-testid="scoring-missing-list" style={{ marginBottom: 8 }}>
              <Text strong style={{ fontSize: 12, color: "var(--bp-error-text)" }}>
                缺失章节（评分项在目录中无对应章节）
              </Text>
              {data.missing.map((m) => (
                <div
                  key={m.item_id}
                  style={{ display: "flex", gap: 8, alignItems: "center", padding: "4px 0" }}
                >
                  <Tag color="red">{m.score === null ? m.score_raw || "?" : `${m.score} 分`}</Tag>
                  <span style={{ flex: 1 }}>
                    {m.item_no ? `${m.item_no} ` : ""}
                    {m.name}
                    {m.page_num !== null && (
                      <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                        P{m.page_num}
                      </Text>
                    )}
                  </span>
                  {m.suggestion && (
                    <Button
                      size="small"
                      icon={<PlusOutlined />}
                      loading={creatingKey === m.item_id}
                      data-testid={`scoring-add-suggested-${m.item_id}`}
                      onClick={() =>
                        void handleCreateSuggested(
                          m.item_id,
                          m.suggestion ? m.suggestion.title : m.name,
                          m.suggestion ? m.suggestion.parent_id : null,
                          m.suggestion ? m.suggestion.level : 3,
                        )
                      }
                    >
                      新增建议章节
                    </Button>
                  )}
                </div>
              ))}
            </div>
          )}

          {data.extra.length > 0 && (
            <div data-testid="scoring-extra-list" style={{ marginBottom: 8 }}>
              <Text strong style={{ fontSize: 12, color: "var(--bp-warning-text)" }}>
                多余章节（目录中无评分项支撑的叶子章节）
              </Text>
              {data.extra.map((n: ScoringExtraNode) => (
                <div key={n.section_id} style={{ padding: "4px 0" }}>
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0 }}
                    onClick={() => onLocate(n.section_id)}
                    data-testid={`scoring-locate-${n.section_id}`}
                  >
                    {n.path} {n.title}
                  </Button>
                  <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                    {n.reason}
                  </Text>
                </div>
              ))}
            </div>
          )}

          {data.unmatched.length > 0 && (
            <div data-testid="scoring-unmatched-list" style={{ marginBottom: 8 }}>
              <Text strong style={{ fontSize: 12, color: "var(--bp-warning-text)" }}>
                未解析来源（这些文档的评分项未纳入校验）
              </Text>
              {data.unmatched.map((s: FailedSource, idx: number) => (
                <div key={`${s.doc_id}-${idx}`} style={{ padding: "2px 0" }}>
                  <Text>{s.file_name || s.doc_id}</Text>
                  <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                    {s.reason}
                  </Text>
                </div>
              ))}
            </div>
          )}

          {data.overloaded_sections.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <Text strong style={{ fontSize: 12 }}>
                多评同章（同一章节承载多个评分项，建议拆分细化）
              </Text>
              {data.overloaded_sections.map((o) => (
                <div key={o.section_id} style={{ padding: "2px 0" }}>
                  <Button
                    type="link"
                    size="small"
                    style={{ padding: 0 }}
                    onClick={() => onLocate(o.section_id)}
                  >
                    {o.title}
                  </Button>
                  <Tag color="purple" style={{ marginLeft: 6 }}>
                    {o.matched_item_count} 个评分项
                  </Tag>
                </div>
              ))}
            </div>
          )}

          {summary.total_items > 0 && data.missing.length === 0 && data.extra.length === 0 && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="评分项与目录已完整对应"
              style={{ margin: "4px 0" }}
            />
          )}
        </>
      )}
    </Card>
  );
}
