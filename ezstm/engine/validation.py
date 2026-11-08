import re
from pathlib import Path


NAME_PATTERN = r"[A-Za-z0-9 _.\-]{1,16}"


def validate(mode, options):
    folder = Path(options["folder"])
    if not folder.is_dir():
        raise RuntimeError("Выберите существующую папку с образами.")

    if mode == "write":
        image = Path(options.get("image", ""))
        if not image.is_file() or image.suffix.lower() != ".bin":
            raise RuntimeError("Выберите BIN-образ.")
        if image.stat().st_size == 0:
            raise RuntimeError("Выбранный BIN-образ пуст.")

        if options["names"]:
            # Исключает внедрение дополнительных CLI-команд.
            # Более длинные имена могут не поддерживаться прошивкой.
            for label, value in (
                ("Имя борта", options["craft"]),
                ("Имя пилота", options["pilot"]),
            ):
                if not re.fullmatch(NAME_PATTERN, value):
                    raise RuntimeError(
                        f"{label}: используйте 1–16 символов: "
                        "латиницу, цифры, пробел, _, . или -."
                    )
