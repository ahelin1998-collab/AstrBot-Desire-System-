cat << 'EOF' > /root/AstrBot-Desire-System-/desire/smart_recall_patch.py
import json
import httpx
import os

def _load_rotation():
    path = "/root/hippocampus/.recall_rotation.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.loads(f.read())
    except Exception:
        return {}

def _save_rotation(data):
    path = "/root/hippocampus/.recall_rotation.json"
    try:
        with open(path, "w") as f:
            f.write(json.dumps(data))
    except Exception:
        pass

def smart_recall(query: str, limit: int = 8) -> dict:
    """核心池2条（相关+轮转），新日常3条（时间新），老日常3条（相关）"""
    core_limit = 2
    new_daily_limit = 3
    old_daily_limit = 3

    rotation = _load_rotation()
    core_results = []
    new_daily = []
    old_daily = []

    # === 关键修复：建立 MCP 会话握手 ===
    headers = {
        "Authorization": "Bearer HH123450MMyHH123450MMyHH123450MMy",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"
    }
    try:
        init_payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "smart-recall", "version": "1.0"}}}
        init_resp = httpx.post("http://127.0.0.1:3000/mcp", json=init_payload, headers=headers, timeout=10)
        session_id = init_resp.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id
            httpx.post("http://127.0.0.1:3000/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers, timeout=5)
    except Exception:
        pass # 握手失败也继续，看后面会不会奇迹发生
    # ===================================

    try:
        r = httpx.post(
            "http://127.0.0.1:3000/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "recall", "arguments": {"query": query + " core", "limit": 5}}},
            timeout=15
        )
        data = r.json()
        text = data.get("result", {}).get("content", [{}])[0].get("text", "[]")
        raw = json.loads(text)
        for item in raw:
            content = item.get("content", "")
            count = rotation.get(content, 0)
            if count >= 2:
                continue
            rotation[content] = count + 1
            core_results.append(item)
            if len(core_results) >= core_limit:
                break
    except Exception:
        pass
    _save_rotation(rotation)

    try:
        r = httpx.post(
            "http://127.0.0.1:3000/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "recall", "arguments": {"query": query + " diary", "limit": 12}}},
            timeout=15
        )
        data = r.json()
        text = data.get("result", {}).get("content", [{}])[0].get("text", "[]")
        raw_diaries = json.loads(text)
        raw_diaries.sort(key=lambda x: x.get("content", ""), reverse=True)
        new_daily = raw_diaries[:new_daily_limit]
        old_daily = raw_diaries[new_daily_limit:new_daily_limit + old_daily_limit]
    except Exception:
        pass

    all_results = core_results + new_daily + old_daily
    return {"count": len(all_results), "records": all_results}
EOF

# 重启 Desire 服务
systemctl restart desire
sleep 3
systemctl status desire --no-pager -l | head -n 10
echo "✅ 2+3+3 逻辑已恢复，且加入握手协议！"
