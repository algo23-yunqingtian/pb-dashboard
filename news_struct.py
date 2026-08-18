# -*- coding: utf-8 -*-
"""
L5 新闻结构化 / 情绪分桶 (news_struct.py)
========================================
把原始新闻列表按 (情感 × 重要性) 分桶，产出结构化 S5 机构情绪段，供 LLM 注入。

背景：
- zhiji news API 返回字段含 sentiment(positive/negative/neutral) 与 importance(高/中/低)。
- fetch_news 需保留这两个字段；本模块负责分桶 + 汇总 + 产出可注入文本。

通用：不硬编码品种。分桶桶名固定，任何品种复用。

S5 机构情绪段（注入 LLM 的模板，见 ARCHITECTURE_universal_v6.md）：
  [S5 机构情绪 / 新闻分桶]
  - 重要利多 X 条：<top titles>
  - 重要利空 X 条：<top titles>
  - 一般情绪统计：中性 n、利多 n、利空 n
  - 市场在交易什么：<归一句话>
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 分桶常量：优先级由 (importance, sentiment) 组合决定
NEWS_BUCKETS = {
    "key_bull": "重要利多",
    "key_bear": "重要利空",
    "key_neutral": "重要中性",
    "bull": "一般利多",
    "bear": "一般利空",
    "neutral": "一般中性",
}

# importance 归一化：中→高算重要
_IMPORTANT = {"高", "高重要", "重要", "high", "High", "HIGH", "中", "中重要", "medium", "Medium", "MEDIUM"}
_SENTIMENT_MAP = {
    "positive": "bull", "pos": "bull", "利多": "bull", "看多": "bull",
    "negative": "bear", "neg": "bear", "利空": "bear", "看空": "bear",
}


def _sent(news: Dict[str, Any]) -> str:
    s = news.get("sentiment", "") or ""
    s = str(s).lower().strip()
    if s in _SENTIMENT_MAP:
        return _SENTIMENT_MAP[s]
    if s in ("neutral", "中性", "中"):
        return "neutral"
    return "neutral"


def _imp(news: Dict[str, Any]) -> bool:
    i = news.get("importance", "") or ""
    return str(i) in _IMPORTANT


def _title(news: Dict[str, Any]) -> str:
    return str(news.get("title") or news.get("content") or news.get("name") or "")[:60]


def bucket_news(news_list: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """把新闻列表分桶，返回每桶 {count, items}。

    返回 dict: 桶名 -> {"count": n, "items": [news...]}（所有 6 桶都有 key）。
    """
    out = {b: {"count": 0, "items": []} for b in NEWS_BUCKETS}
    if not news_list:
        return out
    for n in news_list:
        sent = _sent(n)
        imp = _imp(n)
        if imp and sent == "bull":
            bucket = "key_bull"
        elif imp and sent == "bear":
            bucket = "key_bear"
        elif imp:
            bucket = "key_neutral"
        elif sent == "bull":
            bucket = "bull"
        elif sent == "bear":
            bucket = "bear"
        else:
            bucket = "neutral"
        out[bucket]["items"].append(n)
        out[bucket]["count"] += 1
    return out


def news_summary(news_list: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """分桶后汇总成结构化 summary，供注入 LLM 或写入 data.json。"""
    buckets = bucket_news(news_list)
    total = sum(b["count"] for b in buckets.values())

    def top_titles(bucket: str, n: int = 3) -> List[str]:
        return [_title(x) for x in buckets[bucket]["items"][:n]]

    # "市场在交易什么"：取 key_bull/key_bear 里条数多的那一侧，或给中性归总
    kb = buckets["key_bull"]["count"]
    kbe = buckets["key_bear"]["count"]
    if kb + kbe == 0:
        market_focus = "无明显重要情绪导向"
    elif kb == kbe:
        market_focus = "重要利多利空均衡" + (
            f"，如：利多'{top_titles('key_bull', 1)[0] if top_titles('key_bull') else ''}' vs 利空'{top_titles('key_bear', 1)[0] if top_titles('key_bear') else ''}'"
            if kb else ""
        )
    elif kb > kbe:
        market_focus = f"重要利多占优({kb}:{kbe})，市场偏多关注" + (
            f"，如：{top_titles('key_bull', 2)}" if kb else ""
        )
    else:
        market_focus = f"重要利空占优({kbe}:{kb})，市场偏空关注" + (
            f"，如：{top_titles('key_bear', 2)}" if kbe else ""
        )

    return {
        "total": total,
        "key_bull_count": kb,
        "key_bear_count": kbe,
        "key_neutral_count": buckets["key_neutral"]["count"],
        "bull_count": buckets["bull"]["count"],
        "bear_count": buckets["bear"]["count"],
        "neutral_count": buckets["neutral"]["count"],
        "key_bull_titles": top_titles("key_bull"),
        "key_bear_titles": top_titles("key_bear"),
        "market_focus": market_focus,
    }


def news_summary_text(news_list: Optional[List[Dict[str, Any]]]) -> str:
    """把 summary 转成可直接注入 LLM 的 S5 文本段。"""
    s = news_summary(news_list)
    if s["total"] == 0:
        return "[S5 机构情绪/新闻分桶] 本期无新闻数据。"
    lines = [
        "[S5 机构情绪 / 新闻分桶]",
        f"- 新闻总数 {s['total']} 条",
        f"- 重要利多 {s['key_bull_count']} 条：{('；'.join(s['key_bull_titles']) or '无')}",
        f"- 重要利空 {s['key_bear_count']} 条：{('；'.join(s['key_bear_titles']) or '无')}",
        f"- 情绪分布：利多{bull_count(s)} / 利空{bear_count(s)} / 中性{neutral_count(s)}（含一般）",
        f"- 市场在交易什么：{s['market_focus']}",
    ]
    return "\n".join(lines)


def bull_count(s: Dict[str, Any]) -> int:
    return s.get("bull_count", 0) + s.get("key_bull_count", 0)


def bear_count(s: Dict[str, Any]) -> int:
    return s.get("bear_count", 0) + s.get("key_bear_count", 0)


def neutral_count(s: Dict[str, Any]) -> int:
    return s.get("neutral_count", 0) + s.get("key_neutral_count", 0)


# ============================================================================
# P3 机构观点提取 (institution_sentiment)
# 从新闻 title/content/tags 里识别机构观点关键词，做 看多/看空/中性 计数，
# 并计算"共识强度"（-1..+1），供 LLM 注入 S5 机构段。
# 局限（诚实标注）：zhiji 新闻 tags 是板块级("基本金属")，非机构观点标签；
#   真正的机构研报观点 zhiji 拉不到，只能从新闻语气关键词近似，故输出叫"近似共识"。
# ============================================================================

# 关键词表：bullish / bearish / 机构锚
_INST_BULL = [
    "看多", "看涨", "偏多", "上调", "目标价", "增持", "买入", "看好", "看高",
    "供给收缩", "供应收缩", "去库", "补库", "旺季", "托底", "支撑", "反弹",
    "走强", "上涨", "上行", "利多", "溢价", "短缺", "紧张", "乐观", "积极",
]
_INST_BEAR = [
    "看空", "看跌", "偏空", "下调", "减持", "卖出", "利空", "累库", "过剩",
    "压力", "压制", "回落", "下跌", "下行", "走弱", "悲观", "承压", "滞销",
    "进口", "抛售", "折价", "担忧", "风险", "清仓", "贴水",
]
# 机构锚词：只有命中这些词附近才计"机构观点"，降低误报
_INST_ANCHOR = [
    "机构", "分析师", "研报", "券商", "研究所", "观点", "预计", "预测", "认为",
    "展望", "判断", "建议", "目标", "伦铅", "沪铅", "铜价", "铝价", "锌价",
    "铅价", "电池", "蓄电池", "SMM", "Mysteel", "隆众", "百川", "统计", "调研",
]


def _is_inst(news: Dict[str, Any]) -> bool:
    """是否疑似机构/研究观点类新闻（命中机构锚词）。"""
    text = " ".join([str(news.get("title") or ""), str(news.get("content") or ""),
                     " ".join(news.get("tags", []) or [])])
    return any(a in text for a in _INST_ANCHOR)


def institution_sentiment(news_list: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """机构观点近似共识统计。

    返回:
      {
        "counted": 参与统计的机构类新闻数,
        "bull": 看多计数, "bear": 看空计数, "neutral": 中性计数,
        "score": (bull-bear)/total ∈ [-1,1],
        "consensus": "强烈看多"|"偏多"|"分歧/中性"|"偏空"|"强烈看空"|"无数据",
        "samples": {"bull":[...], "bear":[...]}
      }
    """
    if not news_list:
        return {"counted": 0, "bull": 0, "bear": 0, "neutral": 0,
                "score": 0.0, "consensus": "无数据", "samples": {"bull": [], "bear": []}}
    bull, bear, neu = [], [], []
    for n in news_list:
        if not _is_inst(n):
            continue
        text = " ".join([str(n.get("title") or ""), str(n.get("content") or "")])
        b = sum(1 for k in _INST_BULL if k in text)
        be = sum(1 for k in _INST_BEAR if k in text)
        title = _title(n)
        if b > be:
            bull.append(title)
        elif be > b:
            bear.append(title)
        else:
            neu.append(n)
    total = len(bull) + len(bear) + len(neu)
    if total == 0:
        return {"counted": 0, "bull": 0, "bear": 0, "neutral": 0,
                "score": 0.0, "consensus": "无数据", "samples": {"bull": [], "bear": []}}
    score = (len(bull) - len(bear)) / total
    if score >= 0.6:
        consensus = "强烈看多"
    elif score >= 0.2:
        consensus = "偏多"
    elif score > -0.2:
        consensus = "分歧/中性"
    elif score > -0.6:
        consensus = "偏空"
    else:
        consensus = "强烈看空"
    return {"counted": total, "bull": len(bull), "bear": len(bear),
            "neutral": len(neu), "score": round(score, 2), "consensus": consensus,
            "samples": {"bull": bull[:3], "bear": bear[:3]}}


def institution_text(news_list: Optional[List[Dict[str, Any]]]) -> str:
    """机构观点近似共识 → 可注入 LLM 的文本段。"""
    s = institution_sentiment(news_list)
    if s["counted"] == 0:
        return "[P3 机构观点近似共识] 本期无机构/研究类新闻，机构情绪不纳入判断(避免空想)。"
    lines = [
        "[P3 机构观点近似共识]",
        f"- 参与统计机构/研究类新闻 {s['counted']} 条：看多 {s['bull']} / 看空 {s['bear']} / 中性 {s['neutral']}",
        f"- 共识强度 score={s['score']:+.2f} → {s['consensus']}",
        f"- 看多样本：{('；'.join(s['samples']['bull']) or '无')}",
        f"- 看空样本：{('；'.join(s['samples']['bear']) or '无')}",
        "- 说明：由新闻语气关键词近似得出，非真实机构研报观点(zhiji 无研报库)；仅作参考权重，不作主判依据。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sample = [
        {"title": "LME铅库存大幅累库", "sentiment": "negative", "importance": "高",
         "content": "机构预计库存压力压制铅价", "tags": ["基本金属"]},
        {"title": "再生铅开工率持续下滑", "sentiment": "positive", "importance": "高",
         "content": "分析师认为供应收缩支撑铅价偏多", "tags": ["基本金属"]},
        {"title": "蓄电池旺季备货启动", "sentiment": "positive", "importance": "低",
         "content": "下游补库，机构看好旺季反弹", "tags": ["蓄电池"]},
        {"title": "某厂检修消息", "sentiment": "neutral", "importance": "低",
         "content": "检修影响有限", "tags": []},
    ]
    print(news_summary_text(sample))
    print()
    print(institution_text(sample))
