#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zotero-mineru 核心管线(纯 Python 3 标准库,零第三方依赖,兼容系统 python3.9)。

职责:
  Zotero 本地 API 读取带 todo-parse 标签的条目
  -> 定位 PDF -> MinerU 云端 v4 批量解析(申请上传链接 -> PUT -> 轮询 -> 下载 zip)
  -> 剔除参考文献章节 -> 写 YAML 头 + markdown 到 ~/Documents/Zotero_MD_Output
  -> PATCH 回写 Zotero 标签(todo-parse -> parsed-done)

子命令:
  run            执行解析(日常入口)
  preflight      只读体检:逐项检查环境并给出修复指引
  auth           与 Zotero 本地 API 授权(需在弹窗点「始终允许」)
  set-token      把 MinerU token 写入 ~/.config/zotero-mineru/config.json(chmod 600)

说明:
  - Zotero 本地 API 请求一律绕过系统代理(127.0.0.1 直连),MinerU 请求走系统代理。
  - 防重复靠 state.json(条目 key + PDF 内容哈希),与「md 已存在」双保险。
  - 任何 token 都不出现在本文件或 shell 包装脚本里。
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

VERSION = "1.0.0"
APP_NAME = "zotero-mineru-pipeline"

CONFIG_DIR = os.path.expanduser(os.getenv(
    "ZOTERO_MINERU_CONFIG_DIR", "~/.config/zotero-mineru"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")

OUTPUT_DIR = os.path.expanduser(os.getenv(
    "ZOTERO_MINERU_OUTPUT_DIR", "~/Documents/Zotero_MD_Output"))

ZOTERO_BASE = os.getenv("ZOTERO_LOCAL_API_BASE", "http://127.0.0.1:23119")
ZOTERO_API = ZOTERO_BASE + "/api/users/0"

MINERU_HOST = "https://mineru.net"
MINERU_FILE_URLS = MINERU_HOST + "/api/v4/file-urls/batch"
MINERU_BATCH_RESULTS = MINERU_HOST + "/api/v4/extract-results/batch"

TAG_TODO = "todo-parse"      # 注意:ASCII 连字符 '-'。旧脚本里是 U+2011 非断字连字符,永远匹配不上
TAG_DONE = "parsed-done"

BATCH_SIZE = 50              # MinerU 单次申请上传链接上限
POLL_INITIAL = 8             # 轮询起步间隔(秒),指数退避
POLL_MAX = 60
POLL_TIMEOUT = 1800          # 单批最长轮询 30 分钟

# 参考文献章节标题(独立成行、允许 1-4 级 markdown 标题前缀)
REF_HEADING_RE = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:"
    r"references\s*cited|references|bibliography|"
    r"参考文獻|參考文獻|参考文献|参考资料|參考資料"
    r")\s*[:.]?\s*$",
    re.IGNORECASE)


# ---------------------------------------------------------------- HTTP 基础

def _is_localhost(url):
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def _build_opener():
    """Zotero 本地 API(127.0.0.1)直连、绕过系统代理;其余请求走系统代理。
    注意:urllib 里 ProxyHandler({}) 并不会关闭代理,必须显式设 no_proxy。"""
    if _is_localhost(ZOTERO_BASE):
        return urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"no_proxy": "127.0.0.1,localhost,::1"}),
            urllib.request.HTTPSHandler())
    return urllib.request.build_opener(
        urllib.request.ProxyHandler(), urllib.request.HTTPSHandler())


class HTTPError(Exception):
    def __init__(self, status, body):
        super().__init__("HTTP %s" % status)
        self.status = status
        self.body = body


def http_request(url, method="GET", headers=None, data=None, timeout=120,
                 opener=None):
    """发请求;返回 (status, response_headers, body_bytes)。
    HTTPError(4xx/5xx) 也会把状态码和响应体抛出来,便于区分 401/403/428。"""
    opener = opener or _build_opener()
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = e.read()
        raise HTTPError(e.code, body)
    except urllib.error.URLError as e:
        raise RuntimeError("网络错误: %s (URL: %s)" % (e.reason, url))
    with resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def http_json(url, method="GET", headers=None, data=None, timeout=120):
    """发请求并按 JSON 解析。失败抛 HTTPError/RuntimeError。"""
    if data is not None and not isinstance(data, (bytes, str)):
        data = json.dumps(data).encode("utf-8")
    status, resp_headers, body = http_request(
        url, method=method, headers=headers, data=data, timeout=timeout)
    try:
        parsed = json.loads(body.decode("utf-8")) if body else None
    except ValueError:
        raise RuntimeError("响应不是 JSON: %s" % body[:200])
    return status, resp_headers, parsed


# ---------------------------------------------------------------- 配置与状态

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(CONFIG_PATH, 0o600)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def save_state(state):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- Zotero 本地 API

class ZoteroAuthNeeded(Exception):
    pass


def zotero_request(path, method="GET", data=None, auth=True, cfg=None,
                   version=None):
    """调 Zotero 本地 API。写请求自动带 Zotero-API-Key + Zotero-Server-ID
    + If-Unmodified-Since-Version。"""
    cfg = cfg if cfg is not None else load_config()
    headers = {}
    if auth and (cfg.get("local_api_key") or method != "GET"):
        headers["Zotero-API-Key"] = cfg.get("local_api_key", "")
    if cfg.get("server_id"):
        headers["Zotero-Server-ID"] = cfg["server_id"]
    if version is not None:
        headers["If-Unmodified-Since-Version"] = str(version)
    if data is not None:
        headers["Content-Type"] = "application/json"
    status, resp_headers, body = http_request(
        ZOTERO_API + path, method=method, headers=headers, data=data, timeout=30)
    if status == 401:
        raise ZoteroAuthNeeded("本地 API key 无效或已过期,需重新授权")
    if status in (428, 412):
        raise ZoteroAuthNeeded(
            "本地 API 需要 Zotero-Server-ID / 版本头(%s),需重新授权" % status)
    parsed = json.loads(body.decode("utf-8")) if body else None
    return parsed, resp_headers


def zotero_authorize():
    """POST /api/local/authorize:Zotero 会弹授权窗。
    用户点「始终允许」得持久 key;点「允许」得一次性 key(用完即焚,之后 401 再授权)。
    授权 POST 属于写请求,需带 Zotero-Server-ID —— 先 GET 一次拿到。"""
    print("正在向 Zotero 发起授权请求…")
    print(">>> 请切到 Zotero 窗口,在弹窗中点击「始终允许(Always Allow)」<<<")
    server_id = ""
    try:
        _, ping_headers, _ = http_request(ZOTERO_API + "/items?limit=1",
                                          timeout=10)
        server_id = ping_headers.get("Zotero-Server-ID",
                                     ping_headers.get("zotero-server-id", ""))
    except HTTPError:
        pass   # 拿不到也继续试,让 POST 自己报 428
    data = json.dumps({"appName": APP_NAME}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if server_id:
        headers["Zotero-Server-ID"] = server_id
    status, resp_headers, body = http_request(
        ZOTERO_BASE + "/api/local/authorize", method="POST",
        headers=headers, data=data, timeout=120)
    if status == 403:
        raise SystemExit("❌ 你在弹窗中点了「拒绝」,未授权。需要时重跑 auth。")
    if status != 200:
        raise RuntimeError("授权失败: HTTP %s %s" % (status, body[:200]))
    parsed = json.loads(body.decode("utf-8"))
    key = parsed.get("key")
    if not key:
        raise RuntimeError("授权响应缺少 key: %s" % parsed)
    cfg = load_config()
    cfg["local_api_key"] = key
    cfg["server_id"] = resp_headers.get("Zotero-Server-ID",
                                        resp_headers.get("zotero-server-id", ""))
    cfg.setdefault("remember", bool(parsed.get("remember")))
    save_config(cfg)
    if cfg["remember"]:
        print("✅ 已授权(持久),key 与 server-id 已保存到 %s" % CONFIG_PATH)
    else:
        print("⚠️ 你选的是「允许一次」:这个 key 用完一次即失效,下次会再次弹窗。")


def zotero_find_profile_dir():
    """找到 Zotero profile 目录(用于解析 storage: 附件路径)。"""
    base = os.path.expanduser("~/Library/Application Support/Zotero/Profiles")
    if not os.path.isdir(base):
        return None
    for entry in sorted(os.listdir(base)):
        p = os.path.join(base, entry)
        if os.path.isdir(p):
            return p
    return None


def zotero_find_data_dir():
    """Zotero 数据目录:优先读 prefs.js 的 extensions.zotero.dataDir
    (用户可能自定义,如 ~/Zotero),缺省为 ~/Library/Application Support/Zotero。"""
    default = os.path.expanduser("~/Library/Application Support/Zotero")
    profile = zotero_find_profile_dir()
    prefs = os.path.join(profile, "prefs.js") if profile else None
    if prefs and os.path.isfile(prefs):
        try:
            with open(prefs, "r", encoding="utf-8") as f:
                m = re.search(r'extensions\.zotero\.dataDir"\s*,\s*"([^"]+)"',
                              f.read())
            if m:
                return os.path.expanduser(m.group(1))
        except OSError:
            pass
    return default


def zotero_item_tags(item):
    return [t.get("tag", "") for t in (item.get("data", {}).get("tags") or [])]


def zotero_fetch_todo_items():
    """取带 todo-parse 标签的顶层条目(排除附件/笔记等子项)。
    ?tag= 过滤不可靠时退回全量扫描兜底。"""
    def _has_todo(it):
        return TAG_TODO in zotero_item_tags(it) and \
            it.get("data", {}).get("itemType") not in ("attachment", "note")

    items, _ = zotero_request("/items?tag=" + urllib.parse.quote(TAG_TODO),
                              method="GET", auth=False)
    if items:
        return items
    # 兜底:全量扫描(本地 API 默认不分页,很快)
    all_items, _ = zotero_request("/items", method="GET", auth=False)
    return [it for it in all_items if _has_todo(it)]


def zotero_pdf_attachments(item_key):
    """返回该条目的 PDF 附件列表 [{key, filename, path, linkMode}, ...]。
    只收集 PDF 附件,导入文件(imported_file)排在链接(imported_url)前面。"""
    children, _ = zotero_request(
        "/items/%s/children?itemType=attachment" % item_key, auth=False)
    pdfs = []
    for ch in children:
        data = ch.get("data", {})
        is_pdf = data.get("contentType") == "application/pdf" or \
            (data.get("filename") or "").lower().endswith(".pdf")
        if not is_pdf:
            continue
        pdfs.append({"key": ch.get("key"), "filename": data.get("filename"),
                     "path": data.get("path"),
                     "linkMode": data.get("linkMode")})
    pdfs.sort(key=lambda a: 0 if a["linkMode"] == "imported_file" else 1)
    return pdfs


def resolve_pdf_local_path(profile_dir, att):
    """把附件条目转成本地文件路径。按可能性依次尝试:
    1) <dataDir>/storage/<附件key>/<filename>   —— 最可靠,不依赖 path 字段
    2) <dataDir>/storage/<path 的 storage: 部分>
    3) <dataDir>/assets/... 与 <profile>/assets|storage/... —— 其他布局
    找不到返回 None(调用方回退到 /file 接口下载)。"""
    data_dir = zotero_find_data_dir()
    filename = att.get("filename")
    candidates = []

    if att.get("key") and filename:
        candidates.append(os.path.join(data_dir, "storage",
                                        att["key"], filename))
        candidates.append(os.path.join(data_dir, "assets",
                                        att["key"], filename))
    raw = att.get("path") or ""
    if raw.startswith(("storage:", "assets:")):
        name = raw.split(":", 1)[1]
        candidates.append(os.path.join(data_dir, "storage", name))
        candidates.append(os.path.join(data_dir, "assets", name))
        if profile_dir:
            candidates.append(os.path.join(profile_dir, "assets", name))
            candidates.append(os.path.join(profile_dir, "storage", name))

    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def zotero_download_pdf(item_key, dest):
    """回退方案:GET /items/{key}/file 落盘。本地 API 会 302 到 file:// 或
    http(s) 地址,手动跟随;若附件根本没有本地文件,返回 None(由调用方提示)。"""
    opener = _build_opener()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    no_redir = urllib.request.build_opener(_NoRedirect)
    # 直接用底层 opener 发请求,不自动跟随重定向
    req = urllib.request.Request(ZOTERO_API + "/items/%s/file" % item_key,
                                 method="GET")
    try:
        resp = no_redir.open(req, timeout=120)
    except urllib.error.HTTPError as e:
        status, resp_headers, _ = e.code, dict(e.headers.items()), e.read()
    except urllib.error.URLError as e:
        raise RuntimeError("网络错误: %s" % e.reason)
    else:
        status, resp_headers = resp.status, dict(resp.headers.items())
        resp.close()

    if status in (301, 302, 303, 307, 308):
        loc = resp_headers.get("Location") or resp_headers.get("location") or ""
        if not loc:
            return None   # 附件没有本地文件可下载
        if loc.startswith("file://"):
            local = urllib.request.url2pathname(
                urllib.parse.urlparse(loc).path)
            if os.path.isfile(local):
                with open(local, "rb") as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                return dest
            return None
        # 远程地址:走系统代理拉取
        status2, _, body = http_request(loc, timeout=300, opener=opener)
        if status2 != 200 or not body:
            return None
        with open(dest, "wb") as f:
            f.write(body)
        return dest
    if status != 200:
        return None   # 附件没有本地文件可下载
    # 非重定向的正常 200:直接拉字节(本地 API 一般不走到这里)
    _, _, body = http_request(ZOTERO_API + "/items/%s/file" % item_key,
                              timeout=300, opener=opener)
    if not body:
        return None
    with open(dest, "wb") as f:
        f.write(body)
    return dest


def zotero_update_tags(item_key, item, cfg):
    """PATCH 回写标签:去掉 todo-parse,加上 parsed-done(去重、保持其余标签)。"""
    data = item.get("data", {})
    existing = data.get("tags") or []
    tags = [t for t in existing if t.get("tag") != TAG_TODO]
    if not any(t.get("tag") == TAG_DONE for t in tags):
        tags.append({"tag": TAG_DONE})
    version = data.get("version")
    # 单条 PATCH 直接改 tags 字段(local API 支持 web API v3 语义)
    payload = {"tags": tags}
    try:
        zotero_request("/items/%s" % item_key, method="PATCH",
                       data=json.dumps(payload).encode("utf-8"),
                       cfg=cfg, version=version)
    except ZoteroAuthNeeded:
        raise
    except HTTPError as e:
        raise RuntimeError("标签回写失败: HTTP %s %s" % (e.status, e.body[:200]))


def zotero_add_tag(item_key, item, tag, cfg=None):
    """给条目追加一个标签(保留原有标签)。"""
    cfg = cfg if cfg is not None else load_config()
    data = item.get("data", {})
    tags = list(data.get("tags") or [])
    if not any(t.get("tag") == tag for t in tags):
        tags.append({"tag": tag})
    version = data.get("version")
    try:
        zotero_request("/items/%s" % item_key, method="PATCH",
                       data=json.dumps({"tags": tags}).encode("utf-8"),
                       cfg=cfg, version=version)
    except ZoteroAuthNeeded:
        raise
    except HTTPError as e:
        raise RuntimeError("打标签失败: HTTP %s %s" % (e.status, e.body[:200]))


def cmd_tag(keyword, all_flag):
    """按标题关键词给文献打 todo-parse 标签(不用开 Zotero 界面)。"""
    items, _ = zotero_request("/items", method="GET", auth=False)
    kw = (keyword or "").lower()
    matches = [it for it in items
               if kw in ((it.get("data") or {}).get("title") or "").lower()
               and (it.get("data") or {}).get("itemType")
               not in ("attachment", "note")]
    if not matches:
        print("❌ 没有标题包含「%s」的条目" % keyword)
        return 1
    if len(matches) > 1 and not all_flag:
        print("找到 %d 篇,请用更精确的关键词,或加 --all 全部打标:" % len(matches))
        for i, it in enumerate(matches[:20], 1):
            d = it.get("data", {})
            print("  %d. (%s) %s" % (i, (d.get("date") or "")[:4],
                                     d.get("title")))
        return 1
    cfg = load_config()
    for it in matches:
        d = it.get("data", {})
        zotero_add_tag(it.get("key"), it, TAG_TODO, cfg)
        print("✅ 已打 todo-parse: (%s) %s" % ((d.get("date") or "")[:4],
                                              d.get("title")))
    print("接下来在终端运行: zmd")
    return 0


# ---------------------------------------------------------------- MinerU 云端 v4

def mineru_headers(cfg):
    token = cfg.get("mineru_token")
    if not token:
        raise RuntimeError(
            "未配置 MinerU token。请先运行:\n"
            "  ~/Documents/zotero_mineru_batch.sh set-token <你的新token>")
    return {"Authorization": "Bearer " + token,
            "Content-Type": "application/json"}


def mineru_request_batch(files, cfg):
    """申请上传链接。files: [{name, data_id}]。返回 (batch_id, file_urls)。"""
    status, _, resp = http_json(
        MINERU_FILE_URLS, method="POST",
        headers=mineru_headers(cfg),
        data={"files": files, "model_version": "vlm", "language": "en"},
        timeout=120)
    if status != 200 or resp.get("code") != 0:
        raise RuntimeError("MinerU 申请上传链接失败: HTTP %s %s"
                           % (status, json.dumps(resp, ensure_ascii=False)[:300]))
    data = resp.get("data") or {}
    if not data.get("batch_id") or not data.get("file_urls"):
        raise RuntimeError("MinerU 响应缺少 batch_id/file_urls: %s"
                           % json.dumps(resp, ensure_ascii=False)[:300])
    return data["batch_id"], data["file_urls"]


def mineru_upload_file(upload_url, pdf_path):
    """PUT 原始字节到签名 URL(阿里云 OSS)。
    关键坑:签名里锁定的是「无 Content-Type」。urllib 在 Request 带 data 时会
    自动加 Content-Type: application/x-www-form-urlencoded,导致 403
    SignatureDoesNotMatch —— 必须显式传空 Content-Type 把它压掉。"""
    with open(pdf_path, "rb") as f:
        body = f.read()
    status, _, resp_body = http_request(
        upload_url, method="PUT", data=body,
        headers={"Content-Type": ""}, timeout=600)
    if status not in (200, 201, 204):
        raise RuntimeError("MinerU 上传失败: HTTP %s %s" % (status, resp_body[:200]))


def mineru_wait_batch(batch_id, cfg, expected_files):
    """轮询批量结果直到全部到终态。返回 {file_name: {state, full_zip_url, err_msg}}。"""
    deadline = time.time() + POLL_TIMEOUT
    wait = POLL_INITIAL
    while time.time() < deadline:
        status, _, resp = http_json(
            MINERU_BATCH_RESULTS + "/" + batch_id,
            headers=mineru_headers(cfg), timeout=60)
        if status != 200 or resp.get("code") != 0:
            raise RuntimeError("MinerU 轮询失败: HTTP %s %s"
                               % (status, json.dumps(resp, ensure_ascii=False)[:300]))
        results = (resp.get("data") or {}).get("extract_result") or []
        by_name = {r.get("file_name"): r for r in results}
        states = [r.get("state") for r in results]
        if all(s in ("done", "failed") for s in states) and \
                len(results) >= len(expected_files):
            return by_name
        print("  轮询中… 状态: %s (%.0fs 后重试)"
              % (states, wait))
        time.sleep(wait)
        wait = min(wait * 2, POLL_MAX)
    raise RuntimeError("MinerU 轮询超时(>%ds),batch_id=%s"
                       % (POLL_TIMEOUT, batch_id))


def mineru_download_zip(url, cfg):
    """下载结果 zip。裸下载失败则带 Bearer 重试一次。返回 zip 字节。"""
    for with_auth in (False, True):
        headers = mineru_headers(cfg) if with_auth else {}
        status, _, body = http_request(url, headers=headers, timeout=600)
        if status == 200 and body:
            return body
    raise RuntimeError("下载解析结果 zip 失败")


# ---------------------------------------------------------------- 后处理

def yaml_scalar(s):
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_yaml_header(item, extra):
    data = item.get("data", {})
    # 中文文献 creator 常只有 name 字段(无 lastName),两者都要取
    authors = ", ".join(c.get("lastName") or c.get("name") or ""
                        for c in data.get("creators", []))
    year = (data.get("date") or "")[:4]
    lines = ["---",
             "title: " + yaml_scalar(data.get("title", "")),
             "authors: " + yaml_scalar(authors),
             "year: " + yaml_scalar(year),
             "doi: " + yaml_scalar(data.get("DOI", "")),
             "zotero_key: " + yaml_scalar(item.get("key", "")),
             "source: " + yaml_scalar(extra.get("source", "")),
             "parsed_at: " + yaml_scalar(extra.get("parsed_at", "")),
             "---", ""]
    return "\n".join(lines)


def strip_reference_section(markdown):
    """按标题行切掉参考文献章节。切剩内容过短(说明误切正文)则返回原文。"""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if REF_HEADING_RE.match(line):
            kept = "\n".join(lines[:i]).strip()
            if len(kept) >= 200:   # 保守:剩下太少说明切错了
                return kept
    return markdown.strip()


def safe_filename(title, year, item_key):
    name = (title or "untitled")
    for ch in "/\\:*?\"<>|":
        name = name.replace(ch, "_")
    name = re.sub(r"\s+", " ", name).strip()[:120]
    fname = "%s_%s" % (name, year or "nodate")
    return "%s_%s.md" % (fname, item_key[:8]) if not name else \
        "%s_%s_%s.md" % (name, year or "nodate", item_key[:8])


def write_output_md(item, markdown, source):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    data = item.get("data", {})
    year = (data.get("date") or "")[:4]
    extra = {"source": source,
             "parsed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    body = strip_reference_section(markdown)
    content = build_yaml_header(item, extra) + "\n" + body + "\n"
    # 路径带上 item_key 前 8 位,基本不可能重名;仍防一手
    path = os.path.join(OUTPUT_DIR,
                        safe_filename(data.get("title"), year, item.get("key")))
    n = 0
    while os.path.exists(path):
        n += 1
        stem, ext = os.path.splitext(path)
        path = stem + "_%d" % n + ext
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def extract_full_md(zip_bytes, file_name):
    """从 MinerU 结果 zip 中取 full.md(每文件一个 zip)。"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            candidates = [n for n in names if n.endswith("full.md")]
            target = "full.md" if "full.md" in names else (
                candidates[0] if candidates else None)
            if target is None:
                raise RuntimeError("zip 中没有 full.md: %s" % names[:10])
            return zf.read(target).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        # 有时直接返回 md 文本而非 zip,尝试直接解码
        return zip_bytes.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- preflight

def preflight():
    print("=== zotero-mineru 体检 (v%s) ===\n" % VERSION)
    ok = True

    def check(name, cond, fix_hint):
        nonlocal ok
        if cond:
            print("✅ %s" % name)
        else:
            ok = False
            print("❌ %s\n   → %s" % (name, fix_hint))

    # 1. python
    check("Python 可用 (当前 %s)" % sys.version.split()[0], True, "")
    # 2. 配置与 token
    cfg = load_config()
    if cfg.get("mineru_token"):
        print("✅ MinerU token 已配置 (%s)" % CONFIG_PATH)
    else:
        ok = False
        print("❌ MinerU token 未配置\n"
              "   → 运行: ~/Documents/zotero_mineru_batch.sh set-token <新token>\n"
              "   → ⚠️ 旧脚本里明文写过的 token 已随对话暴露,请先到 MinerU 控制台轮换")
    # 3. 输出目录
    check("输出目录存在: %s" % OUTPUT_DIR, os.path.isdir(OUTPUT_DIR),
          "运行管线时会自动创建,无需处理")
    # 4. 代理探测
    proxied = any(os.environ.get(k) for k in
                  ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
    print(("⚠️ 检测到代理环境变量(管线对 127.0.0.1 已自动直连,不受影响)"
           if proxied else "✅ 无代理环境变量"))
    # 5. Zotero 进程
    alive = False
    try:
        http_request(ZOTERO_BASE + "/connector/ping", timeout=5)
        alive = True
    except Exception:
        pass
    check("Zotero 在运行且 httpServer 已开 (ping 200)", alive,
          "启动 Zotero;设置中保持「允许通过本地 HTTP 服务器访问」(httpServer.enabled)")
    # 6. localAPI 开关
    if alive:
        try:
            _, headers, _ = http_request(ZOTERO_API + "/items?limit=1", timeout=10)
            check("Zotero 本地 API 已启用", True, "")
            check("响应含 Zotero-Server-ID 头",
                  bool(headers.get("Zotero-Server-ID")),
                  "Zotero 版本过旧?")
        except HTTPError as e:
            if e.status == 403:
                check("Zotero 本地 API 已启用", False,
                      "设置 → Advanced → Config Editor → 打开 "
                      "extensions.zotero.httpServer.localAPI.enabled = true,"
                      "重启 Zotero")
            else:
                check("Zotero 本地 API 可访问", False,
                      "GET /api/users/0/items 返回 HTTP %s: %s"
                      % (e.status, e.body[:120]))
    else:
        check("Zotero 本地 API 已启用", False, "Zotero 未在运行,无法探测")
    # 7. 授权状态
    if cfg.get("local_api_key") and cfg.get("server_id"):
        print("✅ 本地 API 已授权 (key 已缓存)")
    else:
        ok = False
        print("❌ 本地 API 未授权\n"
              "   → 运行: ~/Documents/zotero_mineru_batch.sh auth\n"
              "   → 在 Zotero 弹窗点「始终允许」")
    # 8. todo-parse 条目数(未开 API 时跳过)
    if alive and cfg.get("server_id"):
        try:
            items = zotero_fetch_todo_items()
            print(("✅ 带 todo-parse 的条目: %d 篇" % len(items)) if items
                  else "ℹ️  当前没有带 todo-parse 的条目(在 Zotero 里给文献打上该标签即可)")
        except HTTPError as e:
            print("⚠️ 无法统计 todo-parse 条目: HTTP %s(先完成上面的修复)"
                  % e.status)
        except ZoteroAuthNeeded:
            print("⚠️ 授权已过期,重新运行 auth")
    print()
    print("体检结束:%s" % ("全部就绪 ✅" if ok else "存在需修复项,按 → 指引处理后重跑"))
    return 0 if ok else 1


# ---------------------------------------------------------------- run

def process_batch(batch, cfg, state, profile_dir):
    """batch: [{item, key, filename, pdf_path}]。"""
    files = [{"name": f["filename"],
              "data_id": f["key"]} for f in batch]
    batch_id, file_urls = mineru_request_batch(files, cfg)
    print("已创建 MinerU 批量任务: %s (%d 个文件)" % (batch_id, len(files)))
    for f, url in zip(batch, file_urls):
        print("  上传: %s" % f["filename"])
        mineru_upload_file(url, f["pdf_path"])
    print("上传完成,等待解析…")
    results = mineru_wait_batch(batch_id, cfg, expected_files=files)

    tmp_dir = os.path.join(CONFIG_DIR, "tmp_downloads")
    os.makedirs(tmp_dir, exist_ok=True)
    for f in batch:
        r = results.get(f["filename"])
        item, key, item_version = f["item"], f["key"], f["version"]
        title = (item.get("data") or {}).get("title", "(无标题)")
        if not r or r.get("state") == "failed":
            err = (r or {}).get("err_msg", "结果缺失")
            print("❌ 解析失败: %s — %s" % (title, err))
            st = state.setdefault(key, {})
            st["status"] = "failed"
            st["error"] = str(err)
            continue
        if r.get("state") != "done":
            print("⚠️ %s 状态异常: %s,本次跳过" % (title, r.get("state")))
            continue
        try:
            zip_bytes = mineru_download_zip(r.get("full_zip_url"), cfg)
            markdown = extract_full_md(zip_bytes, f["filename"])
            path = write_output_md(item, markdown,
                                   "MinerU v4 batch %s" % batch_id)
            print("✅ %s\n   → %s" % (title, path))
        except Exception as e:
            print("❌ %s 后处理失败: %s" % (title, e))
            st = state.setdefault(key, {})
            st["status"] = "failed"
            st["error"] = str(e)
            continue
        st = state.setdefault(key, {})
        st.update({"status": "done", "batch_id": batch_id,
                   "pdf_md5": f["pdf_md5"], "output": path,
                   "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        # 标签回写:失败不致命,但要在结尾汇总提示
        try:
            zotero_update_tags(key, item, cfg)
            print("   🏷  Zotero 标签: todo-parse → parsed-done")
        except ZoteroAuthNeeded as e:
            print("   ⚠️ 标签回写需重新授权: %s(稍后重跑本命令会自动补写)" % e)
        except Exception as e:
            print("   ⚠️ 标签回写失败: %s(稍后重跑本命令会自动补写)" % e)


def run():
    print("==== Zotero → MinerU 批量解析开始 ====")
    print("输出目录: %s\n" % OUTPUT_DIR)
    cfg = load_config()
    mineru_headers(cfg)  # 没有 token 直接报错退出

    try:
        items = zotero_fetch_todo_items()
    except HTTPError as e:
        if e.status == 403:
            raise SystemExit(
                "❌ Zotero 本地 API 未启用。\n"
                "   设置 → Advanced → Config Editor → 将\n"
                "   extensions.zotero.httpServer.localAPI.enabled 设为 true,"
                "重启 Zotero 后重试。")
        raise
    if not items:
        print("没有找到带 todo-parse 的文献,结束。")
        print("(若你确定已打标签:检查连字符是普通 '-' 而非 U+2011 非断字连字符)")
        return 0

    print("发现 %d 篇待解析文献\n" % len(items))
    state = load_state()
    profile_dir = zotero_find_profile_dir()
    todo_batches = []   # [{item,key,version,filename,pdf_path,pdf_md5}]
    skipped = []

    for it in items:
        key = it.get("key")
        title = (it.get("data") or {}).get("title", "(无标题)")
        pdfs = zotero_pdf_attachments(key)
        if not pdfs:
            print("⚠️ 无 PDF 附件,跳过: %s" % title)
            continue
        att = pdfs[0]
        pdf_path = resolve_pdf_local_path(profile_dir, att)
        tmp_path = None
        if not pdf_path and att.get("linkMode") == "imported_file":
            tmp_path = os.path.join(CONFIG_DIR, "tmp_downloads", key + ".pdf")
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
            try:
                pdf_path = zotero_download_pdf(key, tmp_path)
            except Exception as e:
                print("⚠️ 附件下载失败(%s),跳过: %s" % (e, title))
                continue
        if not pdf_path:
            print("⚠️ 未找到本地 PDF(附件为链接模式,文件未下载),跳过: %s" % title)
            print("   → 在 Zotero 里选中该条目的 PDF 附件,右键「下载 PDF」"
                  "(或同步附件)后再跑本脚本")
            continue
        if not os.path.isfile(pdf_path):
            print("⚠️ PDF 文件缺失,跳过: %s" % title)
            continue
        with open(pdf_path, "rb") as fh:
            pdf_md5 = hashlib.md5(fh.read()).hexdigest()

        st = state.get(key) or {}
        out_exists = st.get("output") and os.path.isfile(st["output"])
        if st.get("status") == "done" and st.get("pdf_md5") == pdf_md5 and out_exists:
            skipped.append(it)
            print("✅ 已解析过(ledger 命中),跳过: %s" % title)
            try:
                zotero_update_tags(key, it, cfg)
            except Exception as e:
                print("   ⚠️ 标签补写失败: %s" % e)
            continue
        # 结果文件已存在(旧版脚本或其他来源),按存在即跳过处理
        year = ((it.get("data") or {}).get("date") or "")[:4]
        guess = os.path.join(OUTPUT_DIR, safe_filename(title, year, key))
        if os.path.isfile(guess) and st.get("pdf_md5") == pdf_md5:
            skipped.append(it)
            print("✅ md 已存在,跳过(不重复消耗配额): %s" % title)
            st.update({"status": "done", "output": guess, "pdf_md5": pdf_md5})
            try:
                zotero_update_tags(key, it, cfg)
            except Exception as e:
                print("   ⚠️ 标签补写失败: %s" % e)
            continue
        todo_batches.append({"item": it, "key": key,
                             "version": (it.get("data") or {}).get("version"),
                             "filename": att.get("filename") or (title + ".pdf"),
                             "pdf_path": pdf_path, "pdf_md5": pdf_md5})

    if not todo_batches:
        print("\n无新文件需要解析。")
    for i in range(0, len(todo_batches), BATCH_SIZE):
        process_batch(todo_batches[i:i + BATCH_SIZE], cfg, state, profile_dir)
        save_state(state)

    save_state(state)
    print("\n==== 全部任务结束 ====")
    print("解析结果: %s" % OUTPUT_DIR)
    return 0


# ---------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="zotero_mineru_pipeline",
        description="Zotero(todo-parse) → MinerU 云端 → markdown 批量解析管线")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="执行批量解析(日常入口)")
    sub.add_parser("preflight", help="只读体检,输出修复指引")
    sub.add_parser("auth", help="与 Zotero 本地 API 授权")
    p = sub.add_parser("set-token", help="写入 MinerU token 到 config.json")
    p.add_argument("token", help="MinerU 的新 token")
    p = sub.add_parser("tag", help="按标题关键词给文献打 todo-parse 标签")
    p.add_argument("keyword", help="标题关键词(子串匹配,忽略大小写)")
    p.add_argument("--all", action="store_true", help="匹配多篇时全部打标")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        return run()
    if args.cmd == "preflight":
        return preflight()
    if args.cmd == "auth":
        zotero_authorize()
        return 0
    if args.cmd == "tag":
        return cmd_tag(args.keyword, args.all)
    if args.cmd == "set-token":
        if len(args.token.strip()) < 10:
            raise SystemExit("token 看起来不对,请整段粘贴")
        cfg = load_config()
        cfg["mineru_token"] = args.token.strip()
        save_config(cfg)
        print("✅ MinerU token 已写入 %s (权限 600)" % CONFIG_PATH)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
