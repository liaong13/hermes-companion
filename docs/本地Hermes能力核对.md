# 本地 Hermes 能力核对

> 本页保留开始开发前的安装快照。此后四个完整组件已安装并完成一次修复与正式重载；当前核对范围见[安装修复与统一接入验收](安装修复与统一接入验收.md)。下列旧版本、缺失目录及“未同步”不作为当前安装结论。

日期：2026-10-31。只读检查用户指定的 `~/.hermes` 和当前仓库。以下使用 `$HERMES_HOME` 表示当时检查的 default home，`$HERMES_SOURCE` 表示其宿主源码，`$SOCIAL_HOME` 表示配置指定的独立社交执行目录；不发布真实绝对路径、账号、群号、服务地址或凭据。

本页是开始实现前的安装快照；随后仓库新增 `hermes-life-memory 0.1.0`，NapCat／存储源码升至 `0.3.3/0.2.2`，见[实现记录](人物记忆首个切片.md)。这些源码修改没有同步到本页所述安装环境。

## 核对边界

读取了配置中与能力选择有关的字段、插件清单／代码比较、宿主相关源码和目录结构。没有读取 `.env`、`auth.json`、密钥文件、真实会话正文、Mem0 语料或私人 SOUL 正文；未连接 QQ、调用模型、创建任务或改动运行环境。配置与文件存在证明安装条件，不能证明当前进程已经加载或客户端行为已验收。

| 对象 | 观察结果 | 对开发方案的影响 |
| --- | --- | --- |
| Companion 仓库 | 起点 `历史修订`，工作区干净 | 社交 `0.1.1`、记忆 `0.2.1` 修复已有提交和离线记录 |
| 宿主源码 | HEAD `5894e750c3dba980d43b25aa29e3958149ba2c61`，另有 18 个已跟踪文件的本地差异 | 精确基线是 HEAD 加本地修改，不能只报 HEAD；本轮保留全部差异 |
| default 记忆选择 | `memory.provider=mem0`，内置 memory／user profile 两开关为 true | 私人链路已有记忆，不以新社交库替换它；新活动不能默认继承私人上下文 |
| 启用插件配置 | `hermes-napcat`、`hermes-social`、`mem0` | scoped-memory 不在启用列表 |
| 已安装 social | 清单 `0.1.0`；controller、routing、manifest 与仓库不同 | 已提交的 `0.1.1` 缺陷修复尚未同步到安装副本 |
| 已安装 scoped-memory 目录 | 只有 `isolated_worker.py`，没有 provider 清单、Store、命令或向量模块；worker 与仓库也不同 | 这是公共执行链复用 worker，不是完整 `0.2.1` memory provider 安装 |
| default scoped-memory 配置 | 不存在 `scoped-memory.json` | 新向量与个人候选命令未在 default 启用 |
| NapCat 配置 | strict 隔离、`social_agent`、runtime contract 5；工具集配置为 `hermes-cli` | 保留真实事件准入与原出站口；工具集配置不单独证明某次工具可用 |
| 独立社交 home | 与 default 不同；配置为 schema 2、local-only、policy revision 5、`host_contract=4` | runtime contract 5 与 worker host contract 4 是不同字段，不要求数字相同；此 prepare 路径仍不等于长期记忆 |
| 社交策略 | enabled=true，日额度 3，idle 阈值 1800 秒，两个精确 idle 目标 | 不通过增加目标或频率代替社交能力建设 |
| 外部能力 | 已配置 Mobile MCP；存在 Skills、cron、knowledge 等目录 | 可继续审阅并复用具体入口；目录和配置不证明设备在线、任务正在运行或能力已经授权给生活实例 |

## 当前宿主中可复用的机制

以下路径相对 `$HERMES_SOURCE`；它们是本次静态核对的实际入口，不要求向任意新版宿主照搬。

| 入口 | 已核对的语义 | 应如何使用 |
| --- | --- | --- |
| `hermes_cli/plugins.py:PluginContext.spawn_task` | 受监督协程，卸载／强制重载会取消，需要当前事件循环 | social／生活层短期后台处理；不把协程存在当持久活动完成记录 |
| `cron/scheduler.py:_construct_cron_agent` | 新建任务 Agent，`load_soul_identity=True`、`skip_memory=False`；项目上下文是否加载取决于 workdir | 生活活动在专属受限 home 执行，明确提供可公开人格和记忆策略，不能直接挂私人 default |
| `cron/scheduler.py:_resolve_cron_disabled_toolsets` | cron 默认限制再调度、交互式 messaging／clarify，并叠加 denylist | 复用现有防循环机制，发送经受限通道，不放开任务自复制 |
| `cron/scheduler.py:_merge_mcp_into_per_job_toolsets` | 会将启用的 MCP 并入任务工具集，存在 `no_mcp` 控制 | 专属活动显式定义 MCP 范围；“只列几个工具集”不保证其他 MCP 不进入 |
| `cron/scheduler_tick.py`、`cron/occurrences.py` | tick 锁、执行归属／租约和 occurrence 管理 | 复用跨进程唤醒与任务生命周期；活动仍需独立进度、结果证据及发送状态 |
| `hermes_cli/goals.py`、`gateway/run_goals.py` | 单会话目标、完成判断和继续执行 | 承载一次有终点的阅读／创作任务，不把“永远生活”设置为无终点目标 |
| `hermes_cli/heartbeat.py` | 会话内周期唤醒，进程／会话需可用，真实用户消息优先 | 会话辅助；跨进程活动恢复使用持久调度 |
| `hermes_cli/loops.py` | 按次数／结束条件唤醒，存在退避和停止上限 | 限期观察，不建立无限空转的生活循环 |
| `agent/periodic_scheduler.py` | 进程内维护计时器，保留任务 Context，非持久调度 | 维护用途，不充当生活活动数据库 |
| `plugins/memory/__init__.py:_ProviderCollector`、`hermes_cli/plugins.py` | 有通用注册和 command 转发，本次未找到所需的 `bound_llm`／`bound_session` 正式接口 | 继续按仓库匹配补丁做隔离验证，不能凭代理存在认为自动提取已经接通 |

官方相应文档见[研究与代码参考](研究与代码参考.md#hermes-官方资料与本地代码的优先级)。用户安装的本地 skill 可作为入口索引，但与源码冲突时以当前执行路径为准；cron 的记忆加载就是本次发现的具体差异。

## 下一次进入实现／部署时的核对顺序

1. 保留宿主已有差异，记录相关文件内容哈希与实际解释器；不要用干净 HEAD 副本冒充现场。
2. 固定 Companion 三组件、宿主修改和 worker 的匹配组合，在临时 home 复验注册、scope、工具、取消和卸载。
3. 为人物记忆定义独立的 local-only 服务入口；不直接改 default 的 Mem0、内置记忆、SOUL 或其他私人数据。
4. 社交准备分支与带记忆分支先形成显式版本合同，再决定部署切片；单纯复制完整 provider 不能让合同 5 自动接入记忆。
5. 经具体部署授权后才安装／切换／重载，并分别验证安装文件、活动进程与真实客户端。

本轮只形成评估和中文开发文档，不更新插件安装、宿主补丁、profile 配置、定时任务或记忆数据。
