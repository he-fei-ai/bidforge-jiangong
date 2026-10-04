import { useCallback, useEffect, useState } from "react";
import {
  Alert, App, Button, Card, Divider, Input, Space, Tag, Typography,
} from "antd";
import {
  CheckCircleOutlined, CloseCircleOutlined, DeleteOutlined,
  KeyOutlined, ReloadOutlined, SafetyCertificateOutlined,
} from "@ant-design/icons";

import { systemApi, clearAuthShortCircuit } from "../api";
import { useAntdMessageHub } from "../utils/activityCenter";
import { PageHero } from "../utils/ui";

const { Title, Text, Paragraph } = Typography;

/** 与 `src/api/index.ts` 的 getApiToken() 必须保持一致 */
const TOKEN_KEY = "api_token";

/**
 * 构建期注入的凭据（`VITE_API_TOKEN`）。
 *
 * ⚠️ 它在 getApiToken() 里**优先于** localStorage —— 一旦构建时写死，
 * 本页面的输入框就完全不起作用。必须显式告知用户，否则会出现
 * 「我明明填了 token 还是 401」这种极难排查的现象。
 */
const BUILD_TOKEN = (() => {
  try {
    return ((import.meta as any)?.env?.VITE_API_TOKEN as string) || "";
  } catch {
    return "";
  }
})();

function readStoredToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

/** 只露头尾，中间打码（避免截图/录屏时整串泄露） */
function maskToken(t: string): string {
  if (!t) return "";
  if (t.length <= 8) return "•".repeat(t.length);
  return `${t.slice(0, 4)}${"•".repeat(Math.min(t.length - 8, 20))}${t.slice(-4)}`;
}

export default function SecuritySettingsPage() {
  const { message: _antdMsg } = App.useApp();
  const msg = useAntdMessageHub(_antdMsg, "访问凭据");

  const [token, setToken] = useState<string>(() => readStoredToken());
  const [testing, setTesting] = useState(false);
  const [probe, setProbe] = useState<{ ok: boolean; text: string } | null>(null);

  // 后端若启用了鉴权，进入本页时自动探一次，直接告诉用户"现在到底通不通"
  const probeNow = useCallback(async (silent = false) => {
    setTesting(true);
    try {
      await systemApi.activity(1);
      setProbe({ ok: true, text: "后端已接受当前凭据，接口可正常访问。" });
    } catch (e: any) {
      const status = e?.response?.status;
      if (status === 401) {
        setProbe({
          ok: false,
          text: "后端拒绝访问（401）：未携带凭据或凭据不正确。",
        });
      } else {
        setProbe({
          ok: false,
          text: e?.message || "探测失败，请确认后端服务已启动。",
        });
      }
      if (!silent) msg.error(e?.message || "探测失败");
    } finally {
      setTesting(false);
    }
  }, [msg]);

  useEffect(() => {
    void probeNow(true);
    // 只在首次进入时自动探测
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = () => {
    const v = token.trim();
    try {
      if (v) localStorage.setItem(TOKEN_KEY, v);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {
      msg.error("无法写入 localStorage（浏览器可能禁用了本地存储）");
      return;
    }
    // ✅ R14 修复（2026-09-22）：凭据变更后清除全局 401 短路标志，
    //    让下一次请求真正发到后端，用户能立即看到新凭据是否生效。
    clearAuthShortCircuit();
    // 拦截器在**每次请求时**读取 localStorage，所以无需刷新即可生效
    msg.success(v ? "凭据已保存，后续请求即刻生效" : "凭据已清除");
    setProbe(null);
    void probeNow(true);
  };

  const clear = () => {
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* ignore */
    }
    // ✅ R14 修复：清除凭据后同样重置短路标志，让下一次 probe 能真正打后端。
    clearAuthShortCircuit();
    setToken("");
    setProbe(null);
    msg.success("已清除浏览器中保存的凭据");
  };

  const effectiveSource = BUILD_TOKEN
    ? "构建期注入（VITE_API_TOKEN）"
    : readStoredToken()
      ? "浏览器本地存储（localStorage.api_token）"
      : "未配置";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, height: "100%" }}>
      <PageHero
        title="访问凭据"
        subtitle="ACCESS CREDENTIALS"
        description="后端启用 API_AUTH_TOKEN 后，前端需携带同一凭据（X-API-Key）才能读写数据。"
        accent={<SafetyCertificateOutlined style={{ fontSize: 28, opacity: 0.6 }} />}
      />

      <div style={{ overflow: "auto", paddingRight: 4 }}>
        {BUILD_TOKEN && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 16 }}
            message="当前凭据来自构建期注入，本页面的输入不会生效"
            description={
              <span>
                构建时已写入 <Text code>VITE_API_TOKEN</Text>（{maskToken(BUILD_TOKEN)}），
                它在取值顺序上优先于浏览器本地存储。如需在本页修改，
                请去掉构建参数后重新构建前端。
              </span>
            }
          />
        )}

        <Card
          title={
            <Space>
              <KeyOutlined />
              <span>API 凭据</span>
            </Space>
          }
          extra={
            <Space size={8}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                当前生效来源：{effectiveSource}
              </Text>
              <Tag color={probe ? (probe.ok ? "green" : "red") : "default"}>
                {probe ? (probe.ok ? "连接正常" : "未通过") : "未探测"}
              </Tag>
            </Space>
          }
        >
          <Paragraph type="secondary" style={{ marginBottom: 16 }}>
            取值顺序：<Text code>VITE_API_TOKEN</Text>（构建期） → <Text code>
              localStorage.api_token</Text>（本页保存）。两端都为空时，
            后端若也未启用鉴权，则一切照常；后端启用了鉴权而这里为空，
            所有请求会返回 401。
          </Paragraph>

          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Input.Password
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="粘贴后端 backend/.env 中 API_AUTH_TOKEN 的值"
              prefix={<KeyOutlined />}
              autoComplete="off"
              onPressEnter={save}
              style={{ maxWidth: 520 }}
            />

            <Space wrap>
              <Button type="primary" icon={<CheckCircleOutlined />} onClick={save}>
                保存并生效
              </Button>
              <Button
                icon={<ReloadOutlined />}
                loading={testing}
                onClick={() => probeNow(false)}
              >
                测试连接
              </Button>
              <Button
                danger
                icon={<DeleteOutlined />}
                onClick={clear}
                disabled={!readStoredToken()}
              >
                清除本地凭据
              </Button>
            </Space>

            {probe && (
              <Alert
                type={probe.ok ? "success" : "error"}
                showIcon
                icon={probe.ok ? <CheckCircleOutlined /> : <CloseCircleOutlined />}
                message={probe.ok ? "凭据可用" : "凭据不可用"}
                description={probe.text}
                style={{ maxWidth: 720 }}
              />
            )}
          </Space>

          <Divider />

          <Title level={5}>如何启用后端鉴权</Title>
          <Paragraph type="secondary" style={{ marginBottom: 8 }}>
            在 <Text code>backend/.env</Text> 中设置一个足够长的随机串，然后重启后端：
          </Paragraph>
          <Paragraph>
            <pre
              style={{
                background: "var(--bp-surface-2, rgba(127,127,127,0.08))",
                border: "1px solid var(--bp-border, rgba(127,127,127,0.25))",
                borderRadius: 4,
                padding: "10px 12px",
                margin: 0,
                fontSize: 12,
                overflowX: "auto",
              }}
            >
{`# backend/.env
API_AUTH_TOKEN=请替换为随机长串

# 生成随机串（任选其一）
python -c "import secrets;print(secrets.token_urlsafe(32))"
openssl rand -base64 32`}
            </pre>
          </Paragraph>
          <Paragraph type="secondary" style={{ marginBottom: 0 }}>
            重启后端时日志会明确打印「API 鉴权已启用 / 未启用」；
            未启用时会附带风险提示。健康检查 <Text code>/api/v1/health</Text>、
            接口文档 <Text code>/docs</Text>、<Text code>/openapi.json</Text>
            始终无需凭据（便于探活与内网排障）。
          </Paragraph>
        </Card>

        <Card title="安全提示" style={{ marginTop: 16 }}>
          <ul style={{ margin: 0, paddingLeft: 20, lineHeight: 1.9 }}>
            <li>
              凭据保存在浏览器 <Text code>localStorage</Text> 中，
              <Text strong>同一台机器的其他使用者可以读取</Text>。
              仅在受控设备上使用；公用电脑用完请点「清除本地凭据」。
            </li>
            <li>
              当前是<Text strong>单凭据全局共享</Text>模型：没有用户维度、
              没有过期与轮换、没有请求级审计。若要多用户使用或对外暴露，
              需要另行接入会话 / OIDC 与按资源的授权。
            </li>
            <li>
              对外暴露时 <Text code>/docs</Text> 与 <Text code>/openapi.json</Text>
              会公开完整 API 结构（内网排障方便，公网建议一并纳入鉴权）。
            </li>
            <li>
              前端通过 <Text code>X-API-Key</Text> 头携带凭据（普通请求走 axios 拦截器，
              SSE 长连接走 <Text code>fetch</Text> 通道），两条链路都已覆盖。
            </li>
          </ul>
        </Card>
      </div>
    </div>
  );
}
