# Hermes Companion

面向 [Hermes Agent](https://hermes-agent.nousresearch.com/docs/) 的模块化集成项目：连接消息通道，组合感知、记忆、技能、人格与外部工具，让使用者搭建自己的智能伙伴。

长期目标是支持伙伴在授权范围内持续探索互联网、阅读、创作、交往和积累可追溯经历，并让经历改善之后的选择。当前已接通连续社交、可恢复的公开阅读、有来源分享及有限反馈试用；具体边界见[本轮实现](docs/连续社交阅读与反馈实现.md)，长期方向见[开发方案](docs/互联网生活能力开发方案.md)。

最新阶段增加[多人请求编排、跨天明确记忆与方法学习](docs/多人请求编排跨天记忆与方法学习.md)：同一话题分别跟踪每人的条件及回应进度，按确认语义延续群／好友记忆，以独立案例验证方法并支持撤销。已有安装使用 `--enhance-social` 显式启用，保留原始采集关闭状态和既有社交范围。

项目不绑定某位角色，也不预设伙伴应该是什么性格。实例的公开自述与逐步形成的认识属于数据，不是身份认证或权限前提。[hermes-social](plugins/hermes-social/README.md) 负责话题参与、阅读与联系活动、实际反馈和可撤销的策略调整。通道不内置具体角色，呼唤名仅用于触发，可分发 Personas 尚未完成。

> **已有 Hermes 和 NapCat 时，从 [INSTALL.md](INSTALL.md) 成套接入。** `python3 scripts/install.py` 自动检查宿主、生成安装计划；加 `--apply --restart` 完成配套插件、补丁、Skills、配置和连接验证。当前支持 Linux 源码宿主，不代登录账号，不部署所有外部服务；Personas 与完整 MCP 方案仍未分发。

给 agent 仓库链接时，可以直接说：“按 INSTALL.md 将这个仓库接入我的 Hermes，先复用现有 NapCat 配置，缺少的账号和群信息集中问我。”

整体方向、项目分层和阶段目标见[项目规划](docs/项目规划.md)。开发前先阅读[可移植性与开发基线](docs/可移植性与开发基线.md)，后续工作见[下一步开发评估](docs/下一步开发评估.md)。[研究与代码参考](docs/研究与代码参考.md)整理斯坦福小镇、Agentopia、Anuttacon 相关公开资料及可借鉴代码，[社交方案](docs/社交能力与分层本地记忆.md)保留第一阶段的具体边界。愿景不等于现成功能；规划变化也不自动授权修改使用者的运行环境。

## 可以用它做什么

- **接入 QQ**：通过 NapCat / OneBot 11 正向 WebSocket 接收与回复消息，配置允许的群、私聊身份及触发方式。
- **在群里自然交谈**：通道独立提供名字／真实 @ 触发；额外启用 `hermes-social` 可对真实未点名消息考虑接话、延续话题，以及在显式目标内考虑空闲开口，也保留安静与退让。日常参与共用通道群白名单，不以显示名判定权限。
- **与普通好友交流**：在独立名单或显式开启的白名单群好友范围内，核验真实好友身份后使用隔离的无工具社交模型；主人私聊保留原私人路径。
- **阅读与有来源分享**：读取当前聊天配置的公开 HTTPS 资料，保留实际来源和摘录，在单独授权的目标内判断是否分享；反馈可形成限时、可回滚的冷却／长度／分享策略试用。
- **处理图片和文件**：在授权范围内分析同条附件或同群明确引用的内容。图片使用辅助视觉模型；文件通过受限解析路径处理，不直接开放主机文件访问。
- **区分私人和公共上下文**：strict 公共群路径不加载主人的私人 agent、SOUL、MEMORY、USER 或历史。工具定义共用主人 NapCat 私聊基准，但每次执行仍按真实身份、数据来源和输出场所授权；群内身份不授予私人资料或管理权限。
- **研究分域记忆**：实验插件提供本地存储、提取和检索等组件；它的多人长期记忆完整链路仍有接线与验收前提，不是默认上线功能。
- **试验同群人物自述**：独立 `hermes-life-memory` 提供显式候选、本人核对、当前发言者召回及修订／忘记；组件默认关闭，统一安装首次基本接入会配置该功能，升级保留原选择。需匹配的通道、social 与完整存储源码，尚未真实 QQ 验收。
- **使用无 API 的本地记忆**：life-memory 使用 SQLite 与规则；scoped-memory 可开启强制 `api_free` 策略。可选的对话／话题状态和规则候选不增加记忆模型调用，旧上下文重建需匹配宿主；见[纯本地阶段说明](docs/纯本地记忆与交流续接.md)。
- **延续共同经历与约定**：可选保存真实交流摘录，由本人确认带时间的约定，在原群经统一额度提起一次并记录实际反馈。可选 `local` 模式不请求 idle 模型；见[当前实现与配置](docs/共同经历约定与联系反馈.md)，尚未真实 QQ 验收。
- **用手机 QQ 备用发送**：当 NapCat 不可用或不适合时，可安装仓库内 Skill，通过独立的 Mobile MCP 操作已登录 QQ 的设备，逐步核对目标、草稿和发出气泡；它不提供持续收信。
- **通过 MoviePilot 搜索和下载种子**：搜索影视作品、比较跨站资源，并在明确授权后下载所选版本；默认只查询，不创建订阅，需连接自己的 MoviePilot 服务。

模型、账号、白名单和密钥由使用者自行配置；云端模型可能产生费用。项目不附带 QQ 账号、模型额度、私人记忆或聊天记录。

## 当前能力与限制

| 组件／能力 | 当前交付 | 使用前必须知道 |
| --- | --- | --- |
| QQ 文本、群成员准入与昵称 | 已有实现、自动化测试及部分真实客户端验收记录 | 需要自己的 NapCat、身份配置与兼容 Hermes 环境；不代表所有场景已验收 |
| 独立主动社交与配套 Skill | `hermes-social` 负责参与策略、统一联系状态和公开风格，首个通道端口为 NapCat | 空闲目标单独授权；约定需本人确认，普通人物档案不供 idle 读取；加好友及其他通道未交付 |
| 图片、同群引用、文件 | 已有实现；图片引用与 TXT 等具体场景有真实使用确认 | 格式、依赖、下载策略、出站授权与部署接线分别配置，不保证任意附件都可解析 |
| 隔离执行与公共问答 | 提供 bridge、worker 及宿主接线相关实现 | 合同、worker、配置必须匹配；profile 本身不是操作系统沙箱 |
| 语音 | 有解码、Whisper 及可选音频模型接口 | 未完成真实 Whisper 推理与全链路验收，不作为默认可用能力 |
| 分域长期记忆 | 提供实验代码、显式本地向量服务接线、个人候选核对命令和离线测试 | provider 群命令需匹配带记忆 worker；多人生产接线仍待验收 |
| 独立人物与交流记忆 | `hermes-life-memory` 复用公共 API，支持本人核对、同群事件／话题、规则候选与依赖撤回 | 组件默认关闭；统一安装基本接入配置显式人物候选，交流采集需另选；受控历史重建需匹配宿主，不代表完整遗忘或生活循环 |
| Mobile QQ Fallback Skill | 已提供可安装 Skill 和接入说明 | 依赖独立安装的 Mobile MCP、在线设备及已登录 QQ；仅是交互式备用出站路径 |
| NapCat QQ Skill | 已提供可安装 Skill 和维护流程，统一安装会同时分发 | 负责通道维护与验收；成套接入由 `hermes-companion-setup` 及安装器完成，仍需自己的 NapCat 环境 |
| MoviePilot Torrents Skill | 已提供 Skill、API 脚本、排错参考和离线测试 | 需显式配置自己的 MoviePilot API 和凭据；只读查询与添加下载分别授权，脚本提交分支仅做模拟验证 |
| QQ／social／本地记忆成套安装 | `scripts/install.py` 与 [agent 接入入口](INSTALL.md) | 检查、备份、安装、配置和激活分层验证；要求兼容的 Linux Hermes 源码与已有 NapCat |
| Personas、完整 MCP 方案与外部服务方案包 | 项目演进方向 | 不将维护环境清单或规划写作现成功能 |

当前安装排查、兼容组合与测试范围见[安装修复与统一接入验收](docs/安装修复与统一接入验收.md)，组件详情见 [NapCat 开发状态](plugins/hermes-napcat/docs/开发状态.md) 和 [分域记忆开发状态](plugins/hermes-scoped-memory/docs/开发状态.md)。测试数量是对应阶段记录，不是每次克隆后自动重跑的结果。

## 从这里开始

**安装或让 agent 接入，优先阅读 [INSTALL.md](INSTALL.md)**。下面保留组件级说明，适用于开发和选择性集成；完整安装不需要逐个复制插件或手工拼接宿主补丁。

### 1. 获取源码

先将 `COMPANION_REPOSITORY_URL` 设置为你选择的源码仓库 URL。

```sh
git clone "$COMPANION_REPOSITORY_URL"
cd hermes-companion
```

本仓库是多个组件的源码集合，不是单个 Hermes 插件。不要把仓库根目录当作插件安装入口，也不要假设一次插件安装命令会启用全部功能。

### 2. 选择需要的组件

| 你的目标 | 先阅读 |
| --- | --- |
| 接入 QQ、配置消息触发和白名单 | [NapCat 插件说明](plugins/hermes-napcat/README.md) |
| 启用自然接话、话题延续或空闲开口 | [社交插件说明](plugins/hermes-social/README.md)、[拆分合同](docs/社交插件拆分.md) |
| 开发、部署或排查 NapCat 通道 | [NapCat QQ Skill](skills/napcat-qq/README.md) |
| 理解群权限与称呼规则 | [群权限与昵称](plugins/hermes-napcat/docs/群权限与昵称.md) |
| 启用图片或文件并确认隔离链路 | [测试部署接线](plugins/hermes-napcat/docs/测试部署接线.md)、[媒体实现与验收](plugins/hermes-napcat/docs/媒体实现与验收.md) |
| 检查已有 NapCat 容器与部署准备 | [部署工具](plugins/hermes-napcat/deploy/README.md) |
| 试验本地分域记忆 | [分域记忆插件说明](plugins/hermes-scoped-memory/README.md) |
| 试验同群人物自述与本人核对 | [人物记忆插件说明](plugins/hermes-life-memory/README.md)、[实现记录](docs/人物记忆首个切片.md) |
| 评估宿主补丁与版本依赖 | [Hermes 宿主接线](integration/hermes-host/README.md) |
| 通过手机 QQ 备用发送私聊或群消息 | [Mobile QQ Fallback Skill](skills/mobile-qq-fallback/README.md) |
| 搜索影视种子、比较站点资源并下载所选版本 | [MoviePilot Torrents Skill](skills/moviepilot-torrents/README.md) |

完整导航见 [文档入口](docs/README.md)。维护环境中已安装的外部 Skills、MCP 服务、辅助工具及 Hermes 插件用途，见[外部扩展清单](docs/外部Skills与MCP插件说明.md)；该清单不是本仓库的安装依赖或发行内容。安装命令、配置字段和实际限制以所选组件文档为准，不必为了使用 QQ 通道就启用实验性记忆功能。

### 3. 准备环境并完成配置

- 安装 Hermes Agent，准备可用的对话模型；基础安装参考 [Hermes 官方文档](https://hermes-agent.nousresearch.com/docs/getting-started/installation)。
- QQ 通道需要可访问的 NapCat / OneBot 11 服务、认证 token 和允许的账号／群配置。
- 隔离及媒体能力需要额外运行条件。当前实现包含 Linux / bubblewrap 路径，不能据 Python 可运行就认定所有平台兼容。
- 图片辅助模型、音频及记忆后端各有独立依赖与出站条件；只配置实际需要的部分。
- `integration/hermes-host/` 包含针对特定宿主基线的补丁与验证工具。先核对所选功能的依赖和版本，不要向任意 Hermes 版本直接应用补丁。

只从脱敏示例生成自己的配置，不复制维护者的运行现场。密钥保留在本地凭据环境，不写入源码、提交或问题报告。

### 4. 按层验收

先完成离线测试与配置检查，再连接自己的测试群验证文本、触发、昵称、图片和文件。还应检查非白名单来源被拒绝、私人上下文不进入群聊、撤回／取消后不产生迟到回复。

自动化通过、文件已安装、网关已加载、真实 QQ 验收是不同层级；不要只凭其中一项宣称完整可用。同一 OneBot 连接只保留一个接收者，避免新旧实例同时接管。

## 仓库结构

```text
plugins/
  hermes-napcat/          QQ 通道、媒体编排与隔离接线
  hermes-social/          独立参与策略、运行状态及 bundled Skill
  hermes-life-memory/     独立人物自述服务，默认关闭
  hermes-scoped-memory/  实验性分域记忆及相关 worker
integration/
  hermes-host/           Hermes 宿主补丁与集成验证
skills/
  hermes-companion-setup/ 成套安装、升级与检查的 agent Skill
  napcat-qq/             NapCat 接入、排障、部署与验收流程
  mobile-qq-fallback/    Mobile MCP 手机 QQ 备用发送流程
  moviepilot-torrents/   MoviePilot 作品／种子搜索、资源比较与授权下载
docs/                   文档导航与跨组件边界
scripts/install.py      成套安装入口，默认只输出计划
tests/                  安装器事务、兼容及隔离 profile 集成验证
```

后续按实际交付继续加入 Skills、Personas、MCP 接入指南与可安装方案，不预建空模块。现有 `napcat-qq`、`mobile-qq-fallback` 与 `moviepilot-torrents` 是具体任务 Skills，不代表已经形成通用 Skill 市场或完整 MCP 发行体系。`personas/` 的定位是容纳任意可合法分发的人格或模板，不指定默认人物，也不导入维护者的个人关系和经历。选择人格不应授予工具权限或改变数据访问边界。

## 源码、安装与运行数据

- 仓库保存源码、测试、脱敏示例和可公开文档。
- Hermes 管理的插件安装位置是 `$HERMES_HOME/plugins/<插件目录>/`；源码移动或改名不会自动更新已安装副本。
- 配置、运行目录、数据库、模型缓存、凭据、QQ 登录状态、聊天记录及备份留在仓库之外。不要把开发任务日志或整份个人 profile 作为发行包上传。
- `HERMES_HOME` 随使用的 profile 解析；不要把维护者的用户名、绝对路径或 Mem0 用户标识作为通用配置。
- 项目改名不会自动迁移现有运行目录、重启服务或修改用户的 SOUL.md。

详细安全边界见 [NapCat SECURITY](plugins/hermes-napcat/SECURITY.md) 与各组件文档。任何隔离承诺都应由代码与实际接线验证，而不是依赖人格提示词。

## 开发与验证

使用装有 Hermes 及对应组件依赖的 Python 环境。在仓库根目录运行 NapCat 离线测试：

```sh
python3 -m unittest discover -s tests -v
python -m unittest discover -s plugins/hermes-napcat/tests -v
python -m unittest discover -s plugins/hermes-social/tests -v
python -m unittest discover -s plugins/hermes-napcat/deploy -p 'test_*.py' -v
python3 scripts/check_portability.py
git diff --check
```

这些命令不替代真实 QQ 和模型服务验收。隔离测试、记忆测试及宿主补丁验证的额外前提与命令见各组件 README；缺少依赖或发生 skip 时应如实报告。

欢迎贡献可独立使用的插件、部署改进、文档和测试。修改应保持：

- 消息适配器不绑定具体人格或记忆后端。
- 身份、触发、能力授权与记忆归属分别处理；昵称和模型输出不能授予权限。
- 新能力同时说明配置前提、成功路径、失败边界及验证层级。
- 不提交密钥、真实个人资料、原始会话和运行数据库；引入第三方内容时核对来源与许可证。

## 许可证与来源

自有代码与文档采用 [MIT 许可证](LICENSE)。各插件目录保留独立 LICENSE，方便单独分发。Hermes Agent、NapCat、第三方依赖、引用材料及未来的人格素材遵循各自许可证，本仓库的 MIT 声明不替第三方重新授权。

本项目是围绕 Hermes 的社区集成项目，不是 Hermes 官方发行版。
