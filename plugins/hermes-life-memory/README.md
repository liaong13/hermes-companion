# Hermes Life Memory

独立本地人物与经历记忆插件，版本 `0.3.0`。记忆计算只使用 SQLite、原始事件和本地规则，不调用 Mem0、LLM、embedding 或本机 HTTP 推理服务。已提供显式自述、对话／话题、规则候选、共同交流摘录、本人确认的定时约定和依赖撤回；组件配置默认关闭，尚未完成真实 QQ 功能验收。

完整接入使用 [INSTALL.md](../../INSTALL.md)，自动配齐四组件与对应宿主增量。首次基本安装配置显式人物候选，普通聊天采集另选 `--features local-memory`，主动约定联系另需明确目标；升级保留原选择。安装、运行加载与功能验收的区别见[安装验收记录](../../docs/安装修复与统一接入验收.md)。

最新配置、匹配版本与验证边界见[共同经历、约定与联系反馈](../../docs/共同经历约定与联系反馈.md)，此前实现见[纯本地记忆与交流续接](../../docs/纯本地记忆与交流续接.md)。QQ 收发和对话模型仍由 Hermes 配置；记忆无需 API 不代表整个伙伴已经离线运行。

## 已实现的范围

- 仅接受已获准 NapCat 群中的原始纯文本命令，可信成员 ID 决定资料主体。昵称相同不合并；他人、引用消息、机器人回复和模型工具请求不能代为确认。
- 支持 `interest`（兴趣）、`preference`（交流偏好）、`boundary`（联系边界）三个字段。每个成员／群／伙伴实例的同一字段只有一个生效版本；确认新值时保留修订关系。
- 资料明确记录伙伴实例、`subject_id`、`asserted_by`、来源修订、证据片段与当前群可见范围。人物资料不跨群、私聊、其他成员或 idle；真实同群 bot 引用只带公开消息的依赖元数据，不开放被引用成员的档案或原文读取。
- 通过 NapCat 合同 1 的 `install_social_memory` 接线，复用原有合同 5 公共群回合及 worker 准入。没有第二个连接、MemoryProvider 注册或模型调用；不会替换 default 的 Mem0。
- 召回最多八条、每个值最多 480 UTF-8 bytes，仅加入当前回合的临时上下文；不改原始 `event.text`。忘记、来源撤回／修订、策略失效、卸载与断线会使相关在途工具和发送许可失效。

本地存储不等于本地推理：启用后，已确认且适用于当前成员的资料会随公开群回合发送给所配置的对话模型。群中的 review 回复也会展示本人资料。不要填写秘密或不愿在该群展示的信息。

忘记会删除本插件资料、来源、证据、索引及依赖它的本地 bot 回复；删除账本约束旧内容库恢复，文本以外的消息墓碑阻止被撤销回复和控制通知重新进入模型引用。匹配本地上下文模式会跳过旧宿主历史／缓存并重建回合；已发布 QQ 消息、模型服务端记录、宿主历史数据库和外部备份不会被擦除，不宣称完整遗忘。

## 接入前提与配置

需要本仓库匹配版本的 `hermes-napcat`（`0.5.0`，`social_memory_contract = 1`）、已启用的 `hermes-social 0.2.0`，以及完整的 `hermes-scoped-memory 0.3.1` 源码。通过版本化公共入口 `api.py / API_VERSION=1` 复用数据层；完整目录仍须同级分发，尚未拆成独立 pip 包。只有 `isolated_worker.py` 的安装副本不够。该依赖不要求启用 scoped-memory provider，也不调用其 `register` 或读取 Mem0 配置。

```text
<profile-home>/plugins/
  hermes-napcat/
  hermes-social/
  hermes-life-memory/
  hermes-scoped-memory/  # 完整匹配源码，provider 可保持禁用
```

宿主须具有 [NapCat 公共群回合](../../integration/hermes-host/napcat-public-turn.md)及[公开风格临时上下文接线](../../integration/hermes-host/napcat-social-participation.md)，并提供正式 `register_platform_handler`、`on_unload` 与 `plugin_data_dir`。本插件不需要 memory-provider 的 `bound_llm/bound_session` 补丁；它的身份来自通道准入后的精确 source，而非环境变量或请求参数。

安装代码、启用插件、配置、网关加载和真实验收分别核对。统一安装器可完成前四层；以下手动示例供选择性集成，仅在获准测试 profile 中，先核对 `hermes config set --help`，再用宿主 CLI 设置：

```sh
hermes config set plugins.entries.hermes-life-memory.settings.memory '{"enabled":true,"instance_id":"companion-demo","domain_id":"people-demo","policy_revision":1,"groups":["20001"]}'
```

示例群号为合成数据，须替换为通道已允许且 social 已启用的实际群；配置不能扩大通道白名单。`instance_id` 是稳定的伙伴实例标识，不是角色显示名。`domain_id` 是本插件存储域，不能为 `legacy`；`policy_revision` 为正整数。未知字段、非布尔开关和不合法群号会拒绝。配置变更一经检查即撤销旧服务，恢复相同配置也不能复活旧许可，需重新加载服务。

当前核对的 SDK 定向 `unload(owner)` 不移除平台工厂列表，旧工厂可能占据相同 wire key。本轮验证的是完整插件重新发现（先 unload-all，再注册新一代）后的附着与资料恢复；定向卸载能立即撤权，但不能据此承诺任意定向重新启用。实际重载仍须在获准环境单独执行。

## 群内命令

直接发送纯文本，不需要 @，只处理已授权群中的本人资料：

```text
/social-memory remember interest 我喜欢讨论天文学
/social-memory pending
/social-memory review <ID>
/social-memory confirm <ID> <核对码>
/social-memory reject <ID> <核对码>
/social-memory mine
/social-memory plan 2026-11-01T20:00:00+08:00 继续讨论恒星演化
/social-memory events
/social-memory cancel <ID>
/social-memory close <ID>
/social-memory forget <ID>
```

`remember` 仅创建候选。`review` 返回内容、使用范围及精确确认／拒绝命令；本人执行确认后才能作为人物事实召回。其他群或成员复制 ID 和核对码无效。修订同一字段时重新 remember／review／confirm；另有下述规则候选，尚无通用语义提取或自然语言控制命令识别。

可选 `conversation.enabled=true` 与 `candidates.enabled=true` 会从已准入、实际交给公共群回合的原始文本识别少量明确自述，例如“我喜欢天文学”“我希望你回复简短一些”。引号、转述、假设、疑问和混合复杂句保持未知；规则候选仍须本人核对，原始聊天记录不等于已确认事实。默认最多 8 条待核对候选，规则候选有效期 30 天；重复原事件不会重新创建已拒绝或已忘记的条目。

对话原文最多 2000 UTF-8 bytes，超限输入不采集、不产生候选，普通问答仍按原通道处理。对话默认保留 7 天、每人每群最多 200 条有效事件；已确认人物资料、共同经历及约定的有效证据保留到对应条目失效或忘记，过期对话不再整段注入。话题依据真实回复、关键词和短时续问规则，尚无通用语义理解。

另可开启 `episodes.enabled=true` 和 `agreements.enabled=true`，均依赖对话采集。共同经历是带证据的字面交流摘录；约定必须提供带时区的明确未来时间、经本人核对确认。`agreements.proactive=true` 与本人确认同时成立后，才可经 social 的原群目标、统一额度及 NapCat 成员检查提起一次；普通人物偏好仍不供 idle 读取。`cancel`／`close` 撤销来源及派生，close 不是外部任务完成证明。详细默认值、命令及反馈语义见最新阶段文档。

开启对话记录必须同时启用 NapCat `local_conversation_context=true` 并具备[本地上下文宿主增量](../../integration/hermes-host/napcat-local-memory-context.md)。它对该适配器的公共群回合使用新 Agent 与空宿主历史，交流连续性来自有效本地事件；插件停用后仍不自动回落旧历史。缺少宿主合同会拒绝开启，不能仅设置 memory 开关后声称已接通。

命令走有界本地控制队列，可以在旧模型回合执行期间处理忘记；响应标记为操作通知，不进入 social 的近期交流或打开续聊窗口。命令仍需原通道准入和 worker 探测，队列满、连接不可用或合同不匹配时拒绝。

## 存储、验证与下一步

数据保存在 `plugin_data_dir('hermes-life-memory')` 下的 `scoped-memory/` 和 `scoped-memory-deletions/`。复用既有 Store 的事务、证据、作用域版本和删除恢复机制，另加 `person_facts` 元数据表，不复制存储实现。内容库与删除账本的恢复策略必须一起审阅；丢弃账本不能保持删除承诺。

使用包含 Hermes 依赖的实际解释器，在仓库根目录运行：

```sh
export TMPDIR=/path/to/reviewed/writable/scratch
export HERMES_SOURCE=/path/to/hermes-agent
export HERMES_PYTHON=/path/to/actual/hermes/python
HERMES_DISABLE_LAZY_INSTALLS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERMES_SOURCE" \
  "$HERMES_PYTHON" -m unittest discover -s plugins/hermes-napcat/tests -p test_life_memory.py -v
HERMES_DISABLE_LAZY_INSTALLS=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$HERMES_SOURCE" \
  "$HERMES_PYTHON" -m pytest -q plugins/hermes-scoped-memory/tests/test_people.py
```

适配器集成测试使用合成 home、真实 bubblewrap 探测和宿主临时上下文方法，OneBot 与 gateway 收发为捕获夹具；不连接 QQ、不调用模型。没有 bubblewrap 时明确 skip。完整本轮结果见[实现记录](../../docs/人物记忆首个切片.md)。

下一步在获准测试域验收，再接一种有真实来源的公开阅读／笔记活动与成果分享。Agentopia 的[中文介绍](https://github.com/Neph0s/Agentopia/blob/da264aa6d82cca98f2051bcf794ff5dd98f10fdb/docs/README.zh.md)提供人物笔记与阶段循环参考；本切片只借鉴结构，未复制上游实现或运行其模拟世界。
