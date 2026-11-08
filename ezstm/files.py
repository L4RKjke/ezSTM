import shutil

from ezstm.config import APPDIR, DEFAULT_IMAGES, PAYLOAD, RUNTIME


REQUIRED_FILES = ("read.bat", "write.bat")


def prepare_files():
    APPDIR.mkdir(parents=True, exist_ok=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    DEFAULT_IMAGES.mkdir(parents=True, exist_ok=True)

    if not PAYLOAD.is_dir():
        raise RuntimeError("Не найдена папка payload.")

    # Рабочие файлы не остаются во временной папке PyInstaller:
    # результат чтения не потеряется после закрытия программы.
    for item in PAYLOAD.iterdir():
        dest = RUNTIME / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    default_fw = DEFAULT_IMAGES / "fw.bin"
    if not default_fw.exists() and (PAYLOAD / "fw.bin").exists():
        shutil.copy2(PAYLOAD / "fw.bin", default_fw)


def verify_runtime_files():
    for filename in REQUIRED_FILES:
        path = RUNTIME / filename
        if not path.is_file():
            raise FileNotFoundError(
                f"Не найден обязательный файл:\n{path}"
            )
