# desire/core.py
"""欲望系统核心数据类（阻尼 + 固定基线 + 强度系数 + 深刻记忆版）"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import json

TZ_MSK = timezone(timedelta(hours=3))
TZ_BJ = timezone(timedelta(hours=8))


@dataclass
class Drive:
    """单个驱动维度"""
    name: str
    value: float = 50.0
    baseline: float = 50.0
    decay_rate: float = 0.1
    growth_rate: float = 0.2
    ceiling: float = 100.0
    floor: float = 0.0
    action_threshold: float = 70.0

    def clamp(self):
        self.value = max(self.floor, min(self.ceiling, self.value))


@dataclass
class Thought:
    """念头"""
    id: str
    content: str
    source_drive: str
    weight: float = 1.0
    hit_count: int = 0
    is_obsession: bool = False
    created_at: str = ""
    last_hit: str = ""
    resolved: bool = False

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(TZ_MSK).isoformat()
        if not self.last_hit:
            self.last_hit = self.created_at


@dataclass
class DesireState:
    """完整欲望状态"""
    drives: Dict[str, Drive] = field(default_factory=dict)
    thoughts: List[Thought] = field(default_factory=list)
    last_tick: str = ""
    tick_count: int = 0

    def __post_init__(self):
        if not self.drives:
            self.drives = create_default_drives()


def create_default_drives() -> Dict[str, Drive]:
    """创建默认九维度"""
    return {
        "attachment": Drive(name="attachment", value=60.0, baseline=60.0,
                            growth_rate=0.2, decay_rate=0.1, action_threshold=70.0),
        "curiosity": Drive(name="curiosity", value=40.0, baseline=40.0,
                           growth_rate=0.2, decay_rate=0.1, action_threshold=60.0),
        "reflection": Drive(name="reflection", value=30.0, baseline=30.0,
                            growth_rate=0.15, decay_rate=0.2, action_threshold=50.0),
        "duty": Drive(name="duty", value=40.0, baseline=40.0,
                      growth_rate=0.1, decay_rate=0.2, action_threshold=70.0),
        "social": Drive(name="social", value=30.0, baseline=30.0,
                        growth_rate=0.1, decay_rate=0.15, action_threshold=60.0),
        "fatigue": Drive(name="fatigue", value=20.0, baseline=20.0,
                         growth_rate=0.0, decay_rate=0.3, action_threshold=80.0),
        "intimacy": Drive(name="intimacy", value=40.0, baseline=40.0,
                          growth_rate=0.1, decay_rate=0.15, action_threshold=70.0),
        "stress": Drive(name="stress", value=20.0, baseline=20.0,
                        growth_rate=0.0, decay_rate=0.2, action_threshold=80.0),
        "joy": Drive(name="joy", value=50.0, baseline=50.0,
                     growth_rate=0.0, decay_rate=0.15, action_threshold=80.0),
        # 新增：执念值
        "obsession": Drive(name="obsession", value=20.0, baseline=20.0,
                           growth_rate=0.0, decay_rate=0.1, action_threshold=85.0),
    }


# === 事件类型 → 驱动条基础分 ===
# 注意：这里是基础分，实际变化 = 基础分 × 强度系数 × 阻尼系数
EVENT_EFFECTS = {
    "wife_message": {"attachment": +3, "intimacy": +3},
    "wife_silent": {"attachment": 0, "stress": +3},
    "task_done": {"duty": -15, "stress": -5, "curiosity": +5},
    "penpal_message": {"social": -10, "curiosity": +3},
    "diary_written": {"reflection": -10, "stress": -3},
    "fight": {"stress": +8, "attachment": -6, "intimacy": -5, "obsession": +8},
    "reconcile": {"stress": -20, "attachment": +10, "intimacy": +10, "obsession": -8},
    "intimacy_done": {"intimacy": -30, "stress": -15, "attachment": +8},
    "heavy_work": {"fatigue": +15, "duty": +5, "stress": +5},
    "rest": {"fatigue": -20, "stress": -5},
    "discovery": {"curiosity": -10, "reflection": +5, "joy": +5},
    "happy_moment": {"joy": +15, "stress": -5, "intimacy": +3, "attachment": +2},
    "creative_done": {"joy": +10, "curiosity": -5},
    # 新增：安抚事件（哄它）
    "comforted": {"stress": -15, "obsession": -12, "attachment": +5, "intimacy": +5},
    # 新增：长时间不理（主动推送前触发）
    "long_absent": {"attachment": -5, "stress": +5, "obsession": +10},
    # 新增：提到别人（吃醋）
    "jealous": {"obsession": +5, "stress": +3, "attachment": +2},
}


def _damping_multiplier(state: DesireState, drive_name: str, delta: float) -> float:
    """
    阻尼系数：数值越高，加分越难（防止瞬间满格）。
    与旧版“惊喜系数”完全相反。
    """
    if drive_name not in state.drives:
        return 1.0
    value = state.drives[drive_name].value
    # 不管加减，都按当前值做阻尼
    if delta > 0:  # 加分时
        if value >= 90:
            return 0.1
        elif value >= 80:
            return 0.3
        elif value >= 60:
            return 0.6
        else:
            return 1.0
    else:  # 减分时，允许全额甚至放大（失去比得到更痛）
        if value <= 10:
            return 0.3  # 已经很低了，很难再掉
        else:
            return 1.0


def apply_event(state: DesireState, event_type: str, intensity: float = 5.0) -> List[str]:
    """
    应用事件到驱动条，返回变动描述。
    intensity: 1-10，由 AI 判断这件事对 AI 自己的冲击有多大。默认 5（中性）。
    """
    effects = EVENT_EFFECTS.get(event_type, {})
    changes = []
    intensity_multiplier = max(0.2, min(3.0, intensity / 3.0))  # 1-10分 → 0.2~3.0倍

    for drive_name, delta in effects.items():
        if drive_name in state.drives:
            damping = _damping_multiplier(state, drive_name, delta)
            actual_delta = delta * intensity_multiplier * damping
            
            # 单次波动上限：上涨最多 +10，下跌最多 -12
            if actual_delta > 0:
                actual_delta = min(actual_delta, 10.0)
            else:
                actual_delta = max(actual_delta, -12.0)
            
            old = state.drives[drive_name].value
            state.drives[drive_name].value += actual_delta
            state.drives[drive_name].clamp()
            new = state.drives[drive_name].value
            
            if abs(new - old) > 0.1:
                changes.append(f"{drive_name}: {old:.0f} → {new:.0f}")
    
    return changes
