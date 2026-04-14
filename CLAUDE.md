# 项目背景
本项目旨在开发一个名为 `claude-feishu-flow` 的 Python 库。它将飞书（Feishu）的企业级协同能力与 Anthropic Claude 的 AI 能力结合，实现对话式的“实验管理 (ChatOps)”。最终目标是打包上传至 PyPI，供用户一键安装。

# 技术栈
- 语言：Python 3.10+
- Web 框架：FastAPI (用于接收飞书 Webhook)
- 异步服务器：Uvicorn
- HTTP 客户端：httpx (用于调用飞书 API 和 Claude API)
- 并发处理：Python `asyncio`
- 环境管理：Poetry 或 pip (requirements.txt)

# 核心架构模块 (强制要求高内聚低耦合)
1. `feishu/`: 飞书 API 交互层 (鉴权、发消息、接 Webhook、读写多维表格 Bitable)。
2. `ai/`: Claude 交互层 (系统 Prompt 管理、Tool Use/Computer Use 封装)。
3. `runner/`: 实验执行层 (基于 subprocess 管理后台任务，捕获 stdout/stderr，未来考虑接入 Docker)。
4. `server/`: 路由与 Webhook 接收层。

# 编码规范与底线原则
1. **异步优先**：所有网络 IO（飞书 API、Claude API）必须使用 `async/await`。
2. **飞书 Webhook 3秒法则**：接收到飞书 Webhook 后，必须立即将任务推入后台（如 `asyncio.create_task` 或后台队列），并立刻返回 `HTTP 200` 给飞书，绝不能因为等待 Claude 的回复导致飞书 Webhook 超时重试。
3. **类型提示**：所有的函数和类必须包含完整的 Python Type Hints。
4. **面向对象设计**：对外的 API 应该尽量简洁，例如用户只需要实例化 `Bot(config)` 即可。
5. **日志记录**：使用标准库 `logging` 记录关键步骤，方便用户排查问题。

# Plan Mode 工作要求
在开始任何实际代码编写前，必须先分析当前需求，给出分步架构设计和执行计划。只有在我（用户）批准后，才能开始编写或修改代码。每一轮执行只完成计划中的一个步骤。

# Edit
- 每次编辑之后请进行git add 和git commit，保存git tree
- 