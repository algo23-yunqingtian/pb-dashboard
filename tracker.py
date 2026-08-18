# -*- coding: utf-8 -*-
"""§0.6 复盘/命中率模块（独立存储每次预判 -> 回测胜率）。零品种逻辑。
记录每条 §2 JSON 预判；事后用实际区间涨跌幅回测方向命中，统计胜率。
"""
import json, os
from datetime import datetime

STORE = "predictions.json"


def record(pred, code="PB"):
    rec = {"code": code, "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "direction": pred.get("direction"), "confidence_pct": pred.get("confidence_pct"),
           "range": pred.get("range"), "dominant_contradiction": pred.get("dominant_contradiction"),
           "evidence": pred.get("evidence"), "hit": None, "actual_chg_pct": None}
    data = []
    if os.path.exists(STORE):
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
    data.append(rec)
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return len(data) - 1  # 返回刚插入的索引，供 evaluate 使用


def evaluate(index, actual_chg_pct):
    if not os.path.exists(STORE):
        return
    data = json.load(open(STORE, encoding="utf-8"))
    if index < 0 or index >= len(data):
        return
    r, d = data[index], data[index].get("direction")
    hit = (actual_chg_pct > 0) if d == "偏多" else (actual_chg_pct < 0) if d == "偏空" else None
    r["actual_chg_pct"] = actual_chg_pct
    r["hit"] = bool(hit) if hit is not None else None
    json.dump(data, open(STORE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def summary():
    if not os.path.exists(STORE):
        return "无记录"
    data = json.load(open(STORE, encoding="utf-8"))
    n = len(data)
    hits = sum(1 for r in data if r.get("hit") is True)
    pending = sum(1 for r in data if r.get("hit") is None)
    scored = n - pending
    return {"total": n, "hit": hits, "pending": pending,
            "win_rate": round(hits / scored, 3) if scored else None}
