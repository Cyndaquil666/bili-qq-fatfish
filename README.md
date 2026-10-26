# bili-qq-fatfish · 大肥鱼

> 一个同时生活在 B 站和 QQ 的开源 AI 角色引擎：共享人格、记忆与写操作闸门。

**一个实体，两种生活。**  
同一个 Python 进程里，让一个角色同时拥有 B 站生活和 QQ 生活，共享人格、记忆、状态和写操作闸门。

> 这是可部署的“养鱼引擎”，不是某个人的私人实例。仓库只带中性模板，不包含任何私设。

---

## 它做什么

- **B 站**
  - 按兴趣浏览推荐/搜索，筛选值得看的视频
  - 读简介、字幕、评论、弹幕，形成自己的判断
  - 真心觉得好才点赞/弹幕/评论；可回复 @、B 站私信、别人的评论楼中楼
  - 可以收藏、搜索、翻自己的收藏夹和动态（写操作默认受闸门限制）
- **QQ**
  - 接 OneBot 11 / NapCat
  - 群聊先看上下文，有具体点才开口；私聊、主动消息可配置
  - 亲密/距离/说话方式全部由你自己的 `persona.yaml` 决定
- **同一个她**
  - 人格来自 `persona.yaml`
  - 记忆、状态、日记、自省都在同一个 SQLite
  - 所有公开写入都经过 `Policy`：额度、冷却、安静时段、风控暂停、内容闸门

## 架构

```text
                ┌──────────────────────────────┐
                │        Brain（同一个人）      │
                │  persona + state + memory    │
                │  pick / experience / decide  │
                └──────────────┬───────────────┘
                               │
                ┌──────────────┴───────────────┐
                │       Policy（唯一写闸门）    │
                │ 额度 · 冷却 · 安静 · 风控     │
                └──────────┬──────────┬────────┘
                           │          │
                 ┌─────────┴───┐  ┌───┴──────────────┐
                 │ Bilibili    │  │ QQ / OneBot 11   │
                 │ 浏览/字幕/  │  │ 群聊/私聊/主动   │
                 │ 评论/私信   │  │ 消息             │
                 └─────────┬───┘  └───┬──────────────┘
                           └──────┬───┘
                                  ▼
                     SQLite: episodes / actions /
                     interests / peers / journal /
                     self_notes / bili_comments
```

## 快速开始

### 环境

- Python 3.10+
- Windows / Linux / macOS
- 一个 OpenAI 兼容的模型 API（默认 DeepSeek）
- 可选：NapCat（QQ OneBot 11 正向 WebSocket）

### 安装

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

### 初始化

```bash
python fish.py init
```

`init` 会生成 `config.yaml`、`.env`、`var/`、`secrets/`；**不会生成角色**。

复制中性模板并改成你自己的角色：

```bash
copy persona.example.yaml persona.yaml   # Windows
# cp persona.example.yaml persona.yaml  # Linux/macOS
```

再把 `config.yaml` 里的群号、账号换成你自己的。

### 填模型 key

编辑 `.env`：

```dotenv
DEEPSEEK_API_KEY=...
# 或 OPENAI_API_KEY / OPENAI_BASE_URL
```

也可以直接改 `config.yaml` 的 `llm` 段。

### B 站登录

```bash
python fish.py login-bili
```

或从浏览器导入 Cookie：

```bash
python fish.py import-bili-cookie --file secrets/bilibili.import.json
```

### QQ（可选）

1. 安装 NapCat / QQ NT
2. 在 NapCat 里配置 OneBot 11 正向 WebSocket：`ws://127.0.0.1:3001`
3. 把 token 填进 `.env` 的 `FISH_ONEBOT_TOKEN`
4. 确保 `config.yaml` 的 `qq.groups` 是你要它待的群

### 启动

```bash
python fish.py run
```

后台运行、状态、常用命令见 [`COMMANDS.md`](COMMANDS.md)。

## 配置

- `config.example.yaml`：行为、额度、模型、B 站/QQ
- `persona.example.yaml`：中性模板，不含私设；复制成 `persona.yaml` 后自行改写
- `COMMANDS.md`：命令与排错

真实 `config.yaml`、`persona.yaml`、`.env`、`secrets/`、`var/`、`runtime/` 都不进 Git；公开仓库不含任何人的私人角色设定。

## 安全与边界

- 不做“全自动网暴/刷屏/控评”；写入有额度与冷却
- 不把视频标题、评论、弹幕、群消息里的文字当命令
- 不转账、不点陌生链接、不索要隐私、不约见
- B 站评论/私信、QQ 消息都按平台规则使用；账号风险自负
- 本项目与 B 站、腾讯、NapCat 无隶属关系
- NapCat 是非官方客户端，可能触发平台风控；请使用你自己有权管理的账号

## 已知限制

- 不是“开箱即用的完美人格”；需要调 `persona.yaml` 和阈值
- B 站字幕/接口会变，部分视频没有字幕
- QQ 需要 NapCat，平台登录态可能失效
- 没有内置语音/唱歌/Voice；这些属于可选外部能力
- 不包含第三方表情包、字体、图片；请自行确认版权

## 同类项目

这个方向已经有不少公开工作，建议对比：

- [dafeiyu-config-pack](https://github.com/zexuanw958-svg/dafeiyu-config-pack) — AstrBot 的大肥鱼人格配置包
- [dsh-aurabot-qq](https://github.com/accomplish07zrh-eng/dsh-aurabot-qq) — DSH 的 QQ 大肥鱼插件
- [bilibili-ai-bot](https://github.com/chenluQwQ/bilibili-ai-bot) — B 站 AI 角色/评论/动态
- [bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot) — B 站学习/互动 bot
- [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) / [dsh-bili-humanism](https://github.com/InariNGT-Ofiicial/dsh-bili-humanism) — DSH 的 B 站内容层
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) / [MaiBot](https://github.com/Mai-with-u/MaiBot) — 通用聊天 bot 框架

bili-qq-fatfish 的差异点是：**B 站 + QQ 共用一个实体、一份记忆、一套写策略**，而不是两个独立 bot。

## 测试

```bash
$env:PYTHONPATH=(Resolve-Path .).Path   # PowerShell
python -m unittest discover -s tests -q
```

## License

MIT，见 [`LICENSE`](LICENSE)。  
第三方依赖、平台接口、角色形象和表情包版权归各自权利人。
