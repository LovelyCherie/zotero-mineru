# zotero-mineru

Zotero 文献批量解析 + 防幻觉分析工作流(Claude Code skill)。

给 Zotero 里的文献打 `todo-parse` 标签 → 一条命令 `zmd` → MinerU 云端解析 PDF 为 markdown(自动剔除参考文献)→ Zotero 自动回写 `parsed-done` → 直接在 Claude Code 里按防幻觉规则分析,或组装 DeepSeek 分析包。

## 安装

```bash
git clone https://github.com/<你的用户名>/zotero-mineru.git ~/.claude/skills/zotero-mineru
cp install/zotero_mineru_batch.sh ~/Documents/
chmod +x ~/Documents/zotero_mineru_batch.sh
echo "alias zmd='~/Documents/zotero_mineru_batch.sh'" >> ~/.zshrc
```

首次设置(见 SKILL.md):
1. `zmd set-token <MinerU token>`(token 只存 `~/.config/zotero-mineru/config.json`,权限 600)
2. Zotero 设置 → Advanced → Config Editor → `extensions.zotero.httpServer.localAPI.enabled` = true → 重启 Zotero
3. `zmd auth` → Zotero 弹窗点「始终允许」
4. `zmd preflight` 全绿即可用

## 日常使用

```bash
zmd tag 关键词   # 按标题关键词打 todo-parse(多篇匹配时先列出,加 --all 全打)
zmd              # 解析:自动开 Zotero → MinerU 解析 → 打开结果文件夹
```

解析结果在 `~/Documents/Zotero_MD_Output/`;分析直接在 Claude Code 里说「梳理研究脉络」等(触发词见 SKILL.md frontmatter)。

## 安全

- 无任何 token/密钥入库;运行时凭据在 `~/.config/zotero-mineru/`(0600)
- 解析出的论文 markdown 不要提交到本仓库(版权)
