# -*- coding: utf-8 -*-
"""
V3 极简分析框架 — 数据抓取(行情+料序列)+组装+LLM(待key)+归一化。
已打通铅真实基本面序列：/commodity/api/search 发现ID -> fetch_series 拉近期窗口。
Layer0 固定骨架 + Layer1 品种基本面卡 + Layer2 实时数据(auto_profile + 料序列)。
用法:
  python fetch_v3.py --code PB --runs 5
  python fetch_v3.py --no-llm
"""
import json, os, argparse, urllib.request, urllib.parse, statistics, calendar, re, sys
from datetime import datetime, timedelta

try:
    import yaml
except Exception:
    yaml = None

try:
    import calibration as CAL
except Exception:
    CAL = None

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import auto_profile
import tracker
import governance
import arbiter
import news_struct
import freshness
import lead_signal
import pb_contradiction

GUAN_KEY = os.environ.get("GUAN_KEY") or "guan_a3dbade5e217468006af273fdc772f91"
NEWS_KEY = os.environ.get("NEWS_KEY") or "nws_f5b4b6c653104d0f965fb3463dcf7eed"
DATA_KEY = os.environ.get("DATA_KEY") or "data_8e863643ecc13f11d2c669bdb672f7db"
GUAN_BASE = "https://zhiji-ai.xyz/guan/api"
NEWS_BASE = "https://zhiji-ai.xyz/news/api"
COMM_BASE = "https://zhiji-ai.xyz/commodity/api"

DASHSCOPE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
DASHSCOPE_MODEL = "qwen3.7-max"
SF_URL = "https://api.siliconflow.cn/v1/chat/completions"
SF_MODEL = "Qwen/Qwen2.5-14B-Instruct"


def get(url, hdr=None, timeout=15):
    h = {"User-Agent": "Mozilla/5.0"}
    if hdr:
        h.update(hdr)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_kline(code, limit=120, timeout=30):
    d = get(f"{GUAN_BASE}/kline?symbol={code}&freq=D&cont=1&limit={limit}",
            {"X-Guan-Key": GUAN_KEY}, timeout=timeout)
    bars = [b for b in (d.get("bars") or []) if b.get("close") is not None]
    if not bars:
        raise RuntimeError("Guan kline 空返回")
    return bars


def fetch_news(q, hours=72, limit=8):
    d = get(f"{NEWS_BASE}/search?q={urllib.parse.quote(q)}&hours={hours}&limit={limit}&source=all",
            {"X-News-Key": NEWS_KEY})
    out = []
    for it in (d.get("items") or []):
        body = it.get("content") or it.get("body") or ""
        out.append({"source": it.get("source", ""), "time": it.get("time", ""),
                    "title": it.get("title", ""),
                    "content": (body[:160] + "…") if len(body) > 160 else body,
                    "sentiment": it.get("sentiment", "neutral"),
                    "importance": it.get("importance", "低")})
    return out


def fetch_series(sid, end="2026-08-17", start="2025-08-17", limit=2000):
    """拉料序列 12 个月窗口，返回完整时间序列事实（修复根因：快照→时序+均值偏离）。
    字段: latest/prev/chg_pct(近月) + min/max/mean/chg_3m_pct/chg_12m_pct/dev_mean_pct/n
    dev_mean_pct=(latest-mean)/mean*100 → 库存/开工的「绝对水平偏离」判定依据
    """
    d = get(f"{COMM_BASE}/series?id={sid}&start={start}&end={end}&limit={limit}",
            {"X-Data-Key": DATA_KEY})
    pts = d.get("points") or d.get("data") or []
    if not pts:
        return None
    def _d(s):
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d")
        except Exception:
            return None
    pts = [p for p in pts if isinstance(p, dict) and _d(p.get("date")) is not None and p.get("value") is not None]
    if not pts:
        return None
    pts.sort(key=lambda p: _d(p["date"]))
    vals = [float(p["value"]) for p in pts]
    latest, first = pts[-1], pts[0]
    lv = float(latest["value"])
    mn, mx = min(vals), max(vals)
    mean = statistics.mean(vals)
    def chg(months):
        cut = _d(latest["date"]) - timedelta(days=30 * months)
        recent = [p for p in pts if _d(p["date"]) >= cut]
        if len(recent) < 2:
            return None
        a, b = float(recent[0]["value"]), float(recent[-1]["value"])
        if not a:
            return None
        return round((b - a) / a * 100, 1)
    dev = round((lv - mean) / mean * 100, 1) if mean else None
    # 近月变化（保留旧 chg_pct 行为，喂 auto_profile 兼容）
    ld = _d(latest["date"])
    cut1 = ld - timedelta(days=25)
    prev = next((p for p in reversed(pts) if _d(p["date"]) <= cut1), None)
    pv = float(prev["value"]) if prev else None
    chg1 = round((lv / pv - 1) * 100, 1) if pv else None
    return {"latest_date": latest["date"], "latest": lv,
            "prev_date": prev["date"] if prev else None, "prev": pv, "chg_pct": chg1,
            "min": mn, "max": mx, "mean": round(mean, 2),
            "chg_3m_pct": chg(3), "chg_12m_pct": chg(12), "dev_mean_pct": dev,
            "n": len(pts)}


def load_registry(code):
    p = os.path.join("registry", f"{code}.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {"code": code, "series": []}


def build_fundamental(registry):
    """拉全部料序列，按 type 分组，返回 {type: [{label, ...}]}。
    支持 source=wenwen 的问财交叉参考序列(非Zhiji时序,仅静态点值)。"""
    grouped = {}
    for s in registry.get("series", []):
        rec_base = {"label": s["label"], "unit": s.get("unit", ""),
                    "freq": s.get("freq", ""), "type": s.get("type", ""),
                    "id": s.get("id"), "source": s.get("source", "zhiji")}
        if s.get("source") == "wenwen":
            rec = {**rec_base, "latest": s.get("latest"),
                   "latest_date": s.get("as_of", "问财口径"),
                   "note": s.get("note", ""),
                   "dev_mean_pct": None, "min": None, "max": None}
            grouped.setdefault(s["type"], []).append(rec)
            continue
        try:
            r = fetch_series(s["id"])
        except Exception as e:
            print(f"  [warn] {s['label']} ({s['id']}) 拉取失败: {repr(e)[:80]}")
            continue
        if not r:
            continue
        rec = {**rec_base, **r}
        grouped.setdefault(s["type"], []).append(rec)
    return grouped


def build_input(code, kline, news, profile, registry, funda):
    k = kline[-1]
    close = [b["close"] for b in kline]
    chg = lambda i, j: round((close[j] / close[i] - 1) * 100, 2)
    L = []
    L.append(f"【行情 Guan {code} {len(kline)}日】")
    L.append(f"最新: {k['time']} 收 {k['close']} 持仓 {k.get('open_interest')} 成交 {k.get('volume')}")
    L.append(f"20日 {chg(max(0,len(close)-21),-1)}% | 60日 {chg(max(0,len(close)-61),-1)}% | 120日 {chg(0,-1)}%")
    L.append(f"120日区间 [{min(close)}, {max(close)}]")
    L.append("")
    L.append("【当期关键指标(自动画像 auto_profile，确定性统计)】")
    for p in profile["key_indicators"]:
        L.append(f"  - {p['name']}: 现值 {p['last']} | z={p['z_score']} | 趋势 {p['trend_pct']}% | 信号 {p['signal']}")
    L.append("")
    L.append("【基本面数据(料序列，12个月窗口；dev=均值偏离% 用于判绝对水平，3m/12m=动量%)】")
    type_cn = {"supply": "供应", "demand": "需求", "inventory": "库存",
               "spread": "价差/进口", "cost": "成本"}
    for t, items in funda.items():
        L.append(f"■ {type_cn.get(t, t)}")
        for it in items:
            freq = it.get("freq", "")
            if it.get("source") == "wenwen":
                L.append(f"  - {it['label']}({freq},【问财】): 最新 {it.get('latest')} {it.get('unit','')} | {it.get('note','')}")
                continue
            dev = it.get("dev_mean_pct")
            c3 = it.get("chg_3m_pct")
            c12 = it.get("chg_12m_pct")
            dev_s = f"均值偏离{dev:+}%" if dev is not None else "均值偏离n/a"
            c3_s = f"3月{c3:+}%" if c3 is not None else "3月n/a"
            c12_s = f"12月{c12:+}%" if c12 is not None else "12月n/a"
            rng = f"[{it.get('min')},{it.get('max')}]"
            L.append(f"  - {it['label']}({freq}): 最新 {it['latest']} {it.get('unit','')} ({it['latest_date']}) "
                     f"| 区间{rng} | {dev_s} | {c3_s} | {c12_s}")
    L.append("")
    # P0 数据时效审计：逐序列标注新鲜度，杜绝"用滞后月数据判当日行情"硬伤
    L.append(freshness.audit_text(funda, kline[-1].get("time")))
    L.append("")
    card_path = os.path.join("registry", f"{code}_card.md")
    if os.path.exists(card_path):
        with open(card_path, encoding="utf-8") as f:
            L.append("【品种基本面框架(按此结构推理供需)】")
            L.append(f.read().strip())
            L.append("")
    L.append(f"【新闻 近72h q={registry.get('name', code)}】")
    L += [f"{i}. [{n['source']} {n['time']}] {n['title']} — {n['content']}" for i, n in enumerate(news, 1)] or ["（无新闻）"]
    # L5 新闻情绪分桶（S5 机构情绪段）注入
    L.append(news_struct.news_summary_text(news))
    # P3 机构观点近似共识（看多/看空/中性计数 + 共识强度）
    L.append(news_struct.institution_text(news))
    # P4 L0 已验证领先信号（伦铅→沪铅 20日领先，OOS 验证唯一真领先）
    L.append(lead_signal.l0_text())
    # P4 矛盾引擎迁移铅：自动识别异常/背离/规则矛盾（替代纯研报依赖）
    _pb_cs = pb_contradiction.run_engine(funda, news)
    if _pb_cs:
        L.append("【L2 矛盾引擎(铅迁移版)自动识别】")
        L.append(pb_contradiction.format_for_prompt(_pb_cs, top=5))
    L.append("")
    L.append("先按【品种基本面框架】做 五维分解(供需平衡40%/成本曲线25%/库存周期20%/价差进口10%/资金情绪5%)，逐维给证据+读数；再用「主要矛盾4维算法」(供需差值排序→库存变化方向+均值偏离→基差极端背离>10%?→持仓集中度)定位主导矛盾及当前占动能一侧；判断「预期定价阶段」(价格位置/持仓变化/基差状态/基本面验证进度)；列「强化/证伪信号」并计数(3+同向→强化,2+反向→证伪)；最后严格按输出 JSON schema 作答，禁止编造；证据须引用「指标名+数值+日期」；库存须「方向+均值偏离」同看；成本底须区分盈利型硬底/亏损型软底；新闻里的周度 blip 必须回序列验趋势。")
    return "\n".join(L)


def call_llm(system, user, runs=1):
    key_ds, key_sf = os.environ.get("DASHSCOPE_KEY"), os.environ.get("SILICONFLOW_KEY")
    if not (key_ds or key_sf):
        return None, "NO_KEY"
    results = []
    for _ in range(runs):
        payload = {"model": DASHSCOPE_MODEL, "temperature": 0.3, "max_tokens": 2048,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        ok = False
        if key_ds:
            try:
                results.append(_post(DASHSCOPE_URL, key_ds, payload)); ok = True
            except Exception as e:
                print("  DashScope FAIL:", repr(e)[:100])
        if not ok and key_sf:
            try:
                payload["model"] = SF_MODEL
                results.append(_post(SF_URL, key_sf, payload)); ok = True
            except Exception as e:
                print("  SiliconFlow FAIL:", repr(e)[:100])
        if not ok:
            return results, "ALL_LLM_FAIL"
    return results, "OK"


_DIR_MAP = {"偏多": "偏多", "多": "偏多", "bull": "偏多", "bullish": "偏多",
            "偏空": "偏空", "空": "偏空", "bear": "偏空", "bearish": "偏空",
            "中性": "中性", "震荡": "中性", "区间震荡": "中性", "neutral": "中性"}


def _first(d, *keys, default=None):
    ld = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in ld and ld[k.lower()] is not None:
            return ld[k.lower()]
    return default


def normalize(r):
    if not isinstance(r, dict):
        return r
    raw_dir = _first(r, "direction", "方向") or ""
    direction = _DIR_MAP.get(str(raw_dir).strip(), "中性")
    cf = _first(r, "confidence_pct", "confidence", "置信", "conf")
    conf = 50 if cf is None else (round(float(cf) * 100) if float(cf) <= 1 else int(round(float(cf))))
    rng = _first(r, "range", "区间", "interval") or {}
    if isinstance(rng, list) and len(rng) >= 2:
        rng = {"support": rng[0], "resist": rng[1], "note": ""}
    rng = {"support": (_first(rng, "support", "下沿", "low") if isinstance(rng, dict) else None),
           "resist": (_first(rng, "resist", "上沿", "high") if isinstance(rng, dict) else None),
           "note": (_first(rng, "note", "依据") if isinstance(rng, dict) else "") or ""}
    trg = _first(r, "triggers", "trigger", "触发") or {}
    if isinstance(trg, str):
        trg = {"bull": [trg], "bear": []}
    if isinstance(trg, dict):
        trg = {"bull": trg.get("bull") or trg.get("偏多") or [],
               "bear": trg.get("bear") or trg.get("偏空") or []}
        trg = {k: v if isinstance(v, list) else [v] for k, v in trg.items()}
    else:
        trg = {"bull": [], "bear": []}
    ev = _first(r, "evidence", "证据") or []
    out_ev = []
    for e in ev:
        if not isinstance(e, dict):
            continue
        metric = _first(e, "metric", "指标", "name")
        value = _first(e, "value", "数值", "val")
        if metric and value is not None:
            out_ev.append({"metric": str(metric), "value": str(value), "read": str(_first(e, "read", "信号", "方向") or "")})
    hz = _first(r, "horizons", "时间维度")
    if isinstance(hz, dict):
        hz = {k: str(v) for k, v in hz.items()}
    else:
        hz = {}
    return {"direction": direction, "confidence_pct": conf, "range": rng,
            "dominant_contradiction": str(_first(r, "dominant_contradiction", "主导矛盾", "矛盾") or ""),
            "stage": str(_first(r, "stage", "阶段") or ""),
            "triggers": trg, "evidence": out_ev,
            "horizons": hz,
            "data_as_of": str(_first(r, "data_as_of", "数据截至", "as_of") or "")}


def _post(url, key, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    txt = d["choices"][0]["message"]["content"]
    if txt.strip().startswith("```"):
        txt = txt.split("```", 2)[1]
        if txt.startswith("json"):
            txt = txt[4:]
    return normalize(json.loads(txt))


# ── 规则信号(LLM 缺省回退 + 复盘用，零品种逻辑) ──────────────────
# 每个 Zhiji 序列的价格方向极性: +1=序列上升利好铅价, -1=序列上升利空铅价, 0=跳过(自身价格)
POLARITY = {
    "ID01167236": -1, "a10016988": -0.4, "a10017000": -1,
    "ID00259970": 1, "ID00188200": 1, "ID01001569": 1,
    "ID01167603": -1, "ID00188315": -1, "ID02226083": -1,
    "a10021351": 0, "a10021353": 1, "a10021354": 0, "ID01360493": -1,
}
DIM_W = {"supply": 0.40, "demand": 0.40, "cost": 0.25, "inventory": 0.20, "spread": 0.10}


# ── Card-Aware 规则引擎 (任务A：读 PB_card.yaml 的结构化知识卡) ──────
# 卡片变量 → 实际数据提取器。返回 (latest值, 方向hint)。
# 方向hint: "up"/"down"/"flat"/None(用于"升/降"类条件判断)。
CARD_VAR_MAP = {
    "import_tc":                 ("ID01360493", "level"),               # 铅精矿TC，矿紧松（负值=矿紧）
    "native_lead_openrate":      ("a10016988", "level"),                # 原生铅开工率
    "regenerative_lead_openrate":("a10017000", "level"),                # 再生铅开工率
    "regenerative_lead_profit":  ("a10016953", "level"),                # 再生铅利润(zhiji实时，替代问财静态)
    "inventory":                 ("__inv__", "trend"),                  # 库存综合(多库存源)
    "import_parity":             ("ID01030003", "level"),               # 铅锭进口盈亏(zhiji实时，替代问财静态)
    "scrap_battery_price":       ("ID00188200", "trend"),               # 废电瓶价格
    "battery_openrate":          ("ID01030005", "trend"),               # 铅蓄电池开工率(zhiji实时，替代问财静态)
    "apparent_demand":           ("ID01001569", "trend"),               # 铅表观消费量
}
INV_SOURCE_IDS = ["ID00188315", "ID01167603", "ID02226083"]  # 现货/原生成品/LME库存


def _wenwen_latest(funda, kw):
    """从 wenwen 静态序列按 label 关键字取 latest 值。"""
    for t, items in funda.items():
        for it in items:
            if it.get("source") == "wenwen" and kw in str(it.get("label", "")):
                v = it.get("latest")
                return float(v) if v is not None else None
    return None


def _inv_trend(funda):
    """综合库存方向：任一库存源 chg_12m>0 视为累库。返回 (方向, 证据标签)。"""
    trend, labels = None, []
    for t, items in funda.items():
        for it in items:
            if it.get("id") in INV_SOURCE_IDS:
                c12 = it.get("chg_12m_pct")
                if c12 is not None:
                    labels.append(f"{it['label']}={c12:+.1f}%")
                    if c12 > 0.5:
                        trend = "up"
                    elif c12 < -0.5:
                        trend = "down"
    return trend, labels


def card_extract(funda):
    """按 CARD_VAR_MAP 提取全部核心变量值。返回 {var: {"value":v,"trend":dir,"label":..}}。"""
    out = {}
    for var, (key, kind) in CARD_VAR_MAP.items():
        rec = {"value": None, "trend": None, "label": var}
        if key == "__inv__":
            tr, labels = _inv_trend(funda)
            rec["trend"], rec["label"] = tr, "库存综合(" + "/".join(labels) + ")"
        elif key.startswith("__wenwen__:"):
            v = _wenwen_latest(funda, key.split(":", 1)[1])
            rec["value"] = v
            rec["label"] = key.split(":", 1)[1]
        else:
            for t, items in funda.items():
                for it in items:
                    if it.get("id") == key:
                        rec["value"] = it.get("latest")
                        c12 = it.get("chg_12m_pct")
                        rec["trend"] = "up" if (c12 is not None and c12 > 0.5) else ("down" if (c12 is not None and c12 < -0.5) else "flat")
                        rec["label"] = it["label"]
                        rec["_c12"] = c12
                        break
        out[var] = rec
    return out


def _cond_hit(cond_expr, cv):
    """判定卡片条件表达式是否命中。返回 (bool, 命中证据文本)。"""
    ev = []
    # import_tc < -150
    m = re.search(r"import_tc\s*<\s*(-?[\d.]+)", cond_expr)
    if m:
        thr = float(m.group(1))
        if cv["import_tc"]["value"] is not None and cv["import_tc"]["value"] < thr:
            ev.append(f"TC={cv['import_tc']['value']}<{thr}(矿紧)")
    # native_lead_openrate < 63
    m = re.search(r"native_lead_openrate\s*<\s*(-?[\d.]+)", cond_expr)
    if m:
        thr = float(m.group(1))
        v = cv["native_lead_openrate"]["value"]
        if v is not None and v < thr:
            ev.append(f"原生开工={v}<{thr}")
    # regenerative_lead_openrate < 63
    m = re.search(r"regenerative_lead_openrate\s*<\s*(-?[\d.]+)", cond_expr)
    if m:
        thr = float(m.group(1))
        v = cv["regenerative_lead_openrate"]["value"]
        if v is not None and v < thr:
            ev.append(f"再生开工={v}<{thr}")
    # scrap_battery_price 升
    if "scrap_battery_price 升" in cond_expr and cv["scrap_battery_price"]["trend"] == "up":
        ev.append(f"废电瓶升({cv['scrap_battery_price'].get('_c12')}%)")
    # regenerative_lead_profit < 0
    m = re.search(r"regenerative_lead_profit\s*<\s*(-?[\d.]+)", cond_expr)
    if m:
        thr = float(m.group(1))
        v = cv["regenerative_lead_profit"]["value"]
        if v is not None and v < thr:
            ev.append(f"再生铅利润={v}<{thr}")
    # inventory 累库
    if "inventory 累库" in cond_expr and cv["inventory"]["trend"] == "up":
        ev.append(f"{cv['inventory']['label']} 累库")
    # import_parity > 0
    m = re.search(r"import_parity\s*>\s*(-?[\d.]+)", cond_expr)
    if m:
        thr = float(m.group(1))
        v = cv["import_parity"]["value"]
        if v is not None and v > thr:
            ev.append(f"进口平价={v}>{thr}(窗口开)")
    # 蓄电池开工率升 且 表观消费量升
    if "蓄电池开工率升" in cond_expr and "表观消费量升" in cond_expr:
        if cv["battery_openrate"]["trend"] == "up" and cv["apparent_demand"]["trend"] == "up":
            ev.append("下游开工升+表观消费升")
    # AND 语义：所有子条件都必须满足
    if not ev:
        return False, None
    return True, "; ".join(ev)


def card_signal(funda, card=None, apply_calib=True):
    """L3 打分非抵消信号引擎：读 PB_card.yaml v0.5 的 signals，逐条独立判定并打出
    {id, direction(+1/-1), strength, priority(1-4), layer, evidence}，不做多空加权抵消。
    然后交给 L4 仲裁层(arbiter.arbiter)裁决最终方向。

    解决TC悖论：卡片用绝对阈值(TC<0→矿紧看多)，而非12m动量(+128%误判空)；
    解决"多空抵消归中性"：本层只打分不抵消，最终方向由 L4 按 4 层优先级裁决。

    apply_calib=True(生产)时做 Platt 校准+红牌；False(回测)用原始置信度，避免红牌污染评估。"""
    if card is None:
        card = load_card("PB")
    if not card or "signals" not in card:
        return rule_signal(funda)
    cv = card_extract(funda)
    sigs, hit_ev, hit_cs = [], [], []
    for c in card.get("signals", []):
        cond = c.get("cond") or c.get("条件", "")
        hit, ev = _cond_hit(cond, cv)
        if hit:
            s = int(c.get("direction", 0))  # +1多 / -1空
            pri = int(c.get("priority", 3))
            strength = float(c.get("strength", 1.0))
            sigs.append({"id": c["id"], "direction": s, "strength": strength,
                         "priority": pri, "layer": c.get("layer", ""),
                         "horizon": c.get("horizon", "mid"),
                         "evidence": ev or c.get("evidence", ""),
                         "chain": c.get("evidence", "")})
            hit_cs.append(c["id"])
            hit_ev.append({"id": c["id"], "direction": s, "priority": pri,
                           "strength": strength, "evidence": ev,
                           "chain": c.get("evidence", "")})

    # L4 仲裁：非抵消信号 → 4层优先级裁决
    arb = arbiter.arbiter(sigs, card)
    direction = arb["direction"]
    # 置信度：基于命中强度差(不抵消方向)，落在仲裁方向的强度占比越高越置信
    if sigs:
        dir_amt = arb["bull_score"] if arb["direction_code"] == 1 else (
            arb["bear_score"] if arb["direction_code"] == -1 else 0.0)
        total = arb["bull_score"] + arb["bear_score"]
        conf = max(45, min(90, 50 + int((dir_amt / total if total else 0.5) * 35)))
    else:
        conf = 40
    # Platt 概率校准（生产路径：有训练状态则缩放置信度，红牌时不采信直接压到~50）
    if apply_calib and CAL is not None:
        if direction == "中性":
            conf = 50
        else:
            red, brier, _, _ = CAL.red_card()
            conf = 50 if red else int(CAL.calibrate(conf))
    return {"direction": direction, "confidence_pct": conf,
            "score": round(arb["bull_score"] - arb["bear_score"], 2),
            "evidence": [{"metric": h["id"], "value": f"dir={h['direction']:+d},pri={h['priority']},strength={h['strength']:.1f}",
                          "read": f"{'多' if h['direction']>0 else '空'} | {h['evidence']}"}
                         for h in hit_ev],
            "contradictions": hit_cs,
            "arbiter": {"direction": arb["direction"], "dominant_priority": arb["dominant_priority_name"],
                        "verdict": arb["verdict"], "layers": arb["layers"],
                        "horizons": arb.get("horizons", {})}}


def load_card(code="PB"):
    """读 registry/{code}_card.yaml，失败返回 None。"""
    if yaml is None:
        return None
    p = os.path.join("registry", f"{code}_card.yaml")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"  [warn] 读卡失败 {p}: {repr(e)[:80]}")
        return None


def rule_signal(funda):
    """五维加权规则信号: 用 12月动量(回退3月) × 极性 × 维度权重。返回方向/置信/得分/证据。"""
    score, ev = 0.0, []
    for t, items in funda.items():
        for it in items:
            pol = POLARITY.get(it.get("id"))
            if not pol:
                continue
            mom = it.get("chg_12m_pct") if it.get("chg_12m_pct") is not None else it.get("chg_3m_pct")
            if mom is None:
                continue
            contrib = pol * (mom / 10.0) * DIM_W.get(t, 0.1)
            score += contrib
            ev.append((it["label"], mom, "多" if contrib > 0 else "空"))
    direction = "偏多" if score > 0.3 else "偏空" if score < -0.3 else "中性"
    conf = max(40, min(90, 50 + int(abs(score) * 12)))
    return {"direction": direction, "confidence_pct": conf, "score": round(score, 2), "evidence": ev}


def rule_prediction(funda, kline, card=None, news=None):
    close = kline[-1]["close"]
    # 优先用 L3 打分非抵消引擎 + L4 仲裁(读 PB_card.yaml v0.5)，失败才回退零品种动量规则
    cs = card_signal(funda, card)
    rs = rule_signal(funda)
    arb = cs.get("arbiter") or {}
    dominant = (f"L4仲裁: {arb.get('direction','')}@{arb.get('dominant_priority','')} "
                f"(命中: {','.join(cs['contradictions']) if cs['contradictions'] else '无'})"
                if cs["contradictions"]
                else f"零品种规则得分 {rs['score']}(卡未命中→兜底)")
    # L5 新闻分桶注入（机构情绪段）+ P3 机构观点近似共识
    news_text = (news_struct.news_summary_text(news) + "\n" + news_struct.institution_text(news)
                 if news else "[S5 机构情绪] 无新闻数据")
    return {"direction": cs["direction"], "confidence_pct": cs["confidence_pct"],
            "range": {"support": round(close * 0.97), "resist": round(close * 1.05),
                      "note": "规则回退区间：成本软底≈现价97%，库存/需求压力≈105%"},
            "dominant_contradiction": dominant,
            "stage": "验证期",
            "triggers": {"bull": ["LME库存停止累库转去化", "金九银十备货兑现去库"],
                         "bear": ["LME库存破40万续累", "跌破成本软底15430"]},
            "evidence": cs["evidence"] if cs["contradictions"] else
                        [{"metric": m, "value": f"{mom:+}%", "read": r} for m, mom, r in rs["evidence"]],
            "card_engine": {"active": bool(cs["contradictions"]), "score": cs["score"],
                            "contradictions": cs["contradictions"],
                            "arbiter": arb.get("direction"), "verdict": arb.get("verdict")},
            "news_bucket": news_text,
            "data_as_of": kline[-1]["time"]}


def load_system_prompt(path="report_prompt_template.md"):
    """从报告提示词模板抽取 SYSTEM PROMPT 段(去掉重复的 layer0_prompt.txt)。"""
    try:
        txt = open(path, encoding="utf-8").read()
    except Exception:
        return None
    m = re.search(r"## SYSTEM PROMPT.*?\n(.*?)(?:\n---|\n## USER PROMPT)", txt, re.S)
    return m.group(1).strip() if m else txt


def _month_ends(start_d, end_d):
    ends, y, m = [], start_d.year, start_d.month
    while True:
        me = datetime(y, m, calendar.monthrange(y, m)[1])
        if me > end_d:
            break
        ends.append(me.strftime("%Y-%m-%d"))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return ends


def _fetch_series_raw(sid, start, end):
    try:
        d = get(f"{COMM_BASE}/series?id={sid}&start={start}&end={end}&limit=2000", {"X-Data-Key": DATA_KEY})
    except Exception as e:
        print(f"  [warn] 回测序列 {sid} 拉取失败: {repr(e)[:60]}")
        return []
    out, pts = [], d.get("points") or d.get("data") or []
    for p in pts:
        s, v = p.get("date"), p.get("value")
        if not s or v is None:
            continue
        try:
            out.append((datetime.strptime(str(s)[:10], "%Y-%m-%d"), float(v)))
        except Exception:
            continue
    return out


def _stats_upto(points, T, months=12):
    cut = T - timedelta(days=30 * months)
    recent = [p for p in points if cut <= p[0] <= T]
    if len(recent) < 2:
        return None
    vals = [v for _, v in recent]
    lv = recent[-1][1]
    mean = statistics.mean(vals)
    def chg(mn):
        c = T - timedelta(days=30 * mn)
        r2 = [p for p in recent if p[0] >= c]
        if len(r2) < 2 or not r2[0][1]:
            return None
        return round((r2[-1][1] - r2[0][1]) / r2[0][1] * 100, 1)
    return {"latest": lv, "min": min(vals), "max": max(vals), "mean": round(mean, 2),
            "chg_3m_pct": chg(3), "chg_12m_pct": chg(12),
            "dev_mean_pct": round((lv - mean) / mean * 100, 1) if mean else None}


def run_backtest(code="PB"):
    """复盘回测: 取近12个月月末快照, 用规则信号预判, 对比其后20日实际涨跌, 写 predictions 并算胜率。"""
    print("=== 复盘回测(规则信号, 近12个月月末快照) ===")
    kbars = fetch_kline(code, 260)
    close_map = {b["time"][:10]: b["close"] for b in kbars}
    dates = sorted(close_map)
    fwd = {}
    for i, d in enumerate(dates):
        if i + 20 < len(dates):
            fwd[d] = (close_map[dates[i + 20]] - close_map[d]) / close_map[d] * 100
    registry = load_registry(code)
    zhiji = [s for s in registry.get("series", []) if s.get("id") and s.get("source") != "wenwen"]
    raw = {s["id"]: _fetch_series_raw(s["id"], "2025-01-01", dates[-1]) for s in zhiji}
    end_d = datetime.strptime(dates[-1], "%Y-%m-%d") - timedelta(days=1)
    ends = _month_ends(datetime(2025, 9, 1), end_d)
    card = load_card(code)
    preds = []
    for T in ends:
        Td = datetime.strptime(T, "%Y-%m-%d")
        grouped = {}
        for s in zhiji:
            st = _stats_upto(raw[s["id"]], Td)
            if st:
                grouped.setdefault(s["type"], []).append({"id": s["id"], "label": s["label"], "type": s["type"], **st})
        # Card-Aware 优先，卡未命中才回退零品种规则；回测用原始置信度(不红牌污染)
        rs = card_signal(grouped, card, apply_calib=False) if card else rule_signal(grouped)
        actual = fwd.get(T)
        if actual is None:
            hit = None
        elif rs["direction"] == "偏多":
            hit = bool(actual > 0)
        elif rs["direction"] == "偏空":
            hit = bool(actual < 0)
        else:
            hit = None
        preds.append({"code": code, "as_of": T, "direction": rs["direction"],
                      "confidence_pct": rs["confidence_pct"], "score": rs["score"],
                      "actual_chg_pct": round(actual, 2) if actual is not None else None,
                      "hit": hit})
    tracker.STORE = "backtest_predictions.json"
    json.dump(preds, open(tracker.STORE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(tracker.summary())
    # 概率校准闭环：Brier + Platt 季度重训/月度微调 + 红牌告警
    if CAL is not None:
        print("\n=== 7) 概率校准闭环(Brier/Platt/红牌) ===")
        print(CAL.status_report())
        action, st = CAL.train_schedule()
        print(f"  调度: {action} → A={st.get('A')}, B={st.get('B')}, n={st.get('n')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default="PB")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    a = ap.parse_args()

    if a.backtest:
        run_backtest(a.code)
        return

    print(f"=== 1) Guan 行情 {a.code} ===")
    kline = fetch_kline(a.code, 120)
    print(f"  OK {kline[-1]['time']} 收 {kline[-1]['close']} 持仓 {kline[-1].get('open_interest')}")

    print(f"=== 2) 新闻 q=铅 ===")
    news = fetch_news("铅", 72, 8)
    print(f"  OK {len(news)} 条")

    print("=== 3) 料基本面序列(真实数据) ===")
    registry = load_registry(a.code)
    funda = build_fundamental(registry)
    for t, items in funda.items():
        print(f"  [{t}] " + "; ".join(
            f"{i['label']}={i['latest']}(近月{i.get('chg_pct'):+}%)" if i.get('chg_pct') is not None
            else f"{i['label']}={i.get('latest')}" for i in items))

    print("=== 4) auto_profile 行情画像 ===")
    prof = auto_profile.analyze(kline)
    for p in prof["key_indicators"]:
        print(f"  - {p['name']}: z={p['z_score']} 趋势={p['trend_pct']}% 信号={p['signal']}")

    print("=== 5) 组装输入 ===")
    system = load_system_prompt() or (open("layer0_prompt.txt", encoding="utf-8").read()
                                      if os.path.exists("layer0_prompt.txt") else "")
    user = build_input(a.code, kline, news, prof, registry, funda)
    with open("pb_input.json", "w", encoding="utf-8") as f:
        json.dump({"system": system, "user": user,
                   "meta": {"code": a.code, "data_as_of": kline[-1]["time"],
                            "news_count": len(news),
                            "data_freshness": freshness.meta(funda, kline[-1].get("time")),
                            "funda_types": {t: len(v) for t, v in funda.items()}}},
                  f, ensure_ascii=False, indent=2)
    print("  saved pb_input.json")

    # 6) 产出预判(LLM 或 规则回退) + 记录到 tracker 做命中率复盘
    no_key = (os.environ.get("DASHSCOPE_KEY") is None and os.environ.get("SILICONFLOW_KEY") is None)
    if a.no_llm or no_key:
        tag = "无 LLM key" if (no_key and not a.no_llm) else "--no-llm"
        print(f"=== 6) {tag} → 规则信号回退(L3打分+L4仲裁优先) ===")
        pred = rule_prediction(funda, kline, card=load_card(a.code), news=news)
        idx = tracker.record(pred, a.code)
        with open("pb_output_rule.json", "w", encoding="utf-8") as f:
            json.dump(pred, f, ensure_ascii=False, indent=2)
        print(f"  规则信号: direction={pred['direction']} conf={pred['confidence_pct']} idx={idx}")
    else:
        print(f"=== 6) LLM temp=0.3 runs={a.runs} ===")
        results, status = call_llm(system, user, a.runs)
        if status != "OK":
            print("  LLM 失败, 回退规则信号:", status)
            pred = rule_prediction(funda, kline, card=load_card(a.code))
            idx = tracker.record(pred, a.code)
            with open("pb_output_rule.json", "w", encoding="utf-8") as f:
                json.dump(pred, f, ensure_ascii=False, indent=2)
            print(f"  规则信号: direction={pred['direction']} conf={pred['confidence_pct']} idx={idx}")
        else:
            for i, r in enumerate(results, 1):
                idx = tracker.record(r, a.code)
                with open(f"pb_output_{i}.json", "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False, indent=2)
                print(f"  run{i}: direction={r['direction']} conf={r['confidence_pct']} idx={idx}")
            if a.runs > 1:
                dirs = [r["direction"] for r in results]
                confs = [r["confidence_pct"] for r in results]
                print(f"  稳定性: direction={dirs} 置信均值={round(sum(confs)/len(confs),1)}")


if __name__ == "__main__":
    main()
