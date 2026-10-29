@echo off
chcp 65001 >nul
setlocal
rem 虚拟环境放在仓库目录下，不污染系统 Python。
rem 想换位置就设环境变量 VK_VENV 再运行。
if "%VK_VENV%"=="" set "VK_VENV=%~dp0.venv"

rem  install.bat        只装识别（约 940 MB），最快能用
rem  install.bat full   连整理功能一起（约 1.9 GB）
set "GROUPS=--group vad,asr"
set "EXTRA=0"
if /i "%~1"=="full" (
  set "GROUPS=--all"
  set "EXTRA=1"
)

echo ============================================
echo   Verbalkey 安装
echo ============================================
echo.

echo [1/4] 创建虚拟环境：%VK_VENV%
if not exist "%VK_VENV%\Scripts\python.exe" (
  python -m venv "%VK_VENV%"
)
if not exist "%VK_VENV%\Scripts\python.exe" (
  echo 创建失败。确认已安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)

echo.
echo [2/4] 安装依赖（首次约 3-5 分钟）...
"%VK_VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%VK_VENV%\Scripts\python.exe" -m pip install sherpa-onnx PySide6 sounddevice numpy opencc-python-reimplemented
if errorlevel 1 (
  echo.
  echo 依赖安装失败。多半是网络，换清华源重试：
  echo   "%VK_VENV%\Scripts\python.exe" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple sherpa-onnx PySide6 sounddevice numpy opencc-python-reimplemented
  pause
  exit /b 1
)

echo.
echo [3/4] 下载模型（支持断点续传，已有的会跳过）...
if %EXTRA%==0 (
  echo       只装识别功能，约 940 MB。
  echo       要连整理功能一起装：install.bat full
)
echo.
"%VK_VENV%\Scripts\python.exe" "%~dp0tools\fetch_models.py" %GROUPS%
if errorlevel 1 (
  echo.
  echo 模型没下全。重跑一次 install.bat 会接着下，不会重来。
  pause
  exit /b 1
)

echo.
echo [4/4] 复查...
"%VK_VENV%\Scripts\python.exe" "%~dp0tools\fetch_models.py" --check

echo.
echo ============================================
echo   装好了。双击 start.vbs 启动。
echo ============================================
if %EXTRA%==0 (
  echo.
  echo 现在只有识别功能：按住右 Alt 说话，松开就上屏。
  echo 想要「去口水词 / 整理成书面语」，补一条命令：
  echo   "%VK_VENV%\Scripts\python.exe" tools\fetch_models.py --group polish,llama
  echo 然后到 设置 - 口述整理，把强度调到 2 或 3。
)
echo.
pause
