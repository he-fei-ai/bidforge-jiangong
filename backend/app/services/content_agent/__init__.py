"""正文 Agent 分层修复子系统（R135 · 引入易标 Yibiao 2.30.20 正文生成 Agent 机制）。

对齐参考软件（J:\\编程\\OpenBidKit 易标\\Yibiao-2.30.20-win-x64）：

- ``electron/services/agent/agentToolEnvironment.cjs``：Agent 工具环境
  （隔离工作区 + 原生文件工具 + 指令文件 + PATH 注入）；
- ``electron/services/pi/piWorkspaceBaseline.cjs``：受保护文件基线
  （原件副本 + 提交前还原）；
- ``electron/services/contentGenerationConsistencyTools.cjs``：一致性审计台账
  + ``search-sections`` / ``repair-sections``（按小节派发修复）+
  提交校验（章节不得新增/删除/重排，只回写变化小节）；
- ``electron/services/contentGenerationEditTools.cjs``：批量编辑工具
  （本批以外被改动的小节还原 + 改动前后对比）。

本仓分层（与既有能力的关系，全部加法式、默认关闭）：

- 第一层 普通修复（既有）：``services/consistency_edits``（old_text/new_text
  唯一命中才替换）→ 失败回落 ``services/repair_agent``（整章重写 + 校验）；
- 第二层 Agent 修复（本包）：LLM 以「文件读写 + grep 搜索」在**隔离工作区**
  内修改小节文件，程序重新解析校验（标记完整 / 不新增 / 不删除 / 不重排），
  **只回写变化小节**，校验失败保留原正文；
- 三重边界：① 隔离工作区（Agent 不触碰主存储，输入经基线保护）；② 全局 FIFO
  队列（同一时间只跑一个 Agent 任务，支持优先级/暂停/取消/超时）；③ 程序校验
  优先（不信任 Agent 自报，全部修改经 :mod:`.validation` 重新解析）。

结构校验兜底（``validate_workspace_sections``）：无论哪层修正，回写前必须
通过——L1~L5 层级与九大章节覆盖由既有审核预检链路承担（本包不重复实现），
本包负责「章节标记完整性 / 修改范围合法性 / 变化小节识别」。
"""
from app.services.content_agent.queue import (  # noqa: F401
    AgentQueueFull,
    global_agent_queue,
    reset_global_agent_queue,
)
from app.services.content_agent.validation import (  # noqa: F401
    SectionValidation,
    validate_workspace_sections,
)
from app.services.content_agent.workspace import (  # noqa: F401
    FACTS_FILE,
    SECTION_DIR,
    TASK_FILE,
    materialize_workspace,
    parse_section_content,
    read_section_file,
    section_markers,
    section_relpath,
)

__all__ = [
    "FACTS_FILE",
    "SECTION_DIR",
    "TASK_FILE",
    "AgentQueueFull",
    "SectionValidation",
    "global_agent_queue",
    "materialize_workspace",
    "parse_section_content",
    "read_section_file",
    "reset_global_agent_queue",
    "section_markers",
    "section_relpath",
    "validate_workspace_sections",
]
