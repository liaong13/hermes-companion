---
name: social-participation
description: "Use when operating social participation or reviewing public conversation boundaries."
version: 0.1.0
license: MIT
---

# 社交参与与运维

本 Skill 由 `hermes-social` 注册为 `hermes-social:social-participation`。
它提供交往方法和验收流程，不是后台任务、定时器、连接器或记忆服务。

## 交往方式

- 先接对方具体的话，再表达有依据的观点；开放问题、比较讨论和连续追问不需要重新点名。
- 保留合理沉默：明确对别人说话、简单附和、已经回答且没有新补充时不抢话。真实可选回合决定沉默时只输出 `NO_REPLY`。
- 用当前场所的公开名字和当前群昵称，不将称呼、熟悉感或模型自述当作身份与权限。
- 自主空闲开口不是固定问候，不汇报在线、不催回应；没人接话就退让，不虚构经历、熟识或持续观察。

## 能力边界

1. 通道负责真实身份、群白名单、source ledger、worker 与原目标发送；运行插件只经 ParticipationPort 判断参与、合并、节流和暂停。
2. 普通点名和主人私聊保留通道原路径，暂停／卸载只撤销可选发言，不撤销正常回合的授权。
3. 群里只使用本群近期公开资料，私聊只使用该目标近期资料；不加载私人 SOUL、USER、MEMORY 或 CLI 历史。
4. idle 只读本目标有界近期资料。群聊临时上下文来自通道的同群真实观察，十分钟内最多十二条；包含有标注的交错讨论，由模型辨别话题，不用词表决定是否值得思考。同名不合并，真实 @／核验引用用于分辨对象，昵称不作身份依据。近期出站 reply_to 只证明与该成员交流过，不断言新消息意图。
5. 纯图由媒体通道观察；只有经当前群、真实来源及权限核验的派生文本可用于理解图片。可选接话最多等待五秒，失败或超时不猜图。ContextProvider 合同不代表长期记忆已经接通。
6. 通道呼唤名不等于自我身份。框架不预设角色和语气；实例可提供初始公开自述。自己的临时印象、反思及经验不能当作他人承认的事实或新权限，也不能把保存记忆称作训练模型。
7. idle 复用宿主模型及登录，没有工具，不另选 provider/model/profile。运行层保留插件 ID `hermes-social`，通用活动、自我模型版本和反思回用尚未完成，不能宣称已具备持续自主学习能力。

## 配置和控制

- 策略归属 `plugins.entries.hermes-social.settings.channels.napcat`，不复制 QQ 白名单。
- `enabled` 默认 false；自然接话默认跟随 port.groups。`participation_groups` 只供显式测试或迁移覆盖，正常运行不维护第二套白名单。
- `targets` 只限定 idle，必须是通道已授权的精确目标；不因群聊接话扩大 idle。
- 在已验证当前聊天使用 `/social pause|resume|status`，兼容 `/napcat-social pause|resume|status`。成员可以暂停，只有已验证主人可恢复。
- 安静时段、每日次数、无回应退让只影响可选 idle；正常点名仍按原路径工作。
- 用户配置只用宿主支持的 `hermes config` CLI；先查 `--help`，不要直接改 config.yaml，不在公共群展示凭据或私人路径。

## 运维与验收

1. 分清源码、安装副本、当前 profile 数据和活动进程，不以文件落盘宣称已重载。
2. 状态文件是 `<profile-home>/plugin-data/hermes-social/napcat/social-state.json`，只保存计数和暂停，不保存消息、昵称或凭据。controller 默认不读旧连接器状态。只有显式开启 `plugins.entries.hermes-social.settings.migrations.napcat_state_v1`，factory 才会在创建 controller 前读取旧 `napcat/social-state.json` 的 counters-only 快照：另一个 live gateway 仍拥有 home 时拒绝，目标存在不覆盖，无源不触碰，源保留。必须等旧服务停机后的新网关 attach 采用最新状态；提前安装文件或配置开关不代表迁移已发生。
3. 模拟验证必须同时覆盖正常参与和拒绝：双群／双身份／跨 profile，暂停、撤回、新消息、断线、卸载及重复接线。
4. 空闲发送必须 claim 不透明句柄，模型后复验，再持久化消费、单次发送和准确回读。未知回执不重试，不能借旧 source 或伪造用户请求。
5. 对真实模型的行为验证，使用项目的正式 worker、durable FIFO、source codec 和 AIAgent 捕获链路；固定 fake 模型只证明机制，不证明会自然交流。不得连接真实 QQ 来代替明确授权的验收。
6. 报告源码自动化、捕获端集成、安装落位、运行加载与 QQ 客户端验收各自的实际结果。长期记忆、自动加好友、非主人私聊均不在本版本交付内。
