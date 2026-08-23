---
name: zotero-mineru
description: Zotero 文献批量解析与防幻觉分析工作流。Zotero 里给文献打 todo-parse 标签 → 跑脚本 → MinerU 云端解析 PDF 为 markdown(剔除参考文献)→ Zotero 自动打 parsed-done → 按防幻觉规则直接分析或组装 DeepSeek 分析包。Triggers: 解析文献, 文献解析, Zotero 批量解析, 跑 zotero_mineru_batch, 批量解析我的文献, 整理研究脉络, 对比文献方法, parse my zotero papers, 处理我的文献, 分析我的文献
---

# zotero-mineru:Zotero 文献批量解析 + 防幻觉分析

把 Zotero 里带 `todo-parse` 标签的文献批量交给 MinerU 云端解析成 markdown,回写 `parsed-done` 标签,然后直接在会话里按防幻觉规则做分析(或组装成 DeepSeek 可用的分析包)。

## 目录结构

```
~/.claude/skills/zotero-mineru/
├── SKILL.md                            # 本文件
├── references/analysis-prompt.md       # 防幻觉 prompt 模板(组装/分析时引用)
└── scripts/
    ├── zotero_mineru_pipeline.py       # 核心管线(纯标准库)
    └── bundle_markdown.py              # 组装分析包
~/Documents/zotero_mineru_batch.sh      # 日常入口(薄包装,保留用户既有习惯)
~/.config/zotero-mineru/config.json     # MinerU token + Zotero 本地 API 授权(权限 600)
~/.config/zotero-mineru/state.json      # 防重复 ledger(条目 key + PDF 哈希)
~/Documents/Zotero_MD_Output/           # 解析出的 markdown(带 YAML 头)
```

## 日常流程(步骤与用户既有习惯一致)

1. Zotero 导入文献,给需要解析的文献打标签 `todo-parse`(**必须是普通连字符 `-`**;历史遗留的 U+2011 非断字连字符标签匹配不上,在 Zotero 里重打即可)
2. 保持 Zotero 软件打开
3. 终端执行 `cd ~/Documents && ./zotero_mineru_batch.sh`(或任何目录下 `zmd`;Zotero 没开会自动启动)
4. 脚本自动:读取 `todo-parse` 条目 → MinerU 云端解析 PDF(批量、自动剔除参考文献章节)→ 输出 md → Zotero 条目标签变为 `parsed-done`,ledger 防重复,不会二次消耗配额
5. 之后按「分析模式」使用结果

### 全 VSCode 操作(推荐,免开 Zotero 界面)

在 VS Code 打开 `~/Documents` 文件夹,集成终端里:

```bash
zmd tag 关键词     # 按标题关键词打 todo-parse(多篇匹配时先列出,加 --all 全打)
zmd                # 解析,自动开 Zotero、完事自动打开结果文件夹
```

或者直接用 VS Code 任务:⌘⇧P → Tasks: Run Task → 「文献解析 (zmd)」。
浏览器导入文献(Connector 一键)仍在浏览器完成,其余全部在 VS Code。

## 首次设置(新机器/新 token 时)

```bash
# 1) 轮换并写入 MinerU token(旧 token 若曾明文出现在脚本或对话里,先到 mineru.net 控制台重新生成)
~/Documents/zotero_mineru_batch.sh set-token <新token>

# 2) 打开 Zotero 本地 API:
#    Zotero 设置 → Advanced → Config Editor → 搜索 extensions.zotero.httpServer.localAPI.enabled → 设为 true → 重启 Zotero

# 3) 授权(弹窗出现后点「始终允许 / Always Allow」)
~/Documents/zotero_mineru_batch.sh auth

# 4) 体检确认
~/Documents/zotero_mineru_batch.sh preflight
```

## 分析模式(解析完成后)

**默认:直接在本会话分析(全流程模式)。** 用户要的是「梳理研究脉络 / 对比方法 / 总结研究局限」等任务时:

1. 读取 `~/Documents/Zotero_MD_Output/` 中相关 md(按用户指定的主题/年份筛选;不确定就全部读)
2. 读入并严格遵守 `references/analysis-prompt.md` 的规则
3. 先列出本次实际读到的文献清单(作者, 年份, 标题),再输出分析;每条结论带(作者, 年份);信息不足写【原文未提及】

**备选:组装 DeepSeek 分析包。** 用户明确说要把结果喂给 DeepSeek 时:

```bash
python3 ~/.claude/skills/zotero-mineru/scripts/bundle_markdown.py \
  [--title 关键词] [--year 2023] [--limit 10]
# 产物: ~/Documents/Zotero_MD_Output/analysis_bundle.md
# 头部已内嵌防幻觉 prompt,整体复制粘贴给 DeepSeek 即可
```

## 故障排查(按状态码,均已实测验证)

| 现象 | 原因 | 解法 |
|---|---|---|
| `/api/` 返回 **403** | 本地 API 未启用(`localAPI.enabled` 默认 false) | 首次设置第 2 步,重启 Zotero |
| 写标签返回 **428 / 412** | 缺 `Zotero-Server-ID` / 版本头或与当前实例不符 | 重跑 `./zotero_mineru_batch.sh auth` |
| 写标签返回 **401** | 本地 API key 过期/一次性 key 已消费 | 重跑 `auth`,点「始终允许」 |
| 请求 Zotero 返回 **502** | 系统代理劫持 localhost(如 Clash 的 7897) | 管线对 127.0.0.1 已自动直连,无需处理;若手工 curl 需加 `--noproxy '*'` |
| MinerU 返回非 0 `code` / `state=failed` | token 失效、配额、单文件 >200MB 或 >200 页 | 看返回 `msg`/`err_msg`;到 mineru.net 控制台查 token 与配额 |
| 一直提示「没有找到 todo-parse 条目」 | 标签用了 U+2011 连字符,或条目无 PDF 附件 | 在 Zotero 里删掉旧标签重打;确认附件为 PDF |
| 提示「附件是链接模式(imported_url),PDF 未下载到本地」 | 从知网等导入时只存了下载链接,文件未落盘;GET /file 会 400 | Zotero 里右键 PDF 附件 → 下载 PDF(需浏览器登录 CNKI),或手动下载 PDF 拖入条目替换 |
| 重跑时「ledger 命中,跳过」但 Zotero 标签没变 | 上次标签回写失败(授权过期等) | 重跑 `auth` 后重跑脚本,会自动补写标签 |

## 实现要点(维护者须知)

- Zotero 本地 API(端口 23119):只支持 web API v3 语法;写操作需 `Zotero-API-Key`(本地 key,经 `POST /api/local/authorize` 弹窗授权)+ `Zotero-Server-ID` + `If-Unmodified-Since-Version`,三件套缺一不可。
- MinerU 云端 v4 为异步流程:`POST /api/v4/file-urls/batch` 申请签名上传链接(≤50 个/次)→ 裸 `PUT` 文件字节(不设 Content-Type)→ 轮询 `GET /api/v4/extract-results/batch/{batch_id}` → 下载 `full_zip_url` 解出 `full.md`。文档无「剔除参考文献」参数,由管线本地后处理。
- PDF 定位优先用附件 `path`(`storage:` 前缀)拼 profile 目录 `assets/` 或 `storage/`,失败回退 `GET /items/{key}/file` 字节流。
- 防重复 = `state.json`(条目 key + PDF md5)+「md 已存在」双保险;解析失败记为 failed,下次重试。
- **任何 token 不得写进本仓库任何文件**;只存在 `~/.config/zotero-mineru/config.json`(0600)。
