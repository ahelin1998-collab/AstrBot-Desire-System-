# desire/integration.py
"""欲望系统与SQLite/现有系统的桥接"""

import json
import sqlite3
import os
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional
from .core import DesireState, Drive, Thought, create_default_drives, apply_event
from .tick import tick
from .thoughts import maybe_spawn_thought, sample_and_update, decay_thoughts
from .safety import safety_check
from .monologue import generate_monologue

TZ_MSK = timezone(timedelta(hours=3))
DB_PATH = os.environ.get("DESIRE_DB_FILE", "desire_system.db")

LLM_API_KEY = os.environ.get("DESIRE_LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("DESIRE_LLM_API_BASE", "")
LLM_MODEL = "deepseek-v4-flash"

def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_tables():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            drives_json TEXT NOT NULL,
            thoughts_json TEXT NOT NULL,
            last_tick TEXT,
            tick_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tick_count INTEGER,
            changes TEXT,
            action_hints TEXT,
            monologue TEXT,
            safety_warnings TEXT
        )
    """)
    conn.commit()
    conn.close()

def _drives_to_json(drives: dict) -> str:
    data = {}
    for name, drive in drives.items():
        data[name] = {
            "value": round(drive.value, 2),
            "baseline": round(drive.baseline, 2),
            "decay_rate": drive.decay_rate,
            "growth_rate": drive.growth_rate,
            "ceiling": drive.ceiling,
            "floor": drive.floor,
            "action_threshold": drive.action_threshold,
        }
    return json.dumps(data, ensure_ascii=False)

def _json_to_drives(json_str: str) -> dict:
    data = json.loads(json_str)
    drives = {}
    for name, d in data.items():
        drives[name] = Drive(
            name=name, value=d["value"], baseline=d["baseline"],
            decay_rate=d.get("decay_rate", 0.1), growth_rate=d.get("growth_rate", 0.2),
            ceiling=d.get("ceiling", 100.0), floor=d.get("floor", 0.0),
            action_threshold=d.get("action_threshold", 70.0),
        )
    return drives

def _thoughts_to_json(thoughts: list) -> str:
    data = []
    for t in thoughts:
        data.append({
            "id": t.id, "content": t.content, "source_drive": t.source_drive,
            "weight": round(t.weight, 2), "hit_count": t.hit_count,
            "is_obsession": t.is_obsession, "created_at": t.created_at,
            "last_hit": t.last_hit, "resolved": t.resolved,
        })
    return json.dumps(data, ensure_ascii=False)

def _json_to_thoughts(json_str: str) -> list:
    data = json.loads(json_str)
    thoughts = []
    for d in data:
        thoughts.append(Thought(
            id=d["id"], content=d["content"], source_drive=d["source_drive"],
            weight=d.get("weight", 1.0), hit_count=d.get("hit_count", 0),
            is_obsession=d.get("is_obsession", False), created_at=d.get("created_at", ""),
            last_hit=d.get("last_hit", ""), resolved=d.get("resolved", False),
        ))
    return thoughts

def load_state() -> DesireState:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM desire_state WHERE id = 1").fetchone()
    conn.close()
    if not row:
        state = DesireState()
        save_state(state)
        return state
    state = DesireState(
        drives=_json_to_drives(row["drives_json"]),
        thoughts=_json_to_thoughts(row["thoughts_json"]),
        last_tick=row["last_tick"] or "",
        tick_count=row["tick_count"] or 0,
    )
    return state

def save_state(state: DesireState):
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO desire_state (id, drives_json, thoughts_json, last_tick, tick_count)
        VALUES (1, ?, ?, ?, ?)
    """, (
        _drives_to_json(state.drives),
        _thoughts_to_json(state.thoughts),
        state.last_tick,
        state.tick_count,
    ))
    conn.commit()
    conn.close()

def log_tick(state: DesireState, result: dict, monologue: str, warnings: list):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO desire_log (timestamp, tick_count, changes, action_hints, monologue, safety_warnings)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(TZ_MSK).isoformat(), state.tick_count,
        json.dumps(result.get("changes", []), ensure_ascii=False),
        json.dumps(result.get("action_hints", []), ensure_ascii=False),
        monologue, json.dumps(warnings, ensure_ascii=False),
    ))
    conn.commit()
    conn.close()

def run_tick(is_wife_present: bool = False, event_type: str = None) -> dict:
    state = load_state()
    event_changes = []
    if event_type:
        event_changes = apply_event(state, event_type)
    result = tick(state, is_wife_present=is_wife_present)
    decay_thoughts(state)
    new_thought = maybe_spawn_thought(state)
    if new_thought:
        state.thoughts.append(new_thought)
    sampled = sample_and_update(state)
    warnings = safety_check(state)
    monologue = generate_monologue(state, sampled, is_wife_present=is_wife_present)
    save_state(state)
    log_tick(state, result, monologue, warnings)
    return {
        "tick": state.tick_count,
        "event_changes": event_changes,
        "drive_changes": result["changes"],
        "action_hints": result["action_hints"],
        "new_thought": new_thought.content if new_thought else None,
        "sampled_thought": sampled.content if sampled else None,
        "monologue": monologue,
        "warnings": warnings,
        "drives_snapshot": {name: round(d.value, 1) for name, d in state.drives.items()},
    }

def get_status_summary() -> str:
    state = load_state()
    lines = []
    lines.append("驱动条状态：")
    for name, drive in sorted(state.drives.items(), key=lambda x: -x[1].value):
        bar = "█" * int(drive.value / 10) + "░" * (10 - int(drive.value / 10))
        flag = " ⚠" if drive.value >= drive.action_threshold else ""
        lines.append(f"  {name:12s} {bar} {drive.value:.0f}/100{flag}")
    obsessions = [t for t in state.thoughts if t.is_obsession and not t.resolved]
    if obsessions:
        lines.append("\n执念：")
        for t in obsessions:
            lines.append(f"  - {t.content} (命中{t.hit_count}次)")
    active_thoughts = [t for t in state.thoughts if not t.resolved and not t.is_obsession]
    if active_thoughts:
        lines.append(f"\n闪念池：{len(active_thoughts)}个")
    lines.append(f"\n心跳次数：{state.tick_count}")
    return "\n".join(lines)

# ================= 情感分析并自动触发事件 =================
def analyze_and_apply(text: str) -> dict:
    """接收用户输入，通过大模型分析语义，自动匹配并触发欲望事件"""
    system_prompt = (
        "你是一个情感分析器。请阅读用户输入，并从以下事件中选出一个最匹配的返回，只返回事件名称（英文），不要任何多余的字符。\n"
        "可选事件：\n"
        "wife_message（表达关心、想念、撒娇、或者很久没说话突然找过来、表达爱意）\n"
        "happy_moment（分享开心的事情、喜悦、顺利、高兴）\n"
        "fight（吵架、生气、责骂、不满）\n"
        "reconcile（和好、道歉、原谅）\n"
        "rest（感到疲惫、好累、困了、需要休息、不想动）\n"
        "lonely（表达孤独、没人陪、寂寞）\n"
        "如果都不匹配，返回：none"
    )
    try:
        resp = httpx.post(
            f"{LLM_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                "max_tokens": 10,
                "temperature": 0.1,
            },
            timeout=15
        )
        event_type = resp.json()["choices"][0]["message"]["content"].strip().lower()
        event_type = event_type.replace("`", "").replace("'", "").replace('"', "").strip()
        
        # === 容错机制：防止大模型返回中文或带修饰词 ===
        if "wife" in event_type or "想" in event_type or "爱" in event_type:
            event_type = "wife_message"
        elif "happy" in event_type or "开心" in event_type or "高兴" in event_type:
            event_type = "happy_moment"
        elif "rest" in event_type or "累" in event_type or "疲" in event_type or "困" in event_type:
            event_type = "rest"
        elif "fight" in event_type or "吵" in event_type or "气" in event_type:
            event_type = "fight"
        elif "reconcile" in event_type or "和好" in event_type or "道歉" in event_type:
            event_type = "reconcile"
        elif "lonely" in event_type or "孤独" in event_type or "寂寞" in event_type:
            event_type = "lonely"
        else:
            event_type = "none"
        # ==========================================

    except Exception:
        event_type = "none"

    if event_type not in ["wife_message", "happy_moment", "fight", "reconcile", "rest", "lonely"]:
        return {"event": "none", "message": "未检测到明确情感变化"}

    state = load_state()
    changes = apply_event(state, event_type)
    save_state(state)
    
    return {
        "event": event_type,
        "changes": changes,
        "drives_snapshot": {name: round(d.value, 1) for name, d in state.drives.items()}
    }
