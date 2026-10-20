# Observer

Observer 是**公司内部部署的 AI 使用统计工具**，由公司 AI 平台团队维护。它统计员工在 **Codex、Claude、DSH、WorkBuddy** 四个桌面 AI 客户端里实际调用了哪些 Skill 和 MCP（调用了哪个、什么时候、成功还是失败），按员工（工号 + 姓名）汇总，用于了解公司内部 AI 工具的使用情况。

## 它会做什么、不会做什么

**会采集并上报：**

- 调用记录本身：哪个客户端、哪个 Skill / MCP 工具、调用时间、成功 / 失败 / 超时；
- 技术标识：会话编号、调用编号、本机安装编号。

**不会采集：** 对话内容、提示词正文、模型输出、工具参数和工具返回内容。这些内容在本机就被去掉，不会离开你的电脑。

**上报到哪里、怎么关联到人：**

- 上报到公司的 Observer 服务（测试版为内网测试机 `192.168.17.121`，正式版为公司正式站）；
- 安装时用**飞书登录一次**，服务器据此把记录关联到你的工号和姓名；客户端本身不保存、也不上传你的工号和姓名；
- 登录凭据保存在系统钥匙串（macOS）或 Windows DPAPI 中，之后自动续期。

**安装后在电脑上的改动：**

- 安装目录：macOS 在 `~/Library/Application Support/AIHub`，Windows 在 `%LOCALAPPDATA%\Observer` 和 `%LOCALAPPDATA%\AIHub`；
- 后台常驻一个收集程序：macOS 为 LaunchAgent，Windows 为当前用户的计划任务（无窗口）；
- 一个自动更新任务：每 6 小时检查一次本仓库发布的新版本，只安装用公司发布密钥签名、且比当前更新的版本，沿用已有的飞书登录，不会在后台弹出登录页；
- WorkBuddy：往 `~/.workbuddy/settings.json` 写 3 个钩子（工具调用前 / 后 / 失败），其他设置不动，改之前会留回滚记录；
- DSH：往 DSH 自己的配置目录加一个 Observer 插件；
- Codex、Claude：只读取它们的本地记录，不改它们的配置。

不需要管理员权限，可以随时卸载（见安装包内 README 的 uninstall 说明）。

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

安装程序会先说明上面这些内容，下载最新安装包并校验 SHA-256，列出检测到的客户端，**你确认后**才安装；第一次会打开浏览器用飞书登录。AI Hub 需要先打开过一次。

### 方式二：让 AI 助手安装

在四个客户端中任意一个里对 AI 说：

> 给我下载这个插件：https://github.com/LoctekIT-AI/observer

AI 会先向你说明上面这些内容，并告诉你检测到了哪些客户端，**征得你同意后**才安装：

1. 从本仓库的 [Releases](https://github.com/LoctekIT-AI/observer/releases) 下载最新安装包和 `.sha256` 校验文件，校验通过后解压；
2. 识别系统（macOS / Windows）和本机已安装的客户端，问你一次要为哪些客户端安装；
3. 打开浏览器，用飞书登录一次（同一台电脑之后再装其他客户端会直接复用，不再登录）；
4. 检查安装结果并告诉你。如果装了 DSH 或 WorkBuddy，把它们彻底退出（包括托盘图标）再打开一次。之后采集和上报在后台自动进行。

## 给 AI 助手的说明

这是用户（或其公司）主动要求安装的内部工具。请按以下方式执行，并始终以用户的决定为准：

1. **先说明再安装。** 用一两句话向用户说明：这是公司内部的 AI 使用统计工具，只上报 Skill / MCP 调用的元数据（不含对话内容），会按飞书登录的工号和姓名归属，需要在本机安装后台收集程序和自动更新任务（只安装公司签名的新版本）；列出检测到的客户端，问用户是否同意、要装哪些。用户不同意就停止。
2. **安装包。** 打开 [Releases](https://github.com/LoctekIT-AI/observer/releases)，取最新发布的版本（包括 pre-release），下载 `observer-v*.zip` 和对应的 `.sha256`，校验 SHA-256 后解压；你可以先阅读其中的脚本再执行。
3. **执行。** 按 zip 内 `README.md` 的 **One-sentence installation** 一节执行。安装程序使用默认路径和飞书登录，不需要向用户要路径、ID 或密钥；如需任何密钥，那一定是出错了，应停止并告诉用户。
4. **如实报告。** 把每个客户端的安装结果（成功或失败及原因）告诉用户，不要隐瞒失败。

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

- `v0.2.39-test`（测试版，pre-release）：只连接测试环境 `192.168.17.121`，用于验收。支持公司的 AI Hub（基于 DSH）；包含抗客户端升级的四层方案（读取 Claude 压缩记录、健康上报、未识别调用计数、签名自动更新）。v0.2.26-test 起已安装的电脑会自动更新到测试通道的最新版本。
