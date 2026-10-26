# 现成方案调研与选型（2026-10-19）

> 目标不是“找一个现成的鱼”，而是“找到最接近的两个器官，再给它们装同一个大脑”。
> 结论先说：**没有现成项目同时做好这两件事**。最终采用「自建统一 runtime + 成熟的 B 站库 + 成熟的 QQ 协议端」，
> 把调研到的三个项目当作行为参考，而不是把两个 bot 拼在一起。

## 1. 候选项目

| 项目 | 覆盖 | 能用的部分 | 为什么没有直接部署 |
|---|---|---|---|
| [MaiBot（麦麦）](https://github.com/Mai-with-u/MaiBot) | QQ 群聊 | 群聊时机控制（frequency / dynamic 两种触发、1 小时滑窗目标频率）、实时思维、表达学习、图记忆、动态人格 | 只做群聊；没有 B 站；体量大、配置复杂；把它和 B 站项目拼起来会得到两个身份、两套记忆。**作为“水群”行为的主要参考。** |
| [bilibili_learning_bot（BiliLearn）](https://github.com/xiaoyaya191/bilibili_learning_bot) | B 站 | 推荐流浏览、兴趣多维度评分、字幕/评论/弹幕理解、点赞/投币/收藏/评论/弹幕、互动审核、提示词注入防护、行为日志与自我反思 | 只做 B 站；54k 行、113 个文件，人格/数据库/面板都和 QQ 无关；把它接到 QQ 需要改它的 brain 和存储。**作为 B 站浏览与互动逻辑的主要参考（MIT，可读可借鉴）。** |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) + [smart_wake](https://github.com/Mikachiyo/astrbot_plugin_smart_wake) | QQ / IM 多平台 | 插件框架、OneBot 适配、人格、WebUI；“关键词抽签 + 模型判定”两级漏斗主动参与群聊 | 框架是聊天机器人中心，B 站不是它的“平台”；在 AstrBot 里写一个长时间运行的浏览循环会和它的会话模型打架。**两级漏斗的思路被采用。** |
| [LangBot](https://github.com/langbot-app/LangBot) | IM 多平台 | 开箱即用的 Agent / RAG / MCP、QQ 适配 | 同上：聊天为中心；没有“一个人格自己去看视频”的运行模型。 |
| [chenluQwQ/bilibili-ai-bot](https://github.com/chenluQwQ/bilibili-ai-bot) | B 站 | B 站 AI 角色、自动回复评论、刷视频、发动态、记忆 | 偏“B 站账号运营”，没有 QQ 群生活；交互偏被动回复。**作为 B 站人设/记忆的旁证。** |
| [Ayabot](https://github.com/yujianke100/Ayabot) | B 站直播间 | 直播间弹幕机器人、AI 回复 | 场景是直播，不是推荐流/群聊。 |
| [bilibili-mcp](https://github.com/lianginx/bilibili-mcp) / [mcp-server-bilibili](https://www.npmjs.com/package/mcp-server-bilibili) | B 站工具层 | 热门、搜索、UP 投稿等 MCP 工具 | 只有工具，没有兴趣、记忆、节奏和人格；适合临时查询，不适合“过日子”。 |

## 2. 选用的底座

| 组件 | 版本 | 角色 | 为什么是它 |
|---|---|---|---|
| [bilibili-api-python](https://pypi.org/project/bilibili-api-python/) | 17.4.2 | B 站读写 | 400+ API、全异步、活跃维护、自带 wbi 签名/风控规避/curl_cffi 指纹；`Video.like/pay_coin/set_favorite/send_danmaku/send_comment` 都在。GPL-3.0。 |
| [NapCat](https://github.com/NapNeko/NapCatQQ) | 4.18.38 | QQ 协议端 | 基于 NTQQ 的 OneBot 11 实现，正向/反向 WS/HTTP 都有，维护活跃；文档明确推荐 AstrBot / MaiBot 这类 LLM 框架。 |
| OneBot 11 | — | QQ 适配协议 | 只依赖一个稳定协议，不把鱼绑死在 NapCat；换 Lagrange / LLOneBot 只改 `ws_url`。 |
| Python 3.12 + httpx + SQLite | — | runtime / 记忆 | 单进程 asyncio，两个循环共享一个内存状态和一个 SQLite；不需要消息队列。 |

## 3. 为什么自建，而不是拼两个项目

1. **同一个实体**：B 站和 QQ 必须共享人格、兴趣权重、心情、社交电量、日记和“最近看过什么”。
   两个 bot 各有一个 process 时，最自然的共享方式是再写一个同步层，复杂度反而更高。
2. **写操作闸门只能有一处**：点赞、投币、发弹幕、群发言都经过同一个 `Policy`，额度/冷却/安静时段/风控暂停
   只在一个地方判断。两个项目各有一套写入逻辑，无法保证“今天总共只说 14 句”。
3. **注入防护要覆盖所有入口**：视频标题/简介/字幕/评论/弹幕/群消息统一走 `guard.wrap()` 和 `guard.scan()`，
   模型只能“提议”，不能直接执行。
4. **可观察性**：一个 `var/fish.db` + 一份日报就能回答“它今天为什么说话/为什么沉默/为什么点赞”。
5. **体积**：本实现约 2k 行，依赖 28 个 wheel；出问题能读完。

## 4. 被借鉴的具体设计

- **MaiBot**：发言不是一个“回复触发器”，而是一个 **Planner 决策**；有目标频率和滑窗，允许打断，也允许不回复。
  本项目的 `Brain.decide_group()` 默认 `speak=false`，用 `confidence` 和状态偏置决定是否开口。
- **BiliLearn**：先对候选做兴趣评分，再对内容做理解，最后才决定互动；互动有审核和额度。
  本项目把“评分”和“体验”拆成两次模型调用，并要求 `evidence`，没有具体证据的高分会被压到 60。
- **AstrBot smart_wake**：两级漏斗（便宜的关键词/抽签 → 贵的模型判定）。
  本项目用硬门槛（白名单/安静时段/冷却/额度）先挡一层，再让模型判断“有没有具体的点”。
- **bilibili-ai-bot**：人格和记忆是同一个 B 站角色的核心。本项目把它扩展为跨渠道共享。

## 5. 已知限制与风险

- **B 站接口可能变**：`bilibili-api-python` 是逆向库，接口改动会失效；尽量保持最新版。
- **账号风险**：自动化互动违反 B 站社区规则的可能性存在；本项目默认 observe、低频、低额度，但仍需自行承担风险。
- **不能真的“看”视频**：没有下载/解码视频，理解来自简介、字幕（需登录）、AI 摘要（需登录）、热评和弹幕抽样。
  人设里明确要求“没看到就说没看到”，不假装看完整。
- **QQ 账号风险**：NapCat 属于非官方协议端，QQ 可能限制/封禁；建议使用小号，先只在一个群、低频率。
- **NapCat 首次登录必须人工扫码**：这不是代码能替代的步骤。
- **模型质量决定人设上限**：同样的提示词，弱模型会退化成“客服腔”；建议用 DeepSeek-chat 或同级模型，温度 0.9。
