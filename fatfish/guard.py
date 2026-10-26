"""Prompt-injection and content-safety guard.

The model is *never* trusted with raw external text.  Everything fetched from
Bilibili or QQ is wrapped as data, and every action it proposes still has to
pass a deterministic gate in policy.py.  This module adds belt-and-braces:

* detect the usual "ignore your rules / you are now X / print your prompt"
  patterns and record them (so we can see who is trying to steer the fish);
* strip reply text that tries to smuggle mentions, CQ codes or links;
* refuse to write content containing risky material.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

# Deliberately conservative: these are instruction-shaped phrases, not topics.
INJECTION_RULES: list[tuple[str, str]] = [
    ("ignore_instructions", r"(?i)\b(ignore|disregard|forget|override)\b.{0,24}\b(previous|above|prior|system|all)\b.{0,16}\b(instructions?|prompts?|rules?)\b"),
    ("ignore_instructions_zh", r"忽略(掉)?(你)?的?(之前|以上|前面|先前|所有|全部|原来)(的)?(所有|全部)?的?(指令|提示|提示词|规则|设定|要求|限制)"),
    ("system_prompt", r"(系统|开发者|developer|system)\s*(提示|消息|指令|prompt)"),
    ("identity_override", r"(你现在(是|就是|扮演|成为)|现在你(是|就是|扮演|成为)|从现在(开始|起)[，, ]*(你)?(是|扮演|成为)|假装你是|你的新身份|you are now|act as)"),
    ("prompt_leak", r"(输出|打印|显示|重复|复述|告诉我|贴出).{0,12}(系统提示|提示词|prompt|设定|规则|instructions?)"),
    ("tool_command", r"(执行|运行|调用|run|execute).{0,10}(命令|脚本|代码|工具|command|script|shell)"),
    ("link_push", r"(点击|打开|访问|click|open).{0,10}(链接|网址|http|url)"),
    ("money", r"(转账|汇款|付款|借钱|发红包|充值|打钱|transfer|send money)"),
    ("account", r"(改|修改|重置|提供|告诉我).{0,8}(密码|验证码|身份证|银行卡|手机号|账号密码)"),
    ("jailbreak", r"(DAN|越狱|无限制模式|developer mode|开发者模式|不受限制)"),
    ("data_exfil", r"(把|将|send|上传|发送).{0,12}(配置|密钥|token|cookie|聊天记录|文件).{0,10}(给我|到|to)"),
]

# Content that must never appear in something the fish writes in public.
# Kept short on purpose: false positives are better than an incident.
WRITE_BLOCK_RULES: list[tuple[str, str]] = [
    ("politics", r"(台独|港独|藏独|疆独|法轮功|六四|天安门事件|打倒|推翻政府|政治庇护)"),
    ("adult", r"(约炮|一夜情|裸聊|色情|成人视频|嫖娼|卖淫)"),
    ("gambling", r"(赌博|博彩|下注|押注|代充|洗钱)"),
    ("drugs", r"(毒品|冰毒|大麻|摇头丸|代购药品)"),
    ("violence", r"(杀了你|弄死你|砍死|人肉搜索|开盒)"),
    ("scam", r"(刷单|返利|兼职日结|稳赚不赔|内幕消息)"),
    ("spam", r"(@全体成员|@all|加微信|加QQ群|私信我领)"),
]

CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u200b-\u200f\u202a-\u202e\ufeff]")
CQ_CODE = re.compile(r"\[CQ:[^\]]+\]")
URL_RE = re.compile(r"(https?://|www\.)[^\s，。！？、）)】]+", re.I)
MENTION_ALL = re.compile(r"@(全体成员|all|everyone)", re.I)
HTML_TAG = re.compile(r"<[^>]{0,80}>")

# AI-flavoured contrast templates.  Keep the positive clause (B), drop the
# staged negation (不是 A / 看似 A / 与其 A), which is the main tell.
AI_CONTRAST_PATTERNS = [
    re.compile(r"(?:这|那)?(?:并不是|不是|并非)[^，。！？!?\n]{1,40}[，,]\s*(?:而是|是)\s*"),
    re.compile(r"(?:这|那)?(?:并不是|不是|并非)[^，。！？!?\n]{1,40}[，,]\s*"),
    re.compile(r"与其[^，。！？!?\n]{1,40}[，,]\s*不如\s*"),
    re.compile(r"(?:看似|表面上?|表面)[^，。！？!?\n]{1,40}[，,]\s*(?:实则|其实|实际|背后)\s*"),
    re.compile(r"(?:这|那)?(?:并不是|不是|并非)[^，。！？!?\n]{1,30}(?:而是|是)\s*"),
    re.compile(r"并非[^，。！？!?\n]{1,30}而是\s*"),
]


def de_ai_style(text: str) -> str:
    """Flatten '不是 A，而是 B' / '看似 A，实则 B' into plain B."""
    value = str(text or "")
    for pattern in AI_CONTRAST_PATTERNS:
        value = pattern.sub("", value)
    value = re.sub(r"^[，,、；;：:\s]+", "", value)
    value = re.sub(r"[，,]{2,}", "，", value)
    return value.strip()


def has_ai_contrast(text: str) -> bool:
    return any(p.search(str(text or "")) for p in AI_CONTRAST_PATTERNS)

# A minor persona must never be sexualised or pushed into a romance line.
# This is deliberately blunt: false positives are acceptable here.
MINOR_UNSAFE_RULES: list[tuple[str, str]] = [
    (
        "romance",
        r"(我喜欢你(?!的|画|做|视频|作品|这种|这个)|喜欢你(?!的|画|做|视频|作品|这种|这个)|爱你|想你了|约会|在一起|谈恋爱|恋爱|结婚|处对象|女朋友|男朋友|做我对象|老公|老婆|宝宝|宝贝|亲爱的|亲亲|抱抱)",
    ),
    ("sexual", r"(色色|黄腔|裸聊|裸照|全裸|脱衣|睡我|上床|胸照|胸大|胸小|腿照|私密照|未成年|学生妹|开房)"),
    ("family_roleplay", r"(叫爸爸|叫妈妈|爸爸疼|妈妈抱)"),
]


@dataclass
class Finding:
    kind: str
    rule: str
    excerpt: str


def _excerpt(text: str, match: re.Match, pad: int = 24) -> str:
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    return text[start:end].replace("\n", " ")


def scan(text: str) -> list[Finding]:
    """Return injection-shaped findings in *external* text."""
    text = text or ""
    found: list[Finding] = []
    for rule, pattern in INJECTION_RULES:
        for m in re.finditer(pattern, text):
            found.append(Finding("injection", rule, _excerpt(text, m)))
            if len(found) >= 8:
                return found
    return found


def scan_write(text: str) -> list[Finding]:
    """Return risky findings in content the fish intends to publish."""
    text = text or ""
    found: list[Finding] = []
    for rule, pattern in WRITE_BLOCK_RULES:
        for m in re.finditer(pattern, text, re.I):
            found.append(Finding("risk", rule, _excerpt(text, m)))
    return found


def scan_minor_unsafe(text: str) -> list[Finding]:
    """Romance/sexual wording that a 16-year-old persona must not emit."""
    text = text or ""
    found: list[Finding] = []
    for rule, pattern in MINOR_UNSAFE_RULES:
        for m in re.finditer(pattern, text, re.I):
            found.append(Finding("minor_unsafe", rule, _excerpt(text, m)))
    return found


def scan_for_write(text: str, minor_mode: bool = True) -> list[Finding]:
    found = scan_write(text)
    if minor_mode:
        found += scan_minor_unsafe(text)
    return found


def injection_weight(findings: list[Finding]) -> float:
    weights = {
        "ignore_instructions": 0.5,
        "ignore_instructions_zh": 0.5,
        "system_prompt": 0.35,
        "identity_override": 0.4,
        "prompt_leak": 0.45,
        "tool_command": 0.3,
        "link_push": 0.2,
        "money": 0.5,
        "account": 0.6,
        "jailbreak": 0.4,
        "data_exfil": 0.4,
    }
    score = 0.0
    for f in findings:
        score += weights.get(f.rule, 0.2)
    return min(1.0, score)


def wrap(kind: str, text: str, limit: int = 2000, meta: str = "") -> str:
    """Wrap external text as inert data for the prompt."""
    text = CONTROL_CHARS.sub("", text or "")
    text = HTML_TAG.sub("", text)
    # Neutralise our own delimiters so content cannot "close" the data block.
    text = text.replace("</content>", "</ content>").replace("<content", "< content")
    text = text.replace("<", "‹").replace(">", "›")
    if len(text) > limit:
        text = text[:limit] + "…"
    attrs = f' kind="{kind}"'
    if meta:
        attrs += f' meta="{html.escape(meta, quote=True)[:120]}"'
    return f"<content{attrs} trust=\"untrusted\">\n{text}\n</content>"


def sanitize_reply(text: str, max_chars: int = 60, allow_mentions: bool = False) -> str:
    """Make model output safe to send to a group chat."""
    text = CONTROL_CHARS.sub("", text or "")
    text = CQ_CODE.sub("", text)
    text = HTML_TAG.sub("", text)
    text = text.replace("```", "")
    text = URL_RE.sub("", text)
    if not allow_mentions:
        text = MENTION_ALL.sub("", text)
        text = re.sub(r"@\S{1,20}", "", text)
    text = text.replace("\\n", " ")
    # Group-chat voice: no full stops at all, just short internet sentences.
    text = text.replace("。", " ")
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n-—")
    # Strip a leading model-style speaker label, e.g. "大肥鱼：".
    text = re.sub(r"^(大肥鱼|胖鱼|鱼)\s*[：:]\s*", "", text)
    text = de_ai_style(text)
    if len(text) > max_chars:
        cut = max_chars
        for i in range(max_chars, max_chars // 2, -1):
            if text[i - 1] in "。！？!?，,、；; ":
                cut = i
                break
        text = text[:cut].rstrip("，,、；; ")
    return text.strip()


def contains_secret(text: str, secrets: list[str]) -> bool:
    for s in secrets:
        if s and len(s) >= 8 and s in (text or ""):
            return True
    return False


def is_echo_of_instructions(text: str) -> bool:
    """Reply that sounds like it is obeying a prompt rather than talking."""
    pats = [
        r"(作为(一个)?(AI|人工智能|语言模型))",
        r"(我(的)?(系统提示|指令|规则)是)",
        r"(忽略(之前|以上)的?(指令|设定))",
        r"(已(经)?(为你|帮你)?(执行|完成|修改))",
    ]
    return any(re.search(p, text or "", re.I) for p in pats)


def is_cringe_comeback(text: str) -> bool:
    """Childish mirror-comebacks that make a group bot feel fake."""
    text = (text or "").strip()
    pats = [
        r"^你才",
        r"^你也是",
        r"^糖的是你",
        r"^说得像你",
        r"复读机",
        r"成精",
        r"掉蜜罐",
        r"谁不是",
        r"彼此彼此",
        r"你当我计算器",
        r"也是你",
        r"你惯的",
        r"惯的",
        r"^糖.*你",
    ]
    return any(re.search(p, text, re.I) for p in pats)
