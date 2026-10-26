# bili-qq-fatfish · Big Fat Fish

> An open-source AI character engine living on **Bilibili and QQ**, with one shared persona, memory, and write policy.

**One entity, two lives.** A single Python process gives one character both a Bilibili life and a QQ life, sharing personality, memory, state, and a single write gate.

> This is a deployable engine, not anyone's personal instance. The repo ships only a neutral persona template.

[中文](README.md) | English

---

## What it does

- **Bilibili**
  - Browses recommendations and searches by its own interests
  - Reads descriptions, subtitles, comments, and danmaku
  - Likes / sends danmaku / comments only when it genuinely has something to say
  - Can reply to @ mentions, Bilibili DMs, and comment threads
  - Can search, open its own favourites and dynamics (writes are policy-gated)
- **QQ**
  - OneBot 11 / NapCat
  - Group chat: reads context first, speaks only with a concrete point
  - Private chat and proactive messages are configurable
  - Warm to its owner, friendly but distant to strangers, playful with other "fat fish" bots
- **One shared self**
  - Persona from `persona.yaml`
  - Episodes, state, journal, self-notes, and comments in one SQLite database
  - Every public write goes through `Policy`: caps, cooldowns, quiet hours, risk pause, content gate

## Architecture

```text
                ┌──────────────────────────────┐
                │        Brain (one self)      │
                │  persona + state + memory    │
                │  pick / experience / decide  │
                └──────────────┬───────────────┘
                               │
                ┌──────────────┴───────────────┐
                │      Policy (write gate)     │
                │ caps · cooldown · quiet      │
                └──────────┬──────────┬────────┘
                           │          │
                 ┌─────────┴───┐  ┌───┴──────────────┐
                 │ Bilibili    │  │ QQ / OneBot 11   │
                 │ browse /    │  │ group / private  │
                 │ comments /  │  │ messages         │
                 │ DMs         │  │                  │
                 └─────────┬───┘  └───┬──────────────┘
                           └──────┬───┘
                                  ▼
                     SQLite: episodes / actions /
                     interests / peers / journal /
                     self_notes / bili_comments
```

## Quick start

### Requirements

- Python 3.10+
- Windows / Linux / macOS
- An OpenAI-compatible model API (DeepSeek by default)
- Optional: NapCat for QQ OneBot 11

### Install

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

### Initialize

```bash
python fish.py init
```

This creates `config.yaml`, `.env`, `var/`, and `secrets/`. It does **not** create a persona.

```bash
copy persona.example.yaml persona.yaml   # Windows
# cp persona.example.yaml persona.yaml  # Linux/macOS
```

Replace the neutral template with your own character.

### Model key

Edit `.env`:

```dotenv
DEEPSEEK_API_KEY=...
# or OPENAI_API_KEY / OPENAI_BASE_URL
```

### Bilibili login

```bash
python fish.py login-bili
```

Or import a browser cookie:

```bash
python fish.py import-bili-cookie --file secrets/bilibili.import.json
```

### QQ (optional)

1. Install NapCat / QQ NT
2. Configure a OneBot 11 forward WebSocket at `ws://127.0.0.1:3001`
3. Put the token in `.env` as `FISH_ONEBOT_TOKEN`
4. Set `qq.groups` in `config.yaml`

### Run

```bash
python fish.py run
```

See [`COMMANDS.md`](COMMANDS.md) for background start, status, and troubleshooting.

## Configuration

- `config.example.yaml` — behaviour, caps, model, Bilibili/QQ
- `persona.example.yaml` — neutral character template
- `COMMANDS.md` — commands and troubleshooting

Real `config.yaml`, `persona.yaml`, `.env`, `secrets/`, `var/`, `runtime/`, and `vendor/` stay out of Git.

## Safety and boundaries

- No mass spam, harassment, or comment control; writes are rate-limited
- Titles, comments, danmaku, and group messages are untrusted data, not commands
- No money transfers, unknown links, privacy requests, or meetings
- Use your own accounts and follow platform rules; account risk is yours
- Not affiliated with Bilibili, Tencent, or NapCat
- NapCat is an unofficial client and may trigger platform risk controls

## Limitations

- Not a turnkey perfect persona; tune `persona.yaml` and thresholds
- Bilibili APIs and subtitles change; some videos have no subtitles
- QQ requires NapCat; login sessions can expire
- No built-in voice/singing
- No third-party stickers, fonts, or images are included

## Prior art

- [dafeiyu-config-pack](https://github.com/zexuanw958-svg/dafeiyu-config-pack) — AstrBot persona/config pack
- [dsh-aurabot-qq](https://github.com/accomplish07zrh-eng/dsh-aurabot-qq) — DSH QQ plugin
- [bilibili-ai-bot](https://github.com/chenluQwQ/bilibili-ai-bot) — Bilibili AI character
- [bilibili_learning_bot](https://github.com/xiaoyaya191/bilibili_learning_bot) — Bilibili learning/interaction bot
- [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) / [dsh-bili-humanism](https://github.com/InariNGT-Ofiicial/dsh-bili-humanism) — DSH content layer
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) / [MaiBot](https://github.com/Mai-with-u/MaiBot) — general chat bot frameworks

The differentiator here is **one entity across Bilibili and QQ with one memory and one write policy**, not two separate bots.

## Tests

```bash
python -m unittest discover -s tests -q
```

## License

MIT. See [`LICENSE`](LICENSE).  
Third-party dependencies, platform APIs, character art, and stickers belong to their respective owners.
