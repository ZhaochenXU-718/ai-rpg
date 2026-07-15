# AIRPG 叙事优先协议 0.3

日期：2026-07-15
实现：`server/engine/llm_protocol.py`、`llm.py`、`llm_deepseek.py`、`iron_laws.py`、`fact_pipeline.py`

## 1. 权威边界

协议 0.3 延续“模型生成与世界事实隔开”的原则，并实现第一版后验闭环：

```text
PerceptionSnapshot
→ Narrative / SuggestedAction / DirectorPlan / NpcTurn（均无状态权限）
→ 散文
→ FactExtraction（不可信抽取结果）
→ IronLawViolation[] 或通过
→ FactBatch（代码准入后的提交载体）
→ CommittedTurn（唯一提交记录）
```

旧的计划、能力步骤、软状态提议和执行许可协议已退役。provider 不再获得一张机械工具菜单，也不能返回可直接执行的状态 patch。

## 2. `PerceptionSnapshot`

一个快照绑定：

- `audience`：`player`、`npc` 或 `director`；
- `subject_id`：这份知识属于谁；
- story / session / turn / revision；
- 当前地点与目标；
- 可见实体、主体持有物、已知事实、近期事件、公开状态；
- `subject_context`：仅给对应主体的私有上下文。

协议只定义形状，`perception.py` 决定披露。玩家快照的 `subject_context` 默认为空；NPC 快照可以包含自己的人物卡私有字段。快照没有 capability、intent 或状态写入权限。

## 3. 旁白请求

`NarrativeRequest` 包含 `kind`、作用域快照、事实清单和文风。provider 只能改写事实清单；快照用于记录视角与 revision，不允许模型从其他 state 补全信息。

Phase 2 允许候选散文明写成功、失败和人物回应，但要求位置、物品、披露与承诺变化明确可抽取。检查失败时，下一次 `NarrativeRequest` 会收到结构化冲突反馈；候选散文不会展示、提交或进入近期记忆。

## 4. `SuggestedAction`

行动卡只有：

- `suggestion_id`；
- `perception_revision`；
- `title`、`action_text`、`focus`、`rationale`；
- `expected_iron_law_touches`。

它是可编辑第一人称散文，不绑定执行计划或验证结果。引擎根据可见出口、物品及文本中的披露/承诺措辞补充“可能触及的铁律”，只用于玩家知情和 trace，不是裁决。

任何状态 revision 变化都会使旧卡失效。玩家采用卡片时，`action_text` 与自由输入走同一条叙事管线。

## 5. `FactExtraction` 与 `FactBatch`

`FactExtraction` 是 provider 的不可信 JSON 输出，绑定输入 revision，只支持五类事实：

- `character_move`；
- `item_transfer`，携带抽取时看到的 from/to placement；
- `secret_disclosure`；
- `commitment`；
- `commitment_update`（fulfilled / broken / cancelled）。

每条事实必须携带散文原文中的连续 `evidence`。模型不能返回任意 dotted path 或状态 patch；一个响应中只要存在畸形事实，整次 JSON 解析就进入有限修复，不能静默丢弃坏条目后提交其余事实。

`FactBatch` 是后验提交边界，字段包括：

- `batch_id`、`state_revision`；
- 玩家原话和本回合散文；
- 检查器从受支持事实生成的 `state_changes` 与玩家已知事实；
- 已接地的可见实体引用。

provider 不能构造可提交 patch；只有 `iron_laws.py` 能把 `FactExtraction` 转成 `FactBatch`。批次 revision 与当前 session 不一致时必须在工作副本创建前拒绝，状态零变化。

## 6. `IronLawViolation`

冲突不是自然语言警告，而是结构化代码结果：

- `code`：稳定机器码；
- `domain`：presence、item_custody、disclosure、commitment、anchor、irreversible、world_boundary；
- `message`、可选 `path` 与 `evidence`；
- `retryable`：是否可以把反馈交给生成器进行有限重写。

当前检查器覆盖：人物必须已知、可见且沿当前出口相邻移动；关键物品必须可见、可携带、当前归属精确匹配并与接收者同场；人物只能本人向同场听众披露作者秘密；承诺双方必须同场，关联物品必须可见；物品承诺只有交付同批成立时才能标记兑现。未知实体和未声明路线也会被代码拒绝。

一批中任一事实冲突则整批拒绝。默认最多重写一次；仍冲突或遇到不可重试错误时，回合、revision、checkpoint、记忆和状态全部不变。作者的自由文本 `boundaries` 仍是生成约束；目前只有实体集合、空间图、物品声明和主体在场等结构化边界能确定性检查，通用边界 DSL 尚未实现。

## 7. `DirectorPlan`

DirectorPlan 绑定提交后 revision，包含 0—2 个 `DirectorBeat` 与最多一条 Local Canon 提议。它本身不权威：

- `enter_scene` 必须复验作者角色、当前位置、目标场景和邻接；
- `react` / `advance_plan` 要求人物已在场；
- 同一人物每回合最多一个节拍；
- Local Canon 另走原型、预算和冲突准入。

provider 故障或整份计划被拒绝不能撤销已完成的玩家事实批次。当前 Director 仍在事实提交后运行，因此未经 Phase 2 抽取的 `react` / `advance_plan` 摘要只记入 trace，不进入玩家输出或 recent memory；合法 `enter_scene` 只显示引擎从已提交人物与地点合成的确定性提示。

## 8. `NpcTurn`

NpcTurn 为 Phase 4 预留，包含 actor、对白、行动文本、目标、待抽取事实与记忆笔记。它不能携带状态 patch。NPC 输出必须和玩家/Director 生成的散文一起进入事实抽取，才能影响世界。

## 9. 提交记录

`CommittedChange` 的 `FactAuthority` 是事实来源，而不是模型请求的权限：

- `iron_law`：通过代码校验的位置、物品等变化；
- `local_canon`：通过生成准入的局部事实；
- `canon_anchor`：作者锚点产生的变化。

`CommittedTurn` 记录 batch、revision 前后、场景前后、散文、变化、Director、Local Canon、锚点、新事实与结局，不再依赖计划 ID 或验证 ID。

## 10. Provider 接口

`LLMProvider` 的当前表面：

- `render_narrative`；
- `extract_facts`；
- `propose_suggestions`；
- `propose_director`；
- `propose_npc_turn`（预留，当前 provider 可不支持）。

DeepSeek 使用纯文本叙事，以及事实抽取、轻量行动卡和 DirectorPlan 三种 JSON 输出。mock provider 对事实抽取采取 fail-closed：返回空事实，不猜测权威变化。

## 11. Revision 与 trace

快照、行动卡、FactExtraction、FactBatch、DirectorPlan 和 NpcTurn 都绑定 `state_revision`。提交或恢复 checkpoint 后 revision 递增，旧产物不得复用。

Trace 记录 `fact_extraction`、`iron_law_validation`、`narrative_regeneration` 与 `turn_committed`，包含 provider 原始响应、结构化 violation、最终 patch 与 `CommittedTurn`。失败候选仍可审计，但不会进入权威时间线。
