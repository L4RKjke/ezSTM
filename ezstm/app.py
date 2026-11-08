import ctypes
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from ezstm.config import APP_NAME, APPDIR, PAYLOAD, RUNTIME
from ezstm.files import prepare_files, verify_runtime_files
from ezstm.gui.main_window import MainWindow


def _show_text_window(app, title, text, size):
    window = QPlainTextEdit()
    window.setReadOnly(True)
    window.setWindowTitle(title)
    window.setPlainText(text)
    window.resize(*size)
    window.show()
    return app.exec()


def main():
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ezSTM.Desktop"
        )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    icon = QIcon(str(PAYLOAD / "icon.ico"))
    app.setWindowIcon(icon)

    # Создание папки приложения тоже может завершиться ошибкой.
    try:
        APPDIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return _show_text_window(
            app,
            "ezSTM — ошибка запуска",
            f"Не удалось создать папку приложения:\n{APPDIR}\n\n{exc}",
            (700, 220),
        )

    # Запрещаем второй экземпляр до копирования рабочих файлов.
    lock = QLockFile(str(APPDIR / "ezSTM.lock"))
    lock.setStaleLockTime(0)

    if not lock.tryLock(100):
        label = QLabel(
            "Не удалось получить блокировку.\n"
            "Возможно, ezSTM уже запущен."
        )
        label.setWindowTitle(APP_NAME)
        label.resize(420, 100)
        label.show()
        return app.exec()

    try:
        prepare_files()
        verify_runtime_files()
    except Exception as exc:
        return _show_text_window(
            app,
            "ezSTM — ошибка запуска",
            "Не удалось подготовить рабочие файлы.\n\n"
            f"Источник: {PAYLOAD}\n"
            f"Назначение: {RUNTIME}\n\n"
            f"{type(exc).__name__}: {exc}",
            (700, 300),
        )

    window = MainWindow()
    window.setWindowIcon(icon)
    window.show()

    return app.exec()