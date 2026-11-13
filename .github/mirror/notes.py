#!/usr/bin/env python3
"""
找回 NapCat 某个版本在上游 NapNeko/NapCatQQ 的原始 Release 说明。

上游的 Release 已全部删除，按以下顺序找，命中即停：

  1. override   本仓库 release-notes/<tag>.md（手工补的，优先级最高）
  2. generator  该 tag 自带 .github/scripts/generate-release-notes.mjs（v4.18.38 起）
                → 用原脚本按 git 历史重新生成，与原文逐字一致
  3. newreleases  newreleases.io 的存档（v4.18.37 及以前是发布时 AI 生成的，只能从存档找回）
                有 NEWRELEASES_API_KEY 走 API，否则抓页面
  4. wayback    Wayback Machine 存档的 GitHub Release 页面
  5. fallback   都没有：按提交记录生成，并在开头写明“原始说明未能找回”

用法:
  notes.py --tag v4.17.23 --repo-dir . --out notes/v4.17.23.md [--link-repo NapNeko/NapCatQQ]
标准输出打印命中的来源名，供工作流写进汇总表。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

UPSTREAM = 'NapNeko/NapCatQQ'
UA = 'Mozilla/5.0 (compatible; napcat-release-mirror/1.0)'
FALLBACK_BANNER = '> ⚠️ 上游原始 Release 说明已无法找回，以下内容根据提交记录重新生成。\n\n'


def log (*args) -> None:
    print(*args, file=sys.stderr)


def http_get (url: str, headers: dict | None = None, timeout: int = 40, tries: int = 3) -> str | None:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as err:
            if err.code in (401, 403, 404, 410):
                log(f'  GET {url} -> HTTP {err.code}')
                return None
            last = err
            if err.code == 429:
                wait = err.headers.get('Retry-After') or '30'
                time.sleep(min(int(wait) if wait.isdigit() else 30, 120))
                continue
        except Exception as err:  # noqa: BLE001 - 网络错误一律重试
            last = err
        time.sleep(3 * (attempt + 1))
    log(f'  GET {url} failed: {last}')
    return None


def git (repo_dir: str, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=repo_dir, capture_output=True, text=True, check=True).stdout


def html_to_markdown (fragment: str) -> str:
    result = subprocess.run(
        ['pandoc', '-f', 'html', '-t', 'gfm-raw_html', '--wrap=none'],
        input=fragment, capture_output=True, text=True, check=True,
    )
    return result.stdout


def run_generator (script_source: str, tag: str, repo_dir: str) -> str:
    """用 generate-release-notes.mjs 生成说明（链接统一指向上游，之后再按需改写）。"""
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, 'generate-release-notes.mjs')
        out = os.path.join(tmp, 'notes.md')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write(script_source)
        subprocess.run(['node', script, tag, UPSTREAM, out], cwd=repo_dir, check=True,
                       stdout=sys.stderr, stderr=sys.stderr)
        with open(out, encoding='utf-8') as fh:
            return fh.read()


# ---------------------------------------------------------------- sources

def from_override (tag: str, override_dir: str) -> str | None:
    path = os.path.join(override_dir, f'{tag}.md')
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        return text if text.strip() else None
    return None


def from_tag_generator (tag: str, repo_dir: str) -> str | None:
    try:
        source = git(repo_dir, 'show', f'refs/tags/{tag}:.github/scripts/generate-release-notes.mjs')
    except subprocess.CalledProcessError:
        return None  # v4.18.38 之前没有这个脚本
    return run_generator(source, tag, repo_dir)


def find_note_container (html: str, tag: str):
    """
    在 newreleases.io 页面里定位说明正文：同时包含版本号和 /compare/ 链接、
    且不含导航/页眉/页脚/表单的最小元素。不依赖站点的 class 命名。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, 'html.parser')
    for junk in soup(['script', 'style', 'noscript', 'svg', 'template']):
        junk.decompose()
    version = tag.lstrip('vV')
    best = None
    best_len = None
    for el in soup.find_all(['div', 'section', 'article', 'main', 'td']):
        if el.find(['nav', 'header', 'footer', 'form']):
            continue
        if not el.find('a', href=re.compile(r'/compare/')):
            continue
        text = el.get_text(' ', strip=True)
        if version not in text or len(text) < 40:
            continue
        if best_len is None or len(text) < best_len:
            best, best_len = el, len(text)
    return best


def from_newreleases (tag: str) -> str | None:
    key = os.environ.get('NEWRELEASES_API_KEY', '').strip()
    if key:
        body = http_get(
            f'https://api.newreleases.io/v1/projects/github/{UPSTREAM}/releases/{urllib.parse.quote(tag)}/note',
            {'X-Key': key, 'Accept': 'application/json'},
        )
        if body:
            try:
                message = json.loads(body).get('message') or ''
            except json.JSONDecodeError:
                message = ''
            if message.strip():
                return html_to_markdown(message)
    page = http_get(f'https://newreleases.io/project/github/{UPSTREAM}/release/{urllib.parse.quote(tag)}')
    if not page:
        return None
    container = find_note_container(page, tag)
    return html_to_markdown(str(container)) if container is not None else None


def from_wayback (tag: str) -> str | None:
    from bs4 import BeautifulSoup

    target = f'github.com/{UPSTREAM}/releases/tag/{tag}'
    info = http_get('https://archive.org/wayback/available?url=' + urllib.parse.quote(target, safe=''))
    if not info:
        return None
    try:
        snap = json.loads(info).get('archived_snapshots', {}).get('closest') or {}
    except json.JSONDecodeError:
        return None
    if not snap.get('available') or not snap.get('timestamp'):
        return None
    # id_ = 原始页面，不改写链接
    page = http_get(f'https://web.archive.org/web/{snap["timestamp"]}id_/https://{target}')
    if not page:
        return None
    soup = BeautifulSoup(page, 'html.parser')
    body = soup.select_one('[data-test-selector="body-content"]') or soup.select_one('.markdown-body')
    return html_to_markdown(str(body)) if body is not None else None


def from_commits (tag: str, repo_dir: str, generator_path: str) -> str:
    try:
        with open(generator_path, encoding='utf-8') as fh:
            return FALLBACK_BANNER + run_generator(fh.read(), tag, repo_dir)
    except Exception as err:  # noqa: BLE001
        log(f'  generator fallback failed: {err}')
    return FALLBACK_BANNER + f'# NapCat {tag}\n'


# ---------------------------------------------------------------- main

def rewrite_links (text: str, link_repo: str) -> str:
    """只改 commit / compare / tag 链接（fork 里哈希一致）；issue、PR 编号在 fork 里对不上，保持原样。"""
    if link_repo.lower() == UPSTREAM.lower():
        return text
    return re.sub(rf'https://github\.com/{re.escape(UPSTREAM)}/(commit|compare|tree|releases/tag)/',
                  rf'https://github.com/{link_repo}/\1/', text, flags=re.IGNORECASE)


def main () -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--repo-dir', default='.')
    parser.add_argument('--out', required=True)
    parser.add_argument('--link-repo', default=UPSTREAM)
    parser.add_argument('--override-dir', default='release-notes')
    parser.add_argument('--generator', default='.github/scripts/generate-release-notes.mjs',
                        help='兜底时用来按提交记录生成说明的脚本')
    parser.add_argument('--skip-network', action='store_true', help='不访问 newreleases / wayback（测试用）')
    args = parser.parse_args()

    tag = args.tag
    steps = [
        ('override', lambda: from_override(tag, os.path.join(args.repo_dir, args.override_dir))),
        ('generator', lambda: from_tag_generator(tag, args.repo_dir)),
    ]
    if not args.skip_network:
        steps += [('newreleases', lambda: from_newreleases(tag)), ('wayback', lambda: from_wayback(tag))]

    text, source = None, None
    for name, fetch in steps:
        try:
            text = fetch()
        except Exception as err:  # noqa: BLE001 - 某个来源出错就换下一个
            log(f'  [{tag}] {name} error: {err}')
            text = None
        if text and text.strip():
            source = name
            break
    if not source:
        text = from_commits(tag, args.repo_dir, os.path.join(args.repo_dir, args.generator))
        source = 'fallback'

    text = rewrite_links(text, args.link_repo).rstrip() + '\n'
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as fh:
        fh.write(text)
    log(f'  [{tag}] notes from {source}')
    print(source)
    return 0


if __name__ == '__main__':
    sys.exit(main())
