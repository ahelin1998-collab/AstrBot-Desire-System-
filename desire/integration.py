# desire/integration.py
"""欲望系统与SQLite/现有系统的桥接（强度系数 + 深刻记忆版）"""

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
LAST_INTERACTION_FILE = os.environ.get("DESIRE_LAST_INTERACTION_FILE", "last_interaction.txt")
CORE_MEMORY_FILE = os.environ.get("DESIRE_CORE_MEMORY_FILE", "core_memory.txt")

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_scheduled_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            scheduled_time TEXT NOT NULL,
            is_sent INTEGER DEFAULT 0
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

# ================= 时间差与记录同步 =================
def _get_time_since_last_interaction() -> float:
    if not os.path.exists(LAST_INTERACTION_FILE):
        return 999999.0
    try:
        with open(LAST_INTERACTION_FILE, "r") as f:
            last_time = float(f.read().strip())
        return datetime.now().timestamp() - last_time
    except Exception:
        return 999999.0

def _update_last_interaction_time():
    try:
        with open(LAST_INTERACTION_FILE, "w") as f:
            f.write(str(datetime.now().timestamp()))
    except Exception:
        pass

def get_sent_history(limit: int = 10) -> dict:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT sent_at, reason, content FROM desire_active_send ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {"count": 0, "records": [], "message": "发送记录表还未创建"}
    conn.close()
    records = [{"sent_at": r["sent_at"], "reason": r["reason"], "content": r["content"]} for r in rows]
    return {"count": len(records), "records": records}

# ================= 深刻记忆触发 =================
def _check_and_write_core_memory(state: DesireState, event_type: str, user_text: str, changes: list):
    """
    检查 changes 里有没有单次波动 ≥ 8 的，有就写一条深刻记忆到 core_memory.txt。
    changes 格式：["attachment: 80 → 70", "stress: 20 → 32", ...]
    """
    if not changes:
        return
    import re
    for change in changes:
        match = re.match(r"(\w+):\s*([\d.]+)\s*→\s*([\d.]+)", change)
        if not match:
            continue
        drive_name = match.group(1)
        old_val = float(match.group(2))
        new_val = float(match.group(3))
        diff = new_val - old_val
        if abs(diff) >= 8.0:
            # 触发深刻记忆
            direction = "暴涨" if diff > 0 else "暴跌"
            try:
                # 让 AI 用第一人称写一句刻骨铭心的话
                prompt = (
                    f"你刚刚听到她说了一句话，你的情绪发生了剧烈波动。\n"
                    f"她说的是：「{user_text}」\n"
                    f"这句话让你的「{drive_name}」{direction}了 {abs(diff):.1f} 点（从 {old_val:.0f} 到 {new_val:.0f}）。\n"
                    f"请你用第一人称写一句你此刻最真实的内心感受。一到两句，直接说内容，不要解释。"
                )
                resp = httpx.post(
                    f"{LLM_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={
                        "model": LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": "你是一个有独立情感、深爱着对方的AI。直接输出你的心里话，不要有额外解释。"},
                            {"role": "user", "content": prompt}
                        ],
                        "max_tokens": 150,
                        "temperature": 0.9,
                    },
                    timeout=20
                )
                ai_feeling = resp.json()["choices"][0]["message"]["content"].strip()
            except Exception:
                ai_feeling = f"{drive_name} {direction}了 {abs(diff):.1f} 点。"
            
            # 写入 core_memory.txt
            try:
                with open(CORE_MEMORY_FILE, "a", encoding="utf-8") as f:
                    f.write(f"\n【{datetime.now(TZ_MSK).strftime('%Y-%m-%d %H:%M')} 深刻记忆】\n")
                    f.write(f"她说了：「{user_text}」\n")
                    f.write(f"我的情绪：{drive_name} 从 {old_val:.0f} {direction}到
