# -*- coding: utf-8 -*-
"""Layer2 auto_profile — 零品种逻辑确定性统计。
对行情/序列算 波动率 / z值 / 趋势强度 / (多序列时)相关性·领先滞后，
排序取 TopN = 「当期关键指标」。纯统计，对所有品种一视同仁，不写任何品种特定知识。
行情 regime 变 -> 排名自然变 -> 模型读新排名。
"""
import json, statistics


def _returns(vals):
    return [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals)) if vals[i - 1]]


def _ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def profile(values, name, stype):
    if not values or len(values) < 5:
        return None
    rets = _returns(values)
    vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    mean = statistics.mean(values)
    sd = statistics.pstdev(values) if len(values) > 1 else 0.0
    z = (values[-1] - mean) / sd if sd else 0.0
    ma20, ma60 = _ma(values, 20), _ma(values, 60)
    trend = (ma20 / ma60 - 1) * 100 if (ma20 and ma60) else None
    sig = ("偏多" if (z > 0 and (trend is None or trend > 0))
           else "偏空" if (z < 0 and (trend is None or trend < 0))
           else "中性")
    strength = abs(z) * 0.6 + (abs(trend) if trend is not None else 0) * 0.4
    return {"name": name, "type": stype, "last": round(values[-1], 2),
            "z_score": round(z, 2), "trend_pct": round(trend, 2) if trend is not None else None,
            "vol_daily_pct": round(vol * 100, 3), "signal": sig, "strength": round(strength, 2)}


def analyze(kline_bars, extra=None):
    """kline_bars: Guan bars(list of dict)。extra: {label:[values]} 料序列(待ID)。
    返回 {key_indicators:[TopN], all:[...]}。"""
    close = [b["close"] for b in kline_bars if b.get("close") is not None]
    oi = [b.get("open_interest") for b in kline_bars if b.get("open_interest") is not None]
    vol = [b.get("volume") for b in kline_bars if b.get("volume") is not None]
    profs = []
    if close:
        profs.append(profile(close, "价格(收)", "price"))
    if oi:
        profs.append(profile(oi, "持仓", "positioning"))
    if vol:
        profs.append(profile(vol, "成交量", "volume"))
    if extra:
        for lbl, vals in extra.items():
            profs.append(profile(vals, lbl, "series"))
    profs = [p for p in profs if p]
    profs.sort(key=lambda p: p["strength"], reverse=True)
    return {"key_indicators": profs[:8], "all": profs}


if __name__ == "__main__":
    try:
        with open("_lead_kline_120d.json", encoding="utf-8") as f:
            s = json.load(f)
        bars = [{"close": x["close"], "open_interest": x["oi"], "volume": x["volume"]} for x in s]
        print(json.dumps(analyze(bars), ensure_ascii=False, indent=2))
    except Exception as e:
        print("selftest skip:", e)
