import { ReactNode, useEffect, useState, useCallback, useRef } from "react";
import { Layout as AntLayout, Menu, theme, Spin, ConfigProvider, Tooltip, Button, Drawer } from "antd";
import {
  ProjectOutlined,
  MenuOutlined,
  SettingOutlined,
  BookOutlined,
  PlusOutlined,
  DownOutlined,
  RightOutlined,
  FileTextOutlined,
  ApiOutlined,
  CodeOutlined,
  SafetyCertificateOutlined,
  DashboardOutlined,
} from "@ant-design/icons";
import { useNavigate, useLocation } from "react-router-dom";
import { projectsApi, schemesApi } from "../api";
import TaskStatusBar from "./TaskStatusBar";
import ActivityHint from "./ActivityHint";
import { SIDEBAR_TOKENS } from "../utils/theme";
import { useBreakpoint, ThemeToggle } from "../utils/ui";

const { Header, Sider, Content } = AntLayout;

// ✅ 性能优化：侧栏深色主题对象提到模块级（编译期常量），避免每次渲染新建
//    { algorithm, token } 触发 antd 重复派生主题 token。
const SIDEBAR_THEME = { algorithm: theme.darkAlgorithm, token: SIDEBAR_TOKENS };

type ProjectItem = { id: string; name: string; scheme_count?: number };
type SchemeItem = { id: string; name: string; status?: string };

/** 侧边栏：项目树 + 系统菜单 */
function ProjectTreeMenu({
  onNavigate,
  selectedKey,
}: {
  onNavigate: (key: string) => void;
  selectedKey: string;
}) {
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [schemesMap, setSchemesMap] = useState<Record<string, SchemeItem[]>>({});
  const [loading, setLoading] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<string[]>([]);

  // 懒加载某个项目的方案
  // ✅ 修复（2026-09-23）：旧实现用 useState 存 loadedProjectIds，但 setLoadedProjectIds
  //    的回调只更新 state，函数体继续执行 API 请求 —— 导致 useEffect 依赖 schemesMap
  //    变化时反复触发 loadSchemes，每 20ms 打一次 DB（实测 50 req/s），引发 SQLite
  //    database is locked，目录生成等写操作直接失败。现改用 useRef 做即时去重，
  //    已加载的项目不再重复请求。
  const loadedProjectIdsRef = useRef<Set<string>>(new Set());

  const loadSchemes = useCallback(async (projectId: string) => {
    // 即时去重：已加载或正在加载的项目直接跳过，不再发 API 请求
    if (loadedProjectIdsRef.current.has(projectId)) return;
    loadedProjectIdsRef.current.add(projectId);
    try {
      const { data } = await schemesApi.list(projectId);
      setSchemesMap((prev) => ({ ...prev, [projectId]: data.items || [] }));
    } catch {
      // 失败则从已加载集合中移除，允许下次重试
      loadedProjectIdsRef.current.delete(projectId);
    }
  }, []);

  // 加载所有项目
  const loadProjects = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await projectsApi.list();
      const list = data.items || [];
      setProjects(list);
      // 默认展开所有有方案的项目，并加载其方案列表
      const withSchemes = list
        .filter((p: ProjectItem) => (p.scheme_count ?? 0) > 0)
        .map((p: ProjectItem) => `/project/${p.id}`);
      setExpandedKeys(withSchemes);
      // 主动加载这些项目的方案
      for (const p of list) {
        if ((p.scheme_count ?? 0) > 0) {
          loadSchemes(p.id);
        }
      }
    } catch {
      // ignore
    }
    setLoading(false);
  }, [loadSchemes]);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  // ✅ 修复（2026-09-23）：useEffect 依赖 schemesMap 导致无限循环 ——
  //    loadSchemes 更新 schemesMap → effect 重跑 → 再次调用 loadSchemes。
  //    现改用 schemesMapRef 读取最新值，effect 只依赖 selectedKey 和 projects。
  const schemesMapRef = useRef<Record<string, SchemeItem[]>>({});
  useEffect(() => { schemesMapRef.current = schemesMap; }, [schemesMap]);

  // 当用户进入某个页面时，自动展开对应项目并加载方案
  useEffect(() => {
    let projectId: string | null = null;
    const m1 = selectedKey.match(/^\/project\/([^/]+)/);
    if (m1) projectId = m1[1];
    const m2 = selectedKey.match(/^\/scheme\/([^/]+)/);
    if (m2) {
      // 在方案页，需要找它属于哪个项目（从 ref 读取最新 schemesMap）
      const sid = m2[1];
      for (const [pid, ss] of Object.entries(schemesMapRef.current)) {
        if (ss.some((s) => s.id === sid)) {
          projectId = pid;
          break;
        }
      }
    }
    if (projectId) {
      const key = `/project/${projectId}`;
      setExpandedKeys((prev) =>
        prev.includes(key) ? prev : [...prev, key]
      );
      loadSchemes(projectId);
    }
  }, [selectedKey, projects, loadSchemes]);

  // 深链直达方案页、且 schemesMap 尚未填充（刷新/分享链接）时，
  // 逐个加载项目方案以定位所属项目；定位后由上方 effect 自动展开并加载。
  // ✅ 修复（2026-09-23）：同样移除 schemesMap 依赖，改用 ref 读取。
  useEffect(() => {
    const m = selectedKey.match(/^\/scheme\/([^/]+)/);
    if (!m) return;
    const sid = m[1];
    for (const ss of Object.values(schemesMapRef.current)) {
      if (ss.some((s) => s.id === sid)) return; // 已定位，交给上方 effect
    }
    let cancelled = false;
    (async () => {
      // ✅ 性能修复：原实现逐个 `await loadSchemes` —— N 个项目 = N 个**串行** HTTP
      //    瀑布请求（30 项目 × ~80ms ≈ 2.4s 首屏白屏），刷新/分享链接直达方案页时
      //    用户只能看到 Spin。改为并发发出（与 loadProjects 中 :79-83 的既有口径
      //    统一）；请求总数不变（loadSchemes 内部仍按 projectId 去重），仅缩短墙钟时间。
      const pending = projects.filter(
        (p) => !schemesMapRef.current[p.id] || schemesMapRef.current[p.id].length === 0,
      );
      await Promise.all(pending.map((p) => loadSchemes(p.id)));
      void cancelled;
    })();
    return () => { cancelled = true; };
  }, [selectedKey, projects, loadSchemes]);

  const onOpenChange = (keys: string[]) => {
    const prevSet = new Set(expandedKeys);
    const nextSet = new Set(keys);
    for (const k of keys) {
      if (!prevSet.has(k)) {
        const m = k.match(/^\/project\/([^/]+)/);
        if (m) loadSchemes(m[1]);
      }
    }
    // 收起项目也不清理缓存
    void nextSet;
    setExpandedKeys(keys);
  };

  // 构建 antd Menu items
  const projectSubItems = projects.map((p) => {
    const projectKey = `/project/${p.id}`;
    const schemes = schemesMap[p.id] || [];
    const loaded = loadedProjectIdsRef.current.has(p.id);

    return {
      key: projectKey,
      icon: <ProjectOutlined />,
      label: (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <span>{p.name}</span>
          {(p.scheme_count ?? 0) > 0 && (
            <span
              style={{
                fontSize: 11,
                color: "#999",
                marginLeft: 2,
              }}
            >
              ({p.scheme_count})
            </span>
          )}
        </span>
      ),
      children: (p.scheme_count ?? 0) === 0
        ? [
            {
              key: `${projectKey}/create-scheme`,
              icon: <PlusOutlined />,
              label: (
                <span style={{ opacity: 0.75 }}>
                  进入项目新建方案
                </span>
              ),
              disabled: true,
            },
          ]
        : !loaded
        ? [{ key: `${projectKey}/loading`, label: "加载中...", disabled: true }]
        : schemes.length === 0
        ? [
            {
              key: `${projectKey}/empty`,
              icon: <PlusOutlined />,
              label: (
                <span style={{ opacity: 0.75 }}>
                  暂无方案
                </span>
              ),
              disabled: true,
            },
          ]
        : schemes.map((s) => ({
            key: `/scheme/${s.id}`,
            icon: <FileTextOutlined />,
            label: (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span>{s.name}</span>
                {s.status && s.status !== "草稿" && (
                  <span
                    style={{
                      fontSize: 10,
                      padding: "0 5px",
                      background: "#e6f4ff",
                      color: "#1677ff",
                      borderRadius: 4,
                      lineHeight: "16px",
                    }}
                  >
                    {s.status}
                  </span>
                )}
              </span>
            ),
          })),
    };
  });

  const items: any[] = [
    {
      type: "group",
      label: (
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            paddingRight: 8,
          }}
        >
          <span>项目列表</span>
          <a
            onClick={(e) => {
              e.stopPropagation();
              onNavigate("/");
            }}
            style={{ fontSize: 12 }}
          >
            管理
          </a>
        </div>
      ),
    },
    ...projectSubItems,
    { type: "divider", style: { margin: "4px 0" } },
    { key: "/outline-library", icon: <BookOutlined />, label: "目录库" },
    { key: "/settings/ai", icon: <SettingOutlined />, label: "文本模型配置" },
    { key: "/settings/prompts", icon: <CodeOutlined />, label: "提示词管理" },
    { key: "/settings/prompt-metrics", icon: <DashboardOutlined />, label: "提示词指标" },
    { key: "/settings/security", icon: <SafetyCertificateOutlined />, label: "访问凭据" },
  ];

  return (
    <Spin spinning={loading && projects.length === 0}>
      <Menu
        mode="inline"
        selectedKeys={[selectedKey]}
        openKeys={expandedKeys}
        onOpenChange={onOpenChange}
        items={items}
        onClick={({ key }) => {
          if (typeof key === "string" && !key.endsWith("/loading") && !key.endsWith("/empty") && !key.endsWith("/create-scheme")) {
            onNavigate(key);
          }
        }}
        style={{ borderRight: 0 }}
        expandIcon={({ isOpen }) =>
          isOpen ? <DownOutlined /> : <RightOutlined />
        }
      />
    </Spin>
  );
}

/** 左侧深色侧边栏 */
function Sidebar({
  collapsed,
  setCollapsed,
  selectedKey,
  navigate,
  style,
}: {
  collapsed: boolean;
  setCollapsed: (v: boolean) => void;
  selectedKey: string;
  navigate: (k: string) => void;
  style?: React.CSSProperties;
}) {
  // ---------- 后端健康状态 ----------
  const [backendOnline, setBackendOnline] = useState<boolean | null>(null);
  const [backendLatency, setBackendLatency] = useState<number>(0);
  const backendPollRef = useRef<number | null>(null);
  // ✅ BUG 修复：把定时器内的 check() 暴露出来，供「点击状态条手动刷新」复用同一入口。
  // 旧实现 onClick 里另写了一份裸 fetch：既绕过 check() 的 inFlight 单飞保护
  // （与 10s 定时器并发，慢网络下双倍请求），也不受 stopping 守卫约束
  // （组件卸载后仍 setBackendOnline）。
  const backendCheckRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    // 关键设计：**卸载时不主动 abort**。
    // fetch 被 abort 时，浏览器 Network/Console 会打 net::ERR_ABORTED，
    // catch 里的 AbortError 只吞掉 JS 层错误，消不掉浏览器那条红字。
    // 真正消除红字的做法：让在飞的请求自然完成或超时，
    // 同时用 stopping 标志禁止 setBackendOnline 之类的副作用更新。
    let stopping = false;
    let inFlight = false; // 单飞：上一次没回来不发下一次，避免累积

    const check = async () => {
      if (stopping || inFlight) return;
      inFlight = true;
      const t0 = Date.now();
      try {
        const resp = await fetch("/api/v1/health", {
          method: "GET",
          cache: "no-store",
        });
        if (!stopping) {
          setBackendOnline(resp.ok);
          setBackendLatency(Date.now() - t0);
        }
      } catch (_err) {
        // 网络失败/代理失败等真实故障 → 标记离线；stopping 后不再改状态
        if (!stopping) setBackendOnline(false);
      } finally {
        inFlight = false;
      }
    };

    check();
    backendCheckRef.current = () => { void check(); };
    backendPollRef.current = window.setInterval(check, 10000);
    return () => {
      stopping = true;
      backendCheckRef.current = null;
      if (backendPollRef.current) window.clearInterval(backendPollRef.current);
    };
  }, []);

  const statusClass =
    backendOnline === null ? "checking"
    : backendOnline ? "online"
    : "offline";

  const statusLabel =
    backendOnline === null ? "检查中..."
    : backendOnline ? "后端在线"
    : "后端离线";
  const tooltipText =
    backendOnline === null ? "正在连接后端服务..."
    : backendOnline
      ? `服务正常 · 延迟 ${backendLatency}ms · 每 10s 自动检查`
      : "无法连接后端服务，请确认后端进程是否启动";

  return (
    <Sider
      collapsible
      collapsed={collapsed}
      onCollapse={setCollapsed}
      width={260}
      className="bp-sidebar"
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        overflow: "hidden",
        ...style,
      }}
    >
      {collapsed ? (
        <div className="bp-logo-collapsed">
          {/* 折叠态：只显示 SVG 标记 */}
          <svg viewBox="0 0 32 32" width="24" height="24" fill="none">
            <rect x="4" y="4" width="24" height="24" rx="4" stroke="#00D4FF" strokeWidth="1.5" />
            <path d="M8 16h16M16 8v16" stroke="#00D4FF" strokeWidth="1" opacity=".5" />
            <rect x="11" y="11" width="10" height="10" stroke="#00D4FF" strokeWidth="1.5" />
            <path d="M13.5 15h5M13.5 18h5" stroke="#00D4FF" strokeWidth="1" />
          </svg>
        </div>
      ) : (
        <div className="bp-logo">
          <div className="bp-logo-mark">
            <svg viewBox="0 0 32 32" fill="none">
              <rect x="4" y="4" width="24" height="24" rx="4" stroke="#00D4FF" strokeWidth="1.5" />
              <path d="M8 16h16M16 8v16" stroke="#00D4FF" strokeWidth="1" opacity=".45" />
              <rect x="11" y="11" width="10" height="10" stroke="#00D4FF" strokeWidth="1.5" />
              <path d="M13.5 15h5M13.5 18h5" stroke="#00D4FF" strokeWidth="1" />
            </svg>
          </div>
          <div className="bp-logo-text">
            <span className="bp-logo-title">专项方案编制平台</span>
            <span className="bp-logo-sub">Blueprint · v5.3</span>
          </div>
        </div>
      )}
      <div
        style={{
          flex: 1,
          overflow: "auto",
          minHeight: 0,
        }}
      >
        {!collapsed ? (
          <ProjectTreeMenu onNavigate={navigate} selectedKey={selectedKey} />
        ) : (
          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            items={[
              { key: "/", icon: <ProjectOutlined /> },
              { key: "/outline-library", icon: <BookOutlined /> },
              { key: "/settings/ai", icon: <SettingOutlined /> },
              { key: "/settings/prompts", icon: <CodeOutlined /> },
              { key: "/settings/prompt-metrics", icon: <DashboardOutlined /> },
              { key: "/settings/security", icon: <SafetyCertificateOutlined /> },
            ]}
            onClick={({ key }) => navigate(key)}
            style={{ borderRight: 0 }}
          />
        )}
      </div>

      {/* 底部：后台任务运行状态栏（实时任务 / AI 调用 / 生成进度）+ 后端服务状态 */}
      <TaskStatusBar collapsed={collapsed} />
      <Tooltip title={tooltipText} placement="right">
        <div
          style={{
            height: 40,
            flexShrink: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: collapsed ? "center" : "flex-start",
            gap: 8,
            padding: "0 12px",
            borderTop: "1px solid rgba(0, 212, 255, 0.15)",
            background: "rgba(15, 43, 79, 0.6)",
            fontSize: 12,
            color: "rgba(255, 255, 255, 0.75)",
            cursor: "default",
          }}
          onClick={() => {
            // ✅ 复用 check()：与 10s 定时器共享 inFlight 单飞 + stopping 守卫
            setBackendOnline(null);
            backendCheckRef.current?.();
          }}
        >
          <span className={`bp-status-dot ${statusClass}`} />
          {!collapsed && (
            <span style={{ whiteSpace: "nowrap" }}>
              <ApiOutlined style={{ marginRight: 4, color: "#00D4FF" }} />
              {statusLabel}
              {backendOnline && backendLatency > 0 && (
                <span style={{ color: "rgba(255, 255, 255, 0.45)", marginLeft: 4, fontFamily: "'JetBrains Mono', monospace", fontSize: 10 }}>
                  · {backendLatency}ms
                </span>
              )}
            </span>
          )}
        </div>
      </Tooltip>
    </Sider>
  );
}

/** 右侧主区域 + 面包屑 Header + 暗色模式切换 */
function MainArea({
  children,
  pathname,
  navigate,
  collapsed,
  onMenuClick,
}: {
  children: ReactNode;
  pathname: string;
  navigate: (k: string) => void;
  collapsed: boolean;
  /** 极窄屏下显示汉堡菜单按钮，点击打开抽屉侧边栏 */
  onMenuClick?: () => void;
}) {
  // ---------- 根据路由生成面包屑 ----------
  const crumbs: { label: string; to?: string }[] = [{ label: "首页", to: "/" }];

  const mScheme = pathname.match(/^\/scheme\/([^/]+)/);
  const mProject = pathname.match(/^\/project\/([^/]+)/);

  if (mScheme) {
    crumbs.push({ label: "方案工作台" });
  } else if (mProject) {
    crumbs.push({ label: "项目详情" });
  } else if (pathname.startsWith("/outline-library")) {
    crumbs.push({ label: "目录库" });
  } else if (pathname.startsWith("/settings/ai")) {
    crumbs.push({ label: "文本模型配置" });
  } else if (pathname.startsWith("/settings/prompts")) {
    crumbs.push({ label: "提示词管理" });
  } else if (pathname.startsWith("/settings/prompt-metrics")) {
    crumbs.push({ label: "提示词指标" });
  } else if (pathname.startsWith("/settings/security")) {
    crumbs.push({ label: "访问凭据" });
  } else if (pathname === "/") {
    crumbs.length = 1; // 首页只显示一个
  }

  return (
    <AntLayout style={{ height: "100%" }}>
      <Header className="bp-header">
        <div className="bp-header-crumbs" style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0, flex: "1 1 auto" }}>
          {onMenuClick && (
            <Button
              type="text"
              size="small"
              icon={<MenuOutlined />}
              onClick={onMenuClick}
              title="打开导航"
              aria-label="打开导航菜单"
              style={{ flexShrink: 0, color: "inherit" }}
            />
          )}
          {crumbs.length === 1 ? (
            <span className="bp-crumb-current">项目列表</span>
          ) : (
            crumbs.map((c, i) => (
              <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                {c.to ? (
                  <span className="bp-crumb-link" onClick={() => navigate(c.to!)}>{c.label}</span>
                ) : (
                  <span className="bp-crumb-current">{c.label}</span>
                )}
                {i < crumbs.length - 1 && (
                  <span className="bp-crumb-sep">/</span>
                )}
              </span>
            ))
          )}
        </div>
        <div className="bp-header-right">
          {/* 全局消息中心：所有功能的提示消息/后台消息统一在此展示（不再弹 toast） */}
          <ActivityHint />
          <ThemeToggle collapsed={collapsed} />
          <span>BLUEPRINT</span>
          <span style={{ color: "#00D4FF" }}>·</span>
          <span>v5.3.0</span>
        </div>
      </Header>
      <Content className="bp-content">
        {children}
      </Content>
    </AntLayout>
  );
}

export function Layout({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  // 手机/小屏时侧边栏改为抽屉（隐藏固定 Sider，用汉堡菜单打开）
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { isCompact, isSmall, width } = useBreakpoint();
  // 手机竖屏及以下启用抽屉模式；平板保持折叠侧边栏
  const drawerMode = isSmall;

  // ---------- 响应式自动折叠 ----------
  // 小于 1200px 自动折叠；用户手动展开后若宽度进一步收窄，也要重新折叠
  useEffect(() => {
    if (isCompact && !collapsed) {
      setCollapsed(true);
    }
    // 宽度足够时不强制展开（让用户保留手动选择）
  }, [isCompact, collapsed]);

  // 抽屉模式下，路由变化自动收起抽屉
  useEffect(() => {
    if (drawerMode) setMobileDrawerOpen(false);
  }, [location.pathname, drawerMode]);

  // 从非抽屉切换到抽屉模式时重置抽屉状态
  useEffect(() => {
    if (drawerMode) setCollapsed(true);
  }, [drawerMode]);

  const selectedKey = (() => {
    if (/^\/scheme\/[^/]+/.test(location.pathname)) return location.pathname;
    if (/^\/project\/[^/]+/.test(location.pathname)) return location.pathname;
    if (location.pathname.startsWith("/outline-library")) return "/outline-library";
    if (location.pathname.startsWith("/settings/ai")) return "/settings/ai";
    if (location.pathname.startsWith("/settings/prompts")) return "/settings/prompts";
    if (location.pathname.startsWith("/settings/prompt-metrics")) return "/settings/prompt-metrics";
    if (location.pathname.startsWith("/settings/security")) return "/settings/security";
    return "/";
  })();

  const navigateAndClose = (k: string) => {
    navigate(k);
    if (drawerMode) setMobileDrawerOpen(false);
  };

  return (
    <AntLayout style={{ height: "100vh", overflow: "hidden" }}>
      {/* 左侧：深色主题（蓝图感）；抽屉模式下不挂载固定侧栏，改由下方 Drawer 内的
          侧栏承载 —— 避免窄屏同时挂载两份 Sidebar（各含一条 SSE 活动流 + 健康轮询 +
          看门狗），造成双倍常驻连接与后端压力。 */}
      {!drawerMode && (
        <ConfigProvider theme={SIDEBAR_THEME}>
          <Sidebar
            collapsed={collapsed}
            setCollapsed={setCollapsed}
            selectedKey={selectedKey}
            navigate={navigate}
          />
        </ConfigProvider>
      )}
      {/* 右侧：主区域（全局 Blueprint 主题已由 main.tsx 注入） */}
      <MainArea
        pathname={location.pathname}
        navigate={navigate}
        collapsed={collapsed}
        onMenuClick={drawerMode ? () => setMobileDrawerOpen(true) : undefined}
      >
        {children}
      </MainArea>
      {drawerMode && (
        <ConfigProvider theme={SIDEBAR_THEME}>
          <Drawer
            placement="left"
            open={mobileDrawerOpen}
            onClose={() => setMobileDrawerOpen(false)}
            width={Math.min(280, width - 20)}
            styles={{ body: { padding: 0, background: "transparent" } }}
            closable={false}
            maskClosable
            destroyOnClose={false}
          >
            <Sidebar
              collapsed={false}
              setCollapsed={() => {}}
              selectedKey={selectedKey}
              navigate={navigateAndClose}
            />
          </Drawer>
        </ConfigProvider>
      )}
    </AntLayout>
  );
}
