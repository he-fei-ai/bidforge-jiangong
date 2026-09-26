/**
 * 「解析提取」(import) 顶层 Tab 的内嵌子 Tab 标签栏（受控组件）。
 *
 * ✅ 2026-09-24 T2：从 SchemeWorkbenchPage 内联的 antd <Tabs> 抽出，使
 *    「文档解析 docs ↔ 项目提取 extract」切换与徽标渲染具备组件级测试。
 *    仅渲染标签栏（swb-sub-tabs-bar-only 隐藏空 pane），内容区由页面按
 *    activeKey 自行渲染——保持 2026-09-23 版式重构后的两行结构不变。
 */
import React from "react";
import { Space, Tabs, Tag } from "antd";
import type { BaSummary } from "../utils/bidAnalysis";

export type ImportSubTabKey = "docs" | "extract";

export type ImportSubTabBarProps = {
  activeKey: ImportSubTabKey;
  onChange: (key: ImportSubTabKey) => void;
  /** 18 项提取汇总：存在时在「项目提取」标签上显示 success/total 徽标 */
  summary?: BaSummary | null;
};

export default function ImportSubTabBar({
  activeKey,
  onChange,
  summary,
}: ImportSubTabBarProps) {
  return (
    <Tabs
      activeKey={activeKey}
      onChange={(k) => onChange(k as ImportSubTabKey)}
      size="small"
      renderTabBar={(tabBarProps, DefaultTabBar) => (
        <DefaultTabBar {...tabBarProps} />
      )}
      items={[
        {
          key: "docs",
          label: <span>文档解析</span>,
          children: null,
        },
        {
          key: "extract",
          label: summary ? (
            <Space size={2}>
              <span>项目提取</span>
              <Tag
                color={summary.all_required_done ? "green" : "blue"}
                style={{ margin: 0, fontSize: 10, lineHeight: "14px", padding: "0 3px" }}
              >
                {summary.success}/{summary.total}
              </Tag>
            </Space>
          ) : (
            <span>项目提取</span>
          ),
          children: null,
        },
      ]}
      // 只渲染 tab bar 标签，隐藏空 tab pane（勿改回 swb-tabs-flex，会吞掉第二行高度）
      tabPosition="top"
      className="swb-sub-tabs-bar-only"
      tabBarStyle={{ margin: 0 }}
      style={{ flexShrink: 0, borderBottom: "1px solid #f0f0f0" }}
    />
  );
}
