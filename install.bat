@echo off
chcp 65001 >nul
title 简历评估系统 — 安装

echo ============================================
echo   简历评估系统 — 安装
echo ============================================
echo.

:: 获取当前目录
set "APP_DIR=%~dp0"
set "APP_EXE=%APP_DIR%简历评估\简历评估.exe"

:: 检查文件是否存在
if not exist "%APP_EXE%" (
    echo [错误] 找不到简历评估.exe
    echo 请确保解压后文件夹结构完整
    pause
    exit /b 1
)

:: 创建桌面快捷方式
echo [1/2] 创建桌面快捷方式...
set "DESKTOP=%USERPROFILE%\Desktop"
set "SHORTCUT=%DESKTOP%\简历评估系统.lnk"

powershell -Command ^
  "$WScriptShell = New-Object -ComObject WScript.Shell; $Shortcut = $WScriptShell.CreateShortcut('%SHORTCUT%'); $Shortcut.TargetPath = '%APP_EXE%'; $Shortcut.WorkingDirectory = '%APP_DIR:\=/%简历评估'; $Shortcut.Description = '简历自动评估系统'; $Shortcut.Save()"

if exist "%SHORTCUT%" (
    echo   桌面快捷方式已创建: 简历评估系统
) else (
    echo   创建快捷方式失败，请手动操作:
    echo   右键 %APP_EXE% → 发送到 → 桌面快捷方式
)

echo.
echo [2/2] ✅ 安装完成！
echo.
echo   桌面上的「简历评估系统」图标，双击即可打开。
echo.
echo ============================================
pause