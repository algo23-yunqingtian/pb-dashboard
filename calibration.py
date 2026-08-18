# -*- coding: utf-8 -*-
"""§0.7 概率校准 + 红牌告警模块（Brier / Platt / 季度重训 / 月度微调）。
消费 backtest_predictions.json（方向+置信度+实际涨跌+命中），
- 计算 Brier Score（校准度）
- 训练 Platt 缩放参数 A,B：logit(p_cal) = A*logit(p_raw) + B
- Brier>0.25 → 红牌告警（预测比随机还差，暂停采信）
- 状态存 calibration_state.json；季度(90d)全量重训 + 月度(30d)增量微调
零依赖（纯 math + json），与 fetch_v3.py 零品种逻辑解耦。
"""
import json, os, math, statistics
from datetime import datetime, timedelta

STATE_FILE = "calibration_state.json"
LOG = "calibration_log.json"


def _norm_direction(d):
    """把可能的乱码/变体方向归一为 偏多/偏空/中性。"""
    if not isinstance(d, str):
        return None
    s = d.strip()
    if "多" in s or "bull" in s.lower():
        return "偏多"
    if "空" in s or "bear" in s.lower():
        return "偏空"
    return "中性"


def _load_records(src="backtest_predictions.json"):
    """读已计分记录，返回 [(f_prob, o)]。f=偏多概率, o=实际(1偏多/0偏空)。"""
    if not os.path.exists(src):
        return []
    try:
        data = json.load(open(src, encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    rows = []
    for r in data:
        d = _norm_direction(r.get("direction"))
        if d not in ("偏多", "偏空"):
            continue
        conf = r.get("confidence_pct")
        ac = r.get("actual_chg_pct")
        if conf is None or ac is None:
            continue
        try:
            conf = float(conf)
            ac = float(ac)
        except (TypeError, ValueError):
            continue
        # 原始偏多概率
        f = conf / 100.0 if d == "偏多" else 1 - conf / 100.0
        f = max(1e-4, min(1 - 1e-4, f))
        o = 1.0 if ac > 0 else 0.0
        rows.append((f, o, r))
    return rows


def _logit(p):
    return math.log(p / (1 - p))


def _sigmoid(x):
    if x > 0:
        e = math.exp(-x)
        return 1.0 / (1.0 + e)
    e = math.exp(x)
    return e / (1.0 + e)


def brier_score(rows=None, src="backtest_predictions.json"):
    """Brier = mean((f_i - o_i)^2)。校准良好时 ~0.17；随机50% ~0.25。"""
    if rows is None:
        rows = _load_records(src)
    if not rows:
        return None, 0
    return sum((f - o) ** 2 for f, o, _ in rows) / len(rows), len(rows)


def _grad(A, B, rows):
    """交叉熵损失对 A,B 的梯度。"""
    ga = gb = 0.0
    for f, o, _ in rows:
        z = A * _logit(f) + B
        p = _sigmoid(z)
        g = p - o
        ga += g * _logit(f)
        gb += g
    return ga / len(rows), gb / len(rows)


def platt_fit(rows=None, iters=2000, lr=0.05):
    """梯度下降拟合 Platt A,B（最小化交叉熵）。返回 (A,B,收敛loss)。"""
    if rows is None:
        rows = _load_records()
    rows = [(f, o, r) for f, o, r in rows if r.get("hit") is True or r.get("hit") is False]
    if len(rows) < 8:
        return 1.0, 0.0, None, rows  # 样本不足，退化为恒等(不缩放)
    A, B = 1.0, 0.0
    for _ in range(iters):
        ga, gb = _grad(A, B, rows)
        A -= lr * ga
        B -= lr * gb
    loss = sum((_sigmoid(A * _logit(f) + B) - o) ** 2 for f, o, _ in rows) / len(rows)
    return A, B, loss, rows


def platt_transform(conf_pct, A, B):
    """Platt 缩放：p_cal = sigmoid(A*logit(p_raw)+B)，返回校准后置信度(0-100)。"""
    p = max(1e-4, min(1 - 1e-4, conf_pct / 100.0))
    p_cal = _sigmoid(A * _logit(p) + B)
    return round(max(5, min(95, p_cal * 100)), 1)


def red_card(rows=None, threshold=0.25):
    """Brier>threshold → 红牌。返回 (is_red, brier, n, advice)。
    优先读已训练校准状态中的 last_brier（稳定口径），无状态才实时重算。"""
    st = _load_state()
    brier = st.get("last_brier") if st else None
    n = st.get("n") if st else 0
    if brier is None:
        brier, n = brier_score(rows)
    if brier is None:
        return False, None, 0, "无已计分样本"
    if brier > threshold:
        return True, brier, n, (f"Brier={brier:.3f}>{threshold}：预测劣于随机，暂停采信并回查卡片/数据")
    return False, brier, n, f"Brier={brier:.3f}≤{threshold}：校准良好"


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_state(st):
    json.dump(st, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def train_schedule(now=None):
    """季度重训 + 月度微调调度。返回 (action, state)。"""
    now = now or datetime.now()
    st = _load_state() or {}
    last = st.get("last_train")
    rows = _load_records()
    need_full = False
    if last:
        days = (now - datetime.strptime(last, "%Y-%m-%d %H:%M")).days
        need_full = days >= 90          # 季度全量重训
        micro = days >= 30 and not need_full  # 月度微调
    else:
        need_full = True                 # 首次：全量训练
        micro = False
    if need_full or micro:
        A, B, loss, used = platt_fit(rows)
        brier, _ = brier_score(rows)
        st = {"A": A, "B": B, "loss": loss, "n": len(used),
              "last_brier": round(brier, 4) if brier is not None else None,
              "last_train": now.strftime("%Y-%m-%d %H:%M"),
              "last_mode": "full" if need_full else "micro"}
        _save_state(st)
        action = "full(季度重训)" if need_full else "micro(月度微调)"
        return action, st
    return "skip(未到重训周期)", st


def calibrate(conf_pct):
    """对单条置信度应用最新 Platt 缩放。无状态则原样返回。"""
    st = _load_state()
    if not st or "A" not in st:
        return conf_pct
    return platt_transform(conf_pct, st["A"], st["B"])


def status_report():
    """生成校准状态报告（文本）。"""
    rows = _load_records()
    brier, n = brier_score(rows)
    is_red, _, _, advice = red_card(rows)
    st = _load_state() or {}
    lines = [
        f"已计分样本 n={n}",
        f"Brier={brier:.3f} (0.17优/0.25随机)  {'[红牌]' if is_red else ''}",
        f"校准建议: {advice}",
        f"Platt A={st.get('A')} B={st.get('B')} 上次训练={st.get('last_train')}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(status_report())
    action, st = train_schedule()
    print(f"调度: {action} → A={st.get('A')}, B={st.get('B')}, n={st.get('n')}")
