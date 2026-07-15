# AIRPG 叙事优先协议 0.2

日期：2026-07-15
实现：`server/engine/llm_protocol.py`、`llm.py`、`llm_deepseek.py`

## 1. 权威边界

协议 0.2 的核心不是“把玩家输入翻译成可执行命令”，而是把模型生成与世界事实隔开：

```text
PerceptionSnapshot
→ Narrative / SuggestedAction / DirectorPlan / NpcTurn（均无状态权限）
→ 散文
→ FactBatch（抽取结果）
→ IronLawViolation[] 或通过
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

Phase 1 的事实清单明确标注过渡期限制：只能描写玩家正在尝试，不能宣告未校验的铁律变化。

## 4. `SuggestedAction`

行动卡只有：

- `suggestion_id`；
- `perception_revision`；
- `title`、`action_text`、`focus`、`rationale`；
- `expected_iron_law_touches`。

它是可编辑第一人称散文，不绑定执行计划或验证结果。引擎根据可见出口、物品及文本中的披露/承诺措辞补充“可能触及的铁律”，只用于玩家知情和 trace，不是裁决。

任何状态 revision 变化都会使旧卡失效。玩家采用卡片时，`action_text` 与自由输入走同一条叙事管线。

## 5. `FactBatch`

`FactBatch` 是后验提交边界，字段包括：

- `batch_id`、`state_revision`；
- 玩家原话和本回合散文；
- 抽取出的 `state_changes` 与新事实；
- 已接地的可见实体引用。

Batch C 只建立该协议和原子提交缝。CLI 构造的批次没有普通行动状态变化；Phase 2 才会由 extractor 填充，并在进入 `commit_facts` 前完成铁律检查。批次 revision 与当前 session 不一致时必须拒绝，状态零变化。

## 6. `IronLawViolation`

冲突不是自然语言警告，而是结构化代码结果：

- `code`：稳定机器码；
- `domain`：presence、item_custody、disclosure、commitment、anchor、irreversible、world_boundary；
- `message`、可选 `path` 与 `evidence`；
- `retryable`：是否可以把反馈交给生成器进行有限重写。

Phase 2 必须做到：检查失败不提交部分状态；超过重生成上限后用世界内语言说明，没有隐性成本。

## 7. `DirectorPlan`

DirectorPlan 绑定提交后 revision，包含 0—2 个 `DirectorBeat` 与最多一条 Local Canon 提议。它本身不权威：

- `enter_scene` 必须复验作者角色、当前位置、目标场景和邻接；
- `react` / `advance_plan` 要求人物已在场；
- 同一人物每回合最多一个节拍；
- Local Canon 另走原型、预算和冲突准入。

provider 故障或整份计划被拒绝不能撤销已完成的玩家事实批次。

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
- `propose_suggestions`；
- `propose_director`；
- `propose_npc_turn`（预留，当前 provider 可不支持）。

DeepSeek 使用纯文本叙事与两种 JSON 输出：轻量行动卡和 DirectorPlan。mock provider 只读取快照中的通用实体，不认识故事专名。

## 11. Revision 与 trace

快照、行动卡、FactBatch、DirectorPlan 和 NpcTurn 都绑定 `state_revision`。提交或恢复 checkpoint 后 revision 递增，旧产物不得复用。

Trace 至少记录输入作用域、provider 原始响应、模型版本、耗时、用量、提案卡、Director 接受/拒绝项、事实抽取、铁律冲突、最终提交和分支恢复。Phase 1 尚没有 extractor/checker 事件，CLI 用 `fact_extraction_deferred` 明确标记，不伪装为完整闭环。
