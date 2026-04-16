# 项目背景
本项目是一个名为 `claude-feishu-flow` 的 Python 库，已进入可运行状态。它将飞书的企业级协同能力与 Claude AI 能力结合，实现对话式的"实验管理 (ChatOps)"：用户在飞书发送指令，Claude 生成并执行 Python 脚本，结果回写飞书消息卡片和多维表格。目标是打包上传至 PyPI。

# 技术栈
- 语言：Python 3.10+
- Web 框架：FastAPI (用于接收飞书 Webhook)
- 异步服务器：Uvicorn
- HTTP 客户端：httpx (用于调用飞书 API 和 Claude API)
- 并发处理：Python `asyncio`
- 环境管理：Poetry 或 pip (requirements.txt)

# 核心架构模块 (强制要求高内聚低耦合)
1. `feishu/`: 飞书 API 交互层 (鉴权、发消息、接 Webhook、读写多维表格 Bitable)。
2. `ai/`: Claude 交互层 (系统 Prompt 管理、Tool Use 封装、Agentic 循环)。
3. `runner/`: 实验执行层 (基于 subprocess 管理后台任务，捕获 stdout/stderr，未来考虑接入 Docker)。
4. `server/`: 路由与 Webhook 接收层。

## 关键实现模式

### Services 容器 (`server/app.py`)

所有单例通过 `app.state.services` 注入，字段包括：

- `config`, `http`, `token_manager`, `feishu`, `messaging`, `bitable`, `claude`, `executor`
- `processing_ids: set[str]` — 飞书 Webhook 去重（按 message_id）
- `edit_sessions: dict[str, EditSession]` — 活跃的 /edit 多轮对话（按 chat_id）
- `user_sessions: dict[str, str]` — open_id → task_id（Sub Agent 会话路由）
- `sub_agent_histories: dict[str, list]` — task_id → 对话历史（最多 60 条，裁剪至 40 条）

### 四阶段 Pipeline (`server/routes.py`)

收到消息后，后台任务按四阶段执行：

- **Phase A（生成）**: Claude Agentic 循环调用 `save_script` 工具，生成 `setting/plan.md` 和 `setting/main.py`
- **Phase B（审阅）**: Review Agent 读取 plan.md 和 main.py，结合用户原始意图进行静态审查；发现问题时自动调用 `save_script` 修复，并将审阅报告写入 `setting/review.md`（非阻塞：Review 失败不中断流水线）
- **Phase C（执行）**: `ScriptExecutor` 异步 subprocess 运行 main.py，支持 `--retry N` 自愈循环（失败时 Claude 调试 stderr 并修复 main.py）
- **Phase D（汇报）**: Claude 生成 `results/summary.md`，写入 Bitable，发送飞书消息卡片

### Session 路由逻辑 (`server/routes.py`)

```text
if open_id in user_sessions:
    → _handle_sub_agent_message (Sub Agent 监控模式)
elif text.startswith("/edit"):
    → _handle_edit_session (多轮对话编辑模式)
else:
    → _handle_message (正常生成模式)
```

### 命令系统

- `/list` — 列出所有实验
- `/review exp_<uuid>` — 独立审阅已生成代码（不触发执行）
- `/edit exp_<uuid> <指令>` — 进入交互式编辑模式
- `/cancel` — 取消活跃的编辑会话
- `--retry N` — 允许最多 N 次自愈重试
- `/exit` — 退出 Sub Agent 会话

## 实验目录结构

每次实验对应 `Experiments/exp_<uuid>/`：

```text
exp_<uuid>/
├── setting/
│   ├── plan.md      ← Claude Phase A 生成
│   ├── main.py      ← Claude Phase A 生成（save_script 工具写入）
│   └── review.md    ← Claude Phase B 审阅报告（Review Agent 写入）
├── output/
│   ├── run.log      ← subprocess stdout 实时流
│   └── error.log    ← subprocess stderr 实时流
└── results/
    └── summary.md   ← Claude Phase D 分析报告
```

# 编码规范与底线原则
1. **异步优先**：所有网络 IO（飞书 API、Claude API）必须使用 `async/await`。
2. **飞书 Webhook 3秒法则**：接收到飞书 Webhook 后，必须立即将任务推入后台（如 `asyncio.create_task` 或后台队列），并立刻返回 `HTTP 200` 给飞书，绝不能因为等待 Claude 的回复导致飞书 Webhook 超时重试。
3. **类型提示**：所有的函数和类必须包含完整的 Python Type Hints。
4. **面向对象设计**：对外的 API 应该尽量简洁，例如用户只需要实例化 `Bot(config)` 即可。
5. **日志记录**：使用标准库 `logging` 记录关键步骤，方便用户排查问题。
6. **Markdown 禁用表格**：所有发往飞书的 AI 输出（summary、prompt）禁止使用 Markdown 表格，改用列表格式（飞书渲染限制）。

# 未来计划

- Docker 集成：替换 subprocess 执行，提供脚本沙箱隔离
- 持久化 Session：`edit_sessions` / `user_sessions` 当前存内存，重启丢失，未来考虑 Redis 或数据库

# Plan Mode 工作要求
在开始任何实际代码编写前，必须先分析当前需求，给出分步架构设计和执行计划。只有在我（用户）批准后，才能开始编写或修改代码。每一轮执行只完成计划中的一个步骤。

# Edit
- 每次编辑之后请进行git add 和git commit，保存git tree
- 如果需要启动参数，则需要修改launch.sh
- **每当新增、删除或修改任何用户可用命令时，必须同步更新 `feishu/messaging.py` 中的 `send_help_card` 内容，确保 `/help` 卡片始终与实际命令保持一致。**
- **每当修改 `README.md` 时，必须同步更新所有其他语言的 README 文件（如 `README_zh.md`），确保所有语言版本内容始终保持一致。**
