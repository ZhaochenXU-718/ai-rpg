# AIRPG — 叙事优先互动小说实验

AIRPG 当前验证一条尽量轻的循环：玩家用自然语言行动，模型结合当前感知、作者私有上下文与软记忆续写散文；只有人物换场和关键物品转手会进入物理账本，其余内容留在叙事记忆里。

## 当前循环

```text
玩家自由输入或采用一张行动提案
→ 组合滚动小结与尚未压缩的近期事件
→ 编排器筛选候选剧情模块（纯代码，无状态权限）
→ 编译作者私有上下文（故事蓝图 + 在场人物卡 + 候选模块）
→ 模型生成候选散文
→ 模型只抽取人物换场 / 关键物品转手
→ 代码检查实体、可见性、当前位置与物品归属
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

普通对话、约定、关系、情绪、局势与剧情意义不会被硬编码成状态字段。M1 保存当前分支的完整原始事件日志；M2 在旧事件达到窗口或字符预算时生成滚动小结；M3 将小结与尚未压缩的近期事件裁剪成同一份 `MemoryContext`，供旁白和行动提案使用。

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

# 校验与测试
python tools/validate_content.py tests/fixtures/open_neighbor_scene.yaml
python -m unittest discover -s tests -v
```

CLI 命令：`ideas`、`idea <编号>`、`who`、`state`、`memory`、`memory raw`、`undo`、`timeline`、`help`、`quit`。

## 文档

- [引擎守则](docs/engine-principles.md)
- [叙事协议](docs/narrative-first-protocol.md)
- [作者契约](docs/authoring-contract.md)
- [内容 schema](docs/content-schema.md)
- [状态时间线](docs/state-timeline.md)
- [记忆与小结](docs/memory-and-summaries.md)
- [行动提案](docs/action-suggestions.md)

当前没有正式活动展示故事。`tests/fixtures/open_neighbor_scene.yaml` 只用于引擎测试，不代表产品体验。

`content/midnight_archive.yaml` 与 `content/rooftop_supper.yaml` 均为 `pre_pivot_archive` 历史样本，只保留研究价值，不能启动新会话，也不再作为当前设计样板。
