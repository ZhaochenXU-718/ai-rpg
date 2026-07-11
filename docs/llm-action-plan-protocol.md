# AIRPG LLM 行动协议

状态：草案 `0.1`
实现：`server/engine/llm_protocol.py`

## 1. 目标

这份协议定义 LLM 认知层与 AIRPG 世界内核之间的边界。LLM 负责理解、规划、创造方案和重新规划；引擎负责验证世界事实、权限、不变量和原子提交。

协议要保证：

- LLM 能提出作者未预写的合理方案，而不被 storylet 白名单限制。
- LLM 的提议、引擎的验证和最终真实结果严格分离。
- 玩家理解模型只能读取玩家可见信息，不能获得隐藏剧情事实。
- 报价和结果叙事只能使用已经验证或已经提交的内容。
- 所有载荷可以 JSON 序列化、校验、记录和重放。
- ActionPlan 从第一版开始使用 `capability + action`，为后续能力模块化保留稳定接口。

本阶段不定义真实模型 provider、Prompt、多 Agent、RAG 或正式 CapabilityRouter。

## 2. 四层事实

协议中的四个顶层载荷具有不同权威性：

| 载荷 | 产生者 | 含义 | 能否作为世界事实 |
|---|---|---|---|
| `PlayerPerception` | 引擎 | 玩家当前可以知道的快照 | 是，但只代表玩家感知 |
| `ActionPlan` | LLM | 对玩家意图的理解和行动提议 | 否 |
| `ValidationResult` | 能力工具/引擎 | 对计划的接受、调整和拒绝 | 仅表示执行许可 |
| `CommittedOutcome` | 引擎 | 实际已经发生的状态变化 | 是，唯一结果真相 |

禁止把 `ActionPlan.proposed_changes`直接写入状态。结果渲染和导演反应必须读取 `CommittedOutcome`，不能把被拒绝的提议叙述成已经发生。

## 3. 生命周期

```text
构建 PlayerPerception
→ LLM 生成 ActionPlan
→ 校验 perception_revision
→ CapabilityRouter 验证步骤和状态提议
→ 生成 ValidationResult
→ 如可修正，最多允许一次 LLM replan
→ 报价与玩家确认
→ 原子提交
→ 生成 CommittedOutcome
→ LLM 结果叙事、导演反应和记忆更新
```

如果计划所依据的 `perception_revision` 与当前 `state_revision` 不同，必须作为过期计划拒绝或重新规划，不能静默套用到新状态。

## 4. PlayerPerception

`PlayerPerception`是行动理解模型的唯一世界输入。它包含：

- 故事、会话、回合和状态版本；
- 当前地点和目标；
- 当前可见实体；
- 玩家持有物；
- 玩家已知事实；
- 可用意图；
- 当前加载能力提供的工具目录；
- 最近公开事件；
- 明确允许公开的状态路径。

`PerceivedEntity.public_state`只能放玩家可观察状态。NPC秘密、未发现物品、隐藏动机、未公开任务条件和作者备注不能进入该载荷。

理解模型和导演模型未来可以使用同一个 LLM provider，但必须使用不同上下文。导演可以读取受控的隐藏状态，行动理解模型不可以。

## 5. CapabilityTool 与 CapabilityAction

能力工具通过 `CapabilityTool`暴露给 LLM：

```yaml
capability: inventory
action: use_item
description: 使用玩家持有的物品作用于当前目标
arguments_schema:
  type: object
  required: [item, target]
allowed_authority_levels:
  - mechanical
```

ActionPlan 中的每一步使用 `CapabilityAction`：

```yaml
capability: inventory
action: use_item
arguments:
  item: servant_key
  target: service_door
purpose: 打开档案室的仆役窄门
optional: false
```

能力和动作 ID 使用稳定机器标识，例如：

- `inventory.use_item`
- `spatial.move`
- `social.influence`
- `creative_resolution.create_visual_distraction`

LLM 可以组合多个工具，但不能调用当前 `PlayerPerception.capability_tools`中没有提供的机械能力。未来允许创造新实体或事实时，也必须通过专门的创建工具完成。

## 6. ActionPlan

ActionPlan 是 LLM 对玩家自由表达的结构化理解。核心字段：

| 字段 | 说明 |
|---|---|
| `plan_id` | 本次计划 ID |
| `perception_revision` | 计划依据的状态版本 |
| `player_text` | 玩家原始输入，不得改写覆盖 |
| `interpretation` | LLM 对玩家意图的简洁理解 |
| `goal` | 玩家希望造成的结果 |
| `intent_id` | 可选，映射到现有意图空间 |
| `references` | 玩家提到或计划依赖的实体 ID |
| `steps` | `capability + action`步骤 |
| `proposed_changes` | LLM 希望产生的状态变化，不代表已接受 |
| `risks` | 可能发生的代价及其状态变化 |
| `assumptions` | 计划成立所依赖但尚未证明的判断 |
| `confidence` | 0–1 的理解置信度 |
| `needs_clarification` | 是否必须先向玩家澄清 |
| `revision` / `parent_plan_id` | 重规划链路 |

示例：

```yaml
protocol_version: "0.1"
plan_id: plan_demo
perception_revision: 6
player_text: 用酒盘反光把守卫引向东窗
interpretation: 玩家想制造一次短暂的视觉误导
goal: redirect_guard_attention
intent_id: create_distraction
references: [wine_tray, east_window, guard]
steps:
  - capability: creative_resolution
    action: create_visual_distraction
    arguments:
      source: wine_tray
      target: guard
      direction: east_window
    purpose: 让守卫暂时看向东窗
    optional: false
proposed_changes:
  - path: guard.attention
    operation: set
    value: east_window
    authority: soft_state
    reason: 闪电反光把守卫的注意力引向东窗
    step_index: 0
    duration_turns: 1
risks:
  - description: 守卫可能发现反光来自玩家
    likelihood: possible
    changes:
      - path: guard.alertness
        operation: increment
        value: 1
        authority: mechanical
        reason: 异常反光可能提高警觉
        step_index: 0
        duration_turns: null
    mitigation: 先移动到阴影角落可以降低暴露风险
assumptions:
  - 酒盘能形成足够明显的反光
confidence: 0.82
needs_clarification: false
clarification_question: null
revision: 0
parent_plan_id: null
```

可执行计划至少包含一个步骤。需要澄清的计划可以没有步骤，但必须提供 `clarification_question`。

`ActionPlan.model_json_schema()`或 `action_plan_json_schema()`返回 provider-neutral JSON Schema，后续真实模型应优先使用结构化输出，而不是从 Markdown 代码块中截取 JSON。

## 7. 状态权限

每个 `StateChangeProposal`必须声明权限层级：

| 层级 | 示例 | 默认处理 |
|---|---|---|
| `presentation` | 动作姿态、气氛、无状态描写 | LLM可以自由生成，但不能伪造状态变化 |
| `soft_state` | 情绪、注意力、短期判断 | 可由白名单和限幅规则验证 |
| `mechanical` | 位置、物品、生命、金钱、时间 | 必须由对应能力工具验证并提交 |
| `canon` | 真凶、关键证据、死亡、任务完成 | 只能由作者锚点、受信规则或明确授权能力改变 |

状态操作显式区分：

- `set`
- `increment`
- `append`
- `remove`

不再用“数字自动代表增量”作为 LLM 协议语义。CapabilityRouter 后续负责把显式操作转换成现有状态事务。

`duration_turns`表示临时变化，必须为正整数；永久变化使用 `null`。

## 8. ValidationResult

ValidationResult 必须回答：

- 哪些步骤被接受；
- 哪些状态变化被接受；
- 引擎调整了哪些代价；
- 哪些部分被拒绝以及原因；
- 缺少什么信息；
- 当前是否可以执行；
- 是否允许 LLM重新规划。

硬规则：

- `can_execute=true`时不能存在 `error`级 issue。
- 可执行结果必须至少接受一个步骤。
- 拒绝结果必须包含错误或缺失信息。
- 接受的状态变化只能引用已接受步骤。
- ValidationResult 的 `state_revision`必须和提交时状态一致。
- 重规划预算第一版最多一次，避免循环调用。

Issue 使用稳定错误码，例如：

- `plan.stale`
- `object.not_visible`
- `item.not_owned`
- `capability.unavailable`
- `change.forbidden`
- `assumption.unverified`

## 9. CommittedOutcome

CommittedOutcome 记录实际结果：

- 提交前后状态版本；
- 接受的步骤；
- 每个真实状态路径的旧值和新值；
- 变化的权限和来源；
- 触发的 storylet 与世界事件；
- 新增玩家事实；
- 被拒绝的效果；
- 场景、结果等级和结局。

后续 LLM 叙事、NPC反应、记忆与反思都必须以它为事实输入。ActionPlan 中存在但 CommittedOutcome 中不存在的效果，不得被描述成已经发生。

## 10. 与当前阶段 2 引擎的关系

当前结构化 CLI 和 walkthrough 继续使用原有确定性 resolver。第一条 LLM 纵向链路从 `custom`自由方案开始：

```text
custom player_text
→ ActionPlan
→ CapabilityRouter（server/engine/capabilities.py）
→ ValidationResult
→ 报价与确认
→ 适配到现有 resolver/事务
→ CommittedOutcome
```

`requires_storylet_match`暂时继续保护无 LLM 的严格结构化动作；创造性 LLM 路径不以 storylet 是否预写作为唯一执行条件。

### 10.1 v0.1 引擎承兑范围

协议表达能力大于当前引擎，写 prompt 时以本表为准，不要教 LLM 使用引擎不认的功能：

| 协议特性 | v0.1 引擎行为 |
|---|---|
| `steps`（多步计划） | 只接受**恰好一个** `intent.*` 步骤；多步返回 `plan.single_intent_step_required`（retryable）。一个计划 = 一个回合 = 一份意图代价，按步计价待真实 trace 后再定 |
| `capability` 词表 | 仅 `intent`（由内容意图派生，含 `intent.move`/`intent.use`）；工具列表来自 `PlayerPerception.capability_tools` |
| `proposed_changes` + `SOFT_STATE` | 经 `resolution_limits` 裁剪后并入兜底 patch；`SET`/`INCREMENT` 支持 |
| `proposed_changes` + `PRESENTATION` | 叙事层内容，不进状态，返回 adjustment |
| `proposed_changes` + `MECHANICAL` / `CANON` | 一律剥离（adjustment）：机械变化由引擎从步骤推导，canon 只能由作者事件卡产生 |
| `ChangeOperation.APPEND` / `REMOVE` | 不支持，adjustment 忽略 |
| `StateChangeProposal.duration_turns` | 不支持，按永久变化处理并附 adjustment |
| `perception_revision` 过期 | `plan.stale_perception` 错误（retryable），必须基于最新感知重规划 |
| `CommittedChange.authority` | 由引擎按路径推导：`positions.*`/`item_locations.*` → mechanical，白名单路径 → soft_state，其余 → canon |

## 11. Trace 最低要求

每次 LLM 行动至少记录：

- PlayerPerception；
- 原始模型响应；
- 解析后的 ActionPlan；
- ValidationResult；
- 重规划前后计划；
- 玩家确认的报价；
- CommittedOutcome；
- 模型、Prompt 版本、延迟、Token 和成本。

这些记录用于复现错误、离线 Replay、协议升级和后续模块边界分析。

## 12. 暂不决定的事项

- 使用哪个模型或 provider；
- 多 Agent 的导演与角色拆分；
- 长期记忆和 RAG；
- 正式 Capability Module Contract；
- 运行时模块热加载；
- 轻量故事格式与 LLM 故事编译器。

这些决定应在第一批真实 ActionPlan trace 出现后再做。
