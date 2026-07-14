# AIRPG — LLM 原生互动叙事引擎

一个"强导演 AI 互动叙事"引擎：**LLM 是大脑，能力模块是手脚，判定核心是账本，作者剧本是锚点，玩家的自由表达是唯一输入。**

本文档描述项目的**终局形态**，为后续实现指明方向。它是活文档：每次里程碑复盘时都应审视一遍——修正不合理之处、补充当时未察觉的必要模块、替换更好的方案（见文末[修订约定](#修订约定)）。

三条不可动摇的底线（历次试玩复盘沉淀，详见 [engine-principles](docs/engine-principles.md)）：

1. **不做某个故事或某个类型的专用引擎**——引擎代码不认识故事专名，词汇全部属于内容。
2. **LLM 不是文字润色器**——它承担理解、规划、创造、扮演与导演，而非只把模板文本说得好听。
3. **判定不交给 LLM**——世界事实、权限、不变量与原子提交由确定性内核裁决，LLM 永远不当裁判。

## 终局形态总图

![AIRPG 终局系统形态](docs/assets/final-architecture.svg)

中间竖列是运行时管线：理解 → 能力验证 → 原子提交 → 世界反应 → 叙事渲染。蓝色环节由 LLM 承担，灰色是确定性引擎，右侧琥珀色是创作者资产。目标循环（2026-07-14 修订）：

```text
玩家自由输入，或选择一张经过验证的 LLM 行动提案
→ LLM 理解并生成 ActionPlan
→ 能力工具验证位置、物品、资源和权限
→ 将拒绝项返回 LLM，允许有限重规划
→ 可逆行动自动执行；不可逆行动显示报价并确认
→ 引擎原子提交 Mechanical / Local Canon 结果
→ Storylet 响应新状态，LLM Director 调度既有人物与世界反应
→ 角色记忆和长期计划更新
→ LLM 渲染叙事与机械回执
→ 玩家继续、撤回修改或从提交点创建分支
```

## 玩家如何与系统交互

玩家可以直接说话，也可以选择 3—5 张 LLM 根据当前局势生成的完整行动提案。CLI 的意图菜单、编号、对象 ID 都是阶段 2 脚手架，终局界面不暴露这些底层工具。一个回合：

1. **自由表达或选择提案**："我假装醉酒撞翻酒盘，趁乱溜向侧梯。"不需要任何系统词汇；提案只是灵感，不是行为白名单。
2. **能力验证**：系统把目标、手段和对象转成计划；接不住就明说或澄清，不猜一个执行、不消耗世界时间。
3. **按可逆性执行**：低风险和可逆行动自动提交；死亡、唯一物品消耗、永久关系破裂等不可逆行为才显示完整报价。
4. **结果 = 散文 + 回执**：LLM 叙事负责沉浸，机械回执（状态变化、来源、主目标状态、判定档位）恒显示。
5. **可撤回的权威状态**：重新生成叙事不改状态；撤回行动恢复父提交并创建新分支，旧验证结果立即失效。最小协议见 [state-timeline](docs/state-timeline.md)。
6. **对话与人物调度即玩法**：NPC 按动机接话；重要人物由作者预定义，Director 负责决定既有人物何时进场和如何反应。

对玩家的四条承诺：**看到的都是真的**（叙事不虚构玩法事实）、**没接住就不收费**（无效输入零成本）、**世界记得一切**（账本从不出错）、**不满意可以回到分岔点**（分支保留完整历史）。

## 故事创作者如何与系统交互

创作者是**声明世界的立法者**，不是枚举所有合法互动的程序员。他们提交四类内容资产（今天是 YAML，终局有编辑器包装；完整边界见 [authoring-contract](docs/authoring-contract.md)，schema 见 [content-schema](docs/content-schema.md)）：

1. **世界事实**：空间图、重要人物及动机 / 秘密、物品位置、初始状态——账本的初始页。重要人物属于 Core Canon，LLM 只能调度而不能临场创造。
2. **因果锚点**：storylet 不是行为白名单，而是关键因果的承诺（"信任到 3，艾拉会交出钥匙"）、节拍与结局。锚点之间的缝隙由 LLM + 能力模块即兴填充。
3. **边界与披露**：`resolution_limits` 声明数值世界的物理常数，`perception` 声明玩家能看见什么以及怎么称呼（感知墙），世界边界声明什么绝不可能发生。这是给 LLM 戴的镣铐，也是作者主权的表达。
4. **能力模块选装单**：类型 = 一组默认能力、UI 与 Prompt 配置，不是另一套引擎。

配套工具链让创作像带 CI 的开发：**校验器**在写作时拦住已知类别的坑（一类 bug 一条规则）、**walkthrough 模拟器**保证可解性回归、**LLM 玩家代理**自动跑几十局做平衡、**trace 回放**让每一局的每个计划、验证、报价、提交都可审计。

创作者永远不写代码、不写 prompt；引擎不认识他们的词汇，他们也不需要认识引擎的。

## 类型 = 能力模块选装

| 能力模块 | 悬疑（当前） | 都市日常 | 修仙 |
|---|---|---|---|
| 空间 / 位置 | ✅ | ✅ | ✅（需大图分层） |
| 物品 / 背包 | ✅（缺拾取、消耗） | ✅ | 需消耗 + 合成 |
| 事实日志 | ✅ | ✅ | ✅ |
| 关系 / 对话 | 部分（已支持请求物品） | **核心，已有首个纵切** | 需要 |
| 时间 | 可选倒计时 | **日程循环，缺** | **跳跃蒙太奇，缺** |
| 战斗 | 不需要 | 不需要 | **核心，缺** |
| 经济 | 不需要 | 轻量，缺 | 中等 |

每个模块不仅提供规则，还向 LLM 暴露工具、观察信息、可提议效果和不变量。执行边界见 [llm-action-plan-protocol](docs/llm-action-plan-protocol.md)，提案与导演节拍见 [action-suggestions-and-director-beats](docs/action-suggestions-and-director-beats.md)。

## 现状对照（2026-07-14）

**已落地**：理解 → 路由 → 报价 → 账本 → 渲染的 v0.1 纵向链路（DeepSeek + mock/replay）；感知墙；报价约束力与旧 fail-forward 路径；澄清延续；trace（含结果来源、主目标状态与纯代价标记）；低风险自由行动自动提交；内存 checkpoint / 撤回 / 状态分支及 CLI 入口；动态 `SuggestedAction`（DeepSeek / mock、生成后校验与去重、revision 失效、CLI `ideas` / `idea <n>`）；首个通用能力 `social.request_item`（用途不泄露隐藏物品、确定性同意 / 拒绝与物品转移）；`DirectorBeat` 的 DeepSeek / mock 生成、既有人物候选、确定性复验、故障隔离与同回合原子提交；最小 Local Canon（作者 `generation` 立法：地点 / 局势原型 + 硬预算；Director 通道每回合最多 1 条提议，经原型 / 预算 / 命名 / 冲突 / 生命周期准入后原子提交 `local_canon` 权限层；生成地点自动可达、局势按期过期、随 checkpoint 参与撤回分支）；无普通行动 storylet 的开放场景回归；校验器 / walkthrough / 单元测试（129 项）。《午夜前的档案室》保留显式倒计时，《晚风中的一桌饭》已切换为无倒计时自由叙事配置。

**雏形**：Director 已能在玩家提交内安排相邻既有人物进场、在场人物的表现层反应与受限生成提议，但结构化日程、长期 NPC 计划状态与节拍冷却尚未建立；能力路由已抽出一个通用社会能力，但正式模块契约和“选装”尚未成立；Local Canon 覆盖局部地点与局势，次要 NPC 晋升和承诺 / 任务类事实未做；现有《午夜前的档案室》和都市日常《晚风中的一桌饭》两个样板故事。

**未开始**：正式可插拔能力模块契约、checkpoint 跨进程持久化与完整消息树 UI、完整对话 / 战斗 / 日程 / 经济模块、跨会话成长、Web UI、LLM 玩家代理。

**演进顺序**（依据与详情见 [mvp-implementation-plan](mvp-implementation-plan.md) 与 [dev-notes](dev-notes/)）：

1. ~~修订作者契约与守则，前置开放性 trace 指标，移除默认倒计时 / 默认报价的政策假设；~~（2026-07-14 完成）
2. ~~实现最小 checkpoint、撤回与状态分支语义；~~（2026-07-14 完成内存版）
3. ~~用 `social.request_item` 完成第一个通用能力纵切，Storylet 退回剧情锚点；~~（2026-07-14 完成，含独立开放场景 fixture）
4. ~~建立经验证的 LLM 行动提案层与既有人物 Director Beat，并接入同回合调度 / 提交；~~（2026-07-14 完成最小纵切）
5. ~~实现最小 Local Canon（先地点 / 局势，重要人物禁止临场生成）；~~（2026-07-14 完成：`generation` 立法 + Director 提议通道 + 准入 / 预算 / 过期 / 分支回滚）
6. 用真实 DeepSeek 在开放场景试玩，评估提案质量、生成质量与开放性指标，再决定正式 Capability Module Contract；
7. 最后建设长期摘要、完整消息树 UI、Web 产品外壳与 LLM 玩家代理。

## 修订约定

- 本文档描述**终点**，[engine-principles](docs/engine-principles.md) 约束**过程**（遇到问题改哪一层）；两者冲突时先在复盘中解决冲突，再改文档。
- 每个里程碑（新故事、新模块、阶段切换）复盘时通读本文档：修正被实践证伪的判断、添加新识别的必要模块、更新现状对照与总图。
- 修改需在下表留痕，重大方向变更应同时在 dev-notes 记录论证过程。

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-07-12 | v0.1 | 初版：终局总图、玩家 / 创作者交互契约、模块选装表、现状对照与演进顺序 |
| 2026-07-14 | v0.2 | 重划作者 / 引擎 / LLM 权限；默认无倒计时、可逆行动自动执行并支持状态分支；定义动态行动提案、既有人物调度与 Local Canon 边界 |
| 2026-07-14 | v0.3 | 实现动态行动提案、`social.request_item` 通用能力与既有人物 Director Beat 校验，补齐内容策略校验和 CLI 入口 |
| 2026-07-14 | v0.4 | 将 Director Beat 接入提交后候选生成、确定性复验和同回合 checkpoint；新增无普通 storylet 的开放场景回归 |
| 2026-07-14 | v0.5 | 实现最小 Local Canon：`generation` 生成边界 schema、`local_canon` 权限层、Director 提议通道与准入 / 预算 / 过期 / 分支回滚；演进顺序第 5 项完成 |

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 游玩（LLM 模式需要 DEEPSEEK_API_KEY）
python server/cli.py --llm deepseek   # 未指定故事时从 content 目录选择
python server/cli.py content/rooftop_supper.yaml --llm deepseek
python server/cli.py content/midnight_archive.yaml --llm deepseek
# 校验内容与可解性回归
python tools/validate_content.py content/midnight_archive.yaml
python tools/check_walkthroughs.py content/walkthroughs/midnight_archive.yaml
# 测试
python -m pytest tests/ -q
```
