#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collab/push.py —— 铅看板 GitHub 推送助手（无需 git CLI）

用法 (PowerShell):
    set GITHUB_TOKEN=ghp_xxx
    python collab/push.py <本地相对路径> <仓库内路径> "<commit message>"

例:
    python collab/push.py index.html index.html "feat: 前端改版"
    python collab/push.py fetch_v3.py fetch_v3.py "fix: 引擎容错"

说明:
    - 自动 GET 现有 sha；存在则更新(PUT+sha)，不存在则新建(PUT)。
    - 遇 409 冲突自动重新拉取 sha 重试一次。
    - token 只从环境变量 GITHUB_TOKEN 读取，绝不写死。
"""
import os
import sys
import json
import base64
import urllib.request
import urllib.error

REPO = "algo23-yunqingtian/lead-dashboard"
BRANCH = "main"
API = "https://api.github.com/repos/%s/contents" % REPO


def _req(method, url, token, data=None):
    headers = {
        "Authorization": "token " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode("utf-8"), resp.status
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace"), e.code


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
        print("PUSHED %s (%d)" % (repo_path, status))
        return
    if status == 409:  # 并发冲突：重拉 sha 再试一次
        c2, s2 = _req("GET", get_url, token)
        if s2 == 200:
            data["sha"] = json.loads(c2).get("sha")
            c3, s3 = _req("PUT", "%s/%s" % (API, repo_path), token, data)
            if s3 in (200, 201):
                print("PUSHED(retry) %s (%d)" % (repo_path, s3))
            else:
                print("FAIL %s %d: %s" % (repo_path, s3, c3[:200]))
        else:
            print("FAIL %s conflict, sha reget failed" % repo_path)
        return
    print("FAIL %s %d: %s" % (repo_path, status, content[:200]))


if __name__ == "__main__":
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        sys.exit("ERROR: 请先设置环境变量 GITHUB_TOKEN")
    if len(sys.argv) < 4:
        sys.exit('用法: python collab/push.py <本地路径> <仓库路径> "<commit message>"')
    push_file(tok, sys.argv[1], sys.argv[2], sys.argv[3])
