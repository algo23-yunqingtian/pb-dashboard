# -*- coding: utf-8 -*-
"""
L4 仲裁层 (arbiter.py)
=======================
4 层优先级裁决引擎，对齐同花顺自曝的"4 层裁决规则"（见 iwencai_pb_compare.md）。

同花顺裁决规则（自曝，最高层优先，只有高层打平才下探）：
  P1 已验证现货现实  > 成本支撑   （现货供需现实最重，成本支撑其次）
  P2 变化方向        > 绝对水平   （拐点/变化比静态高低更重要）
  P3 供应端          > 需求端     （供应端(矿/再生/原生)决定边际，需求端弱相关）
  P4 短期            > 中期 > 长期（时间维度就近优先）

本模块：
- 输入：L3 打出的非抵消信号列表，每条信号带 {id, direction(+1/-1), strength, priority(1-4), layer, evidence}
- 输出：direction 偏多/偏空/中性 + dominant_priority + verdict 理由链（列出每层多空明细）
- 通用：不硬编码品种，任一品种卡都可调用。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 优先级常量：1=最高(P1 已验证现货现实)，4=最低(P4 时间维度)
PRIORITY_P1 = 1
PRIORITY_P2 = 2
PRIORITY_P3 = 3
PRIORITY_P4 = 4

# 优先级名称映射（对齐 v6 架构）
PRIORITY_NAMES = {
    1: "P1 已验证现货现实>成本支撑",
    2: "P2 变化方向>绝对水平",
    3: "P3 供应端>需求端",
    4: "P4 短期>中期>长期",
}

DIR_LONG = +1
DIR_SHORT = -1

_DIR_TEXT = {DIR_LONG: "多", DIR_SHORT: "空", 0: "中性"}


def _sign_strength(sig: Dict[str, Any]) -> float:
    """从信号里取 strength，缺省 1.0。strength 代表该条信号的可信/权重。"""
    s = sig.get("strength")
    if s is None:
        return 1.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 1.0


def _dir(sig: Dict[str, Any]) -> int:
    d = sig.get("direction", 0)
    if d in (DIR_LONG, DIR_SHORT, 0):
        return d
    if isinstance(d, str):
        t = d.lower()
        if t in ("bull", "long", "多", "看多", "偏多", "+"):
            return DIR_LONG
        if t in ("bear", "short", "空", "看空", "偏空", "-"):
            return DIR_SHORT
    return 0


def _pri(sig: Dict[str, Any]) -> int:
    p = sig.get("priority", PRIORITY_P3)
    try:
        p = int(p)
    except (TypeError, ValueError):
        p = PRIORITY_P3
    return max(PRIORITY_P1, min(PRIORITY_P4, p))


def _layer_label(sig: Dict[str, Any]) -> str:
    return sig.get("layer", "未分类")


def _hit_direction_majority(sigs: List[Dict[str, Any]]) -> int:
    """按多空数量(非权重)取组内基调；平手返回 0。"""
    longs = sum(1 for s in sigs if _dir(s) == DIR_LONG)
    shorts = sum(1 for s in sigs if _dir(s) == DIR_SHORT)
    if longs == shorts:
        return 0
    return DIR_LONG if longs > shorts else DIR_SHORT


def _hit_direction_strength(sigs: List[Dict[str, Any]]) -> int:
    """按 strength 加权取组内基调；平手返回 0。"""
    lw = sum(_sign_strength(s) for s in sigs if _dir(s) == DIR_LONG)
    sw = sum(_sign_strength(s) for s in sigs if _dir(s) == DIR_SHORT)
    if abs(lw - sw) < 1e-9:
        return 0
    return DIR_LONG if lw > sw else DIR_SHORT


def _layer_details(sigs: List[Dict[str, Any]], pri: int) -> List[str]:
    """生成某一优先级层内的证据明细行。"""
    rows = []
    for s in sigs:
        if _pri(s) != pri:
            continue
        rows.append(
            f"  [{_DIR_TEXT[_dir(s)]}] {s.get('id', '?')} "
            f"(strength={_sign_strength(s):.1f}, layer={_layer_label(s)}): {s.get('evidence', '')}"
        )
    return rows


# ---------------- P1: 时间维度(horizon)横切 ----------------
# P4 仲裁只给单一方向，会丢掉"短期偏空 / 中期偏多"并存的真相。
# 时间维度横切：把信号按 horizon(short/mid/long) 分组，每组给一个方向，
# 使报告能同出多时段结论，而不用强制抵消成一个方向。

HORIZON_ORDER = ["short", "mid", "long"]
HORIZON_TEXT = {"short": "短期(≤2周)", "mid": "中期(1-3月)", "long": "长期(>3月)"}


def _horizon(sig: Dict[str, Any]) -> str:
    h = sig.get("horizon")
    if h in HORIZON_ORDER:
        return h
    return "mid"  # 缺省中期


def horizon_cut(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按时间维度横切信号。

    返回:
      {
        "has": bool,           # 是否至少一个信号带显式 horizon
        "horizons": {
           "short": {"direction_code", "direction", "bull_strength", "bear_strength", "signals":[...]},
           ...
        },
        "text": "人类可读说明(供 prompt 注入)"
      }
    仅对带 horizon 标签的信号做分组；不带标签的不参与(避免误判)。
    """
    tagged = [s for s in signals if s.get("horizon") in HORIZON_ORDER]
    if not tagged:
        return {"has": False, "horizons": {}, "text": ""}

    groups: Dict[str, Dict[str, Any]] = {}
    for h in HORIZON_ORDER:
        gs = [s for s in tagged if _horizon(s) == h]
        if not gs:
            continue
        lw = sum(_sign_strength(s) for s in gs if _dir(s) == DIR_LONG)
        sw = sum(_sign_strength(s) for s in gs if _dir(s) == DIR_SHORT)
        if abs(lw - sw) < 1e-9:
            dc = 0
        else:
            dc = DIR_LONG if lw > sw else DIR_SHORT
        groups[h] = {
            "direction_code": dc,
            "direction": _DIR_TEXT[dc],
            "bull_strength": round(lw, 2),
            "bear_strength": round(sw, 2),
            "signals": [{"id": s.get("id", "?"), "direction": _DIR_TEXT[_dir(s)],
                         "strength": _sign_strength(s), "layer": _layer_label(s),
                         "evidence": s.get("evidence", "")} for s in gs],
        }

    lines = ["【时间维度横切(short/mid/long)】信号按时间标签分组，多时段结论可并存："]
    for h in HORIZON_ORDER:
        if h not in groups:
            continue
        g = groups[h]
        lines.append(f"- {HORIZON_TEXT[h]}: 基调={g['direction']} (多{g['bull_strength']}/空{g['bear_strength']})")
        for s in g["signals"]:
            lines.append(f"    [{s['direction']}] {s['id']}(strength={s['strength']:.1f},layer={s['layer']}): {s['evidence']}")
    lines.append("若短期与中期方向相反，这是正常的(如短期累库压制 vs 中期供应收缩托底)，不得强行抵消成一个方向；"
                 "报告须分时段给出结论。")
    return {"has": True, "horizons": groups, "text": "\n".join(lines)}


def arbiter(signals: List[Dict[str, Any]], card: Any = None) -> Dict[str, Any]:
    """L4 主裁决函数。

    参数：
      signals: L3 输出的非抵消信号列表，每项含 id/direction/strength/priority/layer/evidence。
      card  : 可选，品种卡的 arbiter 配置（priority_order）。默认用内置 4 层。

    返回：
      {
        "direction": "偏多|偏空|中性",
        "direction_code": +1 / -1 / 0,
        "dominant_priority": 数字(1-4),
        "dominant_priority_name": "P1 ...",
        "bull_score": 多向 strength 合计,
        "bear_score": 空向 strength 合计,
        "verdict": "理由链文本",
        "layers": { "P1": {...}, "P2": {...}, ... }   # 每层多空明细
        "arbitrated": True,
      }
    """
    if not signals:
        return {
            "direction": "中性", "direction_code": 0,
            "dominant_priority": None, "dominant_priority_name": "无信号",
            "bull_score": 0.0, "bear_score": 0.0,
            "verdict": "无可用信号，无法裁决。", "layers": {}, "arbitrated": True,
        }

    # 逐层裁决：从最高优先级(P1)向下，只在某层平手时下探
    layers_detail: Dict[str, Dict[str, Any]] = {}
    dominant = None
    layer_rows: List[str] = []

    for pri in (PRIORITY_P1, PRIORITY_P2, PRIORITY_P3, PRIORITY_P4):
        in_layer = [s for s in signals if _pri(s) == pri]
        if not in_layer:
            continue
        # 组内基调：数量法为主，辅以 strength 法，二者都同向才算该层方向
        cnt_dir = _hit_direction_majority(in_layer)
        st_dir = _hit_direction_strength(in_layer)
        layer_dir = 0
        if cnt_dir != 0 and cnt_dir == st_dir:
            layer_dir = cnt_dir
        layers_detail[f"P{pri}"] = {
            "signals": len(in_layer),
            "direction_code": layer_dir,
            "direction": _DIR_TEXT[layer_dir],
            "bull_strength": round(sum(_sign_strength(s) for s in in_layer if _dir(s) == DIR_LONG), 2),
            "bear_strength": round(sum(_sign_strength(s) for s in in_layer if _dir(s) == DIR_SHORT), 2),
            "details": _layer_details(in_layer, pri),
        }
        head = f"P{pri}({PRIORITY_NAMES[pri]}): {len(in_layer)}条 → 基调={_DIR_TEXT[layer_dir]}"
        layer_rows.append(head)
        layer_rows.extend(layers_detail[f"P{pri}"]["details"])

        if layer_dir != 0:
            dominant = pri
            break

    if dominant is None:
        direction_code = 0
        dominant = max((p for p in (PRIORITY_P1, PRIORITY_P2, PRIORITY_P3, PRIORITY_P4)
                        if p in layers_detail), default=None)
    else:
        direction_code = layers_detail[f"P{dominant}"]["direction_code"]

    # 总 strength（用于展示，不用于抵消决定方向）
    bull = round(sum(_sign_strength(s) for s in signals if _dir(s) == DIR_LONG), 2)
    bear = round(sum(_sign_strength(s) for s in signals if _dir(s) == DIR_SHORT), 2)

    direction_text = {DIR_LONG: "偏多", DIR_SHORT: "偏空"}.get(direction_code, "中性")
    dom_name = PRIORITY_NAMES.get(dominant, "无") if dominant else "无有效优先级"

    # 理由链
    verdict_lines = [
        f"[L4 仲裁] 逐层裁决(高→低)，仅在高层平手才下探。",
    ]
    verdict_lines.extend(layer_rows)
    if dominant is not None:
        verdict_lines.append(
            f"→ 裁决落在 {dom_name}（{direction_text}），总多空 strength：多{bull} / 空{bear}。"
        )
    else:
        verdict_lines.append("→ 全部层平手，无法定方向 → 中性。")

    # P1 时间维度横切：多时段结论可并存
    hz = horizon_cut(signals)
    if hz["has"]:
        verdict_lines.append("")
        verdict_lines.append(hz["text"])

    return {
        "direction": direction_text,
        "direction_code": direction_code,
        "dominant_priority": dominant,
        "dominant_priority_name": dom_name,
        "bull_score": bull,
        "bear_score": bear,
        "verdict": "\n".join(verdict_lines),
        "layers": layers_detail,
        "horizons": hz["horizons"],
        "arbitrated": True,
    }


if __name__ == "__main__":
    # 冒烟自检：P1 现货累库(空) + P1 成本支撑(多) + P2 供应收缩(多)
    sigs = [
        {"id": "LME累库", "direction": -1, "strength": 1.5, "priority": 1, "layer": "库存", "evidence": "LME+85.7%"},
        {"id": "再生成本支撑", "direction": 1, "strength": 1.2, "priority": 1, "layer": "成本", "evidence": "再生利润-585"},
        {"id": "再生开工收缩", "direction": 1, "strength": 1.0, "priority": 2, "layer": "供应", "evidence": "开工29.8%"},
        {"id": "进口窗口关", "direction": 1, "strength": 0.8, "priority": 3, "layer": "进口", "evidence": "亏损-413"},
    ]
    r = arbiter(sigs)
    print(r["direction"], r["dominant_priority_name"])
    print(r["verdict"])
