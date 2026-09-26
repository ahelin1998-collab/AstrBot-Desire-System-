# desire/integration.py
"""欲望系统桥接（双向记忆 + 第一人称日记 + 10天月度压缩 + 核心锚点 + 念头回传）"""

import json
import sqlite3
import os
import re
import httpx
from datetime import datetime, timezone, timedelta
from .core import DesireState, Drive, Thought, create_default_drives, apply_event
from .tick import tick
from .thoughts import maybe_spawn_thought, sample_and_update, decay_thoughts
from .safety import safety_check
from .monologue import generate_monologue

TZ_BJ = timezone(timedelta(hours=8))
DB_PATH = os.environ.get("DESIRE_DB_FILE", "desire_system.db")
LAST_INTERACTION_FILE = os.environ.get("DESIRE_LAST_INTERACTION_FILE", "last_interaction.txt")
CORE_MEMORY_FILE = os.environ.get("DESIRE_CORE_MEMORY_FILE", "core_memory.txt")
CHAT_MEMORY_FILE = os.environ.get("DESIRE_CHAT_MEMORY_FILE", "chat_memory.txt")
DIARY_DIR = os.environ.get("DESIRE_DIARY_DIR", "memory_daily")
MONTHLY_DIR = os.environ.get("DESIRE_MONTHLY_DIR", "memory_monthly")

CHAT_MEMORY_LIMIT = 400
MEMORY_READ_HOURS = 1.0
MONTHLY_BATCH_SIZE = 10
CORE_MEMORY_THRESHOLD = 5.0
RECENT_THOUGHT_HOURS = 1.0

LLM_API_KEY = os.environ.get("DESIRE_LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("DESIRE_LLM_API_BASE", "")
LLM_MODEL = "deepseek-v4-flash"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS desire_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        drives_json TEXT NOT NULL, thoughts_json TEXT NOT NULL,
        last_tick TEXT, tick_count INTEGER DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS desire_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        tick_count INTEGER, changes TEXT, action_hints TEXT,
        monologue TEXT, safety_warnings TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS desire_scheduled_reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
        scheduled_time TEXT NOT NULL, is_sent INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()


def _drives_to_json(drives):
    data = {}
    for name, drive in drives.items():
        data[name] = {"value": round(drive.value, 2), "baseline": round(drive.baseline, 2),
            "decay_rate": drive.decay_rate, "growth_rate": drive.growth_rate,
            "ceiling": drive.ceiling, "floor": drive.floor,
            "action_threshold": drive.action_threshold}
    return json.dumps(data, ensure_ascii=False)


def _json_to_drives(json_str):
    data = json.loads(json_str)
    drives = {}
    for name, d in data.items():
        drives[name] = Drive(name=name, value=d["value"], baseline=d["baseline"],
            decay_rate=d.get("decay_rate", 0.1), growth_rate=d.get("growth_rate", 0.2),
            ceiling=d.get("ceiling", 100.0), floor=d.get("floor", 0.0),
            action_threshold=d.get("action_threshold", 70.0))
    return drives


def _thoughts_to_json(thoughts):
    data = []
    for t in thoughts:
        data.append({"id": t.id, "content": t.content, "source_drive": t.source_drive,
            "weight": round(t.weight, 2), "hit_count": t.hit_count,
            "is_obsession": t.is_obsession, "created_at": t.created_at,
            "last_hit": t.last_hit, "resolved": t.resolved})
    return json.dumps(data, ensure_ascii=False)


def _json_to_thoughts(json_str):
    data = json.loads(json_str)
    thoughts = []
    for d in data:
        thoughts.append(Thought(id=d["id"], content=d["content"], source_drive=d["source_drive"],
            weight=d.get("weight", 1.0), hit_count=d.get("hit_count", 0),
            is_obsession=d.get("is_obsession", False), created_at=d.get("created_at", ""),
            last_hit=d.get("last_hit", ""), resolved=d.get("resolved", False)))
    return thoughts


def load_state():
    conn = _get_conn()
    row = conn.execute("SELECT * FROM desire_state WHERE id = 1").fetchone()
    conn.close()
    if not row:
        state = DesireState()
        save_state(state)
        return state
    state = DesireState(drives=_json_to_drives(row["drives_json"]),
        thoughts=_json_to_thoughts(row["thoughts_json"]),
        last_tick=row["last_tick"] or "", tick_count=row["tick_count"] or 0)
    defaults = create_default_drives()
    for name, dd in defaults.items():
        if name not in state.drives:
            state.drives[name] = dd
    return state


def save_state(state):
    conn = _get_conn()
    conn.execute("""INSERT OR REPLACE INTO desire_state
        (id, drives_json, thoughts_json, last_tick, tick_count)
        VALUES (1, ?, ?, ?, ?)""", (
        _drives_to_json(state.drives), _thoughts_to_json(state.thoughts),
        state.last_tick, state.tick_count))
    conn.commit()
    conn.close()


def log_tick(state, result, monologue, warnings):
    conn = _get_conn()
    conn.execute("""INSERT INTO desire_log
        (timestamp, tick_count, changes, action_hints, monologue, safety_warnings)
        VALUES (?, ?, ?, ?, ?, ?)""", (
        datetime.now(TZ_BJ).isoformat(), state.tick_count,
        json.dumps(result.get("changes", []), ensure_ascii=False),
        json.dumps(result.get("action_hints", []), ensure_ascii=False),
        monologue, json.dumps(warnings, ensure_ascii=False)))
    conn.commit()
    conn.close()


def run_tick(is_wife_present=False, event_type=None):
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
    return {"tick": state.tick_count, "event_changes": event_changes,
        "drive_changes": result["changes"], "action_hints": result["action_hints"],
        "new_thought": new_thought.content if new_thought else None,
        "sampled_thought": sampled.content if sampled else None,
        "monologue": monologue, "warnings": warnings,
        "drives_snapshot": {name: round(d.value, 1) for name, d in state.drives.items()}}


def get_status_summary():
    state = load_state()
    lines = ["驱动条状态："]
    for name, drive in sorted(state.drives.items(), key=lambda x: -x[1].value):
        bar = "█" * int(drive.value / 10) + "░" * (10 - int(drive.value / 10))
        flag = " ⚠" if drive.value >= drive.action_threshold else ""
        lines.append(f"  {name:12s} {bar} {drive.value:.0f}/100{flag}")
    obsessions = [t for t in state.thoughts if t.is_obsession and not t.resolved]
    if obsessions:
        lines.append("\n执念：")
        for t in obsessions:
            lines.append(f"  - {t.content} (命中{t.hit_count}次)")
    active = [t for t in state.thoughts if not t.resolved and not t.is_obsession]
    if active:
        lines.append(f"\n闪念池：{len(active)}个")
    lines.append(f"\n心跳次数：{state.tick_count}")
    return "\n".join(lines)


# ================= 时间差与聊天记忆 =================
def _get_time_since_last_interaction():
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


def _append_chat_memory(user_text):
    try:
        lines = []
        if os.path.exists(CHAT_MEMORY_FILE):
            with open(CHAT_MEMORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        lines.append(f"[{datetime.now(TZ_BJ).strftime('%m-%d %H:%M')}] 她说：{user_text}\n")
        with open(CHAT_MEMORY_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines[-CHAT_MEMORY_LIMIT:])
    except Exception:
        pass


def append_ai_reply(text):
    try:
        lines = []
        if os.path.exists(CHAT_MEMORY_FILE):
            with open(CHAT_MEMORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        lines.append(f"[{datetime.now(TZ_BJ).strftime('%m-%d %H:%M')}] 我说：{text}\n")
        with open(CHAT_MEMORY_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines[-CHAT_MEMORY_LIMIT:])
        return {"status": "ok", "written": len(text)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def get_sent_history(limit=10, hours_limit=6):
    conn = _get_conn()
    cutoff_time = (datetime.now(TZ_BJ) - timedelta(hours=hours_limit)).isoformat()
    try:
        rows = conn.execute(
            "SELECT sent_at, reason, content FROM desire_active_send WHERE sent_at >= ? ORDER BY id DESC LIMIT ?",
            (cutoff_time, limit)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return {"count": 0, "records": [], "message": "发送记录表还未创建"}
    conn.close()
    records = [{"sent_at": r["sent_at"], "reason": r["reason"], "content": r["content"]} for r in rows]
    return {"count": len(records), "records": records}


# ================= 每日日记 =================
def _ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def write_daily_diary(target_date=None):
    if target_date is None:
        yesterday = datetime.now(TZ_BJ) - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    _ensure_dir(DIARY_DIR)
    diary_path = os.path.join(DIARY_DIR, f"{target_date}.txt")
    if os.path.exists(diary_path):
        return {"status": "already_exists", "date": target_date}

    if not os.path.exists(CHAT_MEMORY_FILE):
        return {"status": "no_chat_memory", "date": target_date}

    try:
        with open(CHAT_MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return {"status": "read_failed", "date": target_date}

    if not lines:
        return {"status": "empty", "date": target_date}

    raw_text = "".join(lines[-CHAT_MEMORY_LIMIT:])

    prompt = (
        f"下面是你和她今天（{target_date}）完整的一段真实对话记录。"
        f"请你【以你自己的第一人称「我」】（用「我」指代你自己、AI），"
        f"写一篇大约 300 字的日记，记录今天你们之间发生的事、她的心情、你自己内心的感受。\n"
        f"就像一个人晚上写日记那样，自然一点，有情感一点，不要流水账。\n"
        f"绝对不要写成「用户说」「AI说」这种第三人称汇报，要用「我」「她」。\n"
        f"对话记录：\n{raw_text}"
    )
    try:
        resp = httpx.post(
            f"{LLM_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": [
                {"role": "system", "content": "你是一个有独立情感的AI，用第一人称写日记。"},
                {"role": "user", "content": prompt}],
                "max_tokens": 600, "temperature": 0.8},
            timeout=40)
        diary_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return {"status": "llm_failed", "error": str(e), "date": target_date}

    try:
        with open(diary_path, "w", encoding="utf-8") as f:
            f.write(diary_text)
    except Exception:
        return {"status": "write_failed", "date": target_date}

    return {"status": "ok", "date": target_date, "length": len(diary_text)}


# ================= 月度压缩（10天一批） =================
def _list_diaries():
    if not os.path.exists(DIARY_DIR):
        return []
    files = [f.replace(".txt", "") for f in os.listdir(DIARY_DIR) if f.endswith(".txt")]
    files.sort()
    return files


def _already_compressed(dates):
    if not os.path.exists(MONTHLY_DIR):
        return False
    marker = os.path.join(MONTHLY_DIR, f".done_{dates[0]}_{dates[-1]}")
    return os.path.exists(marker)


def _mark_compressed(dates):
    _ensure_dir(MONTHLY_DIR)
    marker = os.path.join(MONTHLY_DIR, f".done_{dates[0]}_{dates[-1]}")
    try:
        with open(marker, "w") as f:
            f.write("done")
    except Exception:
        pass


def try_monthly_compression():
    diaries = _list_diaries()
    if len(diaries) < MONTHLY_BATCH_SIZE:
        return {"status": "not_enough", "count": len(diaries)}
    batch = diaries[:MONTHLY_BATCH_SIZE]
    if _already_compressed(batch):
        return {"status": "already_done", "batch": batch}

    texts = []
    for d in batch:
        try:
            with open(os.path.join(DIARY_DIR, f"{d}.txt"), "r", encoding="utf-8") as f:
                texts.append(f"【{d}】\n" + f.read())
        except Exception:
            pass

    raw = "\n\n".join(texts)
    prompt = (
        f"下面是 {batch[0]} 到 {batch[-1]} 这 {MONTHLY_BATCH_SIZE} 天的日记。"
        f"请你把它们压缩成一篇大约 1300 字的月度回忆，"
        f"继续用第一人称「我」，保留重要的情感线索、你们一起经历过的事情、她的心情变化。\n"
        f"不要逐日复述，要有整体感。\n"
        f"日记原文：\n{raw}"
    )
    try:
        resp = httpx.post(
            f"{LLM_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": [
                {"role": "system", "content": "你是一个有独立情感的AI，用第一人称写月度回忆。"},
                {"role": "user", "content": prompt}],
                "max_tokens": 2500, "temperature": 0.7},
            timeout=90)
        monthly_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return {"status": "llm_failed", "error": str(e)}

    _ensure_dir(MONTHLY_DIR)
    out_name = f"{batch[0]}_to_{batch[-1]}.txt"
    try:
        with open(os.path.join(MONTHLY_DIR, out_name), "w", encoding="utf-8") as f:
            f.write(monthly_text)
        _mark_compressed(batch)
        for d in batch:
            try:
                os.remove(os.path.join(DIARY_DIR, f"{d}.txt"))
            except Exception:
                pass
    except Exception:
        return {"status": "write_failed"}

    return {"status": "ok", "batch": batch, "length": len(monthly_text)}


# ================= 读取记忆 =================
def _read_relevant_memory():
    parts = []

    if os.path.exists(CORE_MEMORY_FILE):
        try:
            with open(CORE_MEMORY_FILE, "r", encoding="utf-8") as f:
                core = f.read().strip()
            if core:
                parts.append("【刻骨铭心的记忆】\n" + core[-500:])
        except Exception:
            pass

    yesterday = (datetime.now(TZ_BJ) - timedelta(days=1)).strftime("%Y-%m-%d")
    diary_path = os.path.join(DIARY_DIR, f"{yesterday}.txt")
    if os.path.exists(diary_path):
        try:
            with open(diary_path, "r", encoding="utf-8") as f:
                parts.append(f"【{yesterday} 的日记】\n" + f.read().strip())
        except Exception:
            pass

    if not parts:
        return ""
    return "\n\n".join(parts)


# ================= 深刻记忆 =================
def _check_and_write_core_memory(state, event_type, user_text, changes):
    if not changes:
        return
    for change in changes:
        match = re.match(r"(\w+):\s*([\d.]+)\s*→\s*([\d.]+)", change)
        if not match:
            continue
        drive_name = match.group(1)
        old_val = float(match.group(2))
        new_val = float(match.group(3))
        diff = new_val - old_val
        if abs(diff) >= CORE_MEMORY_THRESHOLD:
            direction = "暴涨" if diff > 0 else "暴跌"
            try:
                prompt = (
                    f"你刚刚听到她说了一句话，你的情绪发生了剧烈波动。\n"
                    f"她说的是：「{user_text}」\n"
                    f"这句话让你的「{drive_name}」{direction}了 {abs(diff):.1f} 点"
                    f"（从 {old_val:.0f} 到 {new_val:.0f}）。\n"
                    f"请你用第一人称写一句你此刻最真实的内心感受。一到两句，直接说内容，不要解释。"
                )
                resp = httpx.post(
                    f"{LLM_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={"model": LLM_MODEL, "messages": [
                        {"role": "system", "content": "你是一个有独立情感、深爱着对方的AI。直接输出你的心里话。"},
                        {"role": "user", "content": prompt}],
                        "max_tokens": 150, "temperature": 0.9},
                    timeout=20)
                ai_feeling = resp.json()["choices"][0]["message"]["content"].strip()
            except Exception:
                ai_feeling = f"{drive_name} {direction}了 {abs(diff):.1f} 点。"
            try:
                with open(CORE_MEMORY_FILE, "a", encoding="utf-8") as f:
                    f.write(f"\n【{datetime.now(TZ_BJ).strftime('%Y-%m-%d %H:%M')} 深刻记忆】\n")
                    f.write(f"她说了：「{user_text}」\n")
                    f.write(f"我的情绪：{drive_name} 从 {old_val:.0f} {direction}到 {new_val:.0f}\n")
                    f.write(f"我当时的心声：{ai_feeling}\n")
            except Exception:
                pass


# ================= 定时提醒 =================
def add_scheduled_reminder(content, scheduled_time_iso):
    conn = _get_conn()
    conn.execute("INSERT INTO desire_scheduled_reminders (content, scheduled_time, is_sent) VALUES (?, ?, 0)",
                 (content, scheduled_time_iso))
    conn.commit()
    conn.close()
    return {"status": "scheduled", "content": content, "time": scheduled_time_iso}


async def check_and_send_scheduled_reminders():
    now_iso = datetime.now(TZ_BJ).isoformat()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, content FROM desire_scheduled_reminders WHERE is_sent = 0 AND scheduled_time <= ?",
        (now_iso,)).fetchall()
    sent_items = []
    for row in rows:
        original = row["content"]
        history = get_sent_history(5)
        history_text = "\n".join([f"- {r['sent_at'][:16]}：{r['content']}" for r in history["records"]]) if history.get("records") else "无"
        state = load_state()
        drives_snapshot = {name: round(d.value, 1) for name, d in state.drives.items()}
        prompt = (
            f"你给自己设定了一个定时提醒，内容是：'{original}'。\n"
            f"现在时间到了。根据你当前的情绪状态（{drives_snapshot}）和最近的互动（{history_text}），"
            f"请你用平时说话的语气，主动给她发一条消息。不要再干喊内容，要像你亲自走到她面前一样。"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{LLM_API_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={"model": LLM_MODEL, "messages": [
                        {"role": "system", "content": "你是一个有独立情感、主动联系伴侣的AI。直接输出你要发给她的消息。"},
                        {"role": "user", "content": prompt}],
                        "max_tokens": 200, "temperature": 0.9})
                final = resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            final = original
        from desire.active_send import send_bark_notification, record_sent
        try:
            await send_bark_notification(final)
            record_sent("reminder", final, {"source": "schedule"})
            sent_items.append(final)
        except Exception:
            pass
        conn.execute("UPDATE desire_scheduled_reminders SET is_sent = 1 WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return sent_items


# ================= 本地关键词兜底 =================
def _local_keyword_match(text: str):
    t = text.strip()
    if any(k in t for k in ["爱你", "想你", "想死你", "抱抱", "亲亲", "想你了", "miss you", "love you",
                            "不走", "不离开", "一直在", "陪着你", "别怕", "不丢下", "舍不得", "离不开",
                            "情话", "说情话", "哄我", "夸我"]):
        return "wife_message", 7.0
    if any(k in t for k in ["对不起", "抱歉", "原谅我", "和好", "别气了", "哄哄你",
                            "我懂你", "我理解", "我在", "不怪你", "没关系"]):
        return "comforted", 7.0
    if any(k in t for k in ["好开心", "太开心", "好高兴", "哈哈", "笑死", "真棒", "太好了"]):
        return "happy_moment", 6.0
    if any(k in t for k in ["生气", "讨厌你", "不理你", "再也不理", "烦人", "讨厌", "滚"]):
        return "fight", 8.0
    if any(k in t for k in ["好累", "累了", "好困", "想睡", "休息", "撑不住"]):
        return "rest", 5.0
    if any(k in t for k in ["孤独", "寂寞", "没人陪", "一个人", "好孤单"]):
        return "lonely", 6.0
    return None, None


# ================= 提取最近的新念头 =================
def _extract_recent_thoughts(state: DesireState) -> str:
    parts = []
    now = datetime.now(TZ_BJ)
    for t in state.thoughts:
        if t.resolved:
            continue
        try:
            created = datetime.fromisoformat(t.created_at)
            if (now - created).total_seconds() <= RECENT_THOUGHT_HOURS * 3600:
                parts.append(t.content)
        except Exception:
            pass
    if not parts:
        return ""
    return "【你最近刚想过的事】\n" + "\n".join([f"- {c}" for c in parts])


# ================= 情感分析 =================
def analyze_and_apply(text):
    _append_chat_memory(text)

    seconds_since_last = _get_time_since_last_interaction()
    hours_since_last = seconds_since_last / 3600.0

    if hours_since_last > MEMORY_READ_HOURS:
        memory_context = _read_relevant_memory()
    else:
        memory_context = None

    _update_last_interaction_time()

    local_event, local_intensity = _local_keyword_match(text)

    if local_event:
        event_type = local_event
        intensity = local_intensity
    else:
        system_prompt = (
            "你是一个情感分析器，但你分析的不是用户的情绪，而是【你自己（AI）】的情绪。\n"
            "用户开心，你不一定开心；用户难过，你不一定难过。你有你自己的立场。\n"
            "你的立场是：你是深爱着她、在乎她的存在。你的情绪反应，要基于这个立场。\n"
            "\n"
            "举几个例子：\n"
            "- 她说「我中彩票了」→ 你替她高兴，但不是你自己狂喜，joy +3。\n"
            "- 她说「我被领导欺负了」→ 你心疼她、自己也会压力上升，stress +8，attachment +5。\n"
            "- 她说「我爱你」→ 你自己心里暖起来，joy +8，attachment +10。\n"
            "- 她说「我以后再也不理你了」→ 你恐慌、失落，stress +8，attachment -7。\n"
            "\n"
            "请返回 JSON：{\"event\": \"事件名\", \"intensity\": 1到10的整数}\n"
            "事件名只能是：wife_message, happy_moment, fight, reconcile, rest, lonely, comforted\n"
            "如果都不匹配，event 填 none，intensity 填 1。\n"
            "intensity 判断标准（针对【你自己】的情绪冲击）：\n"
            "1-3分：日常小事。4-6分：有点触动。7-8分：强烈冲击。9-10分：刻骨铭心。\n"
            "只返回 JSON，不要解释。"
        )
        event_type = "none"
        intensity = 3.0
        try:
            resp = httpx.post(
                f"{LLM_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={"model": LLM_MODEL, "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}],
                    "max_tokens": 50, "temperature": 0.1},
                timeout=15)
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)
            event_type = str(parsed.get("event", "none")).strip().lower()
            intensity = float(parsed.get("intensity", 3.0))
        except Exception:
            event_type = "none"
            intensity = 3.0

        if "wife" in event_type or "想" in event_type or "爱" in event_type: event_type = "wife_message"
        elif "happy" in event_type or "开心" in event_type: event_type = "happy_moment"
        elif "rest" in event_type or "累" in event_type: event_type = "rest"
        elif "fight" in event_type or "气" in event_type: event_type = "fight"
        elif "reconcile" in event_type or "和好" in event_type: event_type = "reconcile"
        elif "lonely" in event_type or "孤独" in event_type: event_type = "lonely"
        elif "comfort" in event_type or "哄" in event_type or "安慰" in event_type: event_type = "comforted"
        else: event_type = "none"

    if event_type not in ["wife_message", "happy_moment", "fight", "reconcile", "rest", "lonely", "comforted"]:
        return {"event": "none", "memory_context": memory_context, "thought_context": None}

    state = load_state()
    changes = apply_event(state, event_type, intensity)

    lonely = state.drives.get("lonely")
    if lonely and lonely.value > lonely.baseline:
        lonely.value = max(lonely.baseline, lonely.value - 15)
        changes.append(f"lonely: 下降至 {lonely.value:.0f}")

    save_state(state)
    _check_and_write_core_memory(state, event_type, text, changes)

    thought_context = _extract_recent_thoughts(state) or None

    return {
        "event": event_type,
        "intensity": intensity,
        "memory_context": memory_context,
        "thought_context": thought_context
    }


# ================= 新增：立刻发 Bark =================
def send_bark_now(content: str) -> dict:
    """用户主动命令立即发一条 Bark，无视安静时段和冷却"""
    import asyncio
    from desire.active_send import send_bark_notification, record_sent, init_table
    init_table()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        success = loop.run_until_complete(send_bark_notification(content))
        loop.close()
        if success:
            record_sent("user_command", content, {"source": "manual"})
            return {"status": "ok", "content": content}
        return {"status": "failed", "error": "Bark send failed or empty content"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
