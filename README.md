# 铅(PB) 基本面看板

> **可视化网页（点击查看，不是本仓库文件列表）：**
> ## 🔗 https://algo23-yunqingtian.github.io/pb-dashboard/
>
> 本仓库是看板的**源码 + 数据枢纽**（`main` 分支里放着 `index.html`、分析引擎和每 30 分钟自动生成的 `data.json`）。
> **直接看 `github.com/.../pb-dashboard` 只会看到代码文件**；真正的图表与分析页是上面那个 GitHub Pages 链接。
> 网页每 30 分钟由云端 Actions 自动刷新（拉 zhiji 数据 → 重算 → 重新部署）。

实时铅基本面看板，L0–L6 全链路分析，数据源 zhiji（观 / 料 / 讯）。

## 看板里能看到什么（L0 → L6 全要素）

| 层级 | 内容 | 数据源 |
|------|------|--------|
| L0 | 已验证领先信号：伦铅→沪铅（OOS 唯一真领先，命中 0.652 / 夏普 5.05） | zhiji 观 |
| L1 | 行情确定性画像（auto_profile，零品种逻辑统计：z分数/趋势/波动/信号） | zhiji 观 kline |
| L2 | 数据治理·时效审计（17 条序列逐条新鲜度、滞后标记） | zhiji 料 |
| L3 | 信号打分明细（逐条命中，非抵消：方向/优先级/强度/证据） | zhiji 料 |
| L4 | 仲裁裁决（同花顺 4 层优先级 + 时间维度横切 + 强化/证伪触发） | 规则引擎 |
| L5 | 新闻分桶（key_bull/bear/neutral）+ 机构近似共识 | zhiji 讯 |
| L6 | 校准与置信（规则引擎可复现性 + L0 OOS 验证） | 规则引擎 |
| 全文 | 完整分析正文（引擎生成的 L0–L6 叙述 + 数据来源） | 以上聚合 |

## 架构

- `index.html`：前端（Chart.js 绘制日线 + 全层级渲染）
- `assemble_dashboard.py`：抓数据 → 组装 `data.json`
- `fetch_v3.py` + 9 模块：铅分析引擎
- `.github/workflows/fetch.yml`：**单一工作流**（取数 + 部署合并，带并发锁，避免 Pages 并发冲突）
  - `schedule: */30` 每 30 分钟跑一次
  - 取数 → 组装 `data.json` → push `main` → 上传 Pages 产物 → 部署
- 密钥：zhiji 三套 key（GUAN/NEWS/DATA）走 GitHub Secrets，缺失时回退到文件内明文

## 当前分析结论要点

- L4 仲裁：**偏多 @P3 供应端 > 需求端**（TC 收缩 / 再生开工降 / 成本支撑 vs 库存累库）
- L0 已验证领先：伦铅→沪铅 20 日反向预示（OOS 命中 0.652, 二项 p=0.0138）
- 其余人类叙事关系 OOS 多数 ≤0.5（共动非因果），已据实剔除，不作领先驱动

## 部署

见 `DEPLOY_README.md`。
