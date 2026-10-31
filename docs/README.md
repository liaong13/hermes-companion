# Hermes Companion 文档入口

先按你要完成的事情选入口，再查架构、开发状态与历史记录。项目定位和当前交付范围见 [根 README](../README.md)。

> QQ／social／本地记忆完整接入优先读 [统一安装与 agent 入口](../INSTALL.md)。安装器提供匹配组件、宿主补丁、Skills、配置及连接检查；Mobile MCP、MoviePilot、人格包与其他外部服务仍按各自说明接入。

- [项目规划](项目规划.md)：整体定位、能力分层、当前交付和阶段目标。
- [互联网生活能力开发方案](互联网生活能力开发方案.md)：持续活动、自己的经历、人物记忆、社交、反思与学习的完整主线，当前先完成社交和记忆。
- [研究与代码参考](研究与代码参考.md)：斯坦福小镇、Agentopia、Anuttacon 公开资料及其他论文；固定版本代码、函数入口与移植取舍。
- [本地 Hermes 能力核对](本地Hermes能力核对.md)：脱敏记录安装现状、仓库差异和宿主可复用入口，不将配置存在当作运行验收。
- [人物记忆首个切片](人物记忆首个切片.md)：M1 首次源码实现、原始自述命令、临时上下文、撤销与离线验证边界。
- [纯本地记忆与交流续接](纯本地记忆与交流续接.md)：无 API 的本地方案、公共接口、对话／话题、规则候选、派生撤回及匹配宿主验证。
- [共同经历、约定与联系反馈](共同经历约定与联系反馈.md)：M2 首个闭环，明确约定、无模型机会、统一额度、实际 @ 回读、反馈与撤销；包含当前版本和配置。
- [可移植性与开发基线](可移植性与开发基线.md)：源码、宿主、测试依赖与数据目录的配置契约，以及公开材料的脱敏边界。
- [安装修复与统一接入验收](安装修复与统一接入验收.md)：导入依赖、来源记录、发送及 social 合同修复，当前配套版本、安装器测试和运行验证范围。
- [下一步开发评估](下一步开发评估.md)：当前代码差距、建议优先级、交付切片与验收条件。
- [社交能力与分层本地记忆](社交能力与分层本地记忆.md)：开源参考、本次四项修复、后续人物记忆分层与社交闭环的验收顺序。
- [社交拆分验收](社交插件拆分验收.md)：独立插件、实际测试与重载／QQ 验收边界。
- [外部扩展清单](外部Skills与MCP插件说明.md)：维护环境中的第三方 Skills、MCP、辅助工具和插件分别用于什么；不是仓库依赖表。

## 接入与使用

| 要做的事情 | 文档 | 阅读重点 |
| --- | --- | --- |
| 让 agent 从仓库链接完成成套接入或升级 | [INSTALL.md](../INSTALL.md) | 默认计划、显式应用／激活、旧配置保留、功能选择与失败恢复 |
| 通过 NapCat 接入 QQ | [NapCat README](../plugins/hermes-napcat/README.md) | 安装、连接、身份与触发配置 |
| 启用自然接话、连续交流和空闲开口 | [hermes-social](../plugins/hermes-social/README.md)、[拆分合同](社交插件拆分.md) | 独立插件、配套 Skill、通道能力与记忆边界 |
| 试验本人核对的同群人物资料 | [hermes-life-memory](../plugins/hermes-life-memory/README.md) | 组件默认关闭、安装方案的显式候选、匹配源码与本人确认；普通采集另选 |
| 开发、部署或排查 NapCat | [NapCat QQ Skill](../skills/napcat-qq/README.md) | 源码／安装／运行分层、权限、媒体、回归与真实验收 |
| 准备已有容器与部署参数 | [部署工具 README](../plugins/hermes-napcat/deploy/README.md) | 工具的实际操作范围、凭据与回滚边界 |
| 配置群成员权限与称呼 | [群权限与昵称](../plugins/hermes-napcat/docs/群权限与昵称.md) | 白名单群、普通成员、主人身份与当前群昵称 |
| 接入公共群问答与媒体链路 | [测试部署接线](../plugins/hermes-napcat/docs/测试部署接线.md) | bridge、worker、合同、隔离目录和配置匹配 |
| 使用图片或文件 | [媒体实现与验收](../plugins/hermes-napcat/docs/媒体实现与验收.md) | 支持格式、下载／出站授权、沙箱及真实验收边界 |
| 判断哪些场景已验证 | [NapCat 开发状态](../plugins/hermes-napcat/docs/开发状态.md)、[权限部署验收](../plugins/hermes-napcat/docs/权限部署验收.md) | 以最新阶段的具体场景为准，不将局部成功扩大到全部能力 |
| NapCat 不适用时通过手机 QQ 备用发送 | [Mobile QQ Fallback Skill](../skills/mobile-qq-fallback/README.md) | Mobile MCP 接入、目标核对、发送前草稿检查和发送后回读 |
| 搜索种子、按版本比较站点并授权下载 | [MoviePilot Torrents Skill](../skills/moviepilot-torrents/README.md) | 自有服务与凭据配置、只搜索边界、一次性提交和下载状态核对 |

部署文档中的现场记录是证据，不是可直接复制的通用配置。自己的账号、路径、白名单、模型与密钥需要重新配置。

## 分域记忆：实验性组件

当前公共群聊天路径不等于已启用多人长期记忆。不要为了接入 QQ 而直接启用全部记忆选项。

| 要了解的内容 | 文档 |
| --- | --- |
| 插件入口、配置与限制 | [分域记忆 README](../plugins/hermes-scoped-memory/README.md) |
| 当前实现与阻挡项 | [开发状态](../plugins/hermes-scoped-memory/docs/开发状态.md) |
| 0.2 数据流、配置与验收边界 | [实现与验收](../plugins/hermes-scoped-memory/docs/实现与验收.md) |
| 接线、公共事实和剩余部署项 | [后续需求处理](../plugins/hermes-scoped-memory/docs/后续需求处理.md) |
| 已运行的测试及未验证部分 | [测试报告](../plugins/hermes-scoped-memory/docs/测试报告.md) |
| 宿主补丁、版本基线与集成验证 | [Hermes 宿主接线](../integration/hermes-host/README.md) |

## 架构与安全边界

各组件的职责不能相互替代：

- **NapCat 通道**处理协议认证、可信事件身份、场所／成员准入、触发、消息收发与媒体编排。显示名只用于称呼，不用于认证。
- **独立社交运行插件**处理是否参与、合并、节流、暂停、空闲机会、近期上下文和公开风格；通过通道的受限端口接线，不连接第二个 OneBot，不复制群白名单，不持有私人 agent 或继承旧消息许可。其 bundled Skill 提供方法，不代替执行器。
- **宿主与隔离执行链路**负责上下文、工具和调用能力的强制边界；不能因主模型或向量服务故障就回落到私人 agent。
- **scoped-memory**处理获准范围内的记忆存储、提取和检索；记忆隔离不能单独保证主模型历史、文件或工具隔离。
- **life-memory**复用 Store 处理本人自述候选和当前群回应的授权视图，通过原通道临时上下文接线；不替换 memory provider，不给 idle 提供人物档案。
- **Skills、未来 Personas 与 MCP 指南**提供任务知识、可选表达和外部工具接法，不授予身份、工具或数据权限，不以任意具体人物或维护设备为默认前提。当前 `mobile-qq-fallback` 只定义备用发送流程，Mobile MCP 仍须独立安装授权。

同一 OneBot 连接只保留一个接收者；profile 分开也不等于 OS 沙箱。隔离能力涉及的 Linux / bubblewrap 条件、宿主接口和 worker 合同须分别核对。

进一步阅读：

- [NapCat 安全说明](../plugins/hermes-napcat/SECURITY.md)
- [NapCat 接入与隔离设计](../plugins/hermes-napcat/docs/接入与隔离设计.md)
- [群聊媒体接收设计](../plugins/hermes-napcat/docs/群聊媒体接收设计.md)
- [分域记忆开发设计](../plugins/hermes-scoped-memory/docs/开发设计.md)
- [OpenRouter 嵌入模型选型](../plugins/hermes-scoped-memory/docs/OpenRouter嵌入模型选型.md)

设计文档包含提案与历史取舍，不能覆盖代码现状和最新验收结论。模型目录与选型资料也不等于效果、可用性或费用实测。

## 如何判断“已经完成”

1. **已实现**：能在源码中找到对应行为，说明配置及失败路径。
2. **自动化通过**：记录命令、环境、测试范围、失败和 skip；模拟服务不代表真实服务。
3. **已安装**：目标文件、配置和匹配 worker 已核对，不代表运行进程已加载。
4. **运行生效**：确认网关加载了目标版本及正确链路。
5. **客户端验收**：用真实账号和明确授权场景核验结果，不推广到未测格式、来源或云端效果。

本文不重新核验维护者的生产环境。组件状态页和历史报告记载的是各自阶段的事实；出现前后差异时，优先查看最新记录及其适用范围。

## 历史与维护

- [NapCat 开发日志](../plugins/hermes-napcat/docs/开发日志.md)
- [NapCat 历史测试记录](../plugins/hermes-napcat/docs/历史测试记录.md)
- [分域记忆开发日志](../plugins/hermes-scoped-memory/docs/开发日志.md)
- [分域记忆历史问题评估](../plugins/hermes-scoped-memory/docs/记忆系统问题与改进建议.md)

公开文档使用中文，命令使用显式变量和仓库相对路径。历史结果保留发生时间、数量与适用范围，个人路径、主体标识和备份位置使用通用占位，并标明脱敏。新增组件先提供准确的 README、测试与脱敏示例；不要把本机任务目录、原始日志、凭据、用户资料或数据库作为安装依赖。

自有文档采用 [MIT 许可证](../LICENSE)，第三方材料遵循原授权。
