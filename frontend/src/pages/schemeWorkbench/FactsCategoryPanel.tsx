import { applyFactsFilter, type FactsCategoryEntry } from "./factsShared.ts";
import { AppstoreOutlined } from "@ant-design/icons";
import { Collapse, Space, Tag } from "antd";
import { memo } from "react";
import { Typography } from "antd";
const { Title, Text } = Typography;

export function FactsCategoryPanel({
  entries,
  totalItems,
  selected,
  onSelect,
}: {
  entries: FactsCategoryEntry[];
  totalItems: number;
  selected: string | null;
  onSelect: (category: string) => void;
}) {
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
          <AppstoreOutlined style={{ color: "#1677ff" }} />
          <Text strong style={{ fontSize: 13 }}>
            事实分类
          </Text>
        </Space>
        {totalItems > 0 && (
          <Tag color="blue" style={{ margin: 0 }}>
            {totalItems} 项
          </Tag>
        )}
      </div>
      <div
        className="scroll-area"
        style={{ flex: 1, overflow: "auto", padding: "8px 8px", minHeight: 0 }}
      >
        {entries.length === 0 ? (
          <div style={{ textAlign: "center", padding: "24px 8px", color: "#999", fontSize: 13 }}>
            暂无提取结果
          </div>
        ) : (
          entries.map((e) => {
            const active = e.category === selected;
            return (
              <div
                key={e.category}
                onClick={() => onSelect(e.category)}
                style={{
                  padding: "8px 10px",
                  borderRadius: 6,
                  marginBottom: 4,
                  cursor: "pointer",
                  background: active ? "#e6f4ff" : "transparent",
                  border: active ? "1px solid #91caff" : "1px solid transparent",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span
                    style={{
                      flex: 1,
                      minWidth: 0,
                      fontWeight: active ? 600 : 400,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={e.title}
                  >
                    {e.title}
                  </span>
                  <Tag style={{ margin: 0 }}>{e.itemCount} 项</Tag>
                </div>
                {(e.simulated > 0 || e.conflicts > 0 || e.unresolved > 0) && (
                  <div style={{ display: "flex", gap: 4, marginTop: 4, flexWrap: "wrap" }}>
                    {e.simulated > 0 && (
                      <Tag color="orange" style={{ margin: 0 }}>
                        ⚠️{e.simulated}
                      </Tag>
                    )}
                    {e.conflicts > 0 && (
                      <Tag color="red" style={{ margin: 0 }}>
                        🔸{e.conflicts}
                      </Tag>
                    )}
                    {e.unresolved > 0 && (
                      <Tag color="blue" style={{ margin: 0 }}>
                        📝{e.unresolved}
                      </Tag>
                    )}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/** 事实分组列表：条目级 编辑/确认/矛盾裁决 + 分组级 编辑/删除，全部回调化 */
// ✅ 性能优化：memo 化。本组件是模块级函数组件（非页面内），旧实现无任何记忆化：
// 渲染频率 = 整页渲染频率（正文生成期间 ≤1 次/帧），而每次渲染都要跑
// applyFactsFilter + 每组 3 次 filter 全量扫描 + 为 antd Collapse 重建全部条目。
