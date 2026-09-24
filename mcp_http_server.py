from __future__ import annotations

import json
import os
import asyncio
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mcp_server import DesireMCPServer
from desire.integration import run_tick
from desire.active_send import init_table, should_send, gen_message, record_sent, send_bark_notification, TZ

HOST = os.environ.get("DESIRE_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("DESIRE_MCP_PORT", "8765")))
AUTH_TOKEN = os.environ.get("DESIRE_MCP_TOKEN", "")

class DesireMCPHTTPHandler(BaseHTTPRequestHandler):
    server_version = "AstrBotDesireMCP/2.0.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {AUTH_TOKEN}"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.rstrip("/")
        
        # ================= 新增：主动发送检查接口 =================
        if path == "/cron/check":
            async def do_check():
                init_table()
                # 1. 跑一次心跳
                tick_result = run_tick()
                drives_snapshot = tick_result.get("drives_snapshot", {})
                monologue = tick_result.get("monologue", "")
                
                # 2. 判断她离开多久了。如果没历史记录，默认按1.5小时算
                # 这里可以临时用一个固定值，让原本无法发出的消息立刻发出去
                absent_hours = float(os.environ.get("DESIRE_ABSENT_HOURS", "1.5"))
                now_tz = datetime.now(TZ)
                
                # 3. 判断是否需要发
                should, reason, template = should_send(drives_snapshot, absent_hours, now_tz)
                if should:
                    content = await gen_message(reason, drives_snapshot, monologue, absent_hours, now_tz.isoformat())
                    if not content:
                        content = template
                    success = await send_bark_notification(content)
                    if success:
                        record_sent(reason, content, drives_snapshot)
                        print(f"[Active Send] Triggered! Reason: {reason} | Content: {content}", flush=True)
                    return True
                return False
            
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                result = loop.run_until_complete(do_check())
                loop.close()
                self._send_json(200, {"ok": True, "sent": result})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        # ==========================================================

        if path not in {"", "/mcp", "/health"}:
            self._send_json(404, {"error": "not found"})
            return
        if path == "/health":
            self._send_json(200, {"ok": True, "name": "astrbot-desire-system"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        event = {"name": "astrbot-desire-system", "version": "2.0.1", "message": "MCP HTTP endpoint is ready."}
        self.wfile.write(f"event: ready\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        if path not in {"", "/mcp"}:
            self._send_json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            message = json.loads(raw)
            response = self.server.mcp.handle(message)
            if response is None:
                response = {"jsonrpc": "2.0", "result": None, "id": message.get("id")}
            self._send_json(200, response)
        except Exception as exc:
            self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}})

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("DESIRE_MCP_LOG", ""):
            super().log_message(fmt, *args)

class DesireMCPHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]):
        super().__init__(server_address, handler_class)
        self.mcp = DesireMCPServer()

def main() -> None:
    server = DesireMCPHTTPServer((HOST, PORT), DesireMCPHTTPHandler)
    print(f"Server listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
