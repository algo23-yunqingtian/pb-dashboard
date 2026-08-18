# 铅(PB) 基本面看板

实时铅基本面看板，GitHub Pages 部署。数据源 zhiji（观/料/讯），每 30 分钟自动刷新。

## 架构
- `assemble_dashboard.py`：抓数据 → 组装 `data.json`
- `fetch_v3.py` + 9 模块：铅分析引擎（L3 打分非抵消 + L4 同花顺仲裁 + L5 新闻分桶 + L0 唯一真领先信号）
- `index.html`：前端（Chart.js 绘制日线）
- `.github/workflows/`：fetch.yml（定时取数）+ deploy.yml（Pages 部署）

## 分析结论要点（当前）
- L4 仲裁：偏多@P3 供应端>需求端（TC收缩/再生开工降/成本支撑 vs 库存累库）
- L0 已验证领先：伦铅→沪铅 20日反向预示（OOS命中0.652, p=0.0138）

## 部署
见 `DEPLOY_README.md`。
