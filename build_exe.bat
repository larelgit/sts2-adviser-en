@echo off
REM build_exe.bat — 一键打包 STS2 Adviser 为可分发 EXE
REM 用法：双击运行，或在项目根目录执行 build_exe.bat

echo ======================================================
echo  STS2 Adviser — PyInstaller 打包脚本
echo ======================================================

REM 检查 PyInstaller 是否已安装
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo [!] 未找到 PyInstaller，正在安装...
    pip install pyinstaller
)

REM 清理旧的 build / dist 目录
echo [1/3] 清理旧构建...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

REM 运行打包
echo [2/3] 运行 PyInstaller...
pyinstaller sts2_adviser.spec

if errorlevel 1 (
    echo [!] 打包失败，请检查上方错误信息。
    pause
    exit /b 1
)

REM 提示完成
echo [3/3] 打包完成！
echo.
echo 输出目录：dist\sts2_adviser\
echo 运行文件：dist\sts2_adviser\sts2_adviser.exe
echo.
echo 提示：将整个 dist\sts2_adviser\ 文件夹压缩为 zip 分发给用户。
echo       用户解压后双击 sts2_adviser.exe 即可运行，无需安装 Python。
echo.
pause
