# 启动/关闭软件 —— 进程检查与清理机制

> 适用版本：2026-09-28 起。入口脚本 `cleanup_guard.ps1`，配套 `stop_all.bat` / `start_all.bat`（pass 4）。
> 回归测试：`cleanup_guard_selftest.ps1`（4 类残留）、`kill_port_selftest.ps1`（端口/孤儿 socket）。

## 1. 为什么需要统一机制

原先清理动作分散在 `start_all.bat` 的若干 pass 与 `stop_backend.ps1` 里，各管一块，
存在三类**"清不掉"**的残留（均已实测复现）：

| 残留类型 | 现象 | 旧机制为何漏掉 |
|---|---|---|
| **前端残留** node/npm/vite | 关掉控制台窗口后 dev server 仍在跑 | 只按端口杀；5175 被占时 vite 改绑 5176+ |
| **超时残留**（探活/依赖检查桩进程） | `python -c "import app.main"` / urllib 探活卡死不退出 | 端口不占、签名也不是 pytest，全部漏掉 |
| **孤儿进程**（父进程已死） | 强关窗口后 uvicorn/node 无端口无窗口仍存活 | 端口杀只看 LISTEN，pytest 杀只看 pytest 签名 |
| **僵尸 pytest** | 占用 `logs/backend.log` → 5MB 轮转冻结 | 已有 `kill_zombie_pytest.ps1`，但只被"启动时"调用 |

## 2. 处理机制：一次分类检查 + 分层清理

`cleanup_guard.ps1` 负责一次"**分类 → 判定 → 清理 → 复验**"，四种模式：

| 模式 | 行为 | 退出码 | 使用场景 |
|---|---|---|---|
| `-Mode Check` | **只检查不杀**，打印全部残留分类 | 0=干净 / 2=有残留 | 排障、巡检 |
| `-Mode Clean` | 检查 + 完整清理 | 0 / 1 | 手动 |
| `-Mode Startup` | 同 Clean（幂等，bat 前面几遍已做过同样的事） | 0 / 1 | `start_all.bat` pass 4 |
| `-Mode Shutdown` | Clean + 临时文件/锁/`.pt_*` 残留清扫 | 0 / 1 | `stop_all.bat` |

### 2.1 四类残留的判定口径（单一事实源）

| 分类 | 判定条件 | 清理方式 |
|---|---|---|
| `backend` | 镜像 ∈ python*，命令行匹配 `app.main:app` | 按进程树 taskkill（含崩溃循环/启动卡死的**无端口**后端） |
| `frontend` | 镜像 ∈ node/npm，**或** cmd 且命令行含工具箱根路径**且**带 `npm run dev`/`vite` 特征 | taskkill /T /F |
| `stub`（超时残留） | 镜像 ∈ python*，命令行匹配 `import app.main` 或 `urllib.request…urlopen`，且**存活 > 60s** | taskkill；未超时的视为"正在工作"放过 |
| `pytest`（僵尸测试） | 镜像 ∈ python*，命令行匹配 pytest/py.test/multiprocessing | taskkill /T /F |

外加两类：
- **孤儿**：父 PID 已不在进程表 —— 无论哪一类都清理。
- **工具箱控制台窗口**：`MainWindowTitle` 以 `-专项方案工具箱` 结尾的 `cmd.exe`（start_all 起的"后端-/前端-"窗口）。先关窗口再杀进程，窗口的 `/T` 树杀能顺带带走 node/vite 子进程。

### 2.2 安全护栏（绝不能误杀）

1. **保护 PID 集** = `0/4` + 本进程 + **向上回溯的全部祖先**（父进程链），先建表再动手；
2. **镜像名白名单**：只考虑 `python* / node / npm / cmd.exe`，`powershell` / `conhost` / `OpenConsole` 永不作为候选；
3. **签名门**：命令行必须命中工具箱指纹（`app.main:app` / 工具箱根路径 / 已知桩特征 / pytest 特征）；
4. **健康服务不误报**：正在监听 8000/5175 的后端/前端被标记为 `isService`，`-Mode Check` 不会把"正在正常运行"报成残留；
5. **cmd.exe 额外收紧**：命令行里仅出现工作区路径（如某个工具的 shell 恰好 cd 到本目录）**不**算前端，必须带 `npm run dev`/`vite` 特征 —— 这是实测踩到的坑：早期版本按"根路径"判定，曾误杀 8 个同工作区的工具 shell。

## 3. 与既有脚本的关系

- `kill_port.ps1` / `kill_zombie_pytest.ps1` **保留为独立命令行工具**（`start_backend.ps1` / `stop_backend.ps1` 仍在用），`cleanup_guard.ps1` 内联了同等逻辑 —— 因为每次 `powershell -File` 子进程在本机要 **5~6 秒**，内联后单次清理从 19~25s 降到 **8~9s**。
- ⚠️ **口径同步要求**：端口释放、pytest 判定的逻辑共有两份实现，任一改动必须同时改另一处，并以两个 selftest 为准。

## 4. 常用命令

```bash
# 只检查不杀（排障）
powershell -NoProfile -ExecutionPolicy Bypass -File .\cleanup_guard.ps1 -Mode Check

# 启动/关闭全程清理（start_all.bat pass 4 / stop_all.bat 内部即调用）
powershell -NoProfile -ExecutionPolicy Bypass -File .\cleanup_guard.ps1 -Mode Startup
powershell -NoProfile -ExecutionPolicy Bypass -File .\cleanup_guard.ps1 -Mode Shutdown

# 关闭整个软件（后端+前端+残留+临时文件）
stop_all.bat

# 回归测试（改动清理脚本后必跑）
powershell -NoProfile -ExecutionPolicy Bypass -File .\cleanup_guard_selftest.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\kill_port_selftest.ps1
```

## 5. 已知限制

- **临时文件清扫**：只扫 `backend\data` 等 3 层、且跳过 5738+ 子目录的 `projects` 树（实测递归会卡住数分钟），并有 30s 总预算；`.pt_*` 目录在个别机器上会被杀软/ACL 拒绝删除，此时只汇总提示一行，不影响其它清理。
- **窗口标题匹配**依赖 start_all.bat 起的窗口标题格式（`后端-/前端-专项方案工具箱`）；手工起的 dev server 走"根路径 + npm/vite 特征"这条规则。
- `powershell` 5.1 无 `Get-ChildItem -Depth`，脚本用手写定深遍历替代。