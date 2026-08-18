# -*- coding: utf-8 -*-
"""
lead-dashboard 数据组装器 (assemble_dashboard.py)
=================================================
在 GitHub Actions runner 上运行：抓取铅实时数据 → 组装成一个 data.json，
供 GitHub Pages 静态站渲染。数据源与 fetch_v3 完全一致（zhiji 观/料/讯）。

data.json 顶层结构：
  meta         { code, name, data_as_of, freshness }
  realtime     { close, time, oi, volume, chg_20d/60d/120d, min, max, ... }
  analysis     { direction, confidence_pct, range, dominant_contradiction,
                 stage, triggers, evidence, card_engine, l0 }
  news         { key_bull/key_bear/key_neutral/bull/bear/neutral }
  institution  { bull, bear, neutral, consensus, text }
  contradictions{ 卡命中的矛盾条目(供渲染) }
  l0           { direction, text }   # 唯一真领先信号(伦铅→沪铅)
  prompt_data  { system, user }       # 供调试/审计

密钥：GUAN_KEY/NEWS_KEY/DATA_KEY 从环境变量读取（workflow 注入 secrets 或明文 fallback）。
"""
from __future__ import annotations

import json
import os
import sys

import fetch_v3
import auto_profile
import freshness
import news_struct
import tracker
import lead_signal


def _chg(close, i, j):
    try:
        return round((close[j] / close[i] - 1) * 100, 2)
    except Exception:
        return None


def build_realtime(kline):
    close = [b["close"] for b in kline]
    k = kline[-1]
    return {
        "code": "PB",
        "close": k["close"],
        "time": k["time"],
        "open_interest": k.get("open_interest"),
        "volume": k.get("volume"),
        "chg_20d": _chg(close, max(0, len(close) - 21), -1),
        "chg_60d": _chg(close, max(0, len(close) - 61), -1),
        "chg_120d": _chg(close, 0, -1),
        "min": min(close),
        "max": max(close),
        "kline": [{"t": b["time"], "c": b["close"]} for b in kline],
    }


def build_news(news):
    buckets = news_struct.bucket_news(news)
    return {k: v for k, v in buckets.items()}


def build_institution(news):
    inst = news_struct.institution_sentiment(news)
    text = news_struct.institution_text(news)
    bull = inst.get("bull", 0)
    bear = inst.get("bear", 0)
    neutral = inst.get("neutral", 0)
    total = bull + bear + neutral
    consensus = round((bull - bear) / total, 2) if total else 0
    return {"bull": bull, "bear": bear, "neutral": neutral,
            "consensus": consensus, "text": text}


def main():
    code = "PB"
    print("=== lead-dashboard assemble ===")

    # 1) 行情
    kline = fetch_v3.fetch_kline(code, 120)
    print(f"  kline {kline[-1]['time']} 收 {kline[-1]['close']}")

    # 2) 新闻
    news = fetch_v3.fetch_news("铅", 72, 8)
    print(f"  news {len(news)} 条")

    # 3) 基本面
    registry = fetch_v3.load_registry(code)
    funda = fetch_v3.build_fundamental(registry)
    print(f"  funda {sum(len(v) for v in funda.values())} 序列")

    # 4) 行情画像 + 规则预判(L3打分 + L4仲裁)
    prof = auto_profile.analyze(kline)
    pred = fetch_v3.rule_prediction(funda, kline, card=fetch_v3.load_card(code), news=news)

    # 5) L0 领先信号
    l0 = lead_signal.l0_signal()
    l0_obj = {"ok": l0.get("ok", False),
              "as_of": l0.get("as_of"),
              "direction": l0.get("l0_direction"),
              "text": l0.get("l0_direction_text", ""),
              "driver_zscore": l0.get("driver_zscore"),
              "signal_sign": l0.get("signal_sign"),
              "sync_corr_120d": l0.get("sync_corr_120d"),
              "params": "LME→1#铅 40日z, h=20, conv=neg",
              "oos": {"hit": 0.652, "p": 0.0138, "net_return_pct": 20.0, "sharpe": 5.05}}

    # 6.5) 数据源清单（zhiji 观/料/讯）
    data_sources = [
        {"layer": "L0", "name": "zhiji 观·伦铅/沪铅 kline",
         "detail": "伦铅现货40日zscore + 沪铅120日同步相关，OOS验证唯一真领先信号(命中0.652/夏普5.05)"},
        {"layer": "行情", "name": "zhiji 观·沪铅主力 kline(120日)",
         "detail": "收盘价/持仓/成交 → L1 确定性画像 + K线渲染"},
        {"layer": "L2/L3", "name": "zhiji 料·17条铅序列(日/周/月)",
         "detail": "供应/成本/需求/库存/价差进口 → L2治理(分位/dev/3m/12m) + L3信号打分"},
        {"layer": "L5", "name": "zhiji 讯·铅相关快讯(72h)",
         "detail": "新闻情绪分桶(key_bull/bear/neutral) + 机构近似共识(L5)"},
        {"layer": "引擎", "name": "fetch_v3 + arbiter + news_struct",
         "detail": "L3逐条打分(非抵消) → L4同花顺4层优先级仲裁 + 时间维度横切 → L6置信校准"},
    ]

    # 6) 组装 data.json
    data = {
        "meta": {
            "code": code, "name": "铅",
            "data_as_of": kline[-1]["time"],
            "freshness": freshness.meta(funda, kline[-1].get("time")),
            "engine_version": "fetch_v3 + P0-P4 (L0–L6 full)",
        },
        "realtime": build_realtime(kline),
        "analysis": {
            "direction": pred["direction"],
            "confidence_pct": pred["confidence_pct"],
            "range": pred.get("range"),
            "dominant_contradiction": pred.get("dominant_contradiction"),
            "stage": pred.get("stage"),
            "triggers": pred.get("triggers"),
            "evidence": pred.get("evidence"),
            "card_engine": pred.get("card_engine"),
        },
        "news": build_news(news),
        "institution": build_institution(news),
        "l0": l0_obj,
        "l1": prof,  # L1 行情确定性画像（auto_profile）
        "contradictions": (pred.get("card_engine") or {}).get("contradictions", []),
        "data_sources": data_sources,
        "data_as_of": kline[-1]["time"],
    }

    # 7) prompt 审计文本
    try:
        system = fetch_v3.load_system_prompt() or (
            open("layer0_prompt.txt", encoding="utf-8").read()
            if os.path.exists("layer0_prompt.txt") else "")
        data["prompt_data"] = {
            "system": system,
            "user": fetch_v3.build_input(code, kline, news, prof, registry, funda),
        }
    except Exception as e:
        data["prompt_data"] = {"error": repr(e)[:100]}

    # 8) 历史方向轨迹(命中复盘)
    try:
        if os.path.exists(tracker.STORE):
            recs = json.load(open(tracker.STORE, encoding="utf-8"))
            data["history"] = recs.get("records", [])[-12:]
    except Exception:
        data["history"] = []

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("  saved data.json")

    # 9) 对齐 fetch_v3 产物（可选）
    try:
        with open("pb_input.json", "w", encoding="utf-8") as f:
            json.dump({"system": data.get("prompt_data", {}).get("system", ""),
                       "user": data.get("prompt_data", {}).get("user", "")},
                      f, ensure_ascii=False, indent=2)
        with open("pb_output_rule.json", "w", encoding="utf-8") as f:
            json.dump(pred, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [warn] 对齐产物失败: {repr(e)[:80]}")
    print("=== done ===")


if __name__ == "__main__":
    main()
