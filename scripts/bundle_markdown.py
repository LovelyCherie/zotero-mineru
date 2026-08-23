#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 ~/Documents/Zotero_MD_Output 下选定的 markdown 组装成单一「分析包」:
  - 文件头部内嵌防幻觉 prompt 全文(references/analysis-prompt.md)
  - 每篇 md 之间加分隔线,保留 YAML 头(标题/作者/年份/DOI)
产物可直接整体粘贴给 DeepSeek,也可由 skill 直接读入在本会话分析。

用法:
  python3 bundle_markdown.py [--dir DIR] [--title 关键词] [--year 2023] \
                             [--limit N] [--out 输出文件] [--no-prompt]
"""
import argparse
import os
import re
import sys

DEFAULT_DIR = os.path.expanduser("~/Documents/Zotero_MD_Output")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_PATH = os.path.join(SKILL_DIR, "references", "analysis-prompt.md")

YAML_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def parse_front_matter(text):
    m = YAML_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end():]


def load_papers(directory, title_kw=None, year=None, limit=None):
    papers = []
    if not os.path.isdir(directory):
        return papers
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(directory, name)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        meta, body = parse_front_matter(text)
        if title_kw and title_kw.lower() not in (meta.get("title", "") + name).lower():
            continue
        if year and meta.get("year") != str(year):
            continue
        papers.append({"file": name, "meta": meta, "body": body})
    papers.sort(key=lambda p: (p["meta"].get("year", ""), p["meta"].get("title", "")),
                reverse=True)
    if limit:
        papers = papers[: int(limit)]
    return papers


def main(argv=None):
    ap = argparse.ArgumentParser(description="组装 markdown 分析包")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="md 目录")
    ap.add_argument("--title", help="标题关键词过滤(子串,忽略大小写)")
    ap.add_argument("--year", help="按年份过滤")
    ap.add_argument("--limit", type=int, help="只取按年份排序的前 N 篇")
    ap.add_argument("--out", default=os.path.join(DEFAULT_DIR, "analysis_bundle.md"),
                    help="输出文件(默认 %s)" % os.path.join(DEFAULT_DIR, "analysis_bundle.md"))
    ap.add_argument("--no-prompt", action="store_true", help="不内嵌防幻觉 prompt")
    args = ap.parse_args(argv)

    papers = load_papers(args.dir, args.title, args.year, args.limit)
    if not papers:
        print("❌ 没有匹配的 md 文件(目录: %s)" % args.dir)
        return 1

    parts = []
    if not args.no_prompt and os.path.isfile(PROMPT_PATH):
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            prompt = f.read().strip()
        parts.append("<!-- ===== 以下为防幻觉分析规则,先读后做 ===== -->\n\n"
                     + prompt + "\n")
        parts.append("\n<!-- ===== 以下为待分析文献原文 ===== -->\n")

    for i, p in enumerate(papers, 1):
        m = p["meta"]
        cite = "%s(%s). %s" % (m.get("authors", "作者未知"), m.get("year", "年份未知"),
                               m.get("title", p["file"]))
        if m.get("doi"):
            cite += " DOI:%s" % m["doi"]
        parts.append("\n\n<!-- ============================================ -->\n"
                     "<!-- 文献 %d: %s -->\n"
                     "<!-- ============================================ -->\n\n"
                     % (i, cite))
        parts.append(p["body"].strip())

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")
    print("✅ 已组装 %d 篇文献 → %s" % (len(papers), args.out))
    print("   可直接整体粘贴给 DeepSeek,或让 Claude 直接读入分析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
