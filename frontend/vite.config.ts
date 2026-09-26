import { defineConfig, type Connect } from "vite";
import react from "@vitejs/plugin-react";
import http from "node:http";

/**
 * 自定义中间件：把 /api 路径的 OPTIONS CORS 预检转发到后端
 */
function corsPreflightProxy(): Connect.NextHandleFunction {
  const BACKEND = "http://localhost:8000";
  return (req, res, next) => {
    if (req.method === "OPTIONS" && req.url?.startsWith("/api")) {
      const url = new URL(req.url, BACKEND);
      const proxyReq = http.request(
        {
          hostname: url.hostname,
          port: url.port,
          path: url.pathname + url.search,
          method: "OPTIONS",
          headers: req.headers,
        },
        (proxyRes) => {
          for (const [k, v] of Object.entries(proxyRes.headers)) {
            if (v !== undefined && v !== null) {
              res.setHeader(k, v as string);
            }
          }
          res.statusCode = proxyRes.statusCode || 200;
          proxyRes.pipe(res);
        }
      );
      proxyReq.on("error", () => {
        res.setHeader("Access-Control-Allow-Origin", (req.headers.origin as string) || "*");
        res.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS");
        res.setHeader("Access-Control-Allow-Headers", "Content-Type,Authorization");
        res.statusCode = 204;
        res.end();
      });
      proxyReq.end();
    } else {
      next();
    }
  };
}

export default defineConfig({
  plugins: [
    react(),
    {
      name: "cors-preflight-patch",
      configureServer(server) {
        // Connect 中间件栈在 .stack 属性里，手动插到第一个位置
        const middleware = corsPreflightProxy();
        server.middlewares.stack.unshift({
          route: "",
          handle: middleware,
        } as any);
      },
    },
  ],
  server: {
    port: 5175,
    host: true,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        // SSE 长连接配置：10分钟超时 + ws支持 + 禁止代理缓冲
        timeout: 600_000,
        ws: true,
        configure: (proxy) => {
          // ✅ 判断 SSE 端点的统一函数：
          //   - /api/v1/sse/*        （主 SSE 命名空间，所有长任务）
          //   - /api/v1/*/start-sse  （业务 SSE 命名，如 bid-analysis）
          //   - /api/v1/system/activity/stream （系统状态栏实时推送）
          //   核心判据：路径名含 "sse" 关键字 或 以 "/stream" 结尾的长连接
          const isSSE = (url?: string) => {
            if (!url) return false;
            const lower = url.toLowerCase();
            return (
              lower.includes("sse") ||
              lower.endsWith("/stream") ||
              lower.includes("/stream?")
            );
          };

          // 后端不可达（未启动 / 崩溃 / 端口被占）时，http-proxy 默认直接断开与浏览器的
          // 连接，浏览器只报 net::ERR_ABORTED，看不出真实原因。这里改写成一个明确的
          // 502 响应，前端能正常解析 body，排查时一眼看到"后端不可达"。
          proxy.on("error", (err, _req, res) => {
            // WebSocket 升级失败时 res 是裸 Socket，没有 writeHead，跳过
            if (!res || typeof (res as { writeHead?: unknown }).writeHead !== "function") {
              return;
            }
            const response = res as import("node:http").ServerResponse;
            // 响应头已发出（如 SSE 已开始推流）时无法再改状态码，直接收尾
            if (response.headersSent || response.writableEnded) {
              response.end();
              return;
            }
            response.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
            // ECONNREFUSED 这类 AggregateError 的 message 为空，错误码在 code 上，两个都带上
            const e = err as NodeJS.ErrnoException;
            response.end(
              JSON.stringify({
                detail: "后端服务不可达，请确认 8000 端口已启动",
                target: "http://localhost:8000",
                reason: [e?.code, e?.message].filter(Boolean).join(" ") || String(err),
              })
            );
          });
          proxy.on("proxyReq", (proxyReq, req) => {
            if (isSSE(req.url)) {
              proxyReq.setHeader("Cache-Control", "no-cache");
              proxyReq.setHeader("x-accel-buffering", "no");
            }
          });
          proxy.on("proxyRes", (proxyRes, req) => {
            if (isSSE(req.url)) {
              proxyRes.headers["cache-control"] = "no-cache";
              proxyRes.headers["x-accel-buffering"] = "no";
              proxyRes.headers["content-encoding"] = "none";
              proxyRes.headers["connection"] = "keep-alive";
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // 拆包策略：把稳定不变的第三方依赖与频繁变动的业务代码分离，
        // 升级业务代码时用户浏览器无需重新下载 vendor chunk。
        // 注意：mermaid 故意不列在此处 —— 它内部大量使用动态 import 自行分包
        //      （flowDiagram / ganttDiagram / sequenceDiagram / katex ...），
        //      手动收编反而会把它们合并成一个巨型 chunk，劣化首屏。
        manualChunks: {
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          "vendor-antd": ["antd", "@ant-design/icons"],
          "vendor-markdown": ["marked", "dompurify"],
        },
      },
    },
  },
});
