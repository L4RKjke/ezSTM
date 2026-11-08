import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "ezSTMtool"

# Только распространённые USB VID STM32.
# Порты с другим VID можно явно выбрать в интерфейсе.
STM32_VIDS = {0x0483}

# Идентификатор DFU-устройства в Windows Configuration Manager.
DFU_ID_PREFIX = "USB\\VID_0483&PID_DF11\\"

# Корень проекта: ezstm/config.py -> ezstm -> корень.
# В сборке PyInstaller используется временная папка _MEIPASS.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
PAYLOAD = ROOT / "payload"

APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
APPDIR = APPDATA / APP_NAME
RUNTIME = APPDIR / "runtime"
DEFAULT_IMAGES = Path.home() / "Documents" / APP_NAME / "images"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
