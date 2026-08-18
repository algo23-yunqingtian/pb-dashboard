# -*- coding: utf-8 -*-
"""
P4 L0 注入卡：伦铅(LME铅现货) → 沪铅(1#铅现货) 领先信号 (lead_signal.py)
================================================================================
这是经 OOS 严格验证的**唯一真领先 SIGNAL**（causal_card_pb.json VALIDATED/Stable）：
  driver = LME铅现货价格  →  target = 1#铅均价(现货)
  mode='lvl'(水平zscore), W=40(滚动40日), h=20(约20日领先), L=5(滞后5日)
  conv=neg：LME 相对其 40 日均线越高(zscore高) → 沪铅 20 日后越倾向回落（均值回归/反向预示）
  同期 corr=+0.74(共涨)，但领先方向相反 = "近期伦铅走强 → 沪铅短期见顶回落"的均值回归信号。

OOS 实证：test_hit=0.652, 二项 p=0.0138, OOS 净收益+20%, 夏普 5.05。
用法：
  signal_text = lead_signal.l0_text(history_path)  # -> 注入 prompt 的 L0 段
  out = lead_signal.l0_signal(history_path)         # -> 结构化 dict
历史源默认用 pb_history.json(含 LME/1# 现货日线全序列)。
"""
from __future__ import annotations

import json
import math

import os
import sys
# L0 历史数据源解析顺序：环境变量 PB_HISTORY > 仓库内 pb_history.json > 本机绝对路径(开发用)
_LOCAL_HIST = r"C:\Users\YAQH\CodeBuddy\20260817192636\pb_history.json"
_REPO_HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pb_history.json")
HIST = os.environ.get("PB_HISTORY") or (_REPO_HIST if os.path.exists(_REPO_HIST) else _LOCAL_HIST)

# 已验证参数（causal_card_pb.json, VALIDATED, Stable）
W, H, L = 40, 20, 5
OOS_HIT, OOS_P = 0.652, 0.0138
DRIVER_LABEL = "LME铅现货价格"
TARGET_LABEL = "1#铅均价(现货)"


def _series_map(points):
    """points: [{date,value}] -> {date: value}（升序去重）。"""
    m = {}
    for p in points:
        d = str(p.get("date"))[:10]
        v = p.get("value")
        if d and v is not None:
            try:
                m.setdefault(d, float(v))
            except Exception:
                pass
    return m


def zlevel(arr, i, win):
    """与 causal_backtest 一致的滚动 zscore（只用非 None 值，去均值/去波动）。"""
    vals = [arr[j] for j in range(max(0, i - win + 1), i + 1) if arr[j] is not None]
    if len(vals) < 4:
        return None
    m = sum(vals) / len(vals)
    sd = math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals)) or 1e-9
    if arr[i] is None:
        return None
    return (arr[i] - m) / sd


def l0_signal(history_path: str = HIST) -> dict:
    """计算当前 L0 领先信号。返回结构化 dict；数据不足时 verdict='无数据'。"""
    try:
        d = json.load(open(history_path, encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "verdict": f"读取历史失败: {repr(e)[:60]}"}
    series = d.get("series", {})
    drv = _series_map((series.get(DRIVER_LABEL) or {}).get("points", []))
    tgt = _series_map((series.get(TARGET_LABEL) or {}).get("points", []))
    if not drv or not tgt:
        return {"ok": False, "verdict": "历史缺 LME 铅现货或 1#铅现货 序列，无法计算 L0 信号"}
    # 对齐：用 LME 序列的时间轴；对每个 LME 日期，取滞后 L 天做信号，前瞻 h 天做目标
    dts = sorted(drv.keys())
    # 当前可用最新 LME 日（往前推 L 天，保证有目标序列的点可对比）
    last_sig_idx = None
    for i in range(len(dts) - 1, -1, -1):
        if i - L < 0:
            break
        zi = i - L
        z = zlevel([drv.get(dt) for dt in dts], zi, W)
        if z is not None:
            last_sig_idx = i
            break
    if last_sig_idx is None:
        return {"ok": False, "verdict": "LME 数据不足以计算 zscore"}
    dt_now = dts[last_sig_idx]
    # 当前信号
    sig_vals = [drv.get(dt) for dt in dts]
    z = zlevel(sig_vals, last_sig_idx - L, W)
    sig = 1 if z is not None and z > 0 else (-1 if z is not None and z < 0 else 0)
    # L0 方向 = 反向预示 (conv=neg)
    l0_dir = -sig if sig != 0 else 0
    l0_text_dir = {1: "偏多(伦铅超跌→沪铅20日后反弹)", -1: "偏空(伦铅超买→沪铅20日后回落)", 0: "中性(伦铅处于均值)"}
    # 目标序列当前水平（用于佐证）
    tgt_dts = sorted(tgt.keys())
    tgt_now = tgt.get(dt_now) or (tgt.get(tgt_dts[-1]) if tgt_dts else None)
    # 同期相关性佐证（近120交易日 LME vs 1#）
    corr = _corr_last(drv, tgt, 120)

    signal_out = {
        "ok": True,
        "as_of": dt_now,
        "driver_zscore": round(z, 2) if z is not None else None,
        "signal_sign": sig,
        "l0_direction": l0_dir,
        "l0_direction_text": l0_text_dir[l0_dir],
        "target_latest": tgt_now,
        "sync_corr_120d": corr,
        "params": {"mode": "lvl", "W": W, "h": H, "L": L, "conv": "neg"},
        "oos": {"hit": OOS_HIT, "p": OOS_P, "net_return_pct": 20.0, "sharpe": 5.05},
    }
    return signal_out


def l0_text(history_path: str = HIST) -> str:
    """生成注入 prompt 的 L0 段文本。"""
    s = l0_signal(history_path)
    if not s.get("ok"):
        return f"[L0 已验证领先信号] {s.get('verdict')}"
    lines = [
        "[L0 已验证领先信号(唯一OOS真领先)] 伦铅→沪铅 约20日领先(反向均值回归)",
        f"- 当前(数据截至{s['as_of']})：LME铅现货 40日zscore={s['driver_zscore']:+.2f}"
        f" → 对沪铅现货 L0 方向 = {s['l0_direction_text']}",
        f"- 同期120日相关 = {s['sync_corr_120d']:+.2f}（共涨，但领先方向反向=均值回归）",
        f"- OOS实证：命中{s['oos']['hit']:.3f} / 二项p={s['oos']['p']:.3f} / "
        f"净收益+{s['oos']['net_return_pct']}% / 夏普{s['oos']['sharpe']:.2f}",
        "- 注意：该信号是『统计领先』，20日尺度，不能当日内择时；与L4仲裁可叠加用（若L0偏多且仲裁偏多→强化）。",
    ]
    return "\n".join(lines)


def _corr_last(a: dict, b: dict, n: int):
    """近 n 个共同交易日 LME vs 1# 的 Pearson 相关。"""
    common = sorted(set(a) & set(b))
    common = common[-n:]
    xs, ys = [a[c] for c in common], [b[c] for c in common]
    if len(xs) < 5:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)) or 1e-9
    sy = math.sqrt(sum((y - my) ** 2 for y in ys)) or 1e-9
    return round(cov / (sx * sy), 2)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(l0_text())
