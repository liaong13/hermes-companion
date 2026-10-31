# NapCat 公开交流风格与参与判断增量

本增量依赖 [NapCat 合同 5](napcat-public-turn.md)与[未点名候选安静交付](napcat-ambient-quiet.md)，配套插件说明见[主动聊天正式功能](../../plugins/hermes-napcat/docs/主动聊天.md)。宿主基线仍为 `5894e750c3dba980d43b25aa29e3958149ba2c61` 加上述先决补丁；其他版本必须审阅真实接口和工作树，不能直接覆盖。

## 为什么修改

正式群回合不加载主人的私人 SOUL、USER、MEMORY，这是必须保留的隔离。但“只提供公开资料隐私约定”不等于提供了公开交流风格与参与原则；旧候选注释又强调“不要求回答”，容易让开放提问也变成 `NO_REPLY`。简化的 `ctx.llm.acomplete` 不包含正式 agent 的完整 system、工具与持久化链路，不能用其回答正常就断言正式回合没有问题。

本增量将公开风格与积极参与约定明确接入真实 native 回合，不将任何单一句子宣称为唯一根因，也不通过取消静默来掩盖误判。

## 接线与边界

- 插件的公开风格模块（拆分后在 `hermes-social`）只含审阅过的公开文字，名字取自通道配置，不读取私人身份文件或记忆。QQ 昵称、正文、引用和近期对话继续作为不可信 user 资料。
- `HostedNapCatAdapter.conversation_prompt_for_source(source)` 只服务已明确 opt-in 的目标。群来源复用精确 live permit 和既有路由核验；主人私聊复用真实私聊许可与 route 核验。过期来源不回落私人上下文。
- 宿主按真实 source 使用 `_delivery_adapter_for(source)` 调用钩子。群仍只合并原 privacy 与公开约定，主人私聊保留原上下文再附加公开语气，未选目标不变。固定文本不含动态时间、成员昵称、消息或近期缓冲，保持会话前缀稳定。
- 群参与约定鼓励回应开放问题、观点邀请和连续追问；对他人明确交谈、仅表情或没有新内容等场景仍允许 `NO_REPLY`。未点名不是单独的沉默理由。
- 既有 `non_conversational` 出站标记扩展到 NapCat；工具进度、专用 agent status、运行提醒、自动重置提醒和独立 runtime footer 保留该标记，不写入社交近期缓冲，也不刷新社交活动代数。`_interim_send` 只代表非最终文本，不能单凭它抹掉真实的聊天发言。
- 不修改场所／成员准入、模型登录、可见工具定义、实际工具授权、审批或自动发送许可；不增加新的工具、定时任务或配置字段。

源码补丁：[napcat-social-participation.patch](napcat-social-participation.patch)。该文件是相对于已具备先决补丁的增量，不包含插件源码，也不是自动安装器。

## 静态与离线检查

```sh
# HERMES_SOURCE 是待验证的宿主源码；先检查，不盲目应用
# 目标已应用时用 --reverse --check 验证增量

git -C "$HERMES_SOURCE" apply --check /path/to/napcat-social-participation.patch

# 使用已有 pytest 的隔离测试解释器，不往应用运行时安装测试依赖
HERMES_SOURCE="$HERMES_SOURCE" "$HERMES_TEST_PYTHON" -m pytest \
  /path/to/hermes-companion/integration/hermes-host/test_napcat_ambient_quiet.py \
  /path/to/hermes-companion/integration/hermes-host/test_napcat_public_style.py \
  -q -p no:cacheprovider --basetemp "$TMPDIR/napcat-social-pytest"
```

离线测试核验接线、权限、metadata 和安静交付，不把假模型的固定回答当成社交质量证据。

## 完整网关与真实模型验证

`verify_napcat_social_live.py` 明确使用当前已登录 Codex 模型，只读取指定 profile 的必要模型／工具集和已审核 worker 设置，通过官方 readonly resolver 在内存中借用登录；不复制凭据、私人会话或身份文件。临时 home、状态、报告和合成对话均写到 `$TMPDIR`。需要明确传入 `--live`，不默认发起模型调用。

```sh
python /path/to/verify_napcat_social_live.py \
  --host "$HERMES_SOURCE" --owner-home "$HERMES_HOME" \
  --live --case all --label candidate
```

真实路径为 worker probe／prepare → `GatewayRunner._handle_message` → durable FIFO／source codec → `_run_agent` → `AIAgent.run_conversation` → 原有模型服务。只替换 OneBot 传输为本地捕获端，禁止真实 OneBot 连接；不能把这个层级称为 QQ 客户端验收。

矩阵包括开放提问、游戏比较、连续追问、明确对他人讲话、仅表情、静默后重新加入，以及配置名称的简短自我介绍。报告保存实际最终文本、真实 API 调用数、模型／provider、工具定义、live permit、来源恢复、FIFO 结算、合成私人 canary 和凭据未落盘检查。自我介绍可以很短，不能用任意最低字数迫使它啰嗦。

脚本退出码：`0` 为所选行为用例通过；`1` 为真实行为断言失败；`2` 为基础设施阻塞。任何层失败都保留 `report.json`，不合成替代输出，也不修改报告以伪装通过。

核对实际安装组合时可额外指定 `--plugin-root "$HERMES_HOME/plugins/hermes-napcat"`；仍使用隔离的合成输入和捕获端，不调用运行中网关或真实 QQ。

## 部署、回退与最终验收

1. 先备份当前安装目标与宿主原文件，再审阅最小差异。拆分后同步连接器的 `adapter.py`、`host_runtime.py`、`participation.py` 及独立 `hermes-social` 文件集；旧 `proactive.py`／`social_prompts.py` 不再作为连接器实现分发。不要整目录覆盖本地修改；配置与状态迁移见[主动聊天迁移](../../plugins/hermes-napcat/docs/主动聊天.md)。
2. 对照源码和安装文件哈希，并核对现有 opt-in 配置未扩大。补丁文件、已安装、活动网关已加载是不同状态。
3. 如果当前工具在网关进程树内，由用户在独立终端执行 `hermes gateway restart`，不可通过分离进程或定时器绕过重启保护。
4. 重新核验 PID、启动时间与 NapCat 连接，再在授权测试群发送新的未点名消息，检查实际回复／静默与 QQ 客户端可见性。既有旧消息不代表重启后的行为。
5. 回退时只恢复本次备份覆盖的精确文件，并移除本次新增的插件模块；保留其他本地修改、配置与聊天数据。恢复后同样重启并重新核验，不用清空会话库追求结果。

自动化通过不保证每次模型都作出完美社交判断；持续自然相处仍需在真实互动中检验，且不代表多人长期记忆、持续感知或主观感情已经实现。
