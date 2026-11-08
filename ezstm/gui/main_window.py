import json
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QBrush, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from serial.tools import list_ports

from ezstm.config import APP_NAME, DEFAULT_IMAGES, RUNTIME
from ezstm.engine.worker import Engine
from ezstm.gui.theme import load_stylesheet
from ezstm.gui.widgets import make_scroll


JOURNAL_LIMIT = 5000

def bump_trailing_number(text: str, step: int = 1) -> str:
    """E21-540 -> E21-541, ведущие нули сохраняются: E21-009 -> E21-010."""
    text = text.strip()
    match = re.search(r"(\d+)$", text)
    if not match:
        return text
    digits = match.group(1)
    return text[:match.start(1)] + str(int(digits) + step).zfill(len(digits))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)
        self.setMinimumSize(900, 560)
        self.working = False
        self.connected = False
        self.rows = {}
        self.last_info = {}
        self.settings = QSettings("ezSTM", "ezSTM")
        self.journal = self.load_journal()

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(load_stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Верх: статус подключения и журнал последних дронов.
        self.status = QLabel("FC не подключён")
        root.addWidget(self.status)

        # Низ: слева настройки, справа журналы, операции и консоль.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(make_scroll(self._build_left_panel()))
        self.splitter.addWidget(make_scroll(self._build_right_panel()))
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setSizes([420, 760])
        root.addWidget(self.splitter, 1)

        self.restore_prefs()

        self.engine = Engine()
        self._connect_signals()

        self.refresh_journal_views()
        self.refresh_ports()
        self.reload_images()
        self.on_options_changed()
        self.engine.start()

        self.append_log(f"Рабочая папка: {RUNTIME}")
        self.append_log(
            "Работа со скриптами без изменения их содержимого. "
            "Точный прогресс STM32CLI скрыт самими скриптами."
        )

    # ------------------------------------------------------------ layout


    def _build_left_panel(self):
        left = QWidget()
        left.setMinimumWidth(380)
        layout = QVBoxLayout(left)

        layout.addWidget(self._build_connection_box())
        layout.addWidget(self._build_images_box())
        layout.addWidget(self._build_tabs())
        layout.addWidget(self._build_info_box())
        layout.addStretch()
        return left

    def _build_connection_box(self):
        box = QGroupBox("Подключение")
        layout = QVBoxLayout(box)

        port_row = QHBoxLayout()
        self.port = QComboBox()
        self.port.addItem("Автоматически - STM32 USB", "")
        self.refresh_ports_button = QPushButton("Обновить")
        port_row.addWidget(self.port, 1)
        port_row.addWidget(self.refresh_ports_button)
        layout.addLayout(port_row)

        self.auto_connect = QCheckBox("Подключаться автоматически")
        self.auto_connect.setChecked(True)
        self.auto_write = QCheckBox("Прошивать FC при подключении")
        self.connect_button = QPushButton("Подключиться")

        layout.addWidget(self.auto_connect)
        layout.addWidget(self.auto_write)
        layout.addWidget(self.connect_button)
        return box

    def _build_images_box(self):
        box = QGroupBox("Образы")
        layout = QVBoxLayout(box)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(DEFAULT_IMAGES))
        self.folder.setReadOnly(True)
        self.browse_button = QPushButton("Папка...")
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(self.browse_button)
        layout.addLayout(folder_row)

        self.image = QComboBox()
        self.reload_images_button = QPushButton("Обновить список образов")
        layout.addWidget(self.image)
        layout.addWidget(self.reload_images_button)
        return box

    def _build_tabs(self):
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_write_tab(), "Прошить")
        self.tabs.addTab(self._build_read_tab(), "Считать прошивку")
        return self.tabs

    def _build_write_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        functions_box = QGroupBox("Функции")
        functions_layout = QVBoxLayout(functions_box)
        self.names_check = QCheckBox("Добавить имя пилота / имя борта")
        self.names_check.setChecked(True)
        self.erase = QCheckBox("Полное стирание чипа")
        self.calibrate_box = QCheckBox("Калибровать акселерометр")
        functions_layout.addWidget(self.names_check)
        functions_layout.addWidget(self.erase)
        functions_layout.addWidget(self.calibrate_box)
        layout.addWidget(functions_box)

        layout.addWidget(self._build_names_box())

        self.write_button = QPushButton("Записать образ")
        self.write_button.setProperty("accent", True)
        layout.addWidget(self.write_button)
        layout.addStretch()

        return tab

    def _build_names_box(self):
        self.names_box = QGroupBox("Имя")
        layout = QVBoxLayout(self.names_box)

        form = QFormLayout()
        self.craft = QLineEdit("FC")
        self.pilot = QLineEdit("PILOT")
        self.craft.setMaxLength(16)
        self.pilot.setMaxLength(16)
        form.addRow("Имя борта:", self.craft)
        form.addRow("Имя пилота:", self.pilot)
        layout.addLayout(form)

        self.auto_name = QCheckBox("Увеличивать имя пилота на 1 после прошивки")
        layout.addWidget(self.auto_name)
        return self.names_box


    def _build_read_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.read_button = QPushButton("Считать прошивку")
        self.read_button.setProperty("accent", True)
        layout.addWidget(self.read_button)
        layout.addStretch()
        return tab

    def _build_info_box(self):
        box = QGroupBox("Данные FC")
        layout = QFormLayout(box)
        layout.setVerticalSpacing(4)

        self.info_labels = {}
        for key, text in (
            ("link", "Порт / модель:"),
            ("hardware", "Target / прошивка:"),
            ("names", "Борт / пилот:"),
        ):
            label = QLabel("—")
            label.setWordWrap(True)
            label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.info_labels[key] = label
            layout.addRow(text, label)
        return box

    def _build_right_panel(self):
        right = QWidget()
        right.setMinimumWidth(560)
        layout = QVBoxLayout(right)

        self.journal_title = QLabel("Журнал прошивок")
        layout.addWidget(self.journal_title)
        self.journal_tree = QTreeWidget()
        self.journal_tree.setHeaderLabels(self.JOURNAL_COLUMNS)
        self.journal_tree.setRootIsDecorated(False)
        self.journal_tree.setAlternatingRowColors(True)
        self.journal_tree.setMinimumHeight(200)
        layout.addWidget(self.journal_tree, 3)

        layout.addWidget(QLabel("Операции"))
        self.operations = QTreeWidget()
        self.operations.setHeaderLabels(["Операция", "Статус"])
        self.operations.setRootIsDecorated(False)
        self.operations.setColumnWidth(0, 300)
        self.operations.setMinimumHeight(160)
        layout.addWidget(self.operations, 2)

        layout.addWidget(QLabel("Консоль"))
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(12000)
        self.console.setMinimumHeight(220)
        layout.addWidget(self.console, 4)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p% - выполнение этапов")
        layout.addWidget(self.progress)
        return right

    JOURNAL_COLUMNS = [
        "Дата", "Борт", "Пилот", "Модель", "Target", "Прошивка", "Образ",
    ]

    # ----------------------------------------------------------- signals

    def _connect_signals(self):
        
        engine = self.engine
        engine.log.connect(self.append_log)
        engine.info.connect(self.update_info)
        engine.connection.connect(self.update_connection)
        engine.operation.connect(self.update_operation)
        engine.progress.connect(self.update_progress)
        engine.busy.connect(self.set_busy)
        engine.reset_operations.connect(self.reset_operations)
        engine.images_changed.connect(self.reload_images)
        engine.written.connect(self.on_write_success)

        self.browse_button.clicked.connect(self.choose_folder)
        self.reload_images_button.clicked.connect(self.reload_images)
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.journal_tree.itemDoubleClicked.connect(self.pick_recent)

        self.connect_button.clicked.connect(
            lambda: self.start_command("connect")
        )
        self.write_button.clicked.connect(
            lambda: self.start_command("write")
        )
        self.read_button.clicked.connect(
            lambda: self.start_command("read")
        )

        for check in (self.auto_connect, self.auto_write,
                      self.erase, self.calibrate_box):
            check.toggled.connect(self.send_settings)

        self.names_check.toggled.connect(self.on_options_changed)
        self.auto_name.toggled.connect(self.on_options_changed)
        

        self.craft.textChanged.connect(self.send_settings)
        self.pilot.textChanged.connect(self.send_settings)
        self.image.currentIndexChanged.connect(self.send_settings)
        self.port.currentIndexChanged.connect(self.send_settings)
    # ------------------------------------------------------- preferences

    def restore_prefs(self):
        self.names_check.setChecked(
            self.settings.value("names", True, type=bool)
        )
        self.auto_name.setChecked(
            self.settings.value("auto_name", False, type=bool)
        )
        self.pilot.setText(str(self.settings.value("pilot", "PILOT")))

    def save_prefs(self):
        self.settings.setValue("names", self.names_check.isChecked())
        self.settings.setValue("auto_name", self.auto_name.isChecked())
        self.settings.setValue("pilot", self.pilot.text().strip())


    def on_options_changed(self, *_):
        self.names_box.setVisible(self.names_check.isChecked())
        self.save_prefs()
        self.send_settings()

    # ----------------------------------------------------------- state

    def options(self):
        return {
            "folder": self.folder.text(),
            "image": self.image.currentData() or "",
            "port": self.port.currentData() or "",
            "auto_connect": self.auto_connect.isChecked(),
            "auto_write": self.auto_write.isChecked(),
            "names": self.names_check.isChecked(),
            "craft": self.craft.text().strip(),
            "pilot": self.pilot.text().strip(),
            "erase": self.erase.isChecked(),
            "calibrate": self.calibrate_box.isChecked(),
        }

    def send_settings(self, *_):
        self.engine.submit("settings", self.options())

    def refresh_ports(self):
        selected = self.port.currentData()
        self.port.blockSignals(True)
        self.port.clear()
        self.port.addItem("Автоматически - STM32 USB", "")

        for item in list_ports.comports():
            self.port.addItem(
                f"{item.device} - {item.description}", item.device
            )

        index = self.port.findData(selected)
        self.port.setCurrentIndex(max(index, 0))
        self.port.blockSignals(False)
        self.send_settings()

    def reload_images(self):
        selected = self.image.currentData()
        self.image.blockSignals(True)
        self.image.clear()
        folder = Path(self.folder.text())

        if folder.is_dir():
            for image in sorted(folder.iterdir()):
                if image.is_file() and image.suffix.lower() == ".bin":
                    self.image.addItem(image.name, str(image))

        index = self.image.findData(selected)
        self.image.setCurrentIndex(max(index, 0))
        self.image.blockSignals(False)
        self.send_settings()

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Папка с BIN-образами", self.folder.text()
        )
        if path:
            self.folder.setText(path)
            self.reload_images()

    def start_command(self, command):
        if self.working:
            return
        # Блокируем кнопки немедленно, до обработки команды потоком.
        self.set_busy(True)
        self.engine.submit(command, self.options())

    # ---------------------------------------------------------- journal

    def load_journal(self):
        raw = self.settings.value("journal", "[]")
        try:
            data = json.loads(str(raw))
        except (TypeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [rec for rec in data if isinstance(rec, dict)]

    def save_journal(self):
        self.journal = self.journal[-JOURNAL_LIMIT:]
        self.settings.setValue(
            "journal", json.dumps(self.journal, ensure_ascii=False)
        )

    def add_journal_record(self, craft, pilot):
        info = self.last_info
        record = {
            "time": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "craft": craft,
            "pilot": pilot,
            "model": str(info.get("model", "")),
            "target": str(info.get("target", "")),
            "firmware": str(info.get("firmware", "")),
            "port": str(info.get("port", "")),
            "image": Path(self.image.currentData() or "").name,
        }
        self.journal.append(record)
        self.save_journal()

    def _journal_item(self, rec):
        def text(key):
            return str(rec.get(key, "")) or "—"

        item = QTreeWidgetItem([
            text("time"), text("craft"), text("pilot"), text("model"),
            text("target"), text("firmware"), text("image"),
        ])
        item.setData(0, Qt.ItemDataRole.UserRole, rec)
        return item

    def refresh_journal_views(self):
        self.journal_tree.clear()
        for rec in reversed(self.journal):
            self.journal_tree.addTopLevelItem(self._journal_item(rec))

        self.journal_title.setText(
            f"Журнал прошивок (всего записей: {len(self.journal)})"
        )


    def pick_recent(self, item, _column=0):
        rec = item.data(0, Qt.ItemDataRole.UserRole)
        if not rec:
            return
        self.craft.setText(str(rec.get("craft", "")))
        self.pilot.setText(str(rec.get("pilot", "")))

    def on_write_success(self):
        # Запись в журнал делаем до изменения имени пилота,
        # чтобы сохранить имена, которые реально были записаны в FC.
        names_written = self.names_check.isChecked()
        craft = self.craft.text().strip() if names_written else ""
        pilot = self.pilot.text().strip() if names_written else ""

        self.add_journal_record(craft, pilot)
        self.refresh_journal_views()
        self.append_log("Запись добавлена в журнал прошивок")

        # Пилот меняется только если записывались имена и включена галочка.
        if not (names_written and self.auto_name.isChecked()):
            return

        old_pilot = self.pilot.text().strip()
        new_pilot = bump_trailing_number(old_pilot)
        if new_pilot != old_pilot:
            self.pilot.setText(new_pilot)
            self.save_prefs()
            self.append_log(f"Имя пилота обновлено: {old_pilot} → {new_pilot}")


    # ----------------------------------------------------------- slots

    def append_log(self, text):
        self.console.appendPlainText(text)
        self.console.moveCursor(QTextCursor.MoveOperation.End)

    def update_info(self, information):
        self.last_info = dict(information or {})

        def value(key):
            return str(self.last_info.get(key, "—"))

        self.info_labels["link"].setText(
            f"{value('port')} / {value('model')}"
        )
        self.info_labels["hardware"].setText(
            f"{value('target')} / {value('firmware')}"
        )
        self.info_labels["names"].setText(
            f"{value('craft')} / {value('pilot')}"
        )

    def update_connection(self, connected, text):
        self.status.setText(text)
        color = "#46d58b" if connected else "#ffaf58"
        self.status.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.connected = connected

        if not connected:
            for label in self.info_labels.values():
                label.setText("—")


    def update_progress(self, value):
        self.progress.setValue(value)

        color = "#31c975" if value == 100 else "#e78a30"
        self.progress.setStyleSheet(
            "QProgressBar::chunk {"
            f"background: {color};"
            "border-radius: 4px;"
            "}"
        )

    def reset_operations(self):
        self.rows.clear()
        self.operations.clear()

    def update_operation(self, title, status):
        if title not in self.rows:
            self.rows[title] = QTreeWidgetItem([title, status])
            self.operations.addTopLevelItem(self.rows[title])

        item = self.rows[title]

        display_status = (
            "Операция выполняется"
            if status == "Выполняется"
            else status
        )
        item.setText(1, display_status)

        if status.startswith("Готово") or status == "Команды подтверждены FC":
            color = "#31c975"  # Зелёный
        elif status in ("Выполняется", "Операция выполняется"):
            color = "#ffd54f"  # Жёлтый
        elif status == "Ошибка":
            color = "#ff5252"  # Красный
        else:
            color = "#a9bbcc"  # Ожидание

        item.setForeground(1, QBrush(QColor(color)))

        font = item.font(1)
        font.setBold(status != "Ожидание")
        item.setFont(1, font)

    def set_busy(self, value):
        self.working = value
        for widget in (
            self.connect_button, self.write_button, self.read_button,
            self.browse_button, self.reload_images_button,
            self.refresh_ports_button, self.port, self.image,
            self.auto_connect, self.auto_write, self.erase,
            self.calibrate_box, self.names_check, self.auto_name,
            self.craft, self.pilot, self.journal_tree,
        ):
            widget.setEnabled(not value)



    def closeEvent(self, event):
        if self.working:
            # Не закрываем приложение посреди записи, без диалогов.
            self.append_log("Закрытие доступно после завершения операции.")
            event.ignore()
            return

        self.save_prefs()
        self.engine.stop_requested.set()

        # Поток может в этот момент завершать проверку USB/MSP.
        if not self.engine.wait(150):
            event.ignore()
            QTimer.singleShot(300, self.close)
            return

        event.accept()
