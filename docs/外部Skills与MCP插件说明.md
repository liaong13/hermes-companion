# 外部 Skills、MCP、工具与插件说明

本文记录维护环境中已安装或启用的第三方/外部扩展类别及其用途，帮助读者理解它们解决什么问题。它是维护环境的实例清单，不是 Hermes Companion 的依赖清单，也不代表克隆仓库即可获得这些能力。Hermes 官方内置技能、自行编写的项目组件、外部 Skills、MCP 服务、独立工具和 Hermes 插件是不同层次，不能混为一谈。

## 概念与边界

- **Skill**：给 Hermes 的任务说明、操作流程或工具使用指南；Skill 本身不一定安装软件，也不必然连接外部服务。
- **MCP**：模型上下文协议，用于让客户端连接实现 MCP 的外部服务并调用其提供的工具。安装一个写有 MCP 使用说明的 Skill，不等于 MCP 服务已安装、运行或连通。
- **Hermes 插件**：通过 Hermes 插件机制集成的组件，可提供平台适配、扩展逻辑或依赖。与 Skill、MCP 服务不是同一种安装物。
- **独立工具**：由操作者直接运行的审计、构建或维护程序，不会因为已安装就自动成为 Hermes 工具或运行时防护。
- **外部/第三方**：指独立于本仓库交付的扩展或服务。名称出现在本清单，不表示本项目作者拥有或重新许可其内容。

实际可用性取决于当前 profile、依赖、凭据、网络、服务端配置及权限；不同机器和 profile 的安装集合可能不同。清单以维护环境中检查到的非归档 Skills 及已发现插件清单为依据，后续应按实际变化更新。

## 已安装的外部 Skills（按用途）

以下为维护环境中可见的 Skills，描述其用途而非保证其所有可选依赖均已配置。

| 用途 | Skill | 用途说明 |
| --- | --- | --- |
| 创意与内容制作 | `ascii-art` | 用终端字符生成文字艺术、边框和简易图形。 |
| 创意与内容制作 | `comfyui` | 通过 ComfyUI 工作流生成图像、视频或音频；需可用的 ComfyUI 环境。 |
| 创意与内容制作 | `excalidraw` | 创建 Excalidraw 手绘风格图表及流程、架构示意。 |
| 创意与内容制作 | `humanizer` | 润色文本，减少模板化或生硬表达；不替代事实核查。 |
| 创意与内容制作 | `pretext` | 制作以文本布局为核心的创意浏览器演示。 |
| 创意与内容制作 | `sketch` | 快速制作可比较的网页界面草图。 |
| 创意与内容制作 / MCP | `touchdesigner-mcp` | 通过 twozero MCP 控制 TouchDesigner；需要对应 MCP 服务及 TouchDesigner 环境。 |
| Hermes 管理 | `hermes-agent` | 配置、扩展、排查和编排 Hermes Agent。 |
| Hermes 管理 | `hermes-provider-switching` | 添加或切换模型服务提供方。 |
| Hermes 管理 | `hermes-session-management` | 管理或删除 Hermes 会话历史。 |
| 开发与运维 | `nginx-reverse-proxy` | 配置或检查 Nginx 反向代理。 |
| 开发与运维 | `nginx-tls-vhost-migration` | 迁移或启用 TLS 虚拟主机。 |
| 开发与运维 | `sdlc-review` | 检查看板交接并整理经核实的结果。 |
| 开发与运维 | `aosp-development-evidence` | 排查 AOSP、MTK、UNISOC 开发问题并整理证据。 |
| 开发与运维 | `code-wiki` | 为代码库生成参考文档和 Mermaid 图。 |
| 开发与运维 | `plan` | 在 `.hermes/plans/` 编写计划，不负责执行计划。 |
| 开发与运维 | `pragmatic-development` | 辅助编写、修复和审查代码。 |
| 开发与运维 | `tools-cleanup` | 检查并清理未使用的工具及安装脚本。 |
| GitHub | `github-auth` | 配置 GitHub CLI、SSH 或 HTTPS 凭据。 |
| GitHub | `github-code-review` | 审查 Pull Request 差异并管理评审意见。 |
| GitHub | `github-issues` | 创建、分流、标记和分配 Issue。 |
| GitHub | `github-pr-workflow` | 协助 PR 分支、提交、CI 与合并流程。 |
| GitHub | `github-repo-management` | 克隆、创建、分叉和维护仓库。 |
| GitHub | `github` | 通过 `gh` CLI 执行常见 GitHub 操作。 |
| 邮件与协作 | `email-inbox-triage` | 整理收件箱优先级并起草回复。 |
| 邮件与协作 | `box` | 搜索、管理 Box 云端文件及元数据。 |
| 邮件与协作 | `meeting-action-items` | 从会议记录提取决定、负责人和行动项。 |
| 邮件与协作 | `document-to-action-items` | 从文件中提取带依据的义务、截止日期和任务。 |
| 邮件与协作 | `weekly-review-planning` | 汇总每周承诺、停滞事项并辅助规划。 |
| 文档与资料 | `nano-pdf` | 按指令编辑已有 PDF 中的文字内容。 |
| 文档与资料 | `ocr-and-documents` | 从 PDF 或扫描件提取文本，依赖对应解析工具。 |
| 文档与资料 | `maps` | 通过 OpenStreetMap/OSRM 查询地理编码、地点、路线和时区。 |
| 文档与资料 | `chrome-page-change-monitor` | 监控现有已登录 Chrome 页面中的变化。 |
| 文档与资料 | `product-price-monitor` | 跟踪商品、航班或列表价格，并按条件提示。 |
| 研究与信息 | `blogwatcher` | 使用 blogwatcher-cli 监控博客及 RSS/Atom Feed。 |
| 研究与信息 | `competitor-news-monitor` | 跟踪指定公司的重要新闻并整理带来源摘要。 |
| 研究与信息 | `grounded-citations` | 用可核验来源支撑回答和文档。 |
| 研究与信息 | `polymarket` | 查询 Polymarket 市场、价格、订单簿和历史数据。 |
| 研究与信息 | `research-paper-writing` | 辅助机器学习论文从研究设计到投稿准备。 |
| 研究与信息 | `fictional-character-distillation` | 从故事语料提炼虚构角色资料；不等于现实互动记忆。 |
| 研究与信息 | `game-narrative-analysis` | 跨章节分析游戏叙事。 |
| 模型与机器学习 | `huggingface-hub` | 通过 `hf` CLI 搜索、下载或上传模型与数据集。 |
| 模型与机器学习 | `evaluating-llms-harness` | 使用 lm-eval-harness 运行语言模型基准测试。 |
| 模型与机器学习 | `weights-and-biases` | 记录机器学习实验、管理 Sweep 和模型资产。 |
| 模型与机器学习 | `llama-cpp` | 使用 llama.cpp 进行本地 GGUF 推理，并发现 Hugging Face 模型。 |
| 模型与机器学习 | `serving-llms-vllm` | 使用 vLLM 部署高吞吐推理服务及 OpenAI 兼容 API。 |
| 消息与设备 | `qq-bot` | 接入 Hermes 官方 QQ Bot 或 OneBot；与本仓库 NapCat 插件是不同实现。 |
| 消息与设备 | `qq-group-members` | 在适用 QQ 群聊中按 QQ 昵称称呼并维护群成员认识记录。 |
| 消息与设备 | `openhue` | 通过 OpenHue CLI 控制 Philips Hue 灯具、房间和场景。 |
| Web 与终端 | `blocked-page-recovery` | 在网页抓取遇到 403、限流或反爬时尝试替代访问方式。 |
| Web 与终端 | `zsh-environment-migration` | 安装 Zsh 或迁移 Shell 环境。 |
| 内容整理 | `distilly` | 将人物相关来源材料整理为可复用的人物档案；来源与推断需区分。 |

其中近期通过 Hermes Skills Hub 或独立来源明确安装、复核过的代表项包括：`code-wiki`（代码库文档）、`humanizer`（文本润色）和 `grounded-citations`（可核验引用工作流）。表中也包含随当前 profile 配置存在的内置、自建或此前安装 Skills；不能仅凭目录存在反推统一来源、版本或已完成全部可选依赖。

## 已启用的 MCP 服务

| MCP | 启动方式 | 用途与实际边界 |
| --- | --- | --- |
| `mobile-mcp` | 维护环境：`npx -y @mobilenext/mobile-mcp@latest`；公开指南固定 `1.0.1` | 通过 Mobile MCP 读取和操作 Android／iOS 设备界面，包括截图、元素、点击、滑动、输入、应用管理与日志。维护环境已启用全部工具并关闭匿名遥测；曾实际识别 USB Android 设备、安装并启动 QQ，并完成授权目标的私聊与群消息发送回读。账号登录、验证码、付费和其他敏感动作仍需按对应授权处理。仓库内 [Mobile QQ Fallback Skill](../skills/mobile-qq-fallback/README.md) 提供脱敏的备用发送流程。 |

`@latest` 会随上游更新，不是可复现版本锁。仓库公开指南因此固定编写时核对的 `1.0.1`，并记录 Node／设备调试前提、授权边界和最小真实测试；维护环境尚未同步锁版，升级仍需重新验收。Skill 不包含 Mobile MCP 软件、设备 ID、账号状态或登录数据。

## 已发现的 Hermes 插件

| 插件 | 类型 / 用途 | 状态说明 |
| --- | --- | --- |
| `hermes-napcat` | OneBot 11 正向 WebSocket QQ 通道适配器；接收并回复消息，不提供模型工具。 | 当前运行环境安装有插件副本；具体功能与限制见本仓库 [NapCat 文档](../plugins/hermes-napcat/README.md)。 |
| `mem0` | Mem0 记忆集成，支持服务端事实提取、语义检索、自动去重及可选重排。 | 插件清单声明依赖 `mem0ai`；是否启用及连接哪个后端取决于 profile 配置。 |
| `hermes-scoped-memory` | 本仓库的实验性分域记忆组件，用于隔离不同来源/范围的记忆数据。 | 属于本项目组件，不是第三方插件；完整生产链路有接线与验收限制，见[组件说明](../plugins/hermes-scoped-memory/README.md)。 |

插件目录或 Skill 文件存在，不等于插件已启用、MCP 已连接或外部账号已授权。不要把当前维护环境的安装状态当成面向所有用户的安装步骤。

## 已安装的独立辅助工具

| 工具 | 用途 | 边界 |
| --- | --- | --- |
| [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) `2.12.0` | 在安装或更新 Skill 前执行静态供应链扫描，检查提示注入、数据外传、危险代码、依赖和记忆投毒等信号。 | 已用 `--no-llm` 对现有 Skill 生成过报告；扫描会误报或漏报，不是运行时拦截器，也不替代许可证、脚本、依赖和权限人工审查。 |

维护环境将 SkillSpector 作为自建工具放在 `~/tools/skillspector/`，而不是复制进本仓库或 Hermes Skills 目录。公开文档不依赖该私人绝对路径；需要复现时应从上游核对版本与许可证并独立安装。

## MCP 使用注意

当前 profile 中，`touchdesigner-mcp` 是一份经 twozero MCP 控制 TouchDesigner 的 Skill 指引；实际启用的 MCP 服务则是 `mobile-mcp`。前者的 Skill 文件存在不证明服务已连接，后者出现在 `hermes mcp list` 也不证明某台设备始终在线。其他具备 CLI 或 API 集成的 Skills 不应仅因能调用外部工具就标记为 MCP。

任何外部工具都应遵循最小权限原则。使用前确认服务来源、权限范围、数据是否发送到第三方以及可能产生的费用；本项目不随源码分发维护者的凭据、服务地址、私有数据或运行配置。

## 更新清单

新增、移除或改名扩展时，先核对当前 profile 的安装清单和扩展自身说明，再更新本页。用途描述保持中性、简短；将“已安装”“已启用”“服务可连通”“真实使用验收”分开记录，不能用其中一项替代其他状态。
