# Jev 聊天助手（macOS / Windows）

**挂在微信窗口旁边的对话副驾：读到对方最新消息 → Jev 判断意图和风险 → 给出话术候选 → 一键填入输入框。发送永远由你点。**

| 端 | 目录 | 采集 | 适用 |
|---|---|---|---|
| **macOS** | [`mac/`](mac/) | 窗口截图 + Vision OCR | 电脑版微信 4.x |
| **Windows** | [`windows/`](windows/) | 窗口截图 + 离线 OCR | 电脑版微信 4.x |

---

## 硬约束（两端一样）

1. **不 hook、不改微信、不读数据库。** 只读你屏幕上正在显示的对话。
2. **绝不自动发送。** 只把文字填进输入框，发送键永远由人手点。
3. **不碰钱。** 不触碰转账、红包、收款码。
4. **密钥不进 git、不进日志。** 判断一把、起草一把，写环境变量或系统密钥库。

---

## 它怎么工作

```
微信窗口 ──(窗口截图 + OCR)──▶ 读到对方最新消息
                │
    ┌───────────┴───────────┐
    ▼                       ▼
Jev 判断意图 / 风险     语言模型按话术起草候选
    └───────────┬───────────┘
                ▼
      悬浮窗展示 → 人点「填入」（不发送）
```

- **判断必须走 Jev。** 连不上会提示「Jev 连不上 · 将重试」，不会改走本地兜底当产品路径。
- **候选默认 1 条。** 首页一个话术（高情商话术）；点「增加回复风格」最多 3 条。
- **助手跟着微信窗口走。** 窗口出现就出现，关掉 / 最小化就藏起来。

---

## 快速开始

### macOS

```bash
cd mac
./start.command
```

第一次按提示打开「屏幕录制」；点「填入」还需「辅助功能」。判断层填 `TYPESAFE_API_KEY`（`~/.config/jev-jarvis/env`），起草层填任意 OpenAI 兼容端点。

完整说明：[mac/README.md](mac/README.md)

### Windows

```bat
cd windows
pip install -r requirements.txt
python main.py
```

微信窗口开着、别最小化。首次启动填两把 key：判断 `JEV_API_KEY`、起草 `LLM_API_KEY`（写进用户环境变量，不落文件）。

完整说明：[windows/README.md](windows/README.md)

---

## 仓库结构

```
.
├── README.md            本文件：两端总览
├── mac/                 macOS 悬浮窗（Python + AppKit）
│   ├── src/hud.py       主循环
│   └── start.command    一键启动
└── windows/             Windows 旁挂（Python + Qt）
    ├── main.py          入口
    └── app/overlay.py   悬浮窗
```

两端互不影响：改 Mac 不会自动出现在 Windows 上，反之亦然。

---

## 许可

代码以 [MIT](LICENSE) 协议开源。仅供个人学习与研究：只处理你自己设备上、你自己有权查看的聊天。请遵守微信等软件的许可协议与当地法律法规，作者不对使用后果负责。
