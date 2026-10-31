# Hermes Internet Life（插件 ID：hermes-social）

定位为互联网生活运行层，MIT 许可。当前 `0.5.0` 在话题连续性上增加每人独立请求、群回复编排和有证据的方法试用；配套 NapCat `0.7.0`、life-memory `0.4.0`、scoped-memory `0.3.2` 支持普通好友及跨天明确记忆。保留 `hermes-social` 插件 ID、模块名与数据路径。新阶段的配置、命令与验收边界见[多人请求编排、跨天记忆与方法学习](../../docs/多人请求编排跨天记忆与方法学习.md)。

`0.4.2` 将本轮回应对象与各条资料的原始作者分别提供给模型：明确接用他人的条件时保留其来源，同类但独立的个人请求不能互填条件，历史错误回答不能改变原始作者归属。

`0.4.1` 以同群共同话题接续不同成员的问题、补充和纠错，分别标明作者；重启后精确恢复最近有效条件与实际回答，点名回复与自主接话均使用共同上下文。逐轮刷新当前日期，避免旧会话时间覆盖今天；同轮多段正文沿用原 source 的原有效期，遗忘依赖仍覆盖每一段。

框架不预设任何角色、性格、语气、亲密关系或兴趣。通道的呼唤名仅用于触发，不自动成为智能体身份。可用 `public_self` 提供实例愿意公开的初始自我描述；它不读取私人 SOUL，也不等于已经实现持续成长。后续自我模型的修订应由可追溯经历、反思、验证及回滚构成，见[开发方案](../../docs/互联网生活能力开发方案.md)。

与 NapCat／life-memory 成套接入优先使用 [INSTALL.md](../../INSTALL.md)；首次安装配置自然接话、无 idle 目标，升级保留既有停用和范围选择。本插件配置自身默认关闭，模型 idle 的字段默认值与安装方案显式选择的 `local` 模式分别说明。

`0.3.0` 在每批未点名自然接话前增加一次无工具的宿主模型判断，逐条考虑话题／对象／参与方式，最多选择一个原始消息；判断失败保持安静。原配置无需新增字段，已启用的自然接话升级后会增加这一调用及相应延迟／费用。研究依据、预算和当前验证见[话题参与研究与实现](../../docs/话题参与研究与实现.md)。`0.2.0` 增加 `initiative_mode=local`，只处理 life-memory 的明确约定，不调用 idle 模型；该设置不关闭自然接话的模型调用。详见[共同经历与联系反馈](../../docs/共同经历约定与联系反馈.md)。

`0.4.0` 对冲突的关闭／重开状态做协议校验；同一二十秒期限内最多修正一次模型协议输出，仍不合格则安静。模型模式的阅读分享另做“正文确有原文依据、确实围绕来源”的独立判断，两次审核共用二十五秒期限；来源、当前场所或撤回状态变化均使草稿失效。模型核对能过滤部分错误，不能保证语义判断永远准确。

## 交付边界

- **运行插件**：使用 `ctx.register_platform_handler('napcat', factory)` 接入通道，没有第二个 OneBot 连接。controller 只持有 ParticipationPort 和本插件的 profile/model binding，不导入连接器实现。
- **只读上下文**：idle 提供本目标最多 16 条、1 小时内真实近期记录；正常群聊临时快照以十分钟、十二条为基础，模型选择可保留仍有效的话题锚点。连续话题最多在一天有效期内使用已观察引用，重启后仅精确回取已登记的至多八条，不能扫描历史。独立近期缓冲最多 256 条，话题库不存正文；不读取长期私人记忆。
- **配套 Skill**：注册为 `hermes-social:social-participation`，提供交往和运维方法，不代替常驻任务。
- **原通道**：继续拥有真实事件、source ledger、worker、身份／工具权限、单次出站和准确回读。插件缺失、禁用、卸载或存储故障时，正常点名与主人私聊仍由通道处理。

本插件不自动加好友、不提供长期关系记忆服务或新的核心工具；普通好友私聊由配套通道核验名单和实际好友身份，再进入隔离的无工具社交模型。安装器由仓库根目录单独提供。

模型 idle 根据近期上下文和时机规则判断；公开阅读机会另做有来源的无工具模型审核，可以放弃分享。本地模式可提起已确认约定、公开阅读摘录和显式开启的未完话题邀请，不调用 idle 模型。阅读与学习默认关闭，升级不自动增加来源或主动目标；关系推理和通用自主创作仍属于后续设计。

可选 [hermes-life-memory](../hermes-life-memory/README.md) 提供本人自述、规则候选、同群交流及约定，由原通道独立接线，不更换公开群 `ContextProvider`。idle 不读取普通人物档案；约定联系只取得本人明确批准的该项内容、固定收件人与有效期限。

## 宿主与通道前提

需要现有 Hermes SDK 的 `register_platform_handler`、`register_skill`、`on_unload`、`spawn_task(coro, *, name=None)` 和 `ctx.llm.acomplete(messages=...)`。manifest 使用官方通用插件类型 `kind: standalone`，不声明未经核对的依赖字段。

通道须公开 `participation_port()` 和 `install_participation(controller)`。详见 [端口与控制器合同](ports.py)、[接线入口](__init__.py) 和项目的[拆分合同](../../docs/社交插件拆分.md)。实现还使用双方明确新增的 `set_registration_key(key)`、`detach()`、`last_receipt(chat)`。

controller 附着前要求 `public_context_contract=1`，并核对 `observe_activity`、`wait_visual_context(..., max_seconds=...)` 与 `invalidate_optional(..., keep_pure_image=...)` 签名，混用旧通道时在后台任务启动前拒绝，避免丢失临时公开上下文。后台失败只记录异常类型和代码位置，不记录异常正文、完整路径或含群号的任务名。

SDK 工厂缓存键的 owner 是 `ctx.manifest.name`，不是一定相同的 `ctx.plugin_id`。本插件设置 `(ctx.manifest.name, factory.__qualname__)`；卸载先同步 `active=False`、撤销可选许可并 `port.detach()`，再在 attach 捕获的 loop 上取消与 join。通道的 detach 必须清除该 SDK 缓存键，才能在同一个 native client 上重新启用。

工厂用弱引用键记录已附着的 adapter，以兼容通道每次返回新 port；controller 本身不保存 adapter。重复 factory、新 native 或重连不会新增第二个 controller。旧 factory 在卸载后不再附着。

## 配置

全部社交策略放在：

```text
plugins.entries.hermes-social.settings.channels.napcat
```

没有第二份 QQ 白名单：

| 字段 | 默认与语义 |
|---|---|
| `enabled` | `false`，可选社交总开关 |
| `public_self` | `{}`；可选初始公开资料：`name`、`description`、`communication_preferences`，不提供系统提示词覆盖 |
| `participation_groups` | 省略时跟随 port.groups；仅保留显式测试／迁移覆盖，不能扩大通道授权 |
| `targets` | `[]`；仅为空闲发言的精确目标，绝不从参与群自动推导 |
| `initiative_mode` | `model`；`local` 仅处理已确认约定，不请求 idle 模型 |
| `timezone` | `Asia/Shanghai` |
| `quiet_hours` | `[23, 9]`，idle 安静时段；相等表示无安静窗口 |
| `idle_after_seconds` | `1800`，范围 300–86400 |
| `min_idle_interval_seconds` | `7200`，范围 300–604800 |
| `daily_limit` | `3`，范围 1–12；无回应时退让 |
| `ambient_cooldown_seconds` | `20`，范围 5–300；真实回复后有短续聊窗口 |
| `ambient_debounce_seconds` | `3`，范围 1–30；合并未点名候选 |
| `dialogue` | `enabled=true`；话题有效期 86400 秒，机会 3600 秒；`proactive=false`，未完话题主动接续须另开 |
| `activities` | `enabled=false, proactive=false, sources={}`；精确聊天映射公开 HTTPS 来源，默认每六小时一次、每天三次读取 |
| `learning` | `enabled=false, auto_trials=false`；至少两条实际反馈后提出保守试用，默认门槛三条、试用一天；只调整冷却、长度和分享开关 |

使用宿主配置 CLI，不手改 config.yaml。先检查 `hermes config set --help`；以下仅启用已由通道授权群的自然接话，不开启 idle：

```sh
hermes config set plugins.entries.hermes-social.settings.channels.napcat '{"enabled":true,"targets":[]}'
```

安装、启用插件与活动网关重载是不同步骤；本目录未提供安装／重启脚本。部署时须先检查原通道与宿主合同，不复制凭据、私人历史或另一 profile 的运行数据。

### 聊天内控制

兼容 `/napcat-social pause|resume|status`，也接受 `/social pause|resume|status`。命令只影响当前聊天；成员可以暂停，主人可恢复，允许名单内的好友也可恢复本人的私聊。暂停后仍观察以接收恢复命令。

新增 `/social topics|notes|trials` 查询；`/social read <已配置URL>` 安排读取；`/social feedback <机器人消息ID> positive|negative|correction` 评价当前聊天的一条实际回复。`/social cancel <机会引用>`、`forget-source <来源ID>`、`revert <试用ID>` 限主人或该私聊本人操作。文本命令不改变 ACL，不推断关系。

## 数据与一次性升级

固定状态路径由官方 helper 取得：

```python
from plugins.plugin_storage import plugin_data_dir
path = plugin_data_dir('hermes-social') / 'napcat' / 'social-state.json'
```

即 `<profile-home>/plugin-data/hermes-social/napcat/social-state.json`。只写时间／次数／暂停／无人回应状态，原子持久化消费后才允许发送；消息、昵称、凭据和模型结果不落入此文件。发送回执未知时不重试、不重放。

`0.2.0` 在同目录新增私有 `contacts.sqlite3`，原子保存模型 idle 与约定的一次性消费、统一额度、精确收件人、回执及反馈元数据，不保存正文。载入时保守合并旧 JSON 计数；回滚 JSON 不能恢复已消费额度。定向联系只有收件人的活动才解除无人回应退让；引用实际回执才算回应。恢复时须保留账本，不能只恢复旧内容库并丢弃消费记录。

`0.4.0` 新增 `dialogue.sqlite3`（话题、参与者 ID、引用与待答状态，不存消息正文）和 `activities.sqlite3`（获准公开来源、可核对摘录、活动与反馈试用）。各有独立的 `*-control.sqlite3` 撤销／消费栅栏；只回滚内容库不能复活遗忘资料或未知发送。恢复必须保留最新控制账本；同时回滚控制账本不在该保证内。活动来源默认保留三十天，可配置 1–90 天。

默认 controller **不读取** `<profile-home>/napcat/social-state.json`。显式升级开关是：

```text
plugins.entries.hermes-social.settings.migrations.napcat_state_v1 = true
```

开启后只在 factory 取得 port、创建 controller **之前**尝试迁移；register 时不复制。迁移源必须是当前 owner 的普通 `0600` 文件、最多 128 KiB、schema 1 且仅含原计数字段；私有消息、额外字段、符号链接和畸形值均拒绝。新目录／文件以 `0700`／`0600` 创建，通过原子 link 发布，目标已存在或竞争创建时绝不覆盖，源保留，无源则跳过且不创建迁移目录。

迁移使用公共 `gateway.status.live_gateway_pid_for_home(home)`；若另一个活动网关 PID 仍拥有该 home，则拒绝迁移。应在旧服务停机后由取得 profile 锁的新网关 attach 读取最新状态，避免部署时提前复制后丢失最后的消费计数。配置开关、文件安装完成不等于状态迁移已经发生；必须在实际重载后检查目标。迁移不是长期记忆启用。

## 上下文与模型

[ContextScope / ContextProvider](context.py) 将 profile home、platform、chat 与 subject 绑定。群公开作用域的 subject 为空；单人私有作用域必须显式匹配，DM 作用域必须等于精确收件人。读取前拒绝跨作用域请求，返回记录再核对相同 scope；注入的 provider 自身也必须在 I/O 前核验授权，返回检查不能撤销已经发生的越权读取。昵称不参与身份合并。

默认数据只来自真实 `observe` 和已发送的真实 `record_self`；撤回会使依赖资料的输出失效，断线与卸载撤销可选参与。卸载生活插件不会撤销通道原本已准入的点名回复。通知不应由通道传给 `record_self`。只有真实回复会开启续聊窗口，`NO_REPLY` 不记为发言。

未点名的自然文本经合并与节流获得模型判断机会，不以疑问词、短答词表或主题字面重合判断意图。真实 @／核验引用给其他成员的消息不触发可选回复，但可作为标注过的同群背景。批次选择阶段还判断纯文本面向对象、短期话题关联和参与方式，模型只能选择该批次的真实候选；不再固定偏向最新文本。最终生成仍可返回 `NO_REPLY`。真实出站的 `reply_to_message_id` 与原发言人构成近期交流证据，其他成员插话不会抹掉这对记录，也不会因此断言新消息一定在回答机器人。模型关联只作临时假设，不写入长期人物事实。

点名与可选群聊均通过宿主的临时上下文钩子取得本群十分钟内最多十二条公开记录，预算 5000 字节，避免只看到各自的个人会话；原始用户文本不附加提示词。撤回及忘记会撤销依赖这些资料的输出。普通新消息在首次可选回复发起后最多二十秒、八条内不会自动撤销在途文本回复；不延长原许可，不改变收件人。明确新请求、控制命令、撤回、暂停、断线仍撤销；媒体候选保持更严格的新活动失效规则。

图片识别完成后在完整路由窗口内更新原消息；超过展示窗口仍可更新，超过 64 条、过期或已撤回的消息不会因迟到结果重新出现。自动化验证通过不等于已安装或真实 QQ 验收。

idle 使用 `ctx.llm` 复用宿主模型与登录，没有 provider/model/profile override、工具、私人 `side_question` 或历史加载。新任务在空 Context 中建立，仅绑定自己的 profile 和当前目标 session；群只传固定公开 system 与本群近期 user 资料。真实未点名消息仍经 port 进入原通道完整 gateway/tool 路径，不冒充自动化用户。

## 验证

在本目录执行：

```sh
PYTHONDONTWRITEBYTECODE=1 hermes --run-module unittest discover -s tests -p 'test_*.py' -v
```

测试使用 `$TMPDIR`，没有真实 QQ 连接：

- 独立策略／controller／上下文／状态／迁移测试，真实 SDK `PluginContext`、`PluginManager`、任务监督与 `PluginLlm` facade，模型响应为离线 fixture。
- 仓库中存在相邻插件源码时，额外验证真实 `HostedNapCatAdapter`、ParticipationPort、source ledger、worker 的 `prepare_proactive` 校验函数、SDK late-load／unload／重新启用；仅 OneBot 换为发送／回读捕获端，强制禁止真实 connect。
- 独立分发没有相邻源码时，通道集成用例明确 skip，不把它们记作已通过。

本测试不等于实际模型的自然交流验收，也不覆盖完整 durable FIFO/source codec/AIAgent 真实模型链路；后者由项目集成验收负责。安装副本、活动网关加载、长期记忆和 QQ 客户端可见性均须分别报告。

`0.3.0` 本轮源码完成 143 项离线测试、3 组合成真实模型选择探针和 10 个正式捕获链路场景，均通过；没有更新安装副本或运行网关。细节与限制见[话题参与实现记录](../../docs/话题参与研究与实现.md)。历史源码／安装副本的 107 项记录仍见[安装验收记录](../../docs/安装修复与统一接入验收.md)，不作为新版本已部署的证据。

`0.4.0` 于 2026-11-01 完成 284 项社交源码测试；配套 NapCat 共 398 项（16 项跳过），安装器在真实宿主的一次性 home 中 31 项通过。实际 HTTPS 阅读与话题／反馈真实模型捕获 9 项通过，普通好友真实模型捕获 8 项通过；这些测试没有连接 QQ。安装、运行与客户端验收见[本阶段记录](../../docs/连续社交阅读与反馈实现.md)。

## 许可证

[MIT](LICENSE)。配套 Skill 见 [Skill 说明](skills/social-participation/README.md)。
