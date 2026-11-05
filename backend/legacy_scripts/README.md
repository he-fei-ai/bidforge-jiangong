# legacy_scripts · 一次性排查脚本归档

- 归档时间：2026-11-04
- 归档范围：原先散落在 `backend/` 根目录的 33 个未跟踪脚本（`.py` / `.ps1`，合计 189,625 字节），
  均为 R144–R163 各轮审计、取证、变异验证时的一次性工具。

## 性质

- **不参与任何构建、运行、测试与 CI**。`backend/` 根目录下仍保留的 `check_acl.py`、
  `run_baselines.py`、`run_baselines2.py`、`run_final_baseline.py`、`run_full_baselines.py`
  属于 git 已跟踪文件，未归档，保持原位。
- 归档前已对全仓 `.py` / `.md` / `.yml` / `.bat` / `.json` / `.toml` 做过引用排查：
  没有任何已跟踪文件以文件名引用这些脚本（既无 import，也无 subprocess 调用）。
- 脚本之间确有互相引用，且被引用者同样在本目录内，相对位置一致：
  `_ab_r150.py` ← `_ab_r158.py`；`_r151_clean_outline.py` ← `_r152_ab_closeout.py`；
  `_r160_ab.py`、`_r160_delete_dead_trigger.py` ← `_r160_bytecheck.py`。

## 分类

| 前缀 / 文件 | 用途 |
|---|---|
| `_ab_*.py`、`_r16x_ab.py` | A/B 变异验证（字节备份 → 唯一锚点变异 → 跑护栏 → 还原校验） |
| `_e2e_*.py` | 端到端链路冒烟（目录 → 正文 → 导出） |
| `_r15x_*inventory*.py`、`_r159_ssot_inventory.py` | 单次盘点 / 清单导出 |
| `_tmp_*.py` | 临时查询与探针 |
| `_r154_com_verify.ps1` | Word COM 转换链路验证 |

## 复用前必读

- 脚本内写死了归档当时的路径与数据位置（例如 `_r152_ab_closeout.py`、`_r160_bytecheck.py`
  仍以 `backend/_xxx.py` 形式引用同类脚本），归档后这些路径已失效，
  复用前需先改为 `backend/legacy_scripts/...`。
- 依赖的是归档当时的本地数据库与导出产物，这些目录可能已被清理，需先确认数据源仍然可用。
- 脚本不会跟随产品代码演进，**不要把它当作回归测试**；需要长期守护的行为请写进 `backend/tests/`。
- 如需整体删除本目录，请先确认没有本地流程仍依赖其中的取证脚本。
