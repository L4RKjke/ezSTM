from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGraphicsDropShadowEffect, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QTextCursor  # noqa: F811  (оставлено для ясности)
from pathlib import Path

from serial.tools import list_ports

from ezstm.config import APP_NAME, DEFAULT_IMAGES, RUNTIME
from ezstm.engine.worker import Engine
from ezstm.gui.theme import load_stylesheet
from ezstm.gui.widgets import make_scroll


TITLE_STYLE = """
QLabel {
    font-family: "Segoe UI";
    font-size: 13pt;
    font-weight: 600;
    color: #effaff;

    padding: 8px 20px;

    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 rgba(235, 250, 255, 55),
        stop: 0.45 rgba(185, 225, 255, 18),
        stop: 1 rgba(130, 195, 255, 30)
    );

    border: 1px solid rgba(210, 240, 255, 100);
    border-top-color: rgba(245, 253, 255, 190);
    border-bottom-color: rgba(160, 205, 255, 50);
    border-radius: 15px;
}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 780)
        self.setMinimumSize(760, 500)
        self.working = False
        self.rows = {}

        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(load_stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addWidget(self._build_title())

        self.status = QLabel("FC не подключён")
        root.addWidget(self.status)

        # Вся рабочая область прокручивается, если окно слишком узкое.
        root.addWidget(make_scroll(self._build_workspace()), 1)

        self.engine = Engine()
        self._connect_signals()

        self.refresh_ports()
        self.reload_images()
        self.send_settings()
        self.engine.start()

        self.append_log(f"Рабочая папка: {RUNTIME}")
        self.append_log(
            "Работа со скриптами без изменения их содержимого. "
            "Точный прогресс STM32CLI скрыт самими скриптами."
        )

    # ------------------------------------------------------------------ layout

    def _build_title(self):
        title = QLabel("ezSTM - инструмент прошивки STM32")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(TITLE_STYLE)

        glow = QGraphicsDropShadowEffect(title)
        glow.setBlurRadius(24)
        glow.setOffset(0, 2)
        glow.setColor(QColor(125, 200, 255, 75))
        title.setGraphicsEffect(glow)
        return title

    def _build_workspace(self):
        workspace = QWidget()
        workspace.setMinimumWidth(1060)
        layout = QHBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(self.splitter)

        self.splitter.addWidget(make_scroll(self._build_left_panel()))
        self.splitter.addWidget(self._build_right_panel())
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setSizes([400, 700])
        return workspace

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
        self.port.addItem("Автоматически — STM32 USB", "")
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
        self.browse_button = QPushButton("Папка…")
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

        self.erase = QCheckBox("Полное стирание чипа")
        self.names = QCheckBox("Записать имя")
        self.names.setChecked(True)
        self.calibrate_box = QCheckBox("Калибровка акселерометра")

        layout.addWidget(self.erase)
        layout.addWidget(self.names)

        self.names_widget = QWidget()
        names_layout = QFormLayout(self.names_widget)
        names_layout.setContentsMargins(0, 0, 0, 0)
        self.craft = QLineEdit("FC")
        self.pilot = QLineEdit("PILOT")
        self.craft.setMaxLength(16)
        self.pilot.setMaxLength(16)
        names_layout.addRow("Имя борта:", self.craft)
        names_layout.addRow("Имя пилота:", self.pilot)

        layout.addWidget(self.names_widget)
        layout.addWidget(self.calibrate_box)

        self.write_button = QPushButton("Записать образ")
        self.write_button.setProperty("accent", True)
        layout.addWidget(self.write_button)

        note = QLabel(
            "Для имён нужна поддержка craft_name и pilot_name.\n"
            "Калибровка: FC неподвижен и установлен горизонтально."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        return tab

    def _build_read_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)

        note = QLabel(
            "Результат сохраняется в выбранную папку:\n"
            "read_дата_время.bin\n\n"
            "Исходный read.bat запускается без изменений."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        self.read_button = QPushButton("Считать прошивку")
        self.read_button.setProperty("accent", True)
        layout.addWidget(self.read_button)
        layout.addStretch()
        return tab

    def _build_info_box(self):
        box = QGroupBox("Данные FC")
        layout = QFormLayout(box)
        self.info_labels = {}

        for key, text in (
            ("port", "Подключение:"),
            ("model", "Модель:"),
            ("target", "Target:"),
            ("firmware", "Прошивка:"),
            ("craft", "Имя борта:"),
            ("pilot", "Имя пилота:"),
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
        right.setMinimumWidth(620)
        layout = QVBoxLayout(right)

        layout.addWidget(QLabel("Операции"))
        self.operations = QTreeWidget()
        self.operations.setHeaderLabels(["Операция", "Статус"])
        self.operations.setRootIsDecorated(False)
        self.operations.setColumnWidth(0, 300)
        self.operations.setMinimumHeight(180)
        layout.addWidget(self.operations, 2)

        layout.addWidget(QLabel("Консоль"))
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(12000)
        layout.addWidget(self.console, 5)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p% — выполнение этапов")
        layout.addWidget(self.progress)
        return right

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

        self.browse_button.clicked.connect(self.choose_folder)
        self.reload_images_button.clicked.connect(self.reload_images)
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.names.toggled.connect(self.names_widget.setVisible)

        self.connect_button.clicked.connect(
            lambda: self.start_command("connect")
        )
        self.write_button.clicked.connect(
            lambda: self.start_command("write")
        )
        self.read_button.clicked.connect(
            lambda: self.start_command("read")
        )

        for check in (
            self.auto_connect, self.auto_write, self.names,
            self.erase, self.calibrate_box,
        ):
            check.toggled.connect(self.send_settings)

        self.craft.textChanged.connect(self.send_settings)
        self.pilot.textChanged.connect(self.send_settings)
        self.image.currentIndexChanged.connect(self.send_settings)
        self.port.currentIndexChanged.connect(self.send_settings)

    # ----------------------------------------------------------- state

    def options(self):
        return {
            "folder": self.folder.text(),
            "image": self.image.currentData() or "",
            "port": self.port.currentData() or "",
            "auto_connect": self.auto_connect.isChecked(),
            "auto_write": self.auto_write.isChecked(),
            "names": self.names.isChecked(),
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
        self.port.addItem("Автоматически — STM32 USB", "")

        for item in list_ports.comports():
            self.port.addItem(
                f"{item.device} — {item.description}", item.device
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

    # ----------------------------------------------------------- slots

    def append_log(self, text):
        self.console.appendPlainText(text)
        self.console.moveCursor(QTextCursor.MoveOperation.End)

    def update_info(self, information):
        for key, label in self.info_labels.items():
            label.setText(str(information.get(key, "—")))

    def update_connection(self, connected, text):
        self.status.setText(text)
        color = "#46d58b" if connected else "#ffaf58"
        self.status.setStyleSheet(f"color: {color}; font-weight: 600;")

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
            self.auto_connect, self.auto_write, self.erase, self.names,
            self.craft, self.pilot, self.calibrate_box,
        ):
            widget.setEnabled(not value)

    def closeEvent(self, event):
        if self.working:
            # Не закрываем приложение посреди записи, без диалогов.
            self.append_log("Закрытие доступно после завершения операции.")
            event.ignore()
            return

        self.engine.stop_requested.set()

        # Поток может в этот момент завершать проверку USB/MSP.
        if not self.engine.wait(150):
            event.ignore()
            QTimer.singleShot(300, self.close)
            return

        event.accept()
