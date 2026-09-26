@echo off
title 工程项目专项方案智能编制平台
setlocal EnableExtensions

REM ============================================================================
REM  Encoding: GBK / CP936 + CRLF.   *** DO NOT re-save this file as UTF-8 ***
REM ============================================================================
REM  本文件必须保持 GBK(CP936) 编码 + CRLF 换行，且不要在脚本内执行 chcp 切换代码页。
REM  原因：cmd.exe 是逐段读取 bat 文件并记住「下一个字节偏移」的。
REM  一旦脚本中途用 chcp 切换了代码页，同一段文本按两种代码页算出的「字符数/字节数」
REM  不一致，cmd 的偏移记算就会错位，于是从某一行中间开始把半截中文当命令执行，
REM  表现为一堆「xxx 不是内部或外部命令」，最后脚本直接退出（也就是「闪退」）。
REM  实测：旧脚本（UTF-8 + LF 换行 + chcp 65001）退出码 255，完全跑不起来。
REM  解决办法：不在脚本内切代码页，让文件编码与控制台默认代码页一致（简体中文
REM  系统即 GBK）。若确实需要 UTF-8 版本，请把所有中文提示改成纯 ASCII。
REM
REM  第二个坑：路径带空格的可执行文件必须保留引号。
REM    实测失败：for %%P in ("C:\Program Files\X\x.exe") do %%~P ...
REM               → cmd 按 C:\Program 执行，报「C:\Program 不是内部或外部命令」，
REM                 连 else 分支都不会执行。
REM    正确写法：判断与执行用 %%P（保留原始引号），只在 set 赋值时用 %%~P。
REM ============================================================================

echo ============================================
echo   工程项目专项方案智能编制平台 v5.3
echo   一键启动脚本 (Windows)
echo ============================================
echo.

REM ---- 0.0 控制台代码页自检 ----
REM 本文件是 GBK 编码。若控制台 OEM 代码页不是 936（Windows 11 或开启「使用
REM Unicode UTF-8 提供全球语言支持」后常见的 65001），下面的中文提示会变成乱码，
REM 功能不受影响。这里给一条 ASCII 提示，乱码时也能认出来。
set "CP_OK="
for /f "tokens=*" %%c in ('chcp 2^>nul ^| findstr "936"') do set "CP_OK=1"
if not defined CP_OK (
    echo [!] console code page is not 936/GBK - Chinese text below may look garbled.
    echo     Functions are NOT affected. To restore Chinese: Region - Administration -
    echo     uncheck "Beta: Use Unicode UTF-8 for worldwide language support".
)

REM ========== 0. 环境检查 ==========
echo [0/5] 检查运行环境...

REM 可靠的延时命令：PATH 里可能存在 Git 自带的 GNU timeout.exe，
REM 它会把 "timeout /t 1" 当成非法参数而直接报错（退出码 125），
REM 所以这里显式使用 Windows 自带的 timeout.exe。
set "SLEEP=%SystemRoot%\System32\timeout.exe"
if not exist "%SLEEP%" set "SLEEP=timeout"

REM 就绪探测工具：优先系统自带 curl.exe（更快），缺失时回退到 Python 标准库。
REM 旧脚本裸用 curl：精简系统 / 旧版 Windows 上没有 curl.exe 时，健康检查会
REM 永远失败并误报「后端启动超时」，即使后端其实运行得很好。
set "CURL="
if exist "%SystemRoot%\System32\curl.exe" set "CURL=%SystemRoot%\System32\curl.exe"
if not defined CURL (
    where curl >nul 2>&1 && set "CURL=curl"
)

REM ---- 0.1 选择「装有项目依赖」的 Python 解释器 ----
REM PATH 上的第一个 python 未必是本项目的解释器（可能缺少 fastapi/uvicorn）。
REM 依次探测：PYTHON_EXE 环境变量 > PATH 中的 python > where python 全部命中
REM > 常见安装路径，取第一个能 import 核心依赖的。
set "PY="
if defined PYTHON_EXE (
    if exist "%PYTHON_EXE%" (
        "%PYTHON_EXE%" -c "import fastapi,uvicorn,aiosqlite,pydantic" >nul 2>&1
        if not errorlevel 1 set "PY=%PYTHON_EXE%"
    )
)
if not defined PY (
    python -c "import fastapi,uvicorn,aiosqlite,pydantic" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PY (
            "%%P" -c "import fastapi,uvicorn,aiosqlite,pydantic" >nul 2>&1
            if not errorlevel 1 set "PY=%%P"
        )
    )
)
if not defined PY (
    for %%P in ("%LOCALAPPDATA%\Programs\Python\Python314\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "C:\Program Files\Python314\python.exe" "C:\Program Files\Python313\python.exe" "C:\Program Files\Python312\python.exe" "C:\Program Files\Python311\python.exe" "C:\Python314\python.exe" "C:\Python313\python.exe" "C:\Python312\python.exe") do (
        if not defined PY (
            if exist %%P (
                %%P -c "import fastapi,uvicorn,aiosqlite,pydantic" >nul 2>&1
                if not errorlevel 1 set "PY=%%~P"
            )
        )
    )
)
if not defined PY (
    echo [错误] 未找到装有项目依赖的 Python 解释器
    echo   已尝试 PATH 中的 python 及常见安装路径，均无法 import 核心依赖。
    echo   请先安装依赖：pip install -r backend\requirements.txt
    echo   或显式指定解释器后重试：set PYTHON_EXE=^<python.exe 完整路径^>
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('"%PY%" --version 2^>^&1') do set "PY_VER=%%v"
echo   Python: %PY_VER%
echo            %PY%

REM ---- Node.js ----
REM 先找 node，找不到再探测常见安装目录（含 nvm4w 的符号链接目录）。
REM 实测踩到的坑：某些终端 / 包装器会把 NVM_HOME、NVM_SYMLINK 这两个变量名
REM 原样（未展开）写进 PATH，此时 node 明明装着（本机 C:\nvm4w\nodejs 下
REM node.exe v25.8.0），但 where node 永远找不到，脚本卡在「未找到 Node.js」。
REM 这里与上面的 Python 解释器探测保持一致，做一次自愈。
set "NODE_DIR="
if defined NVM_SYMLINK if exist "%NVM_SYMLINK%\node.exe" set "NODE_DIR=%NVM_SYMLINK%"
if not defined NODE_DIR if exist "C:\nvm4w\nodejs\node.exe" set "NODE_DIR=C:\nvm4w\nodejs"
if not defined NODE_DIR if exist "C:\Program Files\nodejs\node.exe" set "NODE_DIR=C:\Program Files\nodejs"
if not defined NODE_DIR if exist "C:\Program Files (x86)\nodejs\node.exe" set "NODE_DIR=C:\Program Files (x86)\nodejs"
if defined NODE_DIR (
    echo   [!] node 不在 PATH 中，改用: %NODE_DIR%
    set "PATH=%NODE_DIR%;%PATH%"
)
where node >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Node.js
    echo   请安装 Node.js 20+ : https://nodejs.org/
    echo   若用 nvm-windows / Volta 管理 Node，请先执行 nvm use 或 volta install node
    echo   已安装但找不到时，请把 node.exe 所在目录加入 PATH（如 C:\nvm4w\nodejs）
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('node --version 2^>^&1') do set "NODE_VER=%%v"
echo   Node.js: %NODE_VER%

where npm >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 npm ^(随 Node.js 安装^)
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('npm --version 2^>^&1') do set "NPM_VER=%%v"
echo   npm: %NPM_VER%

echo.

REM ========== 1. 清理端口 ==========
echo [1/5] 清理端口占用 (8000 / 5175)...
REM 先 taskkill 再按结果打印：vite 会同时绑定 0.0.0.0 与 [::]，同一 PID 在
REM netstat 里出现两行，若先打印就会重复报一次。第二次 taskkill 必然失败，
REM 因此天然去重，措辞也从「尝试终止」变成准确的「已终止」。
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" (
        taskkill /PID %%a /T /F >nul 2>&1
        if not errorlevel 1 echo   端口 8000 被 PID %%a 占用，已终止
    )
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5175 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" (
        taskkill /PID %%a /T /F >nul 2>&1
        if not errorlevel 1 echo   端口 5175 被 PID %%a 占用，已终止
    )
)
%SLEEP% /t 1 >nul 2>&1
REM 复核：taskkill 失败时（例如被占用的是管理员进程/系统服务）旧脚本会静默跳过，
REM 结果是新起的 uvicorn 报「address already in use」而窗口一闪而过，用户完全不知情；
REM 更糟的是后续健康探测会打到「残留的旧后端」上，脚本误报 [OK] 服务已启动，
REM 用户实际跑的是一份过期实例。因此 8000 仍被占用时直接硬失败并提示用管理员权限重跑，
REM 绝不带着假 OK 继续。

REM ---- pass 2: kill_port.ps1 sweeps the real socket holder -----------------
REM "taskkill /PID <owner> /T /F" alone is NOT enough for uvicorn --reload:
REM the reloader parent creates the listen socket and the worker child
REM inherits its handle. When the parent is already gone, netstat keeps
REM reporting the DEAD parent PID as owner, taskkill fails with "process not
REM found", and the port stays LISTENing forever - which made this script
REM abort below with a misleading "run it as administrator" hint.
REM kill_port.ps1 resolves the living descendants of that PID, kills them
REM and waits until the listener is really gone. Re-run the selftest after
REM changing either file:  powershell -File kill_port_selftest.ps1
REM NOTE: keep this block ASCII-only - the file must stay GBK/CP936.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kill_port.ps1" -Ports 8000,5175 -WaitSec 8
if errorlevel 1 echo   [!] kill_port.ps1 could not release every port, see above.

set "PORT_8000_BUSY="
set "PORT_5175_BUSY="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" set "PORT_8000_BUSY=%%a"
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5175 " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" set "PORT_5175_BUSY=%%a"
)
if defined PORT_8000_BUSY (
    echo   [!] 端口 8000 仍被 PID %PORT_8000_BUSY% 占用（taskkill 可能因权限不足而失败）
    echo      后端无法在新窗口绑定 8000；若仍启动会得到「假 OK」（探测命中残留旧后端）。
    echo      请先结束该进程，或「以管理员身份」重跑本脚本：
    echo      （右键 start_all.bat - 以管理员身份运行）
    pause
    exit /b 1
)
if defined PORT_5175_BUSY (
    echo   [!] 端口 5175 仍被占用（taskkill 可能因权限不足而失败）
    echo      前端可能改绑其它端口或启动失败，请手动结束该进程后重试（建议管理员身份重跑）
)
echo   完成

REM ========== 2. 后端依赖 ==========
echo.
echo [2/5] 后端依赖 (FastAPI + Uvicorn + AI 服务)...
cd /d "%~dp0backend"

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo   已从 .env.example 创建 .env
        echo   [!] 请编辑 backend\.env 填写 FERNET_KEY 和 AI API Key
    ) else (
        echo   [!] 未找到 .env.example，跳过
    )
)

REM 强校验：直接 import 整个应用，等价于 uvicorn 启动时的完整导入图。
REM 旧脚本只查 4 个包：pydantic 被降级到 v1、lxml / pymupdf 缺失等情况下
REM 该检查会通过，但 uvicorn 一启动就崩，用户看到的是「已就绪」却打不开页面。
"%PY%" -c "import app.main" >nul 2>&1
if errorlevel 1 (
    echo   正在安装/修复后端依赖 ^(首次可能较慢，请耐心等待^)...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 后端依赖安装失败
        echo   手动尝试: "%PY%" -m pip install -r requirements.txt
        pause
        exit /b 1
    )
    "%PY%" -c "import app.main" >nul 2>&1
    if errorlevel 1 (
        echo [错误] 依赖已安装但应用仍无法导入，请手动排查：
        echo   cd backend ^&^& "%PY%" -c "import app.main"
        pause
        exit /b 1
    )
    echo   [OK] 后端依赖已安装完成
) else (
    echo   [OK] 后端依赖已就绪
)

REM 检查图表渲染器
REM 注意：python -c 的参数里绝对不能写中文。当控制台 OEM 代码页不是 936 时
REM （Windows 11，或开启「使用 Unicode UTF-8」后常见的 65001），cmd 会把本文件
REM 里的 GBK 中文字节按错误代码页解码成 U+FFFD，python 再以 gbk 编码输出就抛
REM UnicodeEncodeError，导致本行误报「渲染器加载异常」——而渲染器其实是好的。
REM 所以 -c 里只放 ASCII，中文提示改由本脚本的 echo 输出。
"%PY%" -c "from app.services.ai.mermaid_renderer import _chart_cache" >nul 2>&1 && (
    echo   [OK] Mermaid 渲染器可用 ^(PIL v2 回退^)
) || (
    echo   [!] Mermaid 渲染器加载异常，首次启动时会自动重建
)

REM ========== 2.5 可选依赖（OCR 引擎 + Windows PDF 导出控件） ==========
echo.
echo [2.5] 可选依赖 (离线 OCR 引擎 / Windows PDF 导出控件)...

REM 离线 OCR 引擎（扫描件/图片型资料文字识别，约 100~200MB，失败不影响主流程）
"%PY%" -c "import rapidocr_onnxruntime" >nul 2>&1 || (
    echo   正在安装 OCR 引擎 ^(requirements-ocr.txt^)...
    "%PY%" -m pip install -r requirements-ocr.txt >nul 2>&1 && (
        echo   [OK] OCR 引擎已安装
    ) || (
        echo   [!] OCR 引擎安装失败，扫描件识别将走 Tesseract/视觉模型兜底
    )
)

REM Windows 专用：DOCX->PDF 高质量导出（需本机 Microsoft Word）
"%PY%" -c "import win32com" >nul 2>&1 || (
    echo   正在安装 Windows PDF 导出控件 ^(requirements-windows.txt^)...
    "%PY%" -m pip install -r requirements-windows.txt >nul 2>&1 && (
        echo   [OK] Windows PDF 导出控件已安装
    ) || (
        echo   [!] Windows PDF 控件安装失败，将回退 LibreOffice/soffice
    )
)

REM 检查 OCR 引擎（扫描件 / 图片型资料的文字识别）
"%PY%" -c "import sys; from app.services.ocr import ocr_capabilities as c; cap=c(); sys.exit(0 if (cap['tesseract'] or cap['rapidocr']) else 1)" >nul 2>&1 && (
    echo   [OK] OCR 引擎已就绪，扫描件/图片型资料可提取
) || (
    echo   [!] 未检测到本地 OCR 引擎；若未配置视觉模型，扫描件将无法提取文字
    echo      启用方式：cd backend ^&^& pip install -r requirements-ocr.txt
    echo      或安装 Tesseract-OCR 并配置 TESSERACT_PATH，或在「AI 配置」添加视觉模型
    echo      详情：http://localhost:8000/api/v1/diagnostics/capabilities
)

REM ========== 3. 前端依赖 ==========
echo.
echo [3/5] 前端依赖 (React + Vite + Ant Design)...
cd /d "%~dp0frontend"

REM 只判断 node_modules 目录会漏判：npm install 被 Ctrl+C / 断电中断时目录
REM 已存在但不完整，旧脚本会直接报「已就绪」，随后 npm run dev 才以难以
REM 理解的 ENOENT 报错失败。这里检查 vite 的入口脚本是否真的存在。
if not exist "node_modules\.bin\vite.cmd" (
    echo   node_modules 缺失或不完整，正在安装 ^(可能需要 1-3 分钟^)...
    call npm install --registry=https://registry.npmmirror.com
    if errorlevel 1 (
        echo   ^(镜像安装失败，尝试官方源...^)
        call npm install
    )
    if errorlevel 1 (
        echo [错误] 前端依赖安装失败
        echo   手动尝试: cd frontend ^&^& npm install
        pause
        exit /b 1
    )
    if not exist "node_modules\.bin\vite.cmd" (
        echo [错误] 安装完成但仍找不到 node_modules\.bin\vite.cmd
        echo   建议：删除 frontend\node_modules 后重新运行本脚本
        pause
        exit /b 1
    )
    echo   [OK] 前端依赖已安装完成
) else (
    echo   [OK] 前端依赖已就绪
)


REM ========== 4. 启动后端 ==========
echo.
echo [4/5] 启动后端服务 (FastAPI :8000)...
cd /d "%~dp0backend"
REM 用 start /D 指定工作目录，配合 cmd /S /K 保证引号按字面量解析
REM （不再写成 cmd /k "cd /d "path" && ..." 这种嵌套引号写法）
start "后端-专项方案工具箱" /D "%~dp0backend" cmd /S /K ""%PY%" -m uvicorn app.main:app --reload --reload-exclude *.pytest_cache* --reload-exclude *__pycache__* --reload-exclude *.pt_* --host 0.0.0.0 --port 8000"

REM 等后端起来。旧脚本用 for /l %%i in (1,1,15) 只等 15 秒，且直接调裸 curl：
REM 精简系统 / 旧版 Windows 没有 curl.exe 时会永远判定未就绪并误报「超时」，
REM 即使后端其实运行得很好。这里改成 label + set /a 的显式循环，用 %CURL%
REM 探测、缺失时回退到 Python 标准库（Python 此时必然可用）。
echo   等待后端就绪 ^(最多 60 秒^)...
set "PROBE_URL=http://127.0.0.1:8000/api/v1/health"
set "TRIES=0"
:probe_backend
set /a TRIES+=1
%SLEEP% /t 1 >nul 2>&1
if defined CURL goto :probe_backend_curl
"%PY%" -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(os.environ['PROBE_URL'],timeout=3).status==200 else 1)" >nul 2>&1
goto :probe_backend_after
:probe_backend_curl
%CURL% -s %PROBE_URL% >nul 2>&1
:probe_backend_after
if not errorlevel 1 goto :backend_ready
if %TRIES% LSS 60 goto :probe_backend
echo   [!] 60 秒内后端仍未就绪，请查看「后端-专项方案工具箱」窗口的日志
:backend_ready

REM ========== 5. 启动前端 ==========
echo.
echo [5/5] 启动前端服务 (Vite :5175)...
cd /d "%~dp0frontend"
start "前端-专项方案工具箱" /D "%~dp0frontend" cmd /S /K "npm run dev"

REM 等前端起来再打开浏览器（旧脚本固定等 4 秒，慢机器上会打开空白页）
echo   等待前端就绪 ^(最多 30 秒^)...
set "PROBE_URL=http://127.0.0.1:5175/"
set "TRIES=0"
:probe_frontend
set /a TRIES+=1
%SLEEP% /t 1 >nul 2>&1
if defined CURL goto :probe_frontend_curl
"%PY%" -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(os.environ['PROBE_URL'],timeout=3).status==200 else 1)" >nul 2>&1
goto :probe_frontend_after
:probe_frontend_curl
%CURL% -s %PROBE_URL% >nul 2>&1
:probe_frontend_after
if not errorlevel 1 goto :frontend_ready
if %TRIES% LSS 30 goto :probe_frontend
echo   [!] 30 秒内前端仍未就绪，请查看「前端-专项方案工具箱」窗口的日志
:frontend_ready

echo.
echo ============================================
echo   [OK] 服务已启动！
echo.
echo   前端页面: http://localhost:5175
echo   后端 API: http://localhost:8000
echo   API 文档: http://localhost:8000/docs
echo ============================================
echo.

REM 必须带空标题 ""：start 的第三个参数若以引号开头会被当作窗口标题，
REM URL 就不会被打开（写成 start "http://..." 是常见坑）。
start "" http://localhost:5175

echo   后端与前端各运行在独立窗口中，关闭本窗口不会影响它们。
pause

