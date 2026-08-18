# lead-dashboard 部署就绪说明（P5）

本地 staging 目录：`c:\Users\YAQH\CodeBuddy\20260817132404\lead-dashboard\`
目标仓库：`github.com/algo23-yunqingtian/lead-dashboard`（public，GitHub Pages）

## 已就绪内容（本地全部完成、本地实测通过）

| 文件 | 作用 |
|---|---|
| `assemble_dashboard.py` | 抓取铅实时数据(zhiji 观/料/讯) → 组装 `data.json`，本地实测通过（方向偏多@P3，L0=偏多，L4仲裁含时间横切） |
| `fetch_v3.py` + 9个依赖模块 | 铅分析引擎（L3打分+L4仲裁+L5新闻+P0-P4全部升级） |
| `registry/PB.json` `PB_card.yaml` | 序列注册 + v0.5 最小化卡 |
| `pb_history.json` | L0 领先信号历史源（0.14MB） |
| `index.html` | 铅看板前端（自研轻量版，无静态依赖除 Chart.js CDN） |
| `.github/workflows/fetch.yml` | cron 每30min 跑 assemble_dashboard → 更新 data.json |
| `.github/workflows/deploy.yml` | main 推送触发 → 部署 GitHub Pages |
| `collab/push.py` | 无 git CLI 的 GitHub Contents API 推送助手 |
| `requirements.txt` | 唯一依赖 PyYAML |

## 唯一剩余动作：提供一个 GitHub PAT

> 环境里当前**没有** GITHUB_TOKEN，且 zinc 旧 PAT 已暴露需 revoke，**不能复用**。
> 需要一个新 classic PAT，scope = `repo`（至少 `repo`，如需配 Secrets 用 `workflow` 也行）。

拿到 PAT 后，按下面顺序操作（一次性 ~5 分钟，之后全自动）：

### 1. 在 GitHub 建仓库
https://github.com/new → 仓库名 `lead-dashboard`，Public，不要勾选任何初始化文件。

### 2. 本地推送全部文件（一键脚本）
```powershell
cd "c:\Users\YAQH\CodeBuddy\20260817132404\lead-dashboard"
set GITHUB_TOKEN=ghp_你的新TOKEN
python deploy_lead.py --create   # 自动建 public 仓库 + 上传全部文件（一次搞定）
```
> 若仓库已手动建好，去掉 `--create` 直接 `python deploy_lead.py` 即可。
> 脚本自动跳过 .gitignore 排除的中间产物（pb_input.json 等），只传正式文件。

（或用 collab/push.py 逐文件推 / GitHub 网页端 Upload files 拖入，二选一。）

### 3. 配置 GitHub Actions Secrets（可选但推荐，避免密钥明文在 workflow）
Settings → Secrets and variables → Actions 添加：
- `GUAN_KEY` = `guan_a3dbade5e217468006af273fdc772f91`
- `NEWS_KEY` = `nws_f5b4b6c653104d0f965fb3463dcf7eed`
- `DATA_KEY` = `data_8e863643ecc13f11d2c669bdb672f7db`

不配也没关系：workflow 里已有明文 fallback（与 zinc 相同做法），只是密钥会公开在仓库里。

### 4. 开启 GitHub Pages
Settings → Pages → Source 选 `GitHub Actions`（deploy.yml 会自动接管）。

### 5. 触发首次运行
Actions → Fetch Lead Data → Run workflow（手动触发一次），跑完会自动 push data.json → 触发 Deploy Pages → 看板上线。

## 日常维护
- 数据每 30min 自动刷新；方向/置信度/L0/仲裁/新闻/时效自动更新。
- 改引擎/前端：改本地 → `collab/push.py` 推送 → Actions 自动生效（≤30min）。
- 回测/L0 重训：替换 `pb_history.json` 并改 `lead_signal.py` 参数。

## 已知边界
- 前端读取 `data.json`（同目录 fetch），GitHub Pages 下相对路径 fetch 正常。
- zhiji 密钥明文 fallback 与 zinc 一致；若想彻底隐藏，务必配 Secrets（第3步）。
- assemble_dashboard 不走 LLM（规则+L4仲裁），输出稳定可复现，无 key 依赖。
