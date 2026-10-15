#!/bin/bash
# Observer 安装（macOS）：解压后双击运行。第一次如果提示无法打开：右键 → 打开；
# macOS 15 及以上：先点“完成”，再到 系统设置 → 隐私与安全性，点“仍要打开”。
/bin/bash -c "$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/LoctekIT-AI/observer/main/install/install-macos.sh)"
echo
read -r -p "按回车键关闭窗口" _
