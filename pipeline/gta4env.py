"""GTA IV 安装路径等环境配置（其它脚本统一从这里拿，别再到处写死绝对路径）。

环境变量（都可以不设，默认值就是本机实测的那套）：
    GTAIV_DIR   游戏 GTAIV 目录（里面有 GTAIV.exe 和 pc/）
    GPUSTACK_API_KEY  用 GPUStack 跑 ASR/翻译时需要
"""
import os

GTAIV_DIR = os.environ.get(
    "GTAIV_DIR",
    os.path.expanduser("~/.steam/debian-installation/steamapps/common/Grand Theft Auto IV/GTAIV"),
)
SFX_DIR = os.path.join(GTAIV_DIR, "pc/audio/sfx")
PLUGIN_DIR = os.path.join(GTAIV_DIR, "plugins")

GPUSTACK_API = "https://gpustack.rock-chips.com/v1"
GPUSTACK_KEY = os.environ.get("GPUSTACK_API_KEY", "")
ASR_MODEL = os.environ.get("GTA4_ASR_MODEL", "qwen3-asr-1.7b")
