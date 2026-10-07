import { Suspense, lazy } from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { Spin } from "antd";
import { Layout } from "./components/Layout";

// ✅ 性能优化：路由级代码分割。
// 原实现静态 import 全部 6 个页面，导致 antd + 全部页面代码（含 SessionWorkbench
// 2452 行 / AIConfig 825 行）被打进同一个 index chunk（约 1.44 MB）。
// 改为 React.lazy 后，每个页面独立成 chunk，首屏只加载 Layout + 当前路由页面。
const ProjectListPage = lazy(() => import("./pages/ProjectListPage"));
const ProjectDetailPage = lazy(() => import("./pages/ProjectDetailPage"));
const SchemeWorkbenchPage = lazy(() => import("./pages/SchemeWorkbenchPage"));
const OutlineLibraryPage = lazy(() => import("./pages/OutlineLibraryPage"));
const AIConfigPage = lazy(() => import("./pages/AIConfigPage"));
const PromptEditorPage = lazy(() => import("./pages/PromptEditorPage"));
const PromptMetricsPage = lazy(() => import("./pages/PromptMetricsPage"));
const SecuritySettingsPage = lazy(() => import("./pages/SecuritySettingsPage"));

/** 路由切换时的占位骨架（antd 5 要求 Spin 有包裹子元素才能显示 tip） */
function RouteFallback() {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: 360,
      }}
    >
      <Spin size="large">
        <div style={{ width: 160, height: 80 }} />
      </Spin>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Suspense fallback={<RouteFallback />}>
          <Routes>
            <Route path="/" element={<ProjectListPage />} />
            <Route path="/project/:id" element={<ProjectDetailPage />} />
            <Route path="/scheme/:id" element={<SchemeWorkbenchPage />} />
            <Route path="/outline-library" element={<OutlineLibraryPage />} />
            <Route path="/settings/ai" element={<AIConfigPage />} />
            <Route path="/settings/prompts" element={<PromptEditorPage />} />
            <Route path="/settings/prompt-metrics" element={<PromptMetricsPage />} />
            <Route path="/settings/security" element={<SecuritySettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
      </Layout>
    </BrowserRouter>
  );
}
