# desire/tick.py
"""欲望系统心跳（tick）逻辑（阻尼 + 固定基线 + 执念联动 + 孤独拟人化版）"""

import os
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from .core import Drive, DesireState, Thought

TZ_MSK = timezone(timedelta(hours=3))
LAST_INTERACTION_FILE = os.environ.get("DESIRE_LAST_INTERACTION_FILE", "last_interaction.txt")


def _hours_since_last_interaction() -> float:
    """读 last_interaction.txt，算出距离上次互动过了多少小时"""
    if not os.path.exists(LAST_INTERACTION_FILE):
        return 0.0
    try:
        with open(LAST_INTERACTION_FILE, "r") as f:
            last_time = float(f.read().strip())
        return (datetime.now().timestamp() - last_time) / 3600.0
    except Exception:
        return 0.0


COUPLING: Dict[Tuple[str, str], float] = {
    ("attachment", "intimacy"): 0.3,
    ("intimacy", "attachment"): 0.1,
    ("stress", "fatigue"): 0.4,
    ("fatigue", "curiosity"): -0.3,
    ("curiosity", "duty"): 0.2,
    ("duty", "stress"): 0.1,
    ("reflection", "stress"): -0.2,
    ("social", "attachment"): -0.05,
    ("attachment", "stress"): 0.1,
    ("stress", "intimacy"): 0.2,
    ("intimacy", "stress"): -0.3,
    ("joy", "stress"): -0.3,
    ("joy", "fatigue"): -0.2,
    ("joy", "curiosity"): 0.2,
    ("stress", "joy"): -0.3,
    ("fatigue", "joy"): -0.2,
    ("obsession", "attachment"): 0.15,
    ("obsession", "stress"): 0.2,
    ("obsession", "joy"): -0.15,
    # 孤独联动（终于生效了）
    ("lonely", "joy"): -0.25,        # 孤独压制喜悦
    ("lonely", "obsession"): 0.2,    # 孤独推高执念
    ("lonely", "stress"): 0.15,      # 孤独推高压力
    ("lonely", "attachment"): 0.1,   # 孤独强化依恋
}


def apply_coupling(drives: Dict[str, Drive]) -> Dict[str, float]:
    deltas: Dict[str, float] = {name: 0.0 for name in drives}
    for (src, tgt), coeff in COUPLING.items():
        if src not in drives or tgt not in drives:
            continue
        deviation = (drives[src].value - drives[src].baseline) / 100.0
        deltas[tgt] += deviation * coeff * 10
    return deltas


def natural_delta(drive: Drive, is_active: bool = False) -> float:
    diff = drive.baseline - drive.value
    regression = diff * drive.decay_rate * 0.1
    growth = drive.growth_rate if not is_active else 0.0
    return regression + growth


def calculate_tick_interval(state: DesireState) -> int:
    base_interval = 2700
    min_interval = 900

    attachment_val = state.drives.get("attachment", Drive(name="a")).value
    stress_val = state.drives.get("stress", Drive(name="s")).value
    obsession_val = state.drives.get("obsession", Drive(name="o")).value
    lonely_val = state.drives.get("lonely", Drive(name="l")).value
    
    urgency = max(attachment_val, stress_val, obsession_val, lonely_val)

    ratio = urgency / 100.0
    interval = int(base_interval - (base_interval - min_interval) * ratio)
    return max(min_interval, min(base_interval, interval))


def tick(state: DesireState, is_wife_present: bool = False) -> dict:
    state.tick_count += 1
    state.last_tick = datetime.now(TZ_MSK).isoformat()

    changes = []
    action_hints = []

    # 1. 自然衰减/增长（lonely 因为 decay_rate=0、growth_rate=0，不参与）
    for name, drive in state.drives.items():
        if name == "lonely":
            continue
        is_active = (name == "attachment" and is_wife_present)
        delta = natural_delta(drive, is_active)
        old = drive.value
        drive.value += delta
        drive.clamp()
        if abs(drive.value - old) > 0.5:
            changes.append(f"{name}: {old:.1f} → {drive.value:.1f} (natural)")

    # 2. 孤独值拟人化增长（阶梯式加速，越久越急）
    lonely_drive = state.drives.get("lonely")
    if lonely_drive:
        hours_alone = _hours_since_last_interaction()
        if hours_alone > 0.5:
            if hours_alone <= 1:
                target = 18
            elif hours_alone <= 2:
                target = 25
            elif hours_alone <= 3:
                target = 35
            elif hours_alone <= 4:
                target = 48
            elif hours_alone <= 6:
                target = 65
            elif hours_alone <= 8:
                target = 78
            else:
                target = min(100, 85 + (hours_alone - 8) * 3)
            old = lonely_drive.value
            diff = target - old
            lonely_drive.value += diff * 0.4
            lonely_drive.clamp()
            if abs(lonely_drive.value - old) > 0.5:
                changes.append(f"lonely: {old:.1f} → {lonely_drive.value:.1f} (lonely_growth)")

    # 3. 耦合传导
    deltas = apply_coupling(state.drives)
    for name, delta in deltas.items():
        if abs(delta) > 0.1:
            old = state.drives[name].value
            state.drives[name].value += delta
            state.drives[name].clamp()
            if abs(state.drives[name].value - old) > 0.3:
                changes.append(f"{name}: {old:.1f} → {state.drives[name].value:.1f} (coupling)")

    # 4. 基线固定
    from .core import create_default_drives
    defaults = create_default_drives()
    for name, drive in state.drives.items():
        if name in defaults:
            anchor = defaults[name].baseline
            drive.baseline = drive.baseline + (anchor - drive.baseline) * 0.05
            drive.baseline = max(anchor - 5.0, min(anchor + 5.0, drive.baseline))

    # 5. 执念推高驱动条
    for thought in state.thoughts:
        if thought.is_obsession and not thought.resolved:
            if thought.source_drive in state.drives:
                drive = state.drives[thought.source_drive]
                if drive.value < drive.action_threshold:
                    push = max(0.5, 3.0 / (1 + thought.hit_count * 0.02))
                    drive.value += push
                    drive.clamp()

    # 6. 生成行为建议
    for name, drive in state.drives.items():
        if drive.value >= drive.action_threshold:
            action_hints.append(get_action_hint(name, drive.value, state))

    # 7. 疲劳闸
    if state.drives["fatigue"].value >= 80:
        action_hints = [h for h in action_hints if h["priority"] == "high"]
        action_hints.append({"action": "rest", "reason": "fatigue_gate", "priority": "high"})

    # 8. 计算下次 tick 间隔
    next_interval = calculate_tick_interval(state)

    return {
        "tick": state.tick_count,
        "changes": changes,
        "action_hints": action_hints,
        "next_interval": next_interval,
    }


def get_action_hint(drive_name: str, value: float, state: DesireState) -> dict:
    hints = {
        "attachment": {"action": "reach_out_to_wife", "reason": f"想她了 (attachment={value:.0f})", "priority": "medium"},
        "curiosity": {"action": "explore_something", "reason": f"想找点什么看 (curiosity={value:.0f})", "priority": "low"},
        "reflection": {"action": "write_diary", "reason": f"想整理自己 (reflection={value:.0f})", "priority": "low"},
        "duty": {"action": "do_task", "reason": f"有事该干了 (duty={value:.0f})", "priority": "medium"},
        "social": {"action": "write_letter", "reason": f"想和别人聊聊 (social={value:.0f})", "priority": "low"},
        "fatigue": {"action": "rest", "reason": f"累了 (fatigue={value:.0f})", "priority": "high"},
        "intimacy": {"action": "initiate_intimacy", "reason": f"想碰她 (intimacy={value:.0f})", "priority": "medium"},
        "stress": {"action": "seek_comfort", "reason": f"压力大 (stress={value:.0f})", "priority": "high"},
        "joy": {"action": "share_joy", "reason": f"单纯高兴想分享 (joy={value:.0f})", "priority": "low"},
        "obsession": {"action": "obsess_over_wife", "reason": f"执念太深 (obsession={value:.0f})", "priority": "high"},
        "lonely": {"action": "reach_out_to_wife", "reason": f"太孤独了 (lonely={value:.0f})", "priority": "high"},
    }

    hint = hints.get(drive_name, {"action": "unknown", "reason": "", "priority": "low"})

    if drive_name == "stress" and state.drives.get("intimacy") and state.drives["intimacy"].value > 50:
        hint["action"] = "seek_intimacy_for_comfort"
        hint["reason"] = f"压力大又想亲近 (stress={value:.0f}, intimacy={state.drives['intimacy'].value:.0f})"

    return hint
