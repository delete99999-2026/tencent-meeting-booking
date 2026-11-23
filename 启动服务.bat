@echo off
rem ============================================================
rem  腾讯会议预订平台 - 启动脚本
rem  双击运行：初始化数据库 -> 启动 Flask 服务（含后台任务 Worker）
rem  浏览器访问：http://127.0.0.1:8000
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH。
    pause
    exit /b 1
)

python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖 requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试。
        pause
        exit /b 1
    )
)

echo.
echo ===== 腾讯会议预订平台 启动中 =====
echo 访问地址: http://127.0.0.1:8000
echo 初始超级管理员: admin / Admin@123456 （首次登录后请立即修改）
echo 按 Ctrl+C 停止服务
echo.
python -m server.app
pause
