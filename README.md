# Observer

Observer 统计公司员工在 **Codex、Claude、DSH、WorkBuddy** 四个桌面 AI 客户端里实际调用的 Skill 和 MCP（调用了哪个、什么时候、成功或失败），并按员工（工号 + 姓名）汇总。只采集调用元数据，**不采集对话内容、模型输出或工具返回内容**。

## 安装：只需一句话

在四个客户端中任意一个里对 AI 说：

> 给我下载这个插件：https://github.com/reyfeng9-glitch/observer

AI 会自动完成以下步骤：

1. 从本仓库的 [Releases](https://github.com/reyfeng9-glitch/observer/releases) 下载最新安装包和 `.sha256` 校验文件，校验通过后解压；
2. 识别你的系统（macOS / Windows）和当前客户端，安装对应的收集器；
3. 打开浏览器，**用飞书登录一次**。登录结果保存在系统钥匙串（macOS）或 DPAPI（Windows）里，之后自动续期；同一台电脑上的其他客户端直接复用，不用再登录；
4. 检查安装结果并告诉你。之后的采集和上报都在后台自动进行。

## 给 AI 的说明

打开 [Releases](https://github.com/reyfeng9-glitch/observer/releases)，取**最新发布的那个版本（包括 pre-release）**，下载其中的 `observer-v*.zip` 和对应的 `.sha256`，校验后解压，再按 zip 内 `README.md` 的 **One-sentence installation** 一节执行。不要向用户索要路径、ID 或密钥。

## 当前版本

- `v0.2.11-test`（测试版，pre-release）：只连接测试环境 `192.168.17.121`，用于验收。
