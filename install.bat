@echo off
chcp 65001 >nul
rem 虚拟环境放在仓库目录下，不污染系统 Python。
rem 想换位置就设环境变量 VK_VENV 再运行。
if "%VK_VENV%"=="" set "VK_VENV=%~dp0.venv"

echo [1/4] 创建虚拟环境：%VK_VENV%
if not exist "%VK_VENV%\Scripts\python.exe" (
  python -m venv "%VK_VENV%"
)
if not exist "%VK_VENV%\Scripts\python.exe" (
  echo 创建失败。确认已安装 Python 3.10+ 并加入 PATH。
  pause
  exit /b 1
)

echo [2/4] 安装依赖（首次约 3-5 分钟）...
"%VK_VENV%\Scripts\python.exe" -m pip install --upgrade pip
"%VK_VENV%\Scripts\python.exe" -m pip install sherpa-onnx PySide6 sounddevice numpy opencc-python-reimplemented
if errorlevel 1 (
  echo 依赖安装失败，检查网络后重试。
  pause
  exit /b 1
)

echo [3/4] 准备模型目录...
if not exist "%~dp0models" mkdir "%~dp0models"

echo [4/4] 下载 VAD 模型（约 2 MB）...
if not exist "%~dp0models\silero_vad.onnx" (
  curl -L -o "%~dp0models\silero_vad.onnx" "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
)

echo.
echo 安装完成。
echo 还差两个模型，按 README「模型」一节下载后放进 models/：
echo   - Qwen3-ASR-0.6B int8（识别，约 950 MB）
echo   - Qwen3-1.7B-Q4_K_M.gguf（整理，约 1.2 GB），以及 bin/llama/ 里的 llama-server
echo.
echo 之后双击 start.vbs 静默启动，或 run.bat 带控制台启动。
pause
