#!/bin/zsh
# 启动 jev-jarvis 悬浮窗（不装 LaunchAgent，按需手动启动）
cd "$(dirname "$0")" || exit 1
export USE_TF=0
# uv installs to ~/.local/bin; a Finder-launched .command does not inherit a login shell
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# same user-level env the .app launcher uses (API keys live outside the repo)
[ -f "$HOME/.config/jev-jarvis/env" ] && source "$HOME/.config/jev-jarvis/env"
# 本地判断模型走国内镜像；必须在 python 启动前设好，huggingface_hub 导入时读取。
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
export HF_HUB_DISABLE_XET=1

LOG="$HOME/Library/Logs/jev-jarvis.log"
mkdir -p "$(dirname "$LOG")" || exit 1
source ./packaging/bootstrap_uv.sh || exit 1
if ! command -v uv >/dev/null 2>&1; then
    print "未找到 uv，正在自动安装；进度日志：$LOG"
fi
if ! jev_ensure_uv "$LOG"; then
    print -r -- "$JEV_UV_ERROR"
    print -r -- "详情：$LOG。也可手动运行 brew install uv 后重试。"
    exit 1
fi

# One HUD at a time. Earlier restarts used pkill on the absolute path, but the
# live command line is `src/hud.py`, so the old panel stayed up and a second
# one appeared beside it.
existing=$(pgrep -f '[p]ython.*src/hud.py' || true)
if [ -n "$existing" ]; then
    print "已有悬浮窗在跑（pid $existing），先退出再启动"
    kill $existing 2>/dev/null || true
    sleep 0.4
    kill -9 $existing 2>/dev/null || true
fi

exec uv run python src/hud.py
