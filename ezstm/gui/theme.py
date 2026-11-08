from pathlib import Path

from ezstm.config import PAYLOAD


THEME_DIR = Path(__file__).parent
BASE_FILE = THEME_DIR / "theme.qss"
DEFAULT_THEME = "dark"

# ключ -> (название в меню, файл с цветами)
THEMES = {
    "dark": ("Тёмная", "dark_theme.qss"),
    "light": ("Светлая", "white_theme.qss"),
}

CHECK_ICON_PLACEHOLDER = "CHECK_BOX_ICON_PATH"


def load_stylesheet(theme=DEFAULT_THEME):
    """Склеивает общий theme.qss и файл выбранной темы."""
    if theme not in THEMES:
        theme = DEFAULT_THEME

    _, file_name = THEMES[theme]
    base = BASE_FILE.read_text(encoding="utf-8")
    colors = (THEME_DIR / file_name).read_text(encoding="utf-8")
    check_icon = (PAYLOAD / "check.svg").as_posix()

    return (base + "\n" + colors).replace(CHECK_ICON_PLACEHOLDER, check_icon)
