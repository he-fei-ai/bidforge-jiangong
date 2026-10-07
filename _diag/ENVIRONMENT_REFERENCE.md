# 专项方案工具箱 - 运行环境 / 依赖 / 控件参考手册

> 生成日期：2026-10-07
> 基于：start_all.bat、backend/requirements.txt、frontend/package.json、backend/app/config.py

---

## 1. 系统要求

| 项目 | 要求 | 说明 |
|------|------|------|
| **操作系统** | Windows 10/11 (x64) | start_all.bat 仅支持 Windows |
| **控制台代码页** | 936 (GBK/CP936) | 代码页 65001 时中文乱码但不影响功能 |
| **控制台 Unicode** | 建议关闭「UTF-8 全球语言支持」 | 开启后代码页变 65001 |
| **端口** | 8000 (后端) / 5175 (前端) | 可配置但需同步修改 |

---

## 2. 后端依赖 (Python 3.10+)

### 2.1 核心依赖 (requirements.txt)

| 类别 | 包 | 版本 |
|------|------|------|
| **Web 框架** | fastapi | 0.139.0 |
| | uvicorn[standard] | 0.49.0 |
| | python-multipart | 0.0.32 |
| | starlette | 1.3.1 |
| | sse-starlette | 3.4.4 |
| **数据校验** | pydantic | 2.13.4 |
| | pydantic-settings | 2.14.1 |
| | python-dotenv | 1.2.2 |
| **数据库** | aiosqlite | 0.22.1 |
| **HTTP/SSE** | httpx | 0.28.1 |
| | httpx-sse | 0.4.3 |
| | aiohttp | 3.14.1 |
| **加密** | cryptography | 48.0.1 |
| | pyjwt | 2.13.0 |
| **文档解析** | python-docx | 1.2.0 |
| | docx2python | 3.6.2 |
| | pdfplumber | 0.11.9 |
| | pypdf | 6.14.2 |
| | pymupdf | 1.27.2.3 |
| | openpyxl | 3.1.5 |
| | xlrd | 2.0.1 |
| | python-dateutil | 2.9.0.post0 |
| **图表渲染** | pillow | 12.2.0 |
| | lxml | 6.1.1 |
| | orjson | 3.11.9 |
| **OCR (可选)** | pytesseract | 0.3.13 |
| **日志** | loguru | 0.7.3 |
| | rich | 15.0.0 |

### 2.2 OCR 可选依赖

```
pytesseract==0.3.13    # Tesseract OCR 接口 (需系统安装)
rapidocr              # RapidOCR (纯 pip 安装，推荐)
```

> **OCR 引擎优先级** (config.py `ocr_engine`):
> - `auto`: 本地引擎(Tesseract → RapidOCR) → 视觉大模型
> - `rapidocr`: 纯 pip 安装，Windows 零配置推荐
> - `vision`: 复用 AI 配置中的视觉模型
> - `off`: 关闭 OCR

### 2.3 数据库

| 项目 | 说明 |
|------|------|
| **类型** | SQLite (aiosqlite 异步驱动) |
| **路径** | `backend/data/scheme_assistant.db` |
| **WAL 模式** | 启用 |
| **性能优化** | `mmap_size=256MB` (默认关闭) |

---

## 3. 前端依赖 (Node.js 18+)

### 3.1 核心依赖

| 类别 | 包 | 版本 |
|------|------|------|
| **UI 框架** | antd | ^5.29.3 |
| | @ant-design/icons | ^5.6.0 |
| **路由** | react-router-dom | ^7.18.3 |
| **HTTP** | axios | ^1.12.0 |
| **Markdown** | marked | ^14.0.0 |
| | dompurify | ^3.4.15 |
| **公式** | katex | ^0.18.7 |
| **图表** | mermaid | ^11.0.0 |
| **React** | react | ^18.3.0 |
| | react-dom | ^18.3.0 |

### 3.2 开发依赖

| 包 | 版本 |
|------|------|
| vite | ^6.0.0 |
| typescript | ^5.9.0 |
| @vitejs/plugin-react | ^4.7.0 |
| vitest | ^3.2.4 |
| @testing-library/react | ^16.3.3 |
| jsdom | ^30.0.1 |
| eslint | ^9.39.4 |

---

## 4. 可选服务

### 4.1 MinerU 云端解析

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `mineru_provider` | `agent` 或 `accurate` | 空 (关闭) |
| `mineru_token` | API Token | 空 |
| `mineru_enabled` | 总开关 | True |
| `mineru_image_max_bytes` | 单图上限 | 100MB |

### 4.2 旧版 Word 解析 (.doc/.wps)

| 项目 | 说明 |
|------|------|
| **依赖** | 本地 Office 组件 (COM 接口) |
| **配置** | `legacy_office_enabled=True` |

### 4.3 Mermaid 图表渲染

| 项目 | 说明 |
|------|------|
| **渲染方式** | PIL v2 商业级渲染 (核心) |
| **mermaid-http-service** | 独立服务 (可选) |

---

## 5. 环境配置 (.env)

### 5.1 必填

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/scheme_assistant.db` |

### 5.2 主要可选配置

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `FERNET_KEY` | 加密密钥 | 空 |
| `OCR_ENABLED` | OCR 总开关 | True |
| `OCR_ENGINE` | `auto`/`tesseract`/`rapidocr`/`vision`/`off` | auto |
| `OCR_LANG` | Tesseract 语言包 | chi_sim+eng |
| `UPLOAD_MAX_BYTES` | 单文件上限 | 30MB |
| `PDF_TEXT_MAX_PAGES` | PDF 解析页数上限 | 500 |
| `AI_CONFIG_CACHE_TTL` | AI 配置缓存 TTL | 300 |
| `ACTIVE_ENV` | AI 配置生效环境 | 空 |

---

## 6. 启动脚本 (start_all.bat)

### 6.1 执行流程

```
[0/5] 环境检查 → 代码页自检 → Python 探测 → 依赖检测 → OCR 检测 → curl 检测
[1/5] 后端依赖 → pip install -r requirements.txt
[2/5] 数据库初始化 → SQLite + WAL
[3/5] 前端依赖 → npm install
[4/5] 启动后端 → uvicorn :8000
[5/5] 启动前端 → vite :5175
```

### 6.2 已知坑点

1. **编码坑**: 必须 GBK(CP936) + CRLF，不要 chcp 切代码页
2. **路径引号坑**: `for %%P in ("path")` 判断用 `%%P`，赋值用 `%%~P`
3. **timeout 坑**: 显式用 `%SystemRoot%\System32\timeout.exe`
4. **start 坑**: `start "" http://...` 必须带空标题

---

## 7. 端口与网络

| 服务 | 端口 | 说明 |
|------|------|------|
| 后端 API | 8000 | FastAPI + Uvicorn |
| 前端 | 5175 | Vite |
| API 文档 | 8000/docs | Swagger UI |
| 健康检查 | 8000/api/v1/health | 就绪探测 |
| 能力诊断 | 8000/api/v1/diagnostics/capabilities | OCR/MinerU 检测 |

---

## 8. 诊断工具 (_diag/)

| 脚本 | 说明 |
|------|------|
| `probe_env.ps1` | 环境探测：Node/Python/Git/PATH |
| `inspect_bytes.ps1` | 检查 start_all_bat.bytes 字节信息 |
| `dump_probe.ps1` | 导出分析到临时文件 |
| `rebuild_probe.ps1` | 重建探测：解码 + 运行 probe |
| `probe_env_rerun.ps1` | 重新探测：_diag 文件 + bat dump |
| `probe_env_rerun3.ps1` | 重新探测：Node/NVM + 环境检查 |

---

## 9. 快速启动检查清单

```
□ Windows 10/11 x64
□ Python 3.10+ (fastapi/uvicorn/aiosqlite/pydantic 已安装)
□ Node.js 18+ (npm 已安装)
□ 控制台代码页 936 (GBK)
□ 端口 8000 和 5175 未被占用
□ (可选) Tesseract-OCR 已安装
□ (可选) MinerU Token 已配置
□ (可选) Office 已安装
```

### 一键启动

```bat
cd /d "J:\编程\专项方案工具箱"
start_all.bat
```

### 手动启动

```bash
# 后端
cd backend && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# 前端
cd frontend && npm run dev
```

---

*文档结束*