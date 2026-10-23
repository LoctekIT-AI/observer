# Observer

## 安装

### 方式一：双击安装（推荐，不需要 AI 助手）

- **Windows**：下载 [Install-Observer-Windows.zip](https://github.com/LoctekIT-AI/observer/raw/main/install/Install-Observer-Windows.zip)，右键 → 全部解压缩，双击里面的 `Install-Observer-Windows.cmd`（如果出现“Windows 已保护你的电脑”，点“更多信息 → 仍要运行”）。也可以在 PowerShell 里运行：

  ```powershell
  irm https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-windows.ps1 | iex
  ```

- **macOS**：下载 [Install-Observer-macOS.zip](https://github.com/LoctekIT-AI/observer/raw/main/install/Install-Observer-macOS.zip)（浏览器一般会自动解压；没有就双击 zip），双击 `Install-Observer-macOS.command`。第一次如果提示无法打开：右键 → 打开；macOS 15 及以上：先点“完成”，再到 系统设置 → 隐私与安全性，点“仍要打开”。也可以在“终端”里运行（不会有这个提示）：

  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-macos.sh)"
  ```

安装程序会下载最新安装包并校验 SHA-256，列出检测到的客户端，**你确认后**才安装；第一次会打开浏览器用飞书登录。AI Hub 需要先打开过一次。

### 方式二：让 AI 助手安装

在四个客户端中任意一个里对 AI 说：

> 给我下载这个插件：https://github.com/LoctekIT-AI/observer

AI 会告诉你检测到了哪些客户端，**你确认后**才安装：

1. 从本仓库的 [Releases](https://github.com/LoctekIT-AI/observer/releases) 下载最新安装包和 `.sha256` 校验文件，校验通过后解压，按其中 `README.md` 的 **One-sentence installation** 一节执行（不需要向你要路径、ID 或密钥）；
2. 识别系统（macOS / Windows）和本机已安装的客户端，问你一次要为哪些客户端安装；
3. 打开浏览器，用飞书登录一次（同一台电脑之后再装其他客户端会直接复用，不再登录）；
4. 检查安装结果并告诉你。如果装了 DSH 或 WorkBuddy，把它们彻底退出（包括托盘图标）再打开一次。之后采集和上报在后台自动进行。

## 源码与打包

- `bundle/`：安装包的全部源码，内容就是 Releases 里 zip 解压后的样子（`README.md` 是给安装程序和 AI 助手看的安装说明）。源码里默认连接公司正式站。
- `tools/build_bundle.py`：从 `bundle/` 打出安装包和 `.sha256`。同样的源码和版本号，打出来的 zip 完全相同；要核对某个已发布版本，先切到那个版本发布时的提交（`main` 上的 `bundle/` 可能已经比最新 Release 新）：

  ```bash
  python3 tools/build_bundle.py --version 0.2.25 --test   # 测试版（连接 192.168.17.121），结果与 v0.2.25-test 的 SHA-256 一致
  python3 tools/build_bundle.py --version 0.2.26          # 正式版
  ```

- 每次改动后打包都必须提高版本号，否则已安装的电脑不会启用新版本。
- 自动更新：`tools/build_bundle.py` 把 `tools/update-keys/<通道>.pub` 的公钥写进安装包（测试版通道 `test`，正式版通道 `release`；没有公钥的正式包不会自动更新）。发版顺序：先发布 Release 并验收，再在发版电脑上用私钥签名 `python3 tools/sign_channel.py sign ...`，最后把生成的 `channels/<通道>.json` 提交到 `main`，已安装的电脑才会开始更新。私钥只保存在发版电脑上，不进仓库、不发到聊天里。
- `tests/`：`python3 -m pytest tests`（需要 Python 3.11+ 和 pydantic、PyYAML、jsonschema）。

## 当前版本

- `v0.3.5`（正式版，2026-10-23；v0.3.0 起 2026-10-20 上线）：连接公司正式环境 `https://itsub.loctek.com/observer`，飞书登录。双击安装默认安装正式版；已装正式版的电脑每 30 分钟自动检查更新。
- `v0.2.40-test`（测试版，pre-release）：只连接测试环境 `192.168.17.121`，用于发版前验收（安装时设置环境变量 `OBSERVER_CHANNEL=test`）。支持公司的 AI Hub（基于 DSH）；包含抗客户端升级的四层方案（读取 Claude 压缩记录、健康上报、未识别调用计数、签名自动更新）。v0.2.26-test 起已安装的电脑会自动更新到测试通道的最新版本。
