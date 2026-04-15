# claude-feishu-flow

**飞书 × 大模型 × ChatOps — 让 AI 替你跑实验**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-green)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

`claude-feishu-flow` 是一个 ChatOps 风格的**自主实验管理 Agent**。你在飞书群里发一句话，它就能：自动生成代码、静态审查、执行实验、自愈重试、汇总结果，并将报告推回飞书消息卡片和多维表格——全程无需你打开终端。

> **适用场景**：ML 实验、数据分析、服务器自动化脚本等需要反复迭代的批处理任务。

---

## 核心特性

- **自然语言驱动**：用中文/英文描述任务，Claude/Kimi 自动生成完整 Python 脚本
- **四阶段流水线**：生成 → 代码审查 → 执行 → 汇报，每阶段独立且可插拔
- **自动 Debug 重试**：执行失败时，AI 读取 stderr 自动修复代码并重试（`--retry N`）
- **Sub Agent 实时监控**：点击消息卡片按钮，进入与实验进程的持续对话
- **多模型双驱**：Claude（Anthropic）和 Kimi（Moonshot AI）一键切换，支持自定义代理地址
- **飞书深度集成**：消息卡片、多维表格写入、Webhook 加密验签、附件（PDF/图片）解析

---

## 架构总览

```
飞书消息
   │
   ▼
┌──────────────────────────────────────────────────────┐
│                    FastAPI Webhook                   │
│              (立即返回 200，任务后台执行)               │
└──────────────────────────────────────────────────────┘
   │
   ▼
┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│  Phase A │→ │  Phase B │→ │  Phase C │→ │  Phase D │
│  生  成   │  │  审  阅   │  │  执  行   │  │  汇  报   │
│ plan.md  │  │review.md │  │ run.log  │  │summary.md│
│ main.py  │  │(非阻塞)   │  │(自愈重试) │  │Bitable写入│
└──────────┘  └──────────┘  └──────────┘  └──────────┘
```

### 模块结构

```
claude_feishu_flow/
├── config.py          # 环境变量加载 (pydantic-settings)
├── bot.py             # 对外门面：Bot(config).run()
├── feishu/            # 飞书 API 层：鉴权、消息卡片、Bitable、Webhook
├── ai/                # 大模型层：Claude / Kimi Agentic 循环、Tool Use
├── runner/            # 执行层：subprocess + 实时日志捕获
└── server/            # FastAPI 路由、Services 容器、定时任务
```

每次实验产出：

```
Experiments/exp_<uuid>/
├── setting/
│   ├── plan.md       # Phase A：实验策略
│   ├── main.py       # Phase A：可执行脚本
│   └── review.md     # Phase B：静态审查报告
├── output/
│   ├── run.log       # subprocess stdout
│   └── error.log     # subprocess stderr
└── results/
    └── summary.md    # Phase D：AI 分析报告
```

---

## 快速上手

### 1. 安装依赖

```bash
git clone https://github.com/your-org/claude-feishu-flow.git
cd claude-feishu-flow
pip install -e .
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入飞书 App ID/Secret、Claude 或 Kimi API Key 等
```

> 详细说明见下方 [环境变量](#环境变量) 一节。

### 3. 配置飞书机器人

1. 前往 [飞书开放平台](https://open.feishu.cn/app) 创建自建应用
2. 开启**机器人**能力，获取 App ID / App Secret
3. 配置 Webhook 事件订阅地址：`http://your-server:8080/webhook/event`
4. 在 Webhook 事件页面获取 Verification Token（可选：启用加密并填写 Encrypt Key）
5. 订阅事件：`im.message.receive_v1`

### 4. 启动服务

```bash
bash launch.sh
# 或
uvicorn claude_feishu_flow.server.app:create_app_from_env --factory --host 0.0.0.0 --port 8080
```

### Python API（嵌入已有项目）

```python
from claude_feishu_flow import Bot, Config

config = Config()   # 自动从 .env 加载
bot = Bot(config)
bot.run()           # 阻塞启动 uvicorn
```

---

## 环境变量

复制 `.env.example` 并按需填写：

| 变量 | 必填 | 说明 | 默认值 |
|------|------|------|--------|
| `FEISHU_APP_ID` | ✅ | 飞书应用 App ID | — |
| `FEISHU_APP_SECRET` | ✅ | 飞书应用 App Secret | — |
| `FEISHU_VERIFICATION_TOKEN` | ✅ | Webhook 验签 Token | — |
| `FEISHU_ENCRYPT_KEY` | — | Webhook 加密密钥（启用加密时填写） | `""` |
| `BITABLE_APP_TOKEN` | ✅ | 多维表格 App Token（用于写入结果） | — |
| `BITABLE_TABLE_ID` | — | 表格 ID（留空则自动发现/创建） | `""` |
| `LLM_PROVIDER` | — | 大模型提供商：`anthropic` 或 `kimi` | `anthropic` |
| `ANTHROPIC_API_KEY` | * | Claude API Key（`LLM_PROVIDER=anthropic` 时必填） | — |
| `ANTHROPIC_MODEL` | — | Claude 模型名称 | `claude-3-5-sonnet-latest` |
| `ANTHROPIC_BASE_URL` | — | 自定义 API 代理地址（留空使用官方端点） | — |
| `KIMI_API_KEY` | * | Kimi API Key（`LLM_PROVIDER=kimi` 时必填） | — |
| `KIMI_MODEL` | — | Kimi 模型名称 | `moonshot-v1-32k` |
| `KIMI_BASE_URL` | — | Kimi API 端点（可替换为镜像） | `https://api.moonshot.cn/v1` |
| `HOST` | — | 服务监听地址 | `0.0.0.0` |
| `PORT` | — | 服务监听端口 | `8080` |
| `EXPERIMENTS_DIR` | — | 实验目录（自动创建） | `./Experiments` |
| `DEFAULT_MAX_RETRIES` | — | 默认自愈重试次数 | `5` |

---

## 可用命令

在飞书中直接发送以下命令（无需命令前缀之外的任何语法）：

| 命令 | 说明 |
|------|------|
| `<自然语言描述>` | 直接描述任务，AI 生成并执行脚本 |
| `<描述> --retry N` | 同上，失败后最多自愈重试 N 次 |
| `/list` | 列出所有历史实验及状态 |
| `/review exp_<uuid>` | 对已有实验进行独立代码审查（不执行） |
| `/edit exp_<uuid> <指令>` | 进入交互式编辑模式修改实验脚本 |
| `/edit exp_<uuid> <指令> --retry N` | 编辑后执行，含自愈重试 |
| `/alias exp_<uuid> <名称>` | 为实验设置可读别名 |
| `/write <主题> [exp_<uuid>]` | 起草技术文档或实验报告 |
| `/cancel` | 退出当前编辑会话 |
| `/exit` | 退出 Sub Agent 监控模式 |
| `/help` | 查看命令帮助卡片 |

---

## Sub Agent 监控模式

每次实验完成后，消息卡片会显示「进入会话」按钮。点击后进入 Sub Agent 模式，可实时与正在运行的实验对话：

- "当前 loss 是多少？" → AI 读取 `run.log` 最新内容回答
- "把 batch size 改成 64 并重新运行" → AI 修改 `main.py` 并重启进程
- 发送 `/exit` 退出监控模式

---

## 开发与测试

```bash
pip install -e ".[dev]"
pytest
```

测试覆盖：飞书鉴权、Webhook 验签、Bitable 读写、Claude Tool Use、Kimi 兼容层、执行器、路由集成。

---

## Roadmap

- [ ] Docker 沙箱：替换 subprocess，提供脚本隔离执行环境
- [ ] 持久化 Session：将 `edit_sessions` / `user_sessions` 迁移至 Redis，支持重启恢复
- [ ] 更多模型支持：接入 Gemini、DeepSeek 等 OpenAI 兼容接口
- [ ] Web UI：实验列表与日志的浏览器查看界面

---

## Contributing

欢迎提交 Issue 和 PR！请确保：
- 新功能附带对应测试
- 所有函数包含完整 Type Hints
- 不在代码中硬编码任何凭证（通过 `svc.config` 读取）

---

## License

MIT © 2024