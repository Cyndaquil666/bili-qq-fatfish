# 群聊人格/提示词调研（给大肥鱼用）

> 目标不是换框架，而是找“**怎么让群聊 bot 不硬接、不唐、不像客服**”的现成规则。
> 结论：最有用的是 CyberGroupmate 的人格规则和 qq-girlfriend-bot 的短回复约束；插件大多绑定 AstrBot / LangBot / MaiBot，不能直接塞进当前自建 runtime。

## 参考项目

| 项目 | 能借什么 | 为什么不直接装 |
|---|---|---|
| [CyberGroupmate](https://github.com/Archeb/CyberGroupmate) | 最完整的“群聊读空气”规则：闲聊没信息就不接、不主动提问、不科普、不附和；禁止“确实/笑死/还真是/接住/你说得对”；话题 triage 决定值不值得开口 | Node/CodeAct 重型 Agent，整套替换成本太高；只借 prompt 规则 |
| [qq-girlfriend-bot](https://github.com/warthunde/qq-girlfriend-bot) | 每个角色独立 YAML；回复要求 1–2 句、像 QQ 聊天、不写小作文、不写括号动作、不要每句都接满 | 多 Bot 编排 + Docker + 4 个 QQ，和“一个实体”目标冲突；借短回复约束 |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | Planner 的发言时机、表达学习、群聊记忆 | 只做 QQ、体量大；表达学习需要它的整套数据管线 |
| [AstrBot self_evolution](https://github.com/Renyus/astrbot_plugin_self_evolution) | 人格演化、长期记忆、worldbook 式规则注入 | AstrBot 插件，不能直接用于当前 runtime |
| [astrbot_plugin_worldbook](https://github.com/Zhalslar/astrbot_plugin_worldbook) | 用正则触发 system prompt 注入，做“场景规则” | 同上；我们的 Persona 已经能做同样的事 |
| [LangBot Waifu](https://github.com/ElvisChenML/Waifu) | 真实感聊天的 prompt 组织 | LangBot 插件，绑定它的消息管线 |
| [AI-Character-Cards](https://github.com/xmlans/AI-Character-Cards) / SillyTavern 角色卡 | 角色语气、口癖、示例对话 | 很多偏二次元/恋爱向，容易把鱼变成“角色扮演客服”；只作语气参考 |

## 直接采纳的规则

1. **大多数消息不需要接**：只有具体问题、熟人点名、真有新信息才开口。
2. **同一话题/同一轮起哄最多回一次**；回过就闭嘴。
3. **别人复读、起哄、玩梗、发表情包：不回**。
4. **不主动提问、不科普、不总结、不附和、不评价**。
5. **1–2 句，句尾不加句号**；不写小作文、不写括号动作。
6. **禁止填充词**：确实、笑死、还真是、接住、我认、不绕、你说得对。
7. **不要每句都接梗、每句都怼**；傲娇是嘴硬，不是抬杠。
8. **刚说过话 2 分钟内不再开口**（除了明确的新问题）。
9. **短闲聊/复读直接跳过，不调模型**；少花 token，也更像真人。
10. **小学生回敬过滤**：“你才…/你也是/复读机/成精/糖的是你/你惯的”直接丢弃。

## 没有采纳的

- 不装 AstrBot / LangBot / MaiBot 插件：它们和当前自建 runtime 的 Persona / OneBot / 节奏模型不兼容。
- 不用角色卡整包：容易把“少女感”做成二次元台词表演，且很多卡带恋爱/性暗示倾向。
- 不做“表达学习”自动改写人格：需要大量群聊样本和审核，先手动调更可控。

## 当前落到代码里的位置

- `persona.yaml` → `temperament` / `voice` / `qq.how` / `qq.silence`
- `fatfish/persona.py` → `qq_block()` 的【群聊节奏】
- `fatfish/mind.py` → `GROUP_OUTPUT` + `is_cringe_comeback` 过滤
- `fatfish/qq.py` → 短闲聊跳过 + 2 分钟回复冷却
- `config.yaml` → `min_confidence`、`reply_cooldown_seconds`、`debounce_seconds`、窗口额度
