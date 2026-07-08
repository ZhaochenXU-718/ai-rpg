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
-> 系统理解方案并报价风险
-> 玩家确认或修改
-> 规则 + LLM 判定
-> 世界状态更新
-> 导演选择下一事件
-> LLM 渲染叙事反馈
-> 记录成本、延迟、公平感日志
```

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

复杂行动必须先报价，再执行。

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
  scene: great_hall
  time_left: 6
  evidence_status: intact
  current_goal: "午夜前进入档案室，找到证据"
```

### 5.2 玩家状态

```yaml
player:
  injury: 0
  exposed: false
  has_key: false
  has_evidence: false
  reputation: "unknown"
```

### 5.3 NPC 状态

```yaml
npcs:
  butler:
    suspicion: 2
    trust: 0
    location: corridor
    knows_player_goal: false
  maid:
    suspicion: 1
    trust: 2
    location: great_hall
    secret: "看见管家午夜前进入过档案室"
  guard:
    alertness: 2
    location: archive_door
```

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

## 7. 事件卡 / Storylet 设计

第一版只需要 8-12 张事件卡。

事件卡格式：

```yaml
id: maid_warning
title: "侍女的低声提醒"
trigger:
  scene: great_hall
  maid_trust_gte: 2
  evidence_status: intact
once: true
director_intent: reveal_clue
effect:
  flags:
    maid_warned_player: true
  add_clue: "管家午夜前进入过档案室"
narrative_hint: "侍女避开管家的视线，压低声音提醒玩家。"
```

事件卡类型：

- 加压：时间减少、守卫警觉、管家封锁。
- 揭示：线索、秘密、动机。
- 奖励：获得钥匙、信任、进入机会。
- 反转：玩家行为带来意外怀疑。
- 收束：进入档案室、找到证据、触发结局。

## 8. LLM 使用策略

### 8.1 第一版调用点

每回合最多 1-2 次 LLM 调用。

推荐：

1. 理解与报价：把玩家自然语言转成结构化 action，并生成风险报价。
2. 叙事渲染：根据判定结果和状态变化生成一段文本反馈。

判定本身尽量由规则承担，LLM 不单独决定成败。

### 8.2 可替换为人工

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
| 对象面板 | 当前人物、物品、环境、状态 |
| 输入区 | 玩家自然语言方案 |
| 报价卡 | 系统理解、收益、风险、代价、确认按钮 |
| 状态栏 | 时间、怀疑、信任、证据状态 |
| 调试面板 | JSON 状态、LLM 调用、token、延迟 |

调试面板只给内部测试者使用。

## 10. 工程结构建议

第一版建议使用 Python + FastAPI + React/Vite。

仓库结构：

```text
content/
  midnight_archive.yaml

server/
  main.py
  engine/
    state.py
    content_loader.py
    intent.py
    quote.py
    resolver.py
    director.py
    renderer.py
    logger.py
    llm.py

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
  "understanding": "...",
  "benefits": [],
  "risks": [],
  "costs": {},
  "can_execute": true
}
```

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
  "turn_id": "...",
  "felt_understood": 5,
  "felt_fair": 4,
  "comment": "系统理解了我的火情方案，但代价有点重"
}
```

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
| 单回合延迟 | P50 < 5 秒 |
| 单局 LLM 调用成本 | 需要实测并记录 |
| 内容生产工时 | 记录从写作到可玩版本的人时 |

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

### 阶段 1：内容与状态骨架，2 天

任务：

- 定义通用内容协议，并写入 `docs/content-schema.md`。
- 实现 `tools/validate_content.py`，用于校验故事 YAML 是否符合协议。
- 写 `content/midnight_archive.yaml`。
- 定义 3-5 个场景。
- 定义 NPC、对象、状态变量。
- 定义 8-12 张事件卡。
- 写 3 个结局条件。

完成标准：

- `content/midnight_archive.yaml` 通过内容格式校验。
- 不接 LLM，也能人工跑完故事流程。
- 阶段 2 的规则引擎只依赖内容协议，不依赖某个具体故事的硬编码字段。

### 阶段 2：本地规则引擎，2-3 天

任务：

- 实现 session state。
- 实现 intent -> action 映射。
- 实现 quote 生成的规则版。
- 实现 resolve 判定。
- 实现 state patch。
- 实现 storylet trigger。

完成标准：

- 命令行或 API 能跑通一个完整流程。
- 每回合状态变化可追踪。

### 阶段 3：LLM 接入，2-3 天

任务：

- 接入 LLM provider 抽象。
- 用 LLM 辅助生成理解与报价。
- 用 LLM 渲染判定后的叙事反馈。
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

建议从以下任务开始实现：

1. 定义内容协议：`docs/content-schema.md`。
2. 实现内容校验：`tools/validate_content.py`。
3. 创建项目骨架：`content/`、`server/`、`web/`、`data/sessions/`。
4. 写 `content/midnight_archive.yaml` 的第一版故事数据，并通过校验。
5. 实现无 LLM 的规则引擎，让故事能在命令行跑通。
6. 实现 `/api/session`、`/api/action/quote`、`/api/action/resolve`。
7. 做最简 Web UI。
8. 接入 mock LLM，再接真实 LLM。
9. 找 5-10 人测试。

第一句工程目标：

> 先让一个玩家在浏览器里用“意图按钮 + 自然语言方案 + 风险报价 + 判定后果”的方式，完整玩完《午夜前的档案室》。
