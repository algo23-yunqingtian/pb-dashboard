# -*- coding: utf-8 -*-
"""
数据时效审计模块 (P0: 数据时效性)。
核心目标：杜绝"用7月数据判8月行情"的硬伤。
1. 逐序列计算数据时效 (latest_date 距当前交易日天数)。
2. 按频率给"新鲜度阈值"：日>4天 / 周>10天 / 月>35天 判为"滞后"。
3. 产出结构化 freshness 快照 + 人类可读"数据时效审计段"，注入 prompt。
4. 若存在滞后序列，在报告里显式标注"该序列滞后，勿当作当日最新"，降低误用风险。
"""
from datetime import datetime


def _parse(d):
    if not d:
        return None
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d")
    except Exception:
        return None


# 频率 -> 滞后判定阈值(天)。月数据天然滞后，给定更宽阈值但会在审计里显式提示。
FRESH_THRESHOLD = {"日": 4, "周": 10, "月": 35}


def audit(funda, as_of_date):
    """遍历基本面序列，返回 (rows, summary, warn_count)。
    as_of_date: 行情最新交易日(字符串 YYYY-MM-DD 或带时间)。
    rows: [{label, freq, latest_date, staleness_days, stale, note}]
    summary: 统计文本
    """
    asof = _parse(as_of_date)
    rows, stale_n = [], 0
    for t, items in funda.items():
        for it in items:
            freq = it.get("freq", "日")
            thr = FRESH_THRESHOLD.get(freq, 10)
            ld = _parse(it.get("latest_date"))
            days = (asof - ld).days if (asof and ld) else None
            stale = days is not None and days > thr
            note = ""
            if stale:
                stale_n += 1
                note = (f"滞后{days}天(>{thr})：勿当作当日最新，仅作趋势参考"
                        if freq == "月" else f"滞后{days}天")
            elif ld and freq == "月":
                # 月度数据即便在阈值内也提示是月频
                note = "月频序列，天然滞后到月末快照"
            rows.append({"label": it["label"], "freq": freq,
                         "latest_date": it.get("latest_date"),
                         "staleness_days": days, "stale": bool(stale), "note": note})
    # 排序：最滞后的排最前
    rows.sort(key=lambda r: (r["staleness_days"] if r["staleness_days"] is not None else -1),
              reverse=True)
    summary = (f"数据时效审计：共{len(rows)}序列，其中{stale_n}条滞后(超频率阈值)；"
               f"行情截至{as_of_date}。滞后序列不得作为当日方向主判依据，仅可作趋势/均值偏离参考。")
    return rows, summary, stale_n


def audit_text(funda, as_of_date):
    """人类可读审计段，注入 build_input 的 prompt。"""
    rows, summary, _ = audit(funda, as_of_date)
    lines = ["【数据时效审计(逐序列新鲜度)】", f"- {summary}"]
    for r in rows:
        tag = "⚠滞后" if r["stale"] else ("·" + r["note"] if r["note"] else "")
        line = (f"  - {r['label']}({r['freq']}): 最新 {r['latest_date'] or 'n/a'}"
                f" | 距今 {r['staleness_days'] if r['staleness_days'] is not None else 'n/a'}天 {tag}")
        lines.append(line)
    lines.append("铁律：判定当日方向必须优先用最新鲜序列(日/周)；对滞后月度序列只取其趋势与绝对水平参考，不单独据此判方向。")
    return "\n".join(lines)


def meta(funda, as_of_date):
    """结构化 freshness 快照，写入 pb_input.json meta。"""
    rows, _, stale_n = audit(funda, as_of_date)
    return {"stale_count": stale_n, "series": rows,
            "as_of": str(as_of_date)[:10] if as_of_date else None}
