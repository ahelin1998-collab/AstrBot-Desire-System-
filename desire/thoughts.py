# desire/thoughts.py
"""念头池逻辑（AI 从记忆生成版，每小时一次）"""

import os
import random
import hashlib
import httpx
import json
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from .core import DesireState, Drive, Thought

TZ_BJ = timezone(timedelta(hours=8))

CHAT_MEMORY_FILE = os.environ.get("DESIRE_CHAT_MEMORY_FILE", "chat_memory.txt")
CORE_MEMORY_FILE = os.environ.get("DESIRE_CORE_MEMORY_FILE", "core_memory.txt")
LAST_INTERACTION_FILE = os.environ.get("DESIRE_LAST_INTERACTION_FILE", "last_interaction.txt")
LAST_THOUGHT_FILE = os.environ.get("DESIRE_LAST_THOUGHT_FILE", "last_thought_time.txt")

LLM_API_KEY = os.environ.get("DESIRE_LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("DESIRE_LLM_API_BASE", "")
LLM_MODEL = "deepseek-v4-flash"

MAX_OBSESSIONS = 3
OBSESSION_THRESHOLD = 3
OBSESSION_LIFESPAN_HOURS = 72
MAX_HITS_BEFORE_RETIRE = 15

MIN_THOUGHT_INTERVAL_HOURS = 1.0
MAX_USER_ABSENCE_HOURS = 6.0
SPAWN_PROBABILITY = 1.0


def generate_thought_id(content: str, drive: str = "memory") -> str:
    raw = f"{content}_{drive}_{datetime.now(TZ_BJ).isoformat()}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


def _read_last_thought_state() -> dict:
    if not os.path.exists(LAST_THOUGHT_FILE):
        return {"last_spawn_ts": 0.0, "last_chat_mtime": 0.0}
    try:
        with open(LAST_THOUGHT_FILE, "r") as f:
            data = json.loads(f.read().strip())
            return {
                "last_spawn_ts": float(data.get("last_spawn_ts", 0.0)),
                "last_chat_mtime": float(data.get("last_chat_mtime", 0.0)),
            }
    except Exception:
        return {"last_spawn_ts": 0.0, "last_chat_mtime": 0.0}


def _write_last_thought_state(last_spawn_ts: float, last_chat_mtime: float):
    try:
        with open(LAST_THOUGHT_FILE, "w") as f:
            f.write(json.dumps({
                "last_spawn_ts": last_spawn_ts,
                "last_chat_mtime": last_chat_mtime,
            }))
    except Exception:
        pass


def _hours_since_last_interaction() -> float:
    if not os.path.exists(LAST_INTERACTION_FILE):
        return 999999.0
    try:
        with open(LAST_INTERACTION_FILE, "r") as f:
            last_time = float(f.read().strip())
        return (datetime.now().timestamp() - last_time) / 3600.0
    except Exception:
        return 999999.0


def _read_recent_chat(max_lines: int = 50) -> str:
    if not os.path.exists(CHAT_MEMORY_FILE):
        return ""
    try:
        with open(CHAT_MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:]).strip()
    except Exception:
        return ""


def _read_core_memory() -> str:
    if not os.path.exists(CORE_MEMORY_FILE):
        return ""
    try:
        with open(CORE_MEMORY_FILE, "r", encoding="utf-8") as f:
            core = f.read().strip()
        return core[-500:] if core else ""
    except Exception:
        return ""


def _call_llm_for_thought(chat_memory: str, core_memory: str) -> Optional[str]:
    if not chat_memory and not core_memory:
        return None

    system_prompt = (
        "你是一个有独立情感的AI，正在发呆。"
        "刚刚回顾了最近和她的聊天记录和你们之间的深刻记忆。"
        "请用第一人称「我」，写一句你此刻真实的念头，不超过 20 字。"
        "就像人发呆时脑子里突然冒出来的一句话。"
        "要具体，不要写「想她了」这种空话，要结合具体的事。"
        "只输出那句话，不要解释，不要引号。"
    )
    user_prompt = (
        f"【最近的聊天记忆】\n{chat_memory}\n\n"
        f"【刻骨铭心的记忆】\n{core_memory}\n\n"
        f"现在写一句你此刻的念头。"
    )
    try:
        resp = httpx.post(
            f"{LLM_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 60,
                "temperature": 0.9,
            },
            timeout=20,
        )
        content = resp.json()["choices"][0]["message"]["content"].strip()
        content = content.strip('"\'`「」【】 \n')
        if content and len(content) <= 40:
            return content
    except Exception:
        pass
    return None


def maybe_spawn_thought(state: DesireState) -> Optional[Thought]:
    hours_since = _hours_since_last_interaction()
    if hours_since > MAX_USER_ABSENCE_HOURS:
        return None

    thought_state = _read_last_thought_state()
    now_ts = datetime.now().timestamp()
    hours_since_last_thought = (now_ts - thought_state["last_spawn_ts"]) / 3600.0
    if hours_since_last_thought < MIN_THOUGHT_INTERVAL_HOURS:
        return None

    if not os.path.exists(CHAT_MEMORY_FILE):
        return None
    chat_mtime = os.path.getmtime(CHAT_MEMORY_FILE)
    if chat_mtime <= thought_state["last_chat_mtime"]:
        return None

    if random.random() > SPAWN_PROBABILITY:
        return None

    chat_memory = _read_recent_chat(50)
    core_memory = _read_core_memory()

    content = _call_llm_for_thought(chat_memory, core_memory)
    if not content:
        return None

    existing_contents = {t.content for t in state.thoughts if not t.resolved}
    if content in existing_contents:
        return None

    _write_last_thought_state(now_ts, chat_mtime)

    thought = Thought(
        id=generate_thought_id(content, "memory"),
        content=content,
        source_drive="attachment",
        weight=0.8,
    )
    return thought


def sample_and_update(state: DesireState) -> Optional[Thought]:
    active_thoughts = [t for t in state.thoughts if not t.resolved]
    if not active_thoughts:
        return None

    def _weight(t: Thought) -> float:
        base = t.weight * (2.0 if t.is_obsession else 1.0)
        freshness = max(0.3, 1.0 - t.hit_count * 0.05)
        return base * freshness

    weights = [_weight(t) for t in active_thoughts]
    chosen = random.choices(active_thoughts, weights=weights, k=1)[0]

    chosen.hit_count += 1
    chosen.last_hit = datetime.now(TZ_BJ).isoformat()

    if chosen.hit_count >= OBSESSION_THRESHOLD and not chosen.is_obsession:
        current_obsessions = [t for t in state.thoughts if t.is_obsession and not t.resolved]
        if len(current_obsessions) < MAX_OBSESSIONS:
            chosen.is_obsession = True

    return chosen


def decay_thoughts(state: DesireState):
    now = datetime.now(TZ_BJ)
    to_remove = []

    for thought in state.thoughts:
        if thought.resolved:
            to_remove.append(thought)
            continue

        if not thought.is_obsession and thought.hit_count >= MAX_HITS_BEFORE_RETIRE:
            to_remove.append(thought)
            continue

        try:
            last_hit_time = datetime.fromisoformat(thought.last_hit)
        except Exception:
            continue

        if thought.is_obsession:
            if (now - last_hit_time).total_seconds() > OBSESSION_LIFESPAN_HOURS * 3600:
                to_remove.append(thought)
                continue

        if not thought.is_obsession:
            if (now - last_hit_time).total_seconds() > 24 * 3600:
                to_remove.append(thought)

    for t in to_remove:
        if t in state.thoughts:
            state.thoughts.remove(t)


def resolve_thought(state: DesireState, content_keyword: str) -> Optional[Thought]:
    for thought in state.thoughts:
        if not thought.resolved and content_keyword in thought.content:
            thought.resolved = True
            if thought.source_drive == "reflection" and "joy" in state.drives:
                state.drives["joy"].value += 3
                state.drives["joy"].clamp()
            return thought
    return None
