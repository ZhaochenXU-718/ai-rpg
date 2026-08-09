# AIRPG — 叙事优先互动小说实验

AIRPG 当前验证一条尽量轻的循环：玩家用自然语言行动，模型结合当前感知、作者私有上下文与软记忆续写散文；只有人物换场和关键物品转手会进入物理账本，其余内容留在叙事记忆里。

## 当前循环

```text
玩家自由输入或采用一张行动提案
→ 组合滚动小结与尚未压缩的近期事件
→ 编排器筛选候选剧情模块（纯代码，无状态权限）
→ 编译作者私有上下文（故事蓝图 + 在场人物卡 + 候选模块）
→ 模型生成候选散文
→ 模型只抽取人物换场 / 关键物品转手，代码校验初稿
→ 可选行编辑器（默认关闭；shadow 只记录；on 复核物理变化后采用）
→ 通过后原子提交；冲突时最多重写一次
→ 保存 revision、checkpoint、trace 和分支内叙事记忆
→ 旧事件达到阈值时，尽力更新一次软小结
```

当前没有前验意图路由、能力菜单、数值关系、承诺账本、秘密披露账本、任务旗标、storylet/effect、条件结局、Director、Local Canon 或动态生成预算。

这些机制不是被判定为永远无用，而是从默认核心移除。先用记忆、小结和长篇试玩找到真实失真模式，再只为有证据的问题增加最小约束。

## 权威状态

默认权威状态只包含：

- `positions`：人物当前在哪个作者场景；
- `item_locations`：关键物品在场景中，或由哪个人物携带。

普通对话、约定、关系、情绪、局势与剧情意义不会被硬编码成状态字段。M1 保存当前分支的完整原始事件日志；M2 在旧事件达到窗口或字符预算时生成滚动小结；M3 将小结（按预算裁剪）与尚未压缩的近期事件（逐字保留，超限时整条丢弃最旧）组合成同一份 `MemoryContext`，供旁白和行动提案使用。

叙事记忆与物理状态分开：它随 checkpoint 撤回和分支，但不能修改人物位置或关键物品归属，也不会进入事实抽取 prompt。小结失败不会回滚已提交回合，原始事件始终保留。可用 `memory` 查看当前软记忆，用 `memory raw` 查看最近原始事件。

## 保留的安全底座

- 主体感知隔离：玩家看不到远处人物及其私有卡片；
- 作者私有上下文单独走通道：故事蓝图、在场人物卡与候选模块只进旁白，不进玩家感知、行动提案和事实抽取；
- 剧情模块只携带叙事语义：没有 effects 与状态 patch，生命周期是软记忆并随分支恢复；
- 后验物理事实检查：抽取器没有直接 patch 权限；
- 原子提交：整批通过才修改状态；
- revision：旧抽取不能跨状态提交；
- checkpoint / undo / branch：撤回创建保留历史的新分支；
- trace：生成、抽取、冲突、重写和提交可审计；
- 内容校验：只检查引用完整性与物理账本初值，不裁决剧情。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 当前没有正式活动展示故事；可用最小测试夹具做无网络冒烟
python server/cli.py tests/fixtures/open_neighbor_scene.yaml --llm mock

# 使用 DeepSeek
export DEEPSEEK_API_KEY="你的密钥"
python server/cli.py tests/fixtures/open_neighbor_scene.yaml --llm deepseek

# 使用 Kimi（Moonshot AI；也可用 MOONSHOT_API_KEY，模型可用 KIMI_MODEL 覆盖，默认 kimi-k3）
export KIMI_API_KEY="你的密钥"
python server/cli.py tests/fixtures/open_neighbor_scene.yaml --llm kimi

# Web 客户端（同一引擎，浏览器访问 http://127.0.0.1:8642）
python server/web.py content/drafts/hogwarts_before_hogwarts.yaml --llm kimi

# 故事工作室（浏览器访问 http://127.0.0.1:8643）
# mock 提供离线开发建议；也可改为 deepseek 或 kimi 使用真实创作助手。
python server/studio.py --llm mock

# 可选行编辑器：建议先用 shadow 收集初稿/候选稿对照
python server/web.py content/drafts/hogwarts_before_hogwarts.yaml --llm kimi --prose-editor shadow

# 开发用 Web 对照面板：逐回合并排显示编辑前后正文与最终选择
python server/web.py content/drafts/hogwarts_before_hogwarts.yaml --llm kimi --prose-editor shadow --show-editor-comparison

# 校验与测试
python tools/validate_content.py tests/fixtures/open_neighbor_scene.yaml
python -m unittest discover -s tests -v
```

CLI 命令：`ideas`、`idea <编号>`、`who`、`state`、`memory`、`memory raw`、`undo`、`timeline`、`help`、`quit`。

Web 客户端（`server/web.py` + `web/index.html`，零额外依赖）：旁白按 SSE 流式逐块显示；「灵感」按钮弹出行动提案卡，点击卡片直接作为行动执行，支持刷新重新生成；刷新页面后从已提交事件恢复完整对局记录。开发时可用 `--show-editor-comparison` 显示逐回合编辑前后对照，该开关要求编辑器处于 `shadow` 或 `on`。一个服务进程承载一个会话，与 CLI 相同；`undo`/`memory` 等调试入口暂未搬入 Web，需要时仍用 CLI。

故事工作室（`server/studio.py` + `web/studio.html`）把 YAML 隐藏在服务边界之后：创作者通过故事蓝图、玩家角色、人物、场景、关键物品、剧情模块、开场和文风页面手动创作；工作室自动生成稳定 ID、维护实体引用、原子保存草稿并复用现有内容校验。通过全部阻塞检查后，可以从工作室启动使用当前草稿快照的 Web 试玩会话。“题材与类型”采用可自由输入的建议标签，结构化标签可供未来故事库筛选与归类，但不触发引擎专属规则。

内容目录按“可变草稿 / 不可变发布快照”分层：`content/drafts/` 是工作室管理的草稿；`content/releases/<故事id>/` 存放发布产生的不可变版本快照和 `releases.json`（发布记录与当前版本指针）；`content/templates/` 是手写 YAML 参考模板；`content/archive/` 是 pre-pivot 历史样本。发布要求零阻塞校验错误且所有 LLM 内容均已审阅；版本号由系统递增，回滚只移动当前版本指针，快照永不改写。已发布的快照可直接交给运行时游玩，例如 `python server/web.py content/releases/<故事id>/1.0.0.yaml --llm kimi`。

「从想法开始」入口支持从一段创意简报出发：选择篇幅档位（短/中/长，附建议规模区间）后，助手给出 2–3 个可讨论的故事方向；采用的概念以未审阅状态写入故事蓝图，仍由创作者逐项确认。关键文本字段提供「灵感」「补全/润色」「检查」三类创作者 LLM 操作。候选生成时不会修改故事；创作者可以选择「采用，稍后审阅」或「采用并确认」。采用后，字段来源与审阅状态保存在独立创作元数据中，未审阅内容会持续提示。`--llm deepseek` 使用 `DEEPSEEK_API_KEY`，`--llm kimi` 使用 `KIMI_API_KEY`；`mock` 不需要网络或 API key。创作与游玩两侧的模型可分别配置：`--llm` 是同时设置两者的快捷方式，`--authoring-llm` 覆盖创作助手模型，`--play-llm` 覆盖内嵌试玩模型。完整故事的分阶段批量生成仍是后续阶段。

## 文档

- [引擎守则](docs/engine-principles.md)
- [叙事协议](docs/narrative-first-protocol.md)
- [作者契约](docs/authoring-contract.md)
- [内容 schema](docs/content-schema.md)
- [故事工作室方案设计](docs/story-studio-design.md)
- [状态时间线](docs/state-timeline.md)
- [记忆与小结](docs/memory-and-summaries.md)
- [行动提案](docs/action-suggestions.md)
- [可选行编辑器](docs/prose-editor.md)

当前没有正式活动展示故事。`tests/fixtures/open_neighbor_scene.yaml` 只用于引擎测试，不代表产品体验。

`content/archive/midnight_archive.yaml` 与 `content/archive/rooftop_supper.yaml` 均为 `pre_pivot_archive` 历史样本，只保留研究价值，不能启动新会话，也不再作为当前设计样板。
