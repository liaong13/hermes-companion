# Hermes Companion · NapCat 接入组件

通过 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 将 QQ 好友私聊和群聊接入 [Hermes Agent](https://github.com/NousResearch/hermes-agent)。本组件属于 [Hermes Companion](../../README.md)，插件名为 `hermes-napcat`，平台配置名为 `napcat`，清单版本为 `0.7.0`。

`0.7.0` 将群与普通好友正文接入实际回复覆盖检查，并支持按原会话隔离的跨天明确记忆。请求只有在真实送达 ACK 后才更新状态；详见[多人请求编排、跨天记忆与方法学习](../../docs/多人请求编排跨天记忆与方法学习.md)。

**成套安装或升级优先使用 [INSTALL.md](../../INSTALL.md)**，由统一入口配齐插件、完整数据层、宿主增量、Skills、来源和配置。下面的暂存及手动配置保留给组件开发与选择性集成，不是完整安装命令。当前安装修复与验证范围见[验收记录](../../docs/安装修复与统一接入验收.md)。

`0.5.0` 提供约定机会端口：只接受独立记忆服务的已确认约定，检查原群成员、固定 @ 对象和真实回读；复用 social 统一额度及唯一出站能力。取消／未知／晚到 ACK 不重试或恢复已撤销原文，见[本阶段配置与验证](../../docs/共同经历约定与联系反馈.md)。

`0.4.0` 增加显式布尔 `platforms.napcat.extra.local_conversation_context`，默认 false。开启须使用 strict 合同 5 和[匹配宿主增量](../../integration/hermes-host/napcat-local-memory-context.md)；该适配器所有公共群回合从当前有效本地事件重建，跳过旧历史与 Agent 缓存，缺少合同则拒绝。人物资料、对话记录及规则候选仍由独立插件配置，不因通道开关自动采集。

`0.3.3` 增加可选人物服务合同 1，供独立 [hermes-life-memory](../hermes-life-memory/README.md) 接入原始本人命令和公共群临时上下文。默认不附着服务、不启用长期记忆；需合同 5、已启用 social 与匹配存储源码，尚未真实 QQ 验收。

Hermes Companion 是可组合的接入、隔离与记忆方案，不要求特定人格。`0.3.0` 将合同 5 的主动参与、状态与公开风格拆至独立 [hermes-social](../hermes-social/README.md)，本插件只提供可信来源、隔离接线与受限通道能力。历史合同 4 仍保留固定问答文案，修改唤醒词不改变该旧人格；可分发 Personas 尚未完成。公共路径默认不继承私人 SOUL；合同 5 可显式启用[共享人格与日常记忆](docs/共享人格与日常记忆.md)，沿用当前 profile 的 `SOUL.md`，日常记忆先在私密侧筛选与独立审核。

## 能力与验收状态

- 基本文本通道已确认真实 QQ 登录、好友私聊和群聊收发；实现账号校验、自动重连、短期去重、长文本分块和可选私聊输入状态。
- `strict` 路由区分精确主人私聊与社交群聊。显式 `host_llm` 工厂合同 4 下，主人私聊保留宿主原生 agent／工具／记忆，**所有群发言（包括主人）只走无工具、无长期记忆、无跨轮历史的单次问答**。
- 群媒体默认关闭；可选图片、受限文件提取和语音路径。当前开发状态记录了用户确认的普通群友回复、同群第三人图片引用、TXT 读取、私聊测试标记不进入群聊，以及撤回后没有后续回复。这些是特定场景结果，不是所有格式、后台取消、供应商删除或完整隐私认证。
- 正式群消息准入修复已有实际重启与用户确认的 QQ 文本回复验收；该阶段的测试数量见[群回合与持久化准入修复](../../integration/hermes-host/napcat-public-turn.md)。本轮安装修复完成 47 项定向回归及回环模型群回合验证，未重跑全部媒体套件或发送真实 QQ 消息，详见[开发状态](docs/开发状态.md)。引用机器人文字的连续场景仍有缺失，不据文本回复成功宣称全部引用、工具或图片已验收。
- **社交长期记忆不是本模式的已启用能力。** [scoped-memory](../hermes-scoped-memory/README.md) 的本地记忆 worker 与这里的无记忆 prepare worker 是两条不同路径；前者仍有宿主接线、部署与生产验收阻挡。

## 架构与信任边界

```text
QQ ↔ NapCat OneBot11 正向 WebSocket 服务 ← Hermes 外置客户端
```

Hermes 主动连接 `ws://127.0.0.1:13011`。事件、`get_login_info`、发送动作与 echo 响应使用同一 socket；不使用反向 WS、HTTP API 或宿主监听端口。端点只接受带显式端口的字面量回环 IP（`127.0.0.1` 或 `[::1]`），禁止 URL 凭据、查询参数及重定向，不读取代理环境变量。容器端口必须映射到 Hermes 所在主机的回环地址。

身份与展示名分开处理：

- 群聊展示名按群名片 → QQ 昵称 → QQ 号回退，不使用好友备注；私聊按机器人设置的好友备注 → QQ 昵称 → QQ 号回退。
- 展示名写入 `user_name`／`source.user_name`，其他名称字段进入宿主支持的 `channel_context`，不添加自定义入站 `metadata`。
- 好友备注只在授权私聊到达后通过 `get_friend_list` 只读补充，仅缓存白名单用户名称；成功缓存 5 分钟，失败至少间隔 1 分钟重试，最多等待 2 秒。失败不丢消息，修改备注可等刷新或重启。
- 名称与群管理 role 不授予权限。QQ 号是身份匹配键；公共群问答按当前群显示名称称呼，不读取私人称呼。

## 安装前准备

需要 Python ≥3.11、实际 Hermes 运行环境中的 `aiohttp>=3.12,<4`，以及支持外置 platform plugin 的宿主。参阅 [官方平台适配器契约](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)。不要向系统 Python 盲装 Hermes 或假设旧 `venv` 就是网关使用的解释器。

`host_llm` 路径核对的宿主源码基线为 `5894e750c3dba980d43b25aa29e3958149ba2c61`，依赖正式 `ctx.llm.acomplete`、卸载与处理生命周期接口，并在运行时检查签名和实际 sandbox probe。**这条无记忆路径不要求应用 scoped-memory 的宿主补丁**；记忆提供者的 `bound_llm`／`bound_session` 依赖另见 [宿主集成](../../integration/hermes-host/README.md)。不同版本不能只凭版本号宣称兼容。

以下路径均为示例，先替换为本机实际值：

```sh
export COMPANION_SOURCE="$HOME/tools/hermes-companion"
export HERMES_SOURCE="/path/to/hermes-agent"
export HERMES_PYTHON="/path/to/actual/hermes/python"
# 指向目标 profile，而不是默认假设私人 default
export HERMES_HOME="/path/to/approved/profile-home"
cd "$COMPANION_SOURCE/plugins/hermes-napcat"
python scripts/stage_install.py  # DRY RUN，不写文件
# 显式复制到一个尚不存在、非活动安装的暂存目录
python scripts/stage_install.py --destination /path/to/new/staging/hermes-napcat --apply
```

暂存脚本拒绝已存在目录及 plugins／profiles／含 `.hermes` 的目标，不安装依赖、不改配置、不重启服务。部署时经审阅将脚本列出的完整文件集放到 `$HERMES_HOME/plugins/hermes-napcat/`；不要遗漏 `runtime_bridge.py`、`host_runtime.py`、`process_owner.py`、`social_memory.py` 和 `media*.py`，包元数据版本须与清单一致。更新前备份并保留本地修改。

host runtime 还需同版兄弟目录 `hermes-scoped-memory/isolated_worker.py`，暂存脚本不复制它。**分发该 worker 不等于选择或启用 `scoped-memory` 提供者。** 源码 checkout、profile 插件安装目录、NapCat 账号数据与独立社交运行目录必须分开；运行数据和凭据不进 Git。

## 配置：先保持关闭，再逐项启用

[默认关闭模板](examples/config.disabled.yaml)和[host_llm 候选模板](examples/config.host-test.disabled.yaml)用于审阅与合并，不能整体覆盖现有配置。所有号码均应替换为部署者自己的值。凭据唯一来源为实际网关进程环境中的 `NAPCAT_ACCESS_TOKEN`，与 OneBot 服务端一致；通过受保护的目标 profile `.env` 或服务环境注入，勿放进命令行、仓库或日志。WebUI token 是另一份凭据，终端环境也不会自动传给后台服务。

最小结构示意（不是多人安全部署配方）：

```yaml
platforms:
  napcat:
    enabled: false
    gateway_restart_notification: false # strict 没有启动通知的当前回复许可
    extra:
      ws_url: ws://127.0.0.1:13011
      self_id: "100000001"   # 示例机器人 QQ
      owner_id: "100000002"  # 示例主人 QQ，不能与机器人相同
      allowed_users: ["100000002"]
      allowed_groups: []
      group_admission: allowed_users
      require_mention: true
      typing: false
```

通过目标 profile 的 Hermes 配置命令合并，例如：

```sh
hermes config set platforms.napcat.extra.ws_url 'ws://127.0.0.1:13011'
```

strict 接入另设置 `platforms.napcat.gateway_restart_notification=false`；统一安装会处理该项。发送被拒绝时只返回安全错误码，未知送达或拒绝不走宿主纯文本 fallback，以免重复发送；不通过放宽回复许可来允许启动通知。

### 准入与触发

- `self_id`、`owner_id` 必须显式配置且不同；主人必须显式加入 `allowed_users`。`allowed_groups: []` 拒绝所有群。
- 默认 `group_admission: allowed_users` 同时校验用户与群。显式 `all_members` 仅开放白名单群内、经认证 OneBot 事件的真实成员输入，必须有 strict 公共合同 4；它不开放 DM，也不扩大群清单。不要把全部群友加进 DM 白名单。
- 默认 `require_mention: true` 只认指向机器人的真实 at segment；`@all`、伪 CQ 文本和仅引用不算。显式 `group_trigger: {mode: name_or_mention, name: 小助手}` 可改为原始正文的字面名称或真实 @；附件、引用、昵称中的名字不触发，ACL 不变。strict 不允许全消息开放。
- 无 runtime 的 legacy 配置保留宿主原生行为，获准进入的人可能取得宿主工具能力。**不能只扩大白名单而省略隔离接线。** 宿主授权也仍生效：插件注册 `NAPCAT_ALLOWED_USERS` 能力标志，宿主读取对应适配器 `extra.allowed_users`；插件不会自动导出或改环境变量。
- 请使用专用测试 QQ。群内回复全群可见，发言者有权限不意味着可以公开主机秘密。完整权限与迁移表见 [群权限与昵称](docs/群权限与昵称.md)。

### strict／host_llm 接线

下列仅声明路由与运行模式，仍须准备匹配的 worker 和受审阅运行目录；单独写 strict 而没有 bridge 会拒绝连接：

```yaml
isolation:
  mode: strict
  private_profile: default
  social_profile: social
  domain_id: social-example
  policy_revision: 1
  guest_dm_users: []
runtime:
  mode: host_llm
  contract: 4
  social_home: /srv/companion-social-runtime
```

这些字段位于 `platforms.napcat.extra` 下。社交目录必须是独立绝对路径、当前用户所有、权限 `0700`、无软链接，不得含 `.hermes`，也不能是私人 home 或其祖先／子目录。该目录只能包含 [scoped-memory.json 模板](examples/scoped-memory.host-test.json)文件，域与 revision 必须一致，不放 `.env`、`auth.json`、模型 key 或私人记忆。prepare worker 只读挂载该文件与源码，无网络、清空环境；还需可用的 bubblewrap 和用户 namespace。

#### 正式群回合接线（合同 5）

`runtime.mode: social_agent`、`runtime.contract: 5` 在源码中通过 strict worker prepare 后接入正式 gateway 回合。群与成员的会话键强制隔离；宿主在每次工具实际派发前依据群消息准入 permit 检查，不明来源、越权工具或授权异常均拒绝。群内只允许公开搜索、提取、无源图片生成；主人仅有精确 `date -u` 主机命令示例，其他主机和私人资料读取引导私聊。审批卡在群内被拒发。图片出站只接受当前工具结果返回的 HTTPS URL，经独立下载和 QQ 原生发送后以消息 ID 回读。`host_llm` 合同 4 仍保持单次无工具群聊。

`social_home` 仍只能含 `scoped-memory.json`，不得放模型配置或密钥。历史 `agent_home` 字段在合同 5 中不再用于模型运行；群聊沿用宿主已有模型和 web/image 工具配置。本模式必须配套 [napcat-public-turn.patch](../../integration/hermes-host/napcat-public-turn.patch)，不能只安装插件。实际测试部署已完成，并由用户确认重启后的群文本回复；这不等于 web 后端、图片回读和所有连续引用均已验证。

精确 `dm:<主人ID>` 才能进入私人域；群内主人也走公共域。`0.6.0` 增加独立的[受限好友私聊](docs/普通好友私聊.md)：通过 `friend_dm` 显式启用，当前好友资格必须由真实 `get_friend_list` 核验；普通好友使用无工具、无私人历史的宿主模型路径，不进入主人原生 agent。旧 `guest_dm_users` 作为显式名单兼容，标准工厂也须核验好友资格。Route 固定绑定域、profile、策略版本、来源和用户；名字、metadata 与自述不能改路由。准入、执行或回复失败不回落私人 handler。回复只能到本轮原目标、引用原消息；取消、断开、卸载与合同变更使迟到回复失效。

旧 `ProcessIsolationBridge` 可由可信启动代码另行注入，`admit` 须在 5 秒内返回绑定同一 Route 的 Admission；这不是普通工厂已启用长期记忆的证据。完整运行材料、宿主 API、工厂与 worker 差异见 [测试部署接线](docs/测试部署接线.md)及[接入与隔离设计](docs/接入与隔离设计.md)。

### 可选独立社交插件（0.3.0 起拆分）

安装并显式启用 `hermes-social` 后，未点名自然接话共用本插件的 `allowed_groups`，仍走真实合同 5 和既有工具／资料授权；不全局关闭 `require_mention`。空闲文本仅发往社交插件明确配置的目标，不自动跟随群白名单扩大。没有／卸载社交插件时，正常点名和主人私聊仍保留。

新策略命名空间为 `plugins.entries.hermes-social.settings.channels.napcat`；旧 `platforms.napcat.extra.proactive` 不再由连接器解释。迁移、状态采用与回退见[主动聊天迁移](docs/主动聊天.md)及[社交插件说明](../hermes-social/README.md)。仍需兼容宿主安静交付／公开风格钩子与 worker；源码、安装、活动网关与 QQ 验收分别报告。自动加好友及多人长期记忆仍不属于通道能力；普通好友私聊需要独立准入和好友核验。

### 可选媒体与费用

- 媒体默认关闭，关闭时只呈现 `[unsupported:image]` 等标记，不索引、不下载、不调用辅助模型。启用需 strict media bridge v2、独立群／类型许可和云出站许可。
- 支持真实 @ 同条附件、单层明确引用、opt-in 元数据候选和受限文档提取；按最新授权，图片理解使用当前网关配置的 `openai-codex` GPT 默认模型及现有登录（不是 CLI 会话的临时模型），不再固定 Qwen-VL。`0.3.1` 修复自然接话媒体分流，`0.3.2` 补齐“上一张表情包”“这张截图”“看图片内容”等自然短期引用；仍按原群、作者、TTL 和唯一候选校验，不猜选其他附件。后续授权增强：社交参与及群图片云处理均启用时，未点名纯图可识别为本群有界近期上下文，但不会自动发 QQ 回复；未识别的占位符不能作为图像内容。文字＋附件和明确引用仍走原媒体路径，不要求重发 @。语音设计为 Whisper 优先、独立授权的云 fallback，当前状态不能推断其已安装或完成真实推理验收。
- 默认 `media.download_policy: allowlist`，精确小写域名清单为空时拒绝网络下载。显式 `public_https` 可省略 `download_hosts`，但仍强制 HTTPS／443、TLS、公网 DNS 与连接绑定、禁代理／重定向、大小及超时限制和解析沙箱；不抓取正文 URL。**取消域名过滤不等于没有网络或恶意文件风险。**
- `media.budget_mode` 默认 `local`，云调用需完整四项本地预算配置；显式 `provider` 才跳过本地调用数／美元账本，且不得混入本地预算字段。这不提供本地费用兜底，供应商额度需自行核实，主模型与辅助模型分别计费。
- 图片理解通过宿主 Codex OAuth 凭据池，不要求额外的 OpenRouter key；仅启用音频云 fallback 时，OpenRouter key 优先 `OPENROUTER_API_KEY`，仅当变量不存在时兼容 `NAPCAT_MEDIA_OPENROUTER_API_KEY`，存在但为空／非法直接拒绝。解析器与 prepare worker 不接收 key。

依赖、导出目录、限额、沙箱、文件格式及迁移细节见 [媒体实现与验收](docs/媒体实现与验收.md)，切勿仅按概要启用媒体。自然接话的媒体分流、短期候选及本轮分层验证见 [自然接话媒体修复](docs/自然接话媒体修复.md)。

## 启用、运行与切换

配置、凭据、隔离与目标环境验收准备完毕后，才在目标 profile 执行：

```sh
hermes plugins enable hermes-napcat
hermes config set platforms.napcat.enabled true
hermes gateway status
```

启动／重启应从独立管理终端执行。检查 NapCat 在线、登录账号匹配和网关连接，再分别测试授权私聊、指定群触发、非触发、未授权用户与群、私人／群隔离、取消与断开后的回复撤权。`isolation_status().ready` 只代表 transport 完成账号校验，不证明模型有效或 QQ 客户端确认送达。

Docker 仅准备上游服务，参阅 [部署说明](deploy/README.md)。本插件与官方 `qqbot` 独立，不自动迁移配置或停用旧入口；切换须明确确认，保留旧配置与补丁，只运行一个目标 OneBot 接收网关。

## 协议能力与已知限制

- 路由显式使用 `group:<群号>`／`dm:<QQ号>`，拒绝模糊纯数字；不支持临时群私聊。
- OneBot 必须设置 `messagePostFormat: array`。text／at／reply 转成真实 Hermes MessageEvent；基础文本路径只保留引用 ID，不拉历史。可选媒体的单层引用按独立合同处理。
- 出站只用普通 text segment 数组，可附 reply；不解释 Markdown 或 `[CQ:...]` 控制码，不批量 @，不提供管理／任意发送的模型工具。
- 每 4000 字符分块；失败报告已发送 IDs，不自动重放。响应丢失时无法证明是否送达，不承诺 exactly-once。
- 上限为 64 个等待动作、64 个事件队列、1 MiB WS 帧、512 segments、64000 入站字符；溢出丢最新事件并计入 `dropped_events`。去重为进程内 4096 条／10 分钟，重启不保留。
- 重连指数退避上限 30 秒，每次先校验 `get_login_info`；默认连接／动作超时 10 秒，初次失败由宿主处理后续重试。dashboard 状态不持续反映内部短时重连。
- `typing: true` 在私聊尝试可选 `set_input_status`，不支持则静默跳过。
- 未实现跨进程连接锁、持久消息队列、cron standalone sender、独立 `hermes send` 解析、历史回溯、普通消息编辑／撤回同步、富文本、视频、转发和 JSON/XML 理解或出站附件。媒体撤回只做本地清理与取消，不等于上游数据删除。

## 可复现测试与文档

在源码位于 `.hermes` 外的可写 checkout、且实际宿主依赖已就绪时运行：

```sh
cd "$COMPANION_SOURCE/plugins/hermes-napcat"
TMPDIR=/path/to/reviewed/writable/scratch HERMES_SOURCE="$HERMES_SOURCE" \
  "$HERMES_PYTHON" -m unittest discover -s tests -v
"$HERMES_PYTHON" -m unittest discover -s deploy -p 'test_*.py' -v
python scripts/stage_install.py
```

普通临时 home 遵循 `TMPDIR`；真实预算夹具使用 checkout 内随机目录并清理，因为生产预算拒绝含 `.hermes`、软链接及宽松权限的路径。测试不为方便而取消这些边界。离线测试使用真实宿主类型、aiohttp loopback、namespace 与进程回收接口，但合成 provider 响应不是真实云或 QQ 验收。历史 sqlite 警告和阶段失败保留在状态记录，不以累计历史测试数冒充本轮结果。

- [当前状态](docs/开发状态.md) · [开发日志](docs/开发日志.md) · [历史测试记录](docs/历史测试记录.md)
- [权限部署验收](docs/权限部署验收.md) · [安全说明](SECURITY.md) · [群聊媒体设计](docs/群聊媒体接收设计.md)
- [Hermes Plugin LLM Access](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access) · [OneBot 11](https://github.com/botuniverse/onebot-11)
- 社区参考：[ragingbulld/hermes-napcat](https://github.com/ragingbulld/hermes-napcat)、[shubyi/hermes-napcat](https://github.com/shubyi/hermes-napcat)

本组件自有代码、测试与文档采用 [MIT](LICENSE)，第三方依赖遵循各自许可证。
