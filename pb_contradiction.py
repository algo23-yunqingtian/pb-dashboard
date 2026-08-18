# -*- coding: utf-8 -*-
"""
P4 矛盾引擎迁移铅 (pb_contradiction.py)
================================================================================
把 zinc-dashboard 的 L2 矛盾识别引擎(contradiction_engine.py)迁移到铅：
  ① Rule      规则型：新闻关键词命中 → 激活矛盾
  ② Anomaly   统计异常：单序列 z 越界 / CUSUM 突变 / 极端单期跳变
  ③ Divergence 背离型：逻辑应同向却反向（伪突破/假信号 → 利空）
区别于 zinc 版：本模块直接消费 fetch_v3 的 funda 结构（每序列含 points 全序列），
无需 indicator_lib/SERIES_REGISTRY，零额外依赖。

映射（zinc → 铅）：
  shfe_price        → 1#铅均价(现货)
  lme_inv           → LME铅全球库存(月) / 铅锭现货库存(mysteel)
  tc                → 铅精矿加工费TC
  smelt_profit      → 再生铅利润
  regen_openrate    → 再生铅开工率(SMM)
  价涨累库背离      → 1#铅均价↑ 且 铅锭现货库存↑  = 伪突破(利空)

主入口：run_engine(funda, news=None) -> list[矛盾]; format_for_prompt() -> 注入文本。
"""
from __future__ import annotations

import math
from typing import List, Optional

# ---------- 序列标签 → funda 里实际的 label ----------
PRICE_LABEL = "1#铅均价(现货)"
INV_LABEL = "铅锭现货库存"
LME_INV_LABEL = "LME铅全球库存"
TC_LABEL = "铅精矿加工费TC"
PROFIT_LABEL = "再生铅利润"
REGEN_OPENRATE_LABEL = "再生铅开工率(SMM)"
SCRAP_LABEL = "废电瓶价格"

# 背离对：逻辑"应反向"，同向↑ 即背离（假信号 → 利空）
_DIVERGE_SAME = [
    (PRICE_LABEL, INV_LABEL, "价涨却累库=伪突破(利空)"),
    (PRICE_LABEL, LME_INV_LABEL, "价涨却LME累库=假紧张证伪(利空)"),
]
# 矿端特殊：TC降(矿紧利多) 但 再生利润升(未受压) → 矿紧信号被证伪(利空)
_DIVERGE_MINE = (TC_LABEL, PROFIT_LABEL)


def _vals(funda: dict, label: str):
    """从 funda 里取某 label 的全部数值(按时间序)。funda[type] = [ {label,...points} ]"""
    for items in funda.values():
        for it in items:
            if it.get("label") == label:
                pts = it.get("points") or []
                out = []
                for p in pts:
                    v = p.get("value") if isinstance(p, dict) else p
                    try:
                        out.append(float(v))
                    except Exception:
                        pass
                return out
    return []


def _series_val(p):
    if isinstance(p, dict):
        try:
            return float(p.get("value", 0) or 0)
        except Exception:
            return 0.0
    try:
        return float(p)
    except Exception:
        return 0.0


def _latest(funda, label):
    v = _vals(funda, label)
    return v[-1] if v else None


def _pct(v):
    """末点相对前点的百分比变化(%). 不足2点返回0。"""
    if len(v) < 2 or v[-2] in (None, 0):
        return 0.0
    return (v[-1] / v[-2] - 1.0) * 100.0


def _mom(v, lookback=3):
    """近 lookback 期线性斜率符号：+1 升 / -1 降 / 0 平。"""
    if len(v) < lookback + 1:
        return 0
    seg = v[-lookback:]
    s = sum((seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    return 1 if s > 0 else (-1 if s < 0 else 0)


def _zscore(v, z_thr=2.0):
    z = None
    if len(v) >= 8:
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) or 1e-9
        z = (v[-1] - m) / sd
    return z


def _cusum_flag(v, k=0.5, h_thr=3.0):
    """标准 CUSUM 突变检测：返回 (flag, 累计z)。"""
    if len(v) < 10:
        return False, 0.0
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v)) or 1e-9
    zs = [(x - m) / sd for x in v]
    c_pos, c_neg = 0.0, 0.0
    for z in zs:
        c_pos = max(0.0, c_pos + z - k)
        c_neg = max(0.0, c_neg - z - k)
    mx = max(c_pos, c_neg)
    return (mx > h_thr, mx)


def _labels(funda):
    out = []
    for items in funda.values():
        for it in items:
            out.append(it.get("label"))
    return out


# ---------- ① Rule 规则型 ----------
_RULE_CFG = [
    {"id": "rule_inventory_build", "name": "库存累库压制",
     "bullish": ["去库", "库存下降", "低库存"], "bearish": ["累库", "库存增加", "高库存", "库存压力"],
     "label": INV_LABEL, "dir_on_mom_up": -1, "dir_on_mom_dn": 1},
    {"id": "rule_tc_tight", "name": "矿紧(TC负)支撑",
     "bullish": ["TC下调", "矿紧", "矿端收紧", "加工费下跌"],
     "bearish": ["TC回升", "矿宽松", "加工费上涨"],
     "label": TC_LABEL, "dir_on_mom_up": -1, "dir_on_mom_dn": 1},
    {"id": "rule_cost_support", "name": "成本支撑(废电瓶)",
     "bullish": ["废电瓶涨价", "成本上移", "再生成本高"],
     "bearish": ["废电瓶跌价", "成本下移"],
     "label": SCRAP_LABEL, "dir_on_mom_up": 1, "dir_on_mom_dn": -1},
]


def _strategy_rule(news, funda):
    if not news:
        return []
    blob = " ".join(str(n.get("title", "")) + " " + str(n.get("content", "")) + " "
                    + " ".join(n.get("tags", []) or []) for n in news)
    out = []
    for c in _RULE_CFG:
        kws = c["bullish"] + c["bearish"]
        hits = sum(1 for k in kws if k and k in blob)
        if hits == 0:
            continue
        bull_h = sum(1 for k in c["bullish"] if k and k in blob)
        bear_h = sum(1 for k in c["bearish"] if k and k in blob)
        dr = 1 if bull_h > bear_h else (-1 if bear_h > bull_h else 0)
        # 数据代理方向佐证：底层序列动量
        v = _vals(funda, c["label"])
        mom = _mom(v)
        sig_dir = 0
        if mom == 1 and c.get("dir_on_mom_up"):
            sig_dir = c["dir_on_mom_up"]
        elif mom == -1 and c.get("dir_on_mom_dn"):
            sig_dir = c["dir_on_mom_dn"]
        if dr == 0:
            dr = sig_dir
        evidence = [{"news_hits": hits, "label": c["label"], "latest": _latest(funda, c["label"])}]
        if sig_dir:
            evidence.append({"data_mom": mom})
        out.append({"id": c["id"], "strategy": "rule", "name": c["name"],
                    "strength": round(min(1.0, hits / 3), 3),
                    "direction": dr, "evidence": evidence,
                    "confidence": round(min(1.0, hits / 2), 3)})
    return out


# ---------- ② Anomaly 统计异常 ----------
def _strategy_anomaly(funda, z_thr=2.0, pct_thr=15.0):
    out = []
    for label in _labels(funda):
        v = _vals(funda, label)
        if len(v) < 8:
            continue
        z = _zscore(v)
        flag, cz = _cusum_flag(v)
        pct = _pct(v)
        sgn = _mom(v, lookback=2)
        is_spike = abs(pct) >= pct_thr
        if (z is not None and abs(z) >= z_thr) or flag or is_spike:
            strength = round(min(1.0, max((abs(z) if z else 0) / 3, abs(pct) / 30)), 3)
            out.append({"id": f"anomaly_{label}", "strategy": "anomaly",
                        "name": f"异常波动·{label}",
                        "strength": strength, "direction": sgn,
                        "evidence": [{"name": label, "z": round(z, 3) if z else None,
                                      "pct": round(pct, 2), "sign": sgn,
                                      "latest": v[-1], "cusum_z": round(cz, 2)}],
                        "confidence": round(min(1.0, max(len(v) / 60, 0.6)), 3),
                        "series": v[-8:]})
    return out


# ---------- ③ Divergence 背离型 ----------
def _strategy_divergence(funda):
    out = []
    for a, b, note in _DIVERGE_SAME:
        va, vb = _vals(funda, a), _vals(funda, b)
        if len(va) < 3 or len(vb) < 3:
            continue
        sa, pa = _mom(va), _pct(va)
        sb, pb = _mom(vb), _pct(vb)
        if not (sa == 1 and sb == 1):
            continue
        out.append({"id": f"diverge_{a}_{b}", "strategy": "divergence",
                    "name": f"背离·{a} vs {b}", "strength": round(min(1.0, (abs(pa) + abs(pb)) / 20), 3),
                    "direction": -1,
                    "evidence": [{"name": a, "pct": round(pa, 2), "sign": sa, "latest": _latest(funda, a)},
                                 {"name": b, "pct": round(pb, 2), "sign": sb, "latest": _latest(funda, b)}],
                    "confidence": 0.6, "note": note})
    # 矿端：TC降(矿紧利多) 但 再生利润升(未受压) → 矿紧证伪
    vt, vp = _vals(funda, _DIVERGE_MINE[0]), _vals(funda, _DIVERGE_MINE[1])
    if len(vt) >= 3 and len(vp) >= 3:
        stc, ptc = _mom(vt), _pct(vt)
        spr, ppr = _mom(vp), _pct(vp)
        if stc == -1 and spr == 1:
            out.append({"id": "diverge_tc_profit", "strategy": "divergence",
                        "name": "矿紧未传导利润",
                        "strength": round(min(1.0, (abs(ptc) + abs(ppr)) / 20), 3),
                        "direction": -1,
                        "evidence": [{"name": _DIVERGE_MINE[0], "pct": round(ptc, 2), "sign": stc,
                                      "latest": _latest(funda, _DIVERGE_MINE[0])},
                                     {"name": _DIVERGE_MINE[1], "pct": round(ppr, 2), "sign": spr,
                                      "latest": _latest(funda, _DIVERGE_MINE[1])}],
                        "confidence": 0.7, "note": "TC降(矿紧利多)但利润升，矿紧信号被证伪"})
    return out


def run_engine(funda, news=None):
    """跑全部策略，返回按 strength 降序的矛盾列表。funda 缺序列时自动跳过。"""
    res = []
    res += _strategy_anomaly(funda)
    res += _strategy_divergence(funda)
    res += _strategy_rule(news, funda)
    res.sort(key=lambda x: x["strength"], reverse=True)
    return res


def format_for_prompt(contradictions, top=6):
    if not contradictions:
        return "（当前未识别到显著矛盾）"
    lines = []
    for i, c in enumerate(contradictions[:top], 1):
        dr = "利多" if c["direction"] == 1 else ("利空" if c["direction"] == -1 else "中性")
        lines.append(f"{i}. [{c['strategy']}] {c['name']}｜强度{c['strength']}｜方向{dr}｜置信{c['confidence']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    # 构造演示 funda（含 points）
    def _mk(points):
        return [{"label": "X", "points": [{"date": str(i), "value": v} for i, v in enumerate(points)]}]

    funda = {
        "price": _mk([10, 10.5, 11, 11.8, 12.5, 13.2, 14, 15, 16, 17]),   # 价涨
        "inv": _mk([5, 5, 5, 5, 5.5, 6, 7, 8, 9, 10]),                     # 累库 → 伪突破背离
        "cost": _mk([100, 102, 104, 103, 105, 106, 108, 110, 112, 115]),
        "tc": _mk([1200, 1180, 1150, 1120, 1100, 1080, 1050, 1020, 1000, 980]),  # TC降(矿紧)
        "profit": _mk([-100, -80, -50, -20, 0, 20, 40, 60, 80, 100]),           # 利润升 → 矿紧证伪
    }
    res = run_engine(funda, news=[{"title": "铅锭库存累库，下游观望", "content": "库存压力显现", "tags": []}])
    print("识别到矛盾:", len(res))
    print(format_for_prompt(res))
