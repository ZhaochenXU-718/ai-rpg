# AIRPG MVP 实施计划

日期：2026-07-08

本文档用于指导 AIRPG 第一个 MVP 的实现。目标不是先搭完整平台，而是用一个高控制度、半人工、可观测的短篇实验，验证核心体验是否成立。

## 1. MVP 核心命题

本 MVP 只验证一个最关键命题：

> 当玩家提出具体行动方案时，系统是否能认真理解、报价风险、给出公平判定，并把结果转化为后续因果，从而让玩家产生“这是我的故事版本”的感觉。

第一版不追求：

- 无限开放世界。
- 泛角色聊天。
- 完整创作者平台。
- 多模态表现。
- 复杂长期记忆。
- 大规模自动内容生成。

第一版必须追求：

- 玩家输入被认真接住。
- 失败也能推进故事。
- 判定过程让玩家觉得公平。
- 单局成本、延迟和内容生产工时可测量。

## 2. 技术路线判断

### 2.1 Open-Theatre 的定位

Open-Theatre 适合作为参考样机，不适合作为正式 MVP 地基。

可借鉴：

- Director / Actor prompt 组织方式。
- 剧本 YAML 的场景、角色、动机写法。
- 多角色互动戏剧的运行经验。
- 记忆系统与场景摘要思路。

不直接继承：

- 以聊天为中心的 drama loop。
- 玩家输入直接进入 NPC 回复的流程。
- 与其 UI 和脚本结构强绑定的运行时。

### 2.2 AIRPG MVP 自研薄引擎

MVP 应在当前 AIRPG 仓库中从头实现一个很薄的 action/state/director loop。

推荐核心循环：

```text
玩家选择意图
-> 玩家输入执行方案
-> 输入分类（规则内 / 规则外可转化 / 越界）
-> 系统理解方案并报价风险（低风险意图跳过报价）
-> 玩家确认或修改
-> 规则判定：优先匹配 storylet，未命中走受限兜底判定
-> 世界状态更新
-> 导演选择下一事件
-> LLM 渲染叙事反馈
-> 记录成本、延迟、公平感日志
```

遇到体验问题时改哪一层（内容 / 校验器 / 引擎 / 留给 LLM），遵循 `docs/engine-principles.md` 的修改守则——引擎判定核心不为单个故事的体验反馈而改。

输入分类改造自 Open-Theatre 导演 prompt 的"响应准则"（情节内 / 情节外-日常 / 情节外-打破）：

| 分类 | 含义 | 处理 |
|---|---|---|
| 规则内 | 方案落在意图空间和世界规则内 | 正常报价 / 执行 |
| 规则外可转化 | 方案有创意但超出预设，可映射到白名单状态变化 | 走兜底判定（见 6.3） |
| 越界 | 违反 `player_role.constraints` 或 `global_rules.boundaries` | 报价返回 `can_execute: false`，用世界内语言解释（"暴风雪封死了山路"），不消耗时间 |

## 3. MVP 故事范围

### 3.1 推荐题材

首个短篇采用原创“暴风雪山庄 / 午夜档案室”结构。

原因：

- 空间封闭，容易控制边界。
- 目标明确，便于防止故事空转。
- 适合验证潜入、交涉、制造混乱、观察等意图。
- 适合设计时间压力、怀疑值、信任值和证据状态。
- 不依赖外部 IP。

### 3.2 工作标题

《午夜前的档案室》

### 3.3 玩家目标

玩家必须在午夜前进入档案室，找到即将被销毁的证据。

如果午夜前没有拿到证据，证据会被烧毁，故事进入失败或坏结局分支。

### 3.4 时长

目标体验时长：20-30 分钟。

场景数量：3-5 个。

结局数量：3 个即可。

推荐结局：

- 真相曝光：拿到核心证据，并获得关键 NPC 支持。
- 代价胜利：拿到证据，但暴露身份或牺牲某个关系。
- 证据被毁：未能及时进入档案室，但发现另一个线索，为坏结局或续章留钩子。

## 4. 核心玩法结构

### 4.1 每回合交互

每个回合包含：

1. 当前剧情目标。
2. 当前场景对象。
3. 世界状态摘要。
4. 意图按钮。
5. 自然语言执行输入。
6. 系统风险报价。
7. 玩家确认 / 修改。
8. 判定结果。
9. 状态变化与叙事反馈。

### 4.2 意图按钮

第一版固定 6 个意图：

| 意图 | 用途 |
|---|---|
| 观察 | 获取场景、人物、物品或异常信息 |
| 交涉 | 说服、套话、安抚、交换条件 |
| 潜入 | 避开注意，进入限制区域 |
| 制造混乱 | 转移注意力，创造机会 |
| 威胁 | 施压、恐吓、摊牌 |
| 自定义方案 | 玩家提出非预设行动 |

### 4.3 可操作对象

每个场景提供有限对象，帮助玩家组织方案。

示例对象：

| 类型 | 对象 |
|---|---|
| 人物 | 管家、侍女、守卫、醉酒客人、女继承人 |
| 物品 | 油灯、银质钥匙、药箱、旧徽章、密信 |
| 环境 | 侧门、窗台、楼梯、雨声、壁炉、走廊阴影 |
| 状态 | 时间紧迫、守卫多疑、侍女害怕、火势风险 |

### 4.4 判定报价

报价按意图风险分层，不是每回合都报价——否则每回合五步交互（选意图、打字、读报价、确认、读结果）会拖垮节奏：

| 意图 `quote_required` | 行为 |
|---|---|
| `false`（如观察） | 直接执行，结果叙事前用一句话复述系统的理解 |
| `true`（交涉、潜入、制造混乱、威胁、自定义） | 必须先报价，玩家确认后执行 |

报价的约束规则（防"报价刷单"，保护公平感）：

1. 报价对判定有约束力：执行结果的恶化程度不得超出报价列出的风险与代价范围。
2. 重新报价免费、不消耗 time_left，但同一回合最多 3 次。
3. 重新报价次数记入日志——高频改写方案本身是"玩家觉得没被理解"的信号。
4. 越界方案在报价阶段拦截（`can_execute: false` + 世界内解释），不进入判定。
5. `requires_storylet_match: true` 的结构化动作必须在报价前命中当前可用交互；参数不足、对象不可见或组合无规则时不报价、不计回合、不推进世界。
6. 确定性引擎在私有副本预演完整回合，报价分开列出固定代价与 storylet/world step 的预计影响；预演不修改真实状态。

中高风险行动必须先报价，再执行。

示例：

```text
玩家意图：制造混乱
玩家方案：我把油灯碰倒，但控制火势只烧窗帘，让人群往大厅另一侧撤。

系统报价：
我理解你想制造一次可控火情，迫使人群和守卫离开档案室门口。
可能收益：档案室门口短暂无人，潜入难度下降。
主要风险：火势失控、侍女怀疑你过于冷静、管家提前封锁二楼。
可能代价：fire_risk +1，maid_suspicion +1。
是否执行？
```

这样做的目的：

- 让玩家确认系统理解了自己。
- 让失败和代价提前可见。
- 降低“系统冤枉我”的感觉。

## 5. 世界状态设计

第一版世界状态使用手写 JSON / YAML 即可，不需要数据库。

### 5.1 全局状态

```yaml
world:
  chapter: 1
  step: 0
  time_left: 8   # 最优路线 7 回合 + 冗余，依据 walkthrough 用例
  evidence_status: intact
  current_goal: "午夜前进入档案室，找到证据"
```

玩家与 NPC 的物理位置由 schema v2 世界图统一维护：

```yaml
positions:
  player: great_hall
  butler: great_hall
  guard: archive_door
```

### 5.2 玩家状态

```yaml
player:
  injury: 0
  exposed: false
  reputation: "unknown"
```

关键物品不再复制为 `has_*` 布尔值，而由唯一物品位置派生背包：

```yaml
item_locations:
  servant_key: { type: carried_by, id: maid }
  old_badge: { type: carried_by, id: heir }
  forgery_evidence: { type: container, id: hidden_compartment }
```

### 5.3 NPC 状态

```yaml
npcs:
  butler:
    suspicion: 2
    trust: 0
    knows_player_goal: false
  maid:
    suspicion: 1
    trust: 2
    secret: "看见管家午夜前进入过档案室"
  guard:
    alertness: 2
```

注意：`positions` 是角色物理位置的唯一事实来源，`item_locations` 是物品位置/背包所有权的唯一事实来源。场景不维护静态人物名单或重复物品所有权；见 `docs/content-schema.md` 第 4-5 节。

### 5.4 场景状态

```yaml
scene:
  available_objects:
    - oil_lamp
    - side_door
    - medicine_box
    - rain_noise
  hazards:
    fire_risk: 0
    noise_level: 1
```

## 6. Action / 判定模型

每个玩家行动在内部落成结构化 action。

```yaml
action:
  intent: create_distraction
  player_text: "我把油灯碰倒，但控制火势只烧窗帘"
  targets:
    - oil_lamp
    - curtains
    - guard
  interpreted_plan: "制造可控火情，转移档案室门口注意力"
  risk_level: medium
  quoted_costs:
    fire_risk: 1
    maid_suspicion: 1
  possible_benefits:
    - archive_door_unwatched
```

判定结果采用三档：

| 结果 | 含义 |
|---|---|
| 成功 | 达成目标，付出轻微或无代价 |
| 部分成功 | 达成主要目标，但引入代价或新风险 |
| 失败推进 | 未达成目标，但产生新信息、新机会或剧情压力 |

第一版尽量避免“纯失败”。

### 6.1 判定优先级

每个确认执行的行动按以下顺序判定：

1. 应用兜底判定的白名单 patch，然后按内容顺序扫描 action storylet。
2. 推进 world step：从同一快照计算所有 NPC 的唯一移动提案并原子应用。
3. 扫描 after-world storylet，处理到达、相遇和追捕反应。
4. 全部判定都由规则执行，LLM 不单独决定成败。

### 6.2 Storylet 判定

命中 storylet 的行动，效果完全由内容文件定义，引擎只负责执行和记录。这是作者预设的因果，覆盖"作者想到的"创意空间。

### 6.3 兜底判定：MVP 的真正核心

**未命中 storylet 的方案才是"接住玩家"命题的主要考场**——命中预设事件卡的方案本来就不难接。兜底判定采用 Plan-Validate-Apply（参考 Orchestrated Reality 论文）：

```text
LLM 从玩家方案提议一组状态变化（Plan）
-> 引擎按 resolution_limits 白名单过滤、按边界裁剪（Validate）
-> 只应用通过的部分（Apply）
-> 渲染叙事时如实反映实际生效的变化，不夸大、不虚构
```

规则：

- 白名单（`resolution_limits.patchable`）覆盖软状态：NPC 信任 / 怀疑、噪音、火情、注意力等。
- 关键剧情事实（证据、钥匙、`positions.*`、`item_locations.*`、flags）在 `protected` 中，兜底判定永远不能触碰——玩家创意可以改变局面，但不能绕过作者设计的因果关卡。
- 被过滤 / 裁剪的提议记入日志。高频被拒路径 = 玩家普遍想影响某个作者没想到的维度，是内容迭代的直接信号。

## 7. 事件卡 / Storylet 设计

第一版 15-20 张事件卡。正式格式以 `docs/content-schema.md` 第 10 节为准，当前实例见 `content/midnight_archive.yaml`。

事件卡类型（对应 schema 的 `type` 词表）：

- 加压（pressure / failure_pressure）：时间减少、守卫警觉、管家封锁。
- 揭示（reveal）：线索、秘密、动机。
- 机会（opportunity / opportunity_with_cost）：临时窗口，用 `temporary` 块自动过期。
- 奖励（reward）：获得钥匙、信任、进入机会。
- 反转（consequence）：玩家行为带来意外怀疑。
- 推进 / 收束（progress / partial_progress / ending_route）：场景转场、找到证据、触发结局。

两类容易漏写的事件卡（本故事第一稿都漏了，靠 walkthrough 校验才发现）：

1. **转场卡**：初始场景之外的每个场景，都必须有 storylet 通过 `move_entities` 将玩家移动进去，否则世界图虽然存在，故事仍然跑不完。
2. **flag 供给卡**：`initial_state.flags` 里的每个 flag 至少要有一张卡会设置它，否则是死变量。

这两条已加入 `tools/validate_content.py` 的自动检查。

## 8. LLM 使用策略

### 8.1 第一版调用点

每回合最多 1-2 次 LLM 调用。

推荐：

1. 理解与报价：把玩家自然语言转成结构化 action（含输入分类和兜底判定的状态变化提议），并生成风险报价。低风险意图这一步退化为一句话理解复述，可与渲染合并。
2. 叙事渲染：根据判定结果和状态变化生成一段文本反馈。

判定本身尽量由规则承担，LLM 不单独决定成败。

### 8.2 渲染上下文规范

叙事渲染不需要 Open-Theatre 那套四层记忆系统（20-30 分钟短篇用不上），但必须明确定义喂给渲染 LLM 的最小上下文，否则文风和事实一致性会随回合数漂移：

```text
style_bible（文风约束）
+ 当前场景 entry_text
+ 最近 3-5 条叙事记录
+ 当前世界状态摘要（时间、怀疑、关键 flag）
+ 本回合判定结果与命中 storylet 的 narrative_hint
```

超出窗口的历史不滚动进 prompt，用状态变量代替记忆。

### 8.3 可替换为人工（Wizard-of-Oz）

第一版保留 Wizard-of-Oz 开关。

人工可介入：

- 修改系统对玩家方案的理解。
- 修改风险报价。
- 修改判定结果。
- 修改叙事反馈。

目的不是长期依赖人工，而是快速验证体验。

## 9. UI 范围

第一版 UI 做成 Web 原型。

不做复杂视觉小说系统，只做高可用界面。

页面区域：

| 区域 | 内容 |
|---|---|
| 主叙事区 | 当前剧情文本、NPC 反馈、判定结果 |
| 当前目标 | 本场景必须推进的目标 |
| 意图面板 | 6 个固定意图按钮 |
| 对象面板 | 当前人物、场景物品、环境、状态和可用出口 |
| 口袋 | 当前由玩家携带的物品，可作为“使用”意图的来源对象 |
| 输入区 | 玩家自然语言方案 |
| 报价卡 | 系统理解、收益、风险、代价、确认按钮；仅中高风险意图出现（见 4.4） |
| 状态栏 | 时间、怀疑、信任、证据状态 |
| 调试面板 | JSON 状态、LLM 调用、token、延迟 |

调试面板只给内部测试者使用。

## 10. 工程结构建议

第一版建议使用 Python + FastAPI + React/Vite。

仓库结构：

```text
content/
  midnight_archive.yaml
  walkthroughs/
    midnight_archive.yaml

tools/
  validate_content.py
  check_walkthroughs.py

server/
  cli.py            # 阶段 2：命令行游玩入口
  main.py           # 阶段 4：FastAPI
  engine/
    state.py        # 状态模型：命名空间、路径、patch 语义
    conditions.py   # 单一条件求值器（trigger/exit/endings 共用）
    effects.py      # 效果应用 + temporary 过期回滚
    world.py        # 抽象世界图 + 快照式 NPC 移动 + 原子 world step
    content.py      # 故事加载与访问器
    limits.py       # resolution_limits 校验与裁剪（Plan-Validate-Apply 的 V）
    quote.py        # 报价规则版（阶段 3 由 LLM 替换理解与提议）
    resolver.py     # 回合判定核心（10.3 语义）
    director.py     # 场景目标 / exit_conditions 信号
    renderer.py     # 模板叙事渲染（阶段 3 由 LLM 替换）
    session.py      # 会话：报价流程 + 判定 + 日志
    logger.py       # 回合 JSONL 日志
    walkthrough.py  # walkthrough 驱动（验收用例执行器）
    llm.py          # 阶段 3

web/
  src/
    App.tsx
    components/
      NarrativePanel.tsx
      IntentPanel.tsx
      ObjectPanel.tsx
      QuoteCard.tsx
      StatePanel.tsx
      DebugPanel.tsx

data/
  sessions/
```

## 11. API 草案

### 11.1 创建会话

```http
POST /api/session
```

返回：

```json
{
  "session_id": "...",
  "state": {},
  "scene": {},
  "narrative": "..."
}
```

### 11.2 提交行动方案，获取报价

```http
POST /api/action/quote
```

请求：

```json
{
  "session_id": "...",
  "intent": "create_distraction",
  "player_text": "我把油灯碰倒，但控制火势只烧窗帘"
}
```

返回：

```json
{
  "quote_id": "...",
  "classification": "in_rules | creative | out_of_bounds",
  "understanding": "...",
  "benefits": [],
  "risks": [],
  "costs": {},
  "expected_changes": [],
  "can_execute": true,
  "rejection_reason_in_world": null,
  "requote_count": 0
}
```

约定：

- `quote_required: false` 的意图跳过本接口，客户端直接调用 resolve。
- `out_of_bounds` 时 `can_execute` 为 false，`rejection_reason_in_world` 用世界内语言解释，不消耗 time_left。
- 同一回合 `requote_count` 达到 3 后拒绝继续报价。
- 报价中列出的 `risks`、`costs` 和 `expected_changes` 是 resolve 结果的约束；确定性部分必须与确认后的执行一致。

### 11.3 确认执行

```http
POST /api/action/resolve
```

请求：

```json
{
  "session_id": "...",
  "quote_id": "...",
  "confirmed": true
}
```

返回：

```json
{
  "result": "partial_success",
  "state_patch": {},
  "narrative": "...",
  "next_events": [],
  "metrics": {
    "latency_ms": 1200,
    "llm_calls": 1,
    "estimated_tokens": 1800
  }
}
```

### 11.4 反馈公平感

```http
POST /api/feedback
```

请求：

```json
{
  "session_id": "...",
  "scope": "scene | session",
  "scene_id": "great_hall",
  "felt_understood": 5,
  "felt_fair": 4,
  "unfair_turn_ids": ["..."],
  "comment": "系统理解了我的火情方案，但代价有点重"
}
```

反馈时机改为**每场景结束一次 + 局末一次**，不做每回合打分——每回合弹评分会打断沉浸，且样本质量低。局末反馈额外定点追问："哪一次判定让你觉得被冤枉？"（`unfair_turn_ids`），与该回合的报价、判定日志对齐分析。

## 12. 日志与指标

每回合必须记录：

- 玩家选择的意图。
- 玩家自然语言方案。
- 系统理解。
- 风险报价。
- 玩家是否确认或修改。
- 判定结果。
- 状态变化。
- LLM 调用次数。
- 延迟。
- token 成本估算。
- 玩家公平感反馈。

核心验证指标：

| 指标 | 目标 |
|---|---|
| 方案理解满意度 | 测试者主观评分 >= 4/5 |
| 判定公平感 | 测试者主观评分 >= 4/5 |
| 完成率 | 5-10 人测试中 >= 70% 完成 |
| 复述率 | >= 50% 愿意复述自己的独特行动 |
| 重玩意愿 | >= 30% 愿意尝试另一条路径 |
| 报价延迟 | P50 < 3 秒（有报价的回合玩家要等两次，必须分开测） |
| 判定 + 渲染延迟 | P50 < 5 秒 |
| 单局 LLM 调用成本 | 需要实测并记录 |
| 内容生产工时 | 记录从写作到可玩版本的人时 |

兜底判定专属指标（验证"接住玩家"命题）：

| 指标 | 说明 |
|---|---|
| 兜底命中率 | 多少行动未命中 storylet、走了兜底路径 |
| 提议裁剪率 | 兜底判定中 LLM 提议被白名单过滤 / 裁剪的比例，过高说明白名单太窄或 LLM 提议失控 |
| 高频被拒路径 | 玩家普遍想影响但白名单没覆盖的状态维度，内容迭代信号 |
| 重报价率 | 每回合平均重新报价次数，过高说明理解质量差 |

## 13. 实施阶段

### 阶段 0：Open-Theatre 快速拆解，可选，1-2 天

目标：

- 跑通 Open-Theatre。
- 观察 director / actor loop。
- 记录可借鉴的 prompt 和 script 结构。

产出：

- 一页拆解笔记。
- 不把 Open-Theatre 代码并入 AIRPG。

如果当前目标是尽快实现 AIRPG MVP，可跳过此阶段，直接进入阶段 1。

### 阶段 1：内容与状态骨架（已完成，2026-07-09 修订）

已产出：

- `docs/content-schema.md`：通用内容协议 v2（含世界图、人物/物品唯一位置、派生背包、显式使用、返回出口、world step 和判定协议）。
- `tools/validate_content.py`：格式校验 + 世界图 / 唯一位置 / 场景连通性 / 死 flag / 白名单交叉检查。
- `content/midnight_archive.yaml`：5 场景、20 张事件卡、3 结局。
- `content/walkthroughs/midnight_archive.yaml`：每个结局一条通关路线。
- `tools/check_walkthroughs.py`：按引擎语义逐回合模拟 walkthrough，验证三个结局都真实可达、时间预算成立。

完成标准（均已满足）：

- `content/midnight_archive.yaml` 通过内容格式校验。
- 四条 walkthrough 全部通过模拟（等价于"不接 LLM 也能跑完故事流程"，且是自动化的）。
- 阶段 2 的规则引擎只依赖内容协议，不依赖某个具体故事的硬编码字段。

### 阶段 2：本地规则引擎（已完成，2026-07-10 更新位置、对象与动作原子性）

已产出（`server/engine/`，模块职责见第 10 节）：

- session state、报价流程（分层报价、约束性报价、重报价限 3 次）、回合判定、JSONL 日志。
- resolve 判定：storylet 单遍级联优先，未命中走 resolution_limits 兜底（游玩路径裁剪、walkthrough 路径严格校验）。
- state patch 与 temporary 效果过期调度。
- 抽象世界图、每角色唯一位置、快照式 NPC 移动与原子 world step；在场人物和合法人物目标从位置派生。
- 唯一物品位置、派生口袋、显式“使用物品 + 目标”、以及基于世界图出口的返回移动。
- 状态条件对象（`visible_when` / `actionable_when`）、作者交互预检和无效动作原子拒绝。
- 确定性报价预演：固定代价、storylet/world step 影响完整可见且不污染真实状态。
- 单一条件求值器：trigger / exit_conditions / endings 共用；每回合最多一次玩家位置变化，NPC world step 独立结算。
- `server/cli.py`：命令行完整游玩（意图 + 对象 + 报价卡 + 确认 + 模板叙事）。
- `tools/check_walkthroughs.py` 已改为引擎的薄封装，判定语义只有一份实现。

完成标准（均已满足）：

- 命令行能跑通完整流程（真相曝光路线 7 回合实测通关，剩余时间 1，与 walkthrough 数学一致）。
- 每回合状态变化可追踪（日志含 fired storylets、状态 diff、报价/判定延迟、llm_calls=0）。
- 引擎跑四条 walkthrough 全部通过（walkthrough 即验收用例）。

规则版占位（阶段 3 由 LLM 替换，接口不变）：

- 理解与提议：`quote.py` 的 `default_proposal`（交涉→目标 NPC 信任 +1 等规则映射）。
- 叙事渲染：`renderer.py` 的模板文本（entry_text + narrative_hint + 状态摘要）。
- 输入分类：结构化输入下恒为 `in_rules`，`out_of_bounds` 留给自然语言输入。

### 阶段 3：LLM 接入（已完成，2026-07-12；2026-07-13 完成首轮试玩修复）

修改边界见 `docs/engine-principles.md` 第 5 节（LLM 只做理解与渲染，判定核心接口冻结）。

任务：

- 接入 LLM provider 抽象。
- 用 LLM 辅助生成理解与报价：把自由表达映射到意图 + 对象 ID + 白名单 patch 提议（如"我看看壁炉上摆着什么"→ `observe fireplace`）。CLI 阶段的精确别名映射到此为止，模糊理解是这一层的本职。
- 用 LLM 渲染判定后的叙事反馈，prompt 约束"只陈述状态中存在的事实"。
- 理解接不住时显式告知玩家，不猜测执行（公平感协议）。
- 保留规则判定为主。
- 增加 mock provider，支持无 API 开发。

完成标准：

- 每回合最多 1-2 次 LLM 调用。
- 失败时可回退到模板文本。

### 阶段 4：Web 原型，3-5 天

任务：

- 搭 FastAPI。
- 搭 React/Vite。
- 实现主叙事区、意图面板、对象面板、报价卡、状态栏、调试面板。
- 支持创建会话、报价、确认执行、反馈。

完成标准：

- 内部测试者可以在浏览器中完整玩 20-30 分钟。

### 阶段 4.5：LLM 玩家代理自动试玩，1-2 天

真人测试者是稀缺资源，不应消耗在崩溃、死锁和剧情空转上。借鉴 Open-Theatre 的 PlayerAgent 思路（其 10 种人格中"阴谋论者""杠精辩手""情绪冲动型"正是边界测试型玩家），在真人测试前自动跑局：

任务：

- 写一个 LLM 玩家代理，按人格 prompt 生成意图 + 自然语言方案。
- 3-5 种人格（配合型、边界试探型、拖延型、暴力型、创意型）。
- 自动跑 20-50 局，导出完整日志。

要筛掉的问题：

- 剧情空转（连续多回合无状态变化）。
- 状态死锁（无法到达任何结局）。
- 结局分布异常（某结局从未出现）。
- 兜底判定接不住的高频输入模式。
- 单局成本 / 延迟离谱的回合。

完成标准：

- 自动局的完成率和结局分布合理。
- 崩溃和死锁清零后再进入真人测试。

### 阶段 5：小样本测试，3-5 天

任务：

- 找 5-10 名测试者。
- 记录屏幕或日志。
- 每局后访谈 10 分钟。
- 统计复述率、重玩意愿、公平感、延迟、成本。

完成标准：

- 输出 MVP 测试报告。
- 判断是否值得进入完整导演引擎工程化。

## 14. 第一版不做的事情

为了保持验证速度，第一版明确不做：

- 用户账号系统。
- 支付系统。
- 创作者编辑器。
- 多故事市场。
- 图像 / 视频生成。
- 复杂装备和战斗系统。
- 自动长篇生成。
- 复杂多代理自治。
- 真正开放世界地图。

这些不是不重要，而是当前会干扰核心验证。

## 15. 风险与应对

### 15.1 玩家觉得 AI 没接住自己

应对：

- 增加系统复述理解。
- 允许玩家修改方案。
- 保留人工介入。

### 15.2 玩家觉得判定不公平

应对：

- 判定前报价。
- 判定后解释原因。
- 失败采用 fail-forward。
- 关键判定依据写入调试日志。

### 15.3 LLM 成本和延迟过高

应对：

- 每回合限制 1-2 次调用。
- 规则判定优先。
- 使用模板 fallback。
- 记录每回合成本。

### 15.4 内容生产太慢

应对：

- 从第一天记录创作工时。
- 复用事件卡模板。
- 保持短篇范围。
- 不提前承诺 UGC 平台。

## 16. 进入下一阶段的判断标准

只有当以下条件基本成立，才进入更完整的导演引擎开发：

1. 多数测试者觉得自己的具体方案被理解。
2. 多数测试者接受失败或代价。
3. 至少一半测试者能复述自己的独特经历。
4. 有明显重玩或追更意愿。
5. 单局成本和延迟没有明显失控。
6. 内容生产工时没有高到无法支撑下一篇。

如果这些不成立，应先调整题材、交互或判定方式，而不是继续扩大工程规模。

## 17. 下一步任务清单

已完成（阶段 1）：

- ~~定义内容协议：`docs/content-schema.md`。~~
- ~~实现内容校验：`tools/validate_content.py`。~~
- ~~写 `content/midnight_archive.yaml` 的第一版故事数据，并通过校验。~~
- ~~写 walkthrough 用例并实现 `tools/check_walkthroughs.py`，验证三个结局可达。~~

已完成（阶段 2，2026-07-10）：

- ~~创建 `server/` 骨架。~~
- ~~实现无 LLM 的规则引擎（`server/engine/`），命令行可完整玩通，walkthrough 作为验收用例通过。~~
- ~~升级 schema v2 世界图：唯一角色位置、快照式 world step、动态在场派生与通用校验。~~
- ~~实现唯一物品位置与口袋、显式使用意图、玩家返回出口，并补“无徽章破局”回归路线。~~
- ~~实现条件对象、关键交互导演提示、无匹配动作不扣时与确定性完整报价。~~
- ~~定义 LLM ActionPlan Protocol 0.1：玩家感知、能力步骤、状态提议、验证结果、提交结果与世界权限边界。~~
- ~~实现感知墙（`perception.py`）与静态能力路由器（`capabilities.py`）：报价披露过滤、权限映射、v0.1 承兑范围（2026-07-12）。~~
- ~~实现 LLMClient 抽象、Mock/Replay provider、trace 落盘（协议 §11），在 `custom` 自由文本上打通"理解—验证—一次重规划—报价—提交"纵向闭环（`llm.py` / `llm_loop.py` / `trace.py`，CLI `--llm mock`）；规则模式保留为测试与降级路径（2026-07-12）。~~

接下来按顺序：

1. ~~接入 DeepSeek 并完成三轮真实试玩；根据 trace 修复空提议公平性、节拍归因、叙事事实接地、custom 披露、裸自然语言 CLI 和空渲染重试（2026-07-12 至 2026-07-13）。~~ 再打一局验证修复效果。
2. 写故事 #2（关系驱动都市日常），同时做纸面模块化记账，用第二种内容验证模块边界。
3. 在两个故事的 trace 基础上重构正式能力模块，并以对话模块作为第一个正式实现。
4. 接入 LLM 导演反应与 NPC 动机规划。
5. 实现 `/api/session`、`/api/action/quote`、`/api/action/resolve`、`/api/feedback` 和最简 Web UI；随后写 LLM 玩家代理自动跑局。
6. 找 5-10 人测试。

第一句工程目标：

> 先证明同一套“自由表达 → 公平报价 → 确定性提交 → 事实叙事”链路能支撑两个机制重心不同的故事，再把它包装成 Web 产品。
