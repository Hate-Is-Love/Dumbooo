"""A local HTTP server that imitates the Gemini and OpenAI APIs.

The real SDKs are pointed at it, so tests exercise the SDKs' real request
building, response/SSE parsing and exception classes, with no network access,
no API key and no cost.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Reply:
    status: int = 200
    body: dict | None = None
    sse: list[tuple[str | None, dict]] | None = None  # (event name or None, data)
    headers: dict = field(default_factory=dict)
    delay_s: float = 0.0
    sse_gap_s: float = 0.0


class FakeAIServer:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                server.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                        "json": json.loads(raw) if raw else None})
                reply = server.replies.pop(0) if server.replies else Reply(500, {"error": {"message": "no reply"}})
                if reply.delay_s:
                    time.sleep(reply.delay_s)
                try:
                    if reply.sse is not None:
                        self.send_response(reply.status)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Connection", "close")
                        for k, v in reply.headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        for event, data in reply.sse:
                            chunk = (f"event: {event}\n" if event else "") + f"data: {json.dumps(data)}\n\n"
                            self.wfile.write(chunk.encode())
                            self.wfile.flush()
                            if reply.sse_gap_s:
                                time.sleep(reply.sse_gap_s)
                        self.close_connection = True
                        return
                    payload = json.dumps(reply.body or {}).encode()
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for k, v in reply.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(payload)
                except (ConnectionError, OSError):
                    pass  # the client gave up (timeout / cancel)

            do_GET = do_POST = _handle

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_port}"

    def add(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# ---------------------------------------------------------------- Gemini payloads
def gemini_text(text: str, finish: str = "STOP") -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}


def gemini_chunk(text: str | None, finish: str | None = None) -> dict:
    candidate: dict = {"content": {"role": "model", "parts": [{"text": text}] if text else []}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


def gemini_error(code: int, status: str, message: str, details: list | None = None) -> dict:
    return {"error": {"code": code, "status": status, "message": message, "details": details or []}}


def gemini_quota_details(quota_id: str, retry: str = "33s") -> list:
    return [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                         "quotaId": quota_id}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
    ]


# ---------------------------------------------------------------- OpenAI payloads
def openai_response(text: str, status: str = "completed", incomplete_reason: str | None = None) -> dict:
    return {
        "id": "resp_1", "object": "response", "created_at": 0, "status": status, "model": "gpt-test",
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
        "output": [{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}] if text else [],
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    }


def openai_stream(pieces: list[str], final_status: str = "completed", reason: str | None = None) -> list:
    events = [("response.created", {"type": "response.created", "sequence_number": 0,
                                    "response": openai_response("", status="in_progress")})]
    for i, piece in enumerate(pieces, start=1):
        events.append(("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
            "delta": piece, "sequence_number": i, "logprobs": []}))
    done_type = "response.completed" if final_status == "completed" else "response.incomplete"
    events.append((done_type, {"type": done_type, "sequence_number": len(pieces) + 1,
                               "response": openai_response("".join(pieces), status=final_status,
                                                           incomplete_reason=reason)}))
    return events


def openai_error(message: str, type_: str, code: str | None = None, param: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "code": code, "param": param}}
