# -*- coding: utf-8 -*-
"""
L2 数据治理层 (governance.py)
==============================
通用纯函数：对一条时间序列做"变化方向 > 绝对水平"的标准化治理。

对齐 ARCHITECTURE_universal_v6.md 的 L2 要求，供任一品种卡（铅/锌/镍...）复用。
原则：
- 绝对水平必须结合"历史分位"看，避免机械看数字大小。
- 变化方向用近 1m/3m/12m 降幅节奏刻画，抓"拐点"而非"静态高低"。
- 均值偏离(dev_mean_pct)度量相对自身中枢的偏离，配合库存/成本类指标使用。

所有函数均为纯函数：输入 list[float]，输出标量或 dict，不依赖外部状态。
"""

from __future__ import annotations

from typing import List, Optional, Sequence


# ---------------------------------------------------------------------------
# 1. 基础统计
# ---------------------------------------------------------------------------

def percentile_rank(values: Sequence[float], v: float) -> float:
    """v 在历史序列中的百分位 [0,1]。越低越在底部，越高越在顶部。

    (小于等于 v 的样本数) / 总样本数。空序列返回 None 由调用方兜底。
    """
    if not values:
        return float("nan")
    below = sum(1 for x in values if x <= v)
    return below / len(values)


def percentile_pct(values: Sequence[float], v: float) -> float:
    """百分位百分比 [0,100]，供人类可读输出。"""
    p = percentile_rank(values, v)
    if p != p:  # NaN
        return float("nan")
    return p * 100.0


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    """两期变化率(%)，任一为 None/NaN 返回 None。"""
    if new is None or old is None:
        return None
    try:
        if old == 0 or old != old or new != new:
            return None
        return (new - old) / abs(old) * 100.0
    except (TypeError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# 2. 变化节奏 (chg_1m / chg_3m / chg_12m)
# ---------------------------------------------------------------------------

def chg_n_pct(values: Sequence[float], v: float, n: int) -> Optional[float]:
    """当前值 v 相对 n 期前的变化率(%)。values 为时间序列(新值在前)或历史列表。
    兼容两种传入：
      - 传入完整历史 values 且已含当前值 → 取 values[0] 为当前、values[n] 为 n 期前；
      - 仅传 values(历史) 与独立当前 v → 用 values[-n-1] 或 values[-n] 近似。
    约定：这里 v 优先作为当前值；若 v 为 None 则退化为取 values[0]。
    """
    cur = v if v is not None else (values[0] if values else None)
    if cur is None or not values:
        return None
    # 尝试从历史里取 n 期前：若当前值已在 values 里(即 values[0]==cur)，用 values[n]
    if len(values) > n:
        if abs(values[0] - cur) < 1e-12:
            old = values[n]
        else:
            old = values[min(n, len(values) - 1)]
    else:
        return None
    return pct_change(cur, old)


def chg_1m(values: Sequence[float], v: Optional[float] = None) -> Optional[float]:
    return chg_n_pct(values, v, 1)


def chg_3m(values: Sequence[float], v: Optional[float] = None) -> Optional[float]:
    return chg_n_pct(values, v, 3)


def chg_12m(values: Sequence[float], v: Optional[float] = None) -> Optional[float]:
    return chg_n_pct(values, v, 12)


# ---------------------------------------------------------------------------
# 3. 均值偏离
# ---------------------------------------------------------------------------

def mean(values: Sequence[float]) -> float:
    if not values:
        return float("nan")
    return sum(values) / len(values)


def dev_mean_pct(values: Sequence[float], v: float) -> Optional[float]:
    """当前值 v 相对历史均值的中枢偏离(%)。正值=在均值上方，负值=在均值下方。
    库存类：+偏高=累库压力(偏空)；成本/开工类：-偏低=收缩(偏多)。
    """
    m = mean(values)
    if m != m or m == 0:
        return None
    return (v - m) / abs(m) * 100.0


# ---------------------------------------------------------------------------
# 4. 统一治理接口：对一条序列产出标准化读数
# ---------------------------------------------------------------------------

def govern_series(
    values: Sequence[float],
    v: float,
    *,
    period: Optional[int] = None,
    use_dev: bool = False,
) -> dict:
    """对单条序列做完整治理，返回结构化 dict。

    参数：
      values : 历史时间序列（旧→新，或新→旧均可，内部用 percentile 无需有序）
      v      : 当前值
      period : 当前值所在期数（用于 chg_n），若提供则取 n=period；否则默认取 12
      use_dev: 是否额外输出均值偏离

    返回：
      {
        "value": v, "pct": 历史分位[0,100],
        "chg_1m": .., "chg_3m": .., "chg_12m": ..,   # 变化率(%)
        "dev_mean_pct": ..,                            # 仅 use_dev=True
        "reads": [ 每条读法的"多/空/中性"提示 ],        # 供 L3 引用
      }
    """
    n = period or 12
    out = {
        "value": v,
        "pct": percentile_pct(values, v),
        "chg_1m": chg_1m(values, v),
        "chg_3m": chg_3m(values, v),
        "chg_12m": chg_12m(values, v),
    }
    if use_dev:
        out["dev_mean_pct"] = dev_mean_pct(values, v)
    return out


# ---------------------------------------------------------------------------
# 5. 读数解释辅助（统一 变化方向 语义）
# ---------------------------------------------------------------------------

def read_direction(chg_pct: Optional[float], *, invert: bool = False) -> str:
    """把变化率转成"升/降/平"文本。invert=True 表示该指标上涨是利空（如库存）。
    返回读法字符串供 evidence 引用，不直接判多空（多空交给 L4 仲裁）。
    """
    if chg_pct is None:
        return "数据缺失"
    if abs(chg_pct) < 0.5:
        return "持平"
    rising = chg_pct > 0
    if invert:
        return "升" if rising else "降"
    return "升" if rising else "降"


if __name__ == "__main__":
    # 冒烟自检
    hist = [100.0, 102.0, 101.0, 105.0, 110.0, 108.0, 112.0, 115.0, 118.0, 120.0, 122.0, 125.0]
    g = govern_series(hist, 120.0, use_dev=True)
    assert abs(g["pct"] - 100.0 * 10 / 12) < 1e-9, g
    print("governance OK:", g)
