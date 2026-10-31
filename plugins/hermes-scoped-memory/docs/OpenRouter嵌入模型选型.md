# OpenRouter 嵌入模型选型（待基准验证）

检索时间：2026-10-26 至 2026-10-27（Asia/Shanghai）；2026-10-27 再次复核公开目录与请求参考，下表三个模型的 ID、目录单价和上下文数值仍一致。自有文档 MIT。先查看官方 [请求量榜](https://openrouter.ai/rankings/embeddings) 与 [Embeddings 说明](https://openrouter.ai/docs/api_reference/embeddings)，再核对 [公开嵌入模型目录](https://openrouter.ai/api/v1/embeddings/models) 和下列模型页。本轮只获取公开元数据，没有读取密钥、调用收费 embedding／LLM 或发送用户文本。

## 推荐结论

**建议首个默认候选 `baai/bge-m3`，质量对照备选 `qwen/qwen3-embedding-8b`，通用模型对照备选 `openai/text-embedding-3-small`。** 这里的默认是后续实现／评测建议，当前插件没有 embedding 功能。BGE-M3 在本次目录中与 Qwen 8B 同价，1024 维可控制本地存储，适合以短中文关键摘要开始。Qwen 具有多语与指令检索依据，若使用原厂完整 4096 维，裸向量存储更大；endpoint 默认维度尚未验证，须用合成中文集证明收益再切换。OpenAI small 成本仍低，作为服务兼容性对照，中文效果同样待测。不同模型品牌不代表实际托管 provider 或故障域独立，供应链独立性需查证 endpoint。

默认候选需先满足可用性与隐私门槛；目前未验证各候选存在可用于本部署的合规 ZDR endpoint，因此**不能直接批准云端启用**。没有满足条件的 provider 就使用现有本地关键词。请求失败不自动改用另一模型生成同代次向量；改模型必须重建并切换索引。

榜单明确按 requests 排名，不能当中文质量榜、可用性 SLA 或延迟测试。参数量、排名及厂商基准均不能替代本任务实测。本轮没有 P50/P95、召回率或真实吞吐数据。

## 候选及可验证参数

价格单位为美元，不含充值手续费、税费和其他服务费用；均是检索当时目录的起始元数据价，具体 provider 价格可能不同，启用前复核并设置价格上限。

| 精确请求模型 ID | 输入美元/token | 输入美元/百万 token | 目录 completion 价 | 维度依据 | 上下文依据与差异 |
| --- | --- | --- | --- | --- | --- |
| `baai/bge-m3` | 0.00000001 | 0.01 | 0 | 目录描述及原厂卡 1024；实际 endpoint 输出未验证 | 目录 8194，原厂卡 8192；采用较低 8192，摘要另设更低输入上限 |
| `qwen/qwen3-embedding-8b` | 0.00000001 | 0.01 | 0 | 原厂卡最高 4096，可选 32–4096；OpenRouter endpoint 自定义 dimensions 支持未验证 | 目录 40960，原厂卡 32K；按原厂 32768 保守限制，不据目录较大值放宽 |
| `openai/text-embedding-3-small` | 0.00000002 | 0.02 | 0 | OpenRouter API 示例为 1536；本方案按 1536 估算，实际返回与降维支持未验证 | 目录 8192、模型页 8K；实际单项／批量总限制需 endpoint 验证 |

上述 completion=0 是目录字段，embedding 返回数值向量，不是生成式输出 token；不能据此认为请求免费。价格统一按输入计费 token 估算，不能把字符／汉字数当 token。目录 `per_request_limits: null` 和空 `supported_parameters` 代表未给出，不能解释成无限制或支持任意维度。`top_provider.max_completion_tokens` 对 embedding 不作为可生成文本长度。

目录中 BGE canonical slug 带日期后缀，公开请求 ID 仍是表中 `baai/bge-m3`；不可把 Hugging Face 的大写名称直接作为 OpenRouter ID。

来源逐项：

- [BGE-M3 模型页](https://openrouter.ai/baai/bge-m3) 可识别模型，本次静态页面没有呈现可读价格；价格以成功返回的公开目录为准。[BAAI 原厂模型卡](https://huggingface.co/BAAI/bge-m3) 提供维度、多语检索及上下文依据；dense／sparse／ColBERT 是原模型能力，OpenRouter 标准返回不等于开放全部三种输出。
- [Qwen3 Embedding 8B 模型页](https://openrouter.ai/qwen/qwen3-embedding-8b) 本次静态页也未显示可读价格，使用目录核价。[Qwen 原厂模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-8B) 提供 100+ 语言、指令感知和维度依据；其多语能力是中文候选理由，本地中文记忆表现未测，不称“中文最佳”。
- [OpenAI small 模型页](https://openrouter.ai/openai/text-embedding-3-small) 显示 $0.02/M tokens，与目录一致；[OpenRouter 请求参考](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request) 示例含 1536 dimensions。本轮不声称已实测中文、降维质量或 OpenRouter endpoint 的完整原厂功能。

中文适用性：BGE-M3 和 Qwen 是多语检索候选，需覆盖简繁体、口语、同名、否定与时间改写；OpenAI small 可作为通用对照。本任务没有中文质量排名证据。短摘要不需要利用候选的最大长上下文，避免长文本稀释与无谓出站。

资料获取限制：shell `urllib` 获取目录因 DNS 失败；随后使用浏览工具成功读取目录，不伪造 shell 输出。目录返回 35 个模型。按目录 details 路径获取 Qwen/BGE endpoint 明细失败，因此实际 provider 名单、每个 endpoint 价格／维度／限流／ZDR 状态均未确认；没有用猜测补齐。

## 接口与输入输出合同

官方请求参考确认 API base 为 `https://openrouter.ai/api/v1`，完整 URL 为 `POST https://openrouter.ai/api/v1/embeddings`；公开模型目录是 GET，不带真实授权信息也可获取本次元数据。下面只是请求体草图，不执行调用：

```json
{
  "model": "baai/bge-m3",
  "input": ["合成示例：项目评审改到周五。", "合成示例：只在本群共享的会议约定。"],
  "encoding_format": "float",
  "provider": {
    "allow_fallbacks": false,
    "data_collection": "deny",
    "zdr": true
  }
}
```

批处理使用 input 字符串数组；不是离线 Batch API，也没有自动半价折扣。拟议初始上限为每批 16 条、每项 512 个真实 tokenizer token、每批 4096 token，三项限制同时满足；这是本地策略建议，不是官方限额。若计数器不适配候选 tokenizer，不用字符数伪装精确值：暂缓批量构建或采用明确标注的保守长度保护，实际计费以返回 usage 为准。空输入拒绝；超长摘要先重新摘要或有证据地切片，不静默截断关键否定／日期。

仅使用文本输入和 dense float 输出，不启用图片／音频。返回按 `data[].index` 对齐原输入，核对数量、唯一且在输入范围内的整数 index、维度、数值有限性和模型信息；记录 usage 中的计费输入 token（若提供），缺失时费用记估算／未知，不能记 0。整体响应损坏或部分项目缺失时不把缺失项标成功，按幂等作业单独重试。HTTP 成功不代表向量有效。

BGE 查询无需仿照旧 BGE 模型添加检索指令；Qwen 候选拟采用原厂推荐的 query instruction 格式，document 不盲目加同一前缀。[请求参考](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request) 还定义 `input_type`（例如 search_query／search_document）；参数透传和 provider 是否自动处理前缀仍需验证，不能假设填写该字段就等同原厂模板。前缀、文本归一化和 query/document 类型都写入预处理版本，避免双重指令。OpenRouter 的统一 API 参数存在不代表所有 provider 实现一致。

## 出站与 provider 隐私

云 embedding 只接收有效、被授权出站的关键摘要和最小化查询，不上传 sources 整库、聊天全文、私人文件、真实 QQ 号或令牌。群公共事实和个人事实按各自权限选择；本地存向量不等于没有向云端发送文本。自动提取走宿主 LLM 是另一条出站链路，需要独立审查。

[provider 路由说明](https://openrouter.ai/docs/guides/routing/provider-selection) 中 `data_collection: deny` 可限制数据政策；`allow_fallbacks: false` 只是禁止路由 fallback，并非零保留保证。[ZDR 说明](https://openrouter.ai/docs/guides/features/zdr) 提供 `provider.zdr: true`，其政策仍允许某些内存缓存；ZDR 不等于字节从未在 provider 内存出现，也不保证所有模型都有合规 endpoint。OpenRouter 自身日志选项和上游保留政策要分别核对，本轮不读取账户设置。

启用前必须同时满足：审核过的 provider／endpoint 明细、数据保留与训练政策、该 embedding 接口确实执行所需路由字段、受保护密钥渠道、价格上限和匿名费用观测。明确固定已审核的 provider 集合；具体 `only`／order 列表等在 endpoint 查证后填写，不凭空写供应商名。即使字段被接受，也要核对实际路由和政策结果；不支持或无法验证时阻断云请求，退回关键词，不删除隐私字段继续试。

## 运行策略建议（全部待实现）

| 项目 | 初始策略 | 未验证项 |
| --- | --- | --- |
| 并发／限流 | 后台并发 1，按全域和 scope 分别限额，避免单一用户耗尽预算 | 实际 RPM／TPM 与账号、provider 有关，本轮未知 |
| 超时 | 后台连接 5s、单次总 20s；查询向量最多等 800ms，超时当轮关键词返回 | 数值是待调优预算，没有延迟实测支持 |
| 重试 | 429 尊重 Retry-After；网络错误／5xx 指数退避加抖动；最多 3 次 HTTP 尝试且纳入作业总尝试上限 | 超时可能已计费；服务端幂等未验证 |
| 永久错误 | 400 参数、401／403 权限、维度错误立即停用对应任务并告警 | 不转用私人配置，不打印响应正文或凭据 |
| 熔断 | 连续失败后暂停云调用，保留持久任务，管理员检查后再恢复 | 阈值按后续故障演练确定 |
| 嵌入缓存 | 本地按 domain/scope＋模型指纹＋预处理＋输入类型＋内容哈希复用 | 不声称 provider prompt caching 产生折扣；删除同步失效 |
| 费用 | 合成基准先建议 $0.10 封顶；运行期建议 embedding $1/月、$0.10/日预警及硬上限，待用户评估 | 不含 LLM 提取成本，预算超限只停止云能力 |

本地硬上限需在每次 HTTP 尝试前按输入计费量的可靠上界和 provider 价格上限预留额度，并计入在途请求与重试。超时或 usage 缺失不能立即释放预留额度，先保守记账、待可验证账单对账；无法得到计费上界时暂停云请求，不以“事后累计 usage”声称能严格封顶。供应商侧额度如可用仍需另验，不自动修改账户设置。

在线查询若后台任务超时后完成，可为后续授权查询缓存结果；不得把旧 scope 的响应注入当前回合。取消、关闭、权限变更与删除后完成的请求须检查 revision／epoch。实际最大输入、批量条数、`dimensions` 支持、usage 完整性和故障响应都留到合成请求验证。

## 脚本计算的成本与存储示例

这是人为假设的**计费 token**，不是从真实聊天提取，也不是字符换算：首次 10000 条摘要×300 token；每日新增 100×300；每日查询 1000×40；30 天增量与查询。各模型 tokenizer 不同，同一中文文本未必具有相同 token 数；这里固定计费 token 比较单价，真实成本以后续 usage／账单为准。无缓存、无重试，不含 LLM 提取、索引和运维费用。

本轮实际用 Python `Decimal` 运行如下计算（只做算术，不发请求）：

```python
from decimal import Decimal
for rate in ("0.00000001", "0.00000002"):
    p = Decimal(rate)
    print(p * 1000000, p * 10000 * 300,
          p * 100 * 300, p * 1000 * 40,
          p * 30 * (100 * 300 + 1000 * 40))
```

| 单价 USD/百万 token | 首次 300万 token | 每日新增 3万 | 每日查询 4万 | 30天增量＋查询 210万 |
| --- | --- | --- | --- | --- |
| 0.01（BGE／Qwen） | $0.03000000 | $0.00030000 | $0.00040000 | $0.02100000 |
| 0.02（OpenAI small） | $0.06000000 | $0.00060000 | $0.00080000 | $0.04200000 |

首次建库与每月增量分开列示；重建、重试与查询扩展会增加计费。表明在此假设规模下单价差很小，质量、合规、可用性和存储更值得优先验证，不能仅为省几分钱选不合规服务。

存储同样实际计算 `10000 * dimensions * 4`（float32，只算裸向量）：1024 维 40960000 bytes＝39.0625 MiB；1536 维 61440000 bytes＝58.59375 MiB；4096 维 163840000 bytes＝156.25 MiB。未含 SQLite 索引／元数据／WAL、备份和影子代次；JSON 网络体积也不等于 float32 裸大小。Qwen 若 endpoint 确认降到 1024 维且中文质量合格，存储差异可消除，但不能未测先宣称。

## 本地索引与后续验收

向量只存本地，SQLite 保存事实和来源权威内容。KNN 期间执行域／用户／来源／可见性过滤；无法证明前置过滤则停用该后端或仅对已授权集合做精确距离计算，不能全库 Top-K 后过滤。缓存同样带权限和数据修订，命中后复查 TTL／删除状态。

模型 ID、provider／可获得的修订、维度、归一化、距离函数和预处理版本组成索引指纹。切备选模型时构建影子 generation，校验后原子切换；查询与文档必须同指纹，旧代次可回滚但需应用删除墓碑。更详细生命周期见 [开发设计](开发设计.md)。

后续仅用合成中文资料对比三候选与现有关键词：固定开发／验收集，覆盖同义、否定、纠正、同名、时间、简繁体、群事件、引用、无关与越权。记录 Recall@K、MRR／nDCG、误召回及跨域泄露（必须为零）、端到端 P50/P95、批大小、限流率、token 与费用；同时比较 1024／完整维度（如支持）。无合规 endpoint 时基准也不能发送敏感文本，更不能用免费模型绕过政策。

最终选择规则：安全条件必过；中文质量较关键词的收益和无关抑制达到验收目标，再以实测成本／延迟／存储选型。当前没有这些数据，BGE-M3 是有依据的优先评测默认，不是已验证赢家。
