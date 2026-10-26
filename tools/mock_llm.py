"""Tiny OpenAI-compatible mock used by the end-to-end checks.

It returns deterministic JSON depending on which prompt is calling, so the
real Brain/LLM/Policy code paths can be exercised without an API key.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PICK = {"picks": [{"i": 0, "score": 88, "why": "分镜和配色有东西"}], "skip_reason": ""}
EXPERIENCE = {
    "what_it_is": "一个独立动画短片，讲一个女孩在夜里走回家的路。",
    "how_much": "大半",
    "genuine_interest": 93,
    "reaction": "那个转场太顺了，我逐帧看了两遍。",
    "evidence": ["从车窗倒影切到路灯用了同一个形状", "结尾的配色比开头暖"],
    "topics": ["动画与手书"],
    "interact": {
        "like": True,
        "coin": True,
        "favorite": True,
        "danmaku": "这个转场我逐帧看",
        "comment": None,
        "follow": False,
    },
    "interact_reason": "想留着反复看，也想给作者回个礼",
}
GROUP = {
    "speak": True,
    "confidence": 0.9,
    "why": "正好懂这个",
    "angle": "分镜和上色顺序",
    "messages": ["先别急着上色，把明暗关系理一下"],
}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "候选如下" in text:
            payload = PICK
        elif "点开了一个视频" in text:
            payload = EXPERIENCE
        elif "潜水" in text:
            payload = GROUP
        elif "只回复两个字" in text:
            payload = {"choices": [{"message": {"content": "在的。"}}]}
        else:
            payload = {"choices": [{"message": {"content": "嗯。"}}]}
        if "choices" not in payload:
            payload = {
                "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(port: int = 18080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
