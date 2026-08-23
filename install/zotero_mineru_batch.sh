#!/bin/zsh
# ============================================================
# zotero_mineru_batch.sh — 日常入口(薄包装)
# 真正实现: ~/.claude/skills/zotero-mineru/scripts/zotero_mineru_pipeline.py
# 用法:
#   ./zotero_mineru_batch.sh            # 执行解析(日常)
#   ./zotero_mineru_batch.sh preflight  # 只读体检
#   ./zotero_mineru_batch.sh auth       # Zotero 本地 API 授权(点「始终允许」)
#   ./zotero_mineru_batch.sh set-token <token>  # 写入 MinerU token
# 注意:token 不保存在本文件,统一在 ~/.config/zotero-mineru/config.json (0600)
# ============================================================

PIPELINE="$HOME/.claude/skills/zotero-mineru/scripts/zotero_mineru_pipeline.py"
CONFIG="$HOME/.config/zotero-mineru/config.json"

if [[ ! -f "${PIPELINE}" ]]; then
    echo "❌ 找不到管线脚本: ${PIPELINE}"
    echo "   请确认 skill 已安装于 ~/.claude/skills/zotero-mineru/"
    exit 1
fi

if [[ ! -x "$(command -v python3)" ]]; then
    echo "❌ 未找到 python3,请先安装 Xcode Command Line Tools: xcode-select --install"
    exit 1
fi

CMD="${1:-run}"

case "${CMD}" in
    run)
        if [[ ! -f "${CONFIG}" ]] || ! python3 -c "
import json,sys
cfg=json.load(open('${CONFIG}'))
sys.exit(0 if cfg.get('mineru_token') else 1)" 2>/dev/null; then
            echo "❌ MinerU token 未配置。"
            echo "   请先到 mineru.net 控制台轮换生成新 token,然后执行:"
            echo "   ./zotero_mineru_batch.sh set-token <新token>"
            exit 1
        fi
        # Zotero 没开就自动启动并等待就绪
        if ! curl -s -m 2 --noproxy '*' -o /dev/null \
             http://127.0.0.1:23119/connector/ping; then
            echo "Zotero 未运行,正在启动…"
            open -a Zotero
            for _ in {1..60}; do
                curl -s -m 2 --noproxy '*' -o /dev/null \
                     http://127.0.0.1:23119/connector/ping && break
                sleep 1
            done
        fi
        python3 "${PIPELINE}" run "${@:2}"
        rc=$?
        [[ $rc -eq 0 ]] && open "${HOME}/Documents/Zotero_MD_Output"
        exit $rc
        ;;
    preflight|auth|tag)
        exec python3 "${PIPELINE}" "${CMD}" "${@:2}"
        ;;
    set-token)
        if [[ -z "$2" ]]; then
            echo "用法: ./zotero_mineru_batch.sh set-token <新token>"
            exit 1
        fi
        exec python3 "${PIPELINE}" set-token "$2"
        ;;
    -h|--help|help)
        exec python3 "${PIPELINE}" --help
        ;;
    *)
        echo "未知子命令: ${CMD}(支持: run / preflight / auth / set-token)"
        exit 1
        ;;
esac
