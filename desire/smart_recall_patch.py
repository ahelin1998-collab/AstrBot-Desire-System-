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

    try:
        r = httpx.post(
            "http://127.0.0.1:3000/mcp",
            headers={"Authorization": "Bearer HH123450MMyHH123450MMyHH123450MMy", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
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
            headers={"Authorization": "Bearer HH123450MMyHH123450MMyHH123450MMy", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
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
