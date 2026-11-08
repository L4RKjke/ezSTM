from pathlib import Path

from ezstm.config import PAYLOAD


THEME_PATH = Path(__file__).with_name("theme.qss")
CHECK_ICON_PLACEHOLDER = "__CHECK_BOX_ICON_PATH__"


def load_stylesheet():
    """Читает theme.qss и подставляет путь к иконке галочки."""
    check_icon = (PAYLOAD / "check.svg").as_posix()
    text = THEME_PATH.read_text(encoding="utf-8")
    return text.replace(CHECK_ICON_PLACEHOLDER, check_icon)
