#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy_lead.py —— 铅看板 P5 一键部署脚本（无需 git CLI）
============================================================================
用途：把 lead-dashboard 本地目录一次性推送到 GitHub 仓库 pb-dashboard，建仓 + 上传全部文件。
用法（PowerShell）：
    set GITHUB_TOKEN=ghp_你的TOKEN
    python deploy_lead.py --create
或直接改下面 TOKEN 变量（不推荐，token 会明文写进文件）。

前提：
    1. 目标仓库为 public 仓库 pb-dashboard（同名已存在则自动续传）
       （或本脚本传入 --create 自动建仓，需 token 有 repo 权限）
    2. GITHUB_TOKEN 为有效 classic PAT，scope 至少 = repo

流程：
    - 可选 --create 自动建 public 仓库
    - 遍历本地 staging 目录，跳过 .gitignore 排除项，逐个 Contents API 上传
    - 打印每个文件上传结果
    - 结束后提示：去开 Pages + Actions 手动首跑
============================================================================
"""
import os
import sys
import json
import base64
import urllib.request
import urllib.error

REPO = "algo23-yunqingtian/pb-dashboard"
BRANCH = "main"
API = "https://api.github.com/repos/%s/contents" % REPO
REPOS_API = "https://api.github.com/user/repos"
LOCAL_ROOT = os.path.dirname(os.path.abspath(__file__))

# .gitignore 对应要跳过的路径（与 .gitignore 保持一致）
IGNORE_DIRS = {"__pycache__", ".pytest_cache", "venv", ".git", ".github"}
IGNORE_FILES = {"pb_input.json", "pb_output_rule.json", "predictions.json"}

# 明确要上传的顶层文件（白名单优先于黑名单，排除临时产物）
# 用 None 表示"读取 .gitignore 之外的剩余文件"
FILE_WHITELIST = None


def _req(method, url, token, data=None):
    headers = {
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "lead-deploy",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace"), resp.status
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace"), e.code
    except Exception as e:  # noqa
        return str(e), 0


def create_repo(token):
    """自动建 public 仓库（若 token 有 repo 权限）。返回 (ok, msg)。"""
    data = {
        "name": "pb-dashboard",
        "description": "PB (Lead) metal analysis dashboard - automated via GitHub Actions",
        "private": False,
        "auto_init": True,
    }
    content, status = _req("POST", REPOS_API, token, data)
    if status in (200, 201):
        return True, "repo created"
    if status == 422:
        return False, "repo likely exists (422)"  # 已存在，继续上传
    return False, "create failed %d: %s" % (status, content[:200])


def collect_files(root, ignore_dirs, ignore_files):
    """递归收集要上传的相对路径。目录本身不传，只传文件。"""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 排除 git 目录与缓存
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel in ignore_files or os.path.basename(rel) in ignore_files:
                continue
            files.append(rel)
    return sorted(files)


def push_file(token, local_path, repo_path, message):
    get_url = "%s/%s?ref=%s" % (API, repo_path, BRANCH)
    content, status = _req("GET", get_url, token)
    sha = json.loads(content).get("sha") if status == 200 else None

    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    data = {"message": message, "content": b64, "branch": BRANCH}
    if sha:
        data["sha"] = sha

    content, status = _req("PUT", "%s/%s" % (API, repo_path), token, data)
    if status in (200, 201):
        return True, ""
    if status == 409:  # 并发冲突重试一次
        c2, s2 = _req("GET", get_url, token)
        if s2 == 200:
            data["sha"] = json.loads(c2).get("sha")
            c3, s3 = _req("PUT", "%s/%s" % (API, repo_path), token, data)
            if s3 in (200, 201):
                return True, ""
            return False, "%d %s" % (s3, c3[:150])
    return False, "%d %s" % (status, content[:150])


def main():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("ERROR: 请先 `set GITHUB_TOKEN=ghp_xxx` 再运行")

    if "--create" in sys.argv:
        ok, msg = create_repo(token)
        print("[create-repo]", msg)
    else:
        print("[note] 未传 --create，假定仓库已存在（或传入 --create 自动建仓）")

    # 收集文件
    files = collect_files(LOCAL_ROOT, IGNORE_DIRS, IGNORE_FILES)
    print("待上传文件 %d 个：" % len(files))
    for f in files:
        print("   ", f)

    ok_cnt, fail_cnt = 0, 0
    for f in files:
        local = os.path.join(LOCAL_ROOT, f)
        ok, err = push_file(token, local, f, "deploy lead-dashboard (init)")
        if ok:
            ok_cnt += 1
        else:
            fail_cnt += 1
            print("  FAIL %s : %s" % (f, err))
    print("完成：成功 %d，失败 %d" % (ok_cnt, fail_cnt))

    if ok_cnt and not fail_cnt:
        print("\n全部上传成功！下一步：")
        print("  1) Settings → Pages → Source 选 GitHub Actions")
        print("  2) Actions → Fetch Lead Data → Run workflow（手动首跑）")
        print("  3) 看板上线后每 30min 自动刷新")


if __name__ == "__main__":
    main()
