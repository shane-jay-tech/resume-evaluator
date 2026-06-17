#!/bin/bash
# 简历评估系统 — Mac 一键安装（创建桌面快捷方式）

echo "🚀 简历评估系统 — 安装中..."

# 获取当前目录（安装包所在位置）
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$APP_DIR/简历评估.app"
DESKTOP="$HOME/Desktop"
LAUNCHER="$DESKTOP/简历评估系统.command"

# 创建桌面启动器（双击它就能打开系统）
cat > "$LAUNCHER" << 'LAUNCHER_EOF'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
# 启动服务器并打开浏览器
open "$DIR/../简历评估.app"
echo "简历评估系统已启动，浏览器将自动打开。"
echo "如果没有自动打开，请访问 http://127.0.0.1:18980"
LAUNCHER_EOF

chmod +x "$LAUNCHER"

# 尝试设为可信任（避免 macOS Gatekeeper 警告）
xattr -d com.apple.quarantine "$APP_PATH" 2>/dev/null
xattr -d com.apple.quarantine "$LAUNCHER" 2>/dev/null

echo ""
echo "✅ 安装完成！"
echo "桌面已创建快捷方式：简历评估系统.command"
echo "双击它即可打开系统。"
echo ""
read -p "按回车键退出..." dummy
