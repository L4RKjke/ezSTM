import ctypes
import datetime
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial
from serial.tools import list_ports

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QLockFile
from PySide6.QtGui import QTextCursor, QColor, QBrush, QIcon
from PySide6.QtWidgets import (
    QApplication, QGraphicsDropShadowEffect, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLabel, QPushButton, QCheckBox, QLineEdit,
    QComboBox, QFileDialog, QTabWidget, QScrollArea, QSplitter,
    QPlainTextEdit, QProgressBar, QTreeWidget, QTreeWidgetItem,
    QGroupBox,
)


APP_NAME = "ezSTM32"

# Только распространённые USB VID STM32.
# Порты с другим VID можно явно выбрать в интерфейсе.
STM32_VIDS = {0x0483}

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAYLOAD = ROOT / "payload"
APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
APPDIR = APPDATA / APP_NAME
RUNTIME = APPDIR / "runtime"
DEFAULT_IMAGES = Path.home() / "Documents" / APP_NAME / "images"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def dfu_devices():
    """Список присутствующих STM32 DFU через Windows Configuration Manager."""
    if os.name != "nt":
        return []

    cm = ctypes.WinDLL("cfgmgr32")
    get_size = cm.CM_Get_Device_ID_List_SizeW
    get_size.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    ]
    get_size.restype = ctypes.c_ulong

    get_list = cm.CM_Get_Device_ID_ListW
    get_list.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    get_list.restype = ctypes.c_ulong

    PRESENT = 0x00000100
    size = ctypes.c_ulong()

    if get_size(ctypes.byref(size), None, PRESENT) != 0:
        raise RuntimeError("Не удалось получить список USB-устройств Windows.")

    buffer = ctypes.create_unicode_buffer(size.value + 1)
    if get_list(None, buffer, size.value, PRESENT) != 0:
        raise RuntimeError("Список USB изменился. Повторите подключение.")

    ids = "".join(buffer).split("\0")
    return sorted({
        item for item in ids
        if item.upper().startswith("USB\\VID_0483&PID_DF11\\")
    })


def open_serial(port, timeout=0.15):
    device = serial.Serial()
    device.port = port
    device.baudrate = 115200
    device.timeout = timeout
    device.write_timeout = 2
    device.dtr = False
    device.rts = False
    device.open()
    return device


class MSP:
    """MSP v1 для стандартных команд Betaflight."""

    def __init__(self, port):
        self.device = open_serial(port)

    def close(self):
        self.device.close()

    def request(self, command, payload=b"", timeout=1.5):
        size = len(payload)
        checksum = size ^ command
        for byte in payload:
            checksum ^= byte

        frame = (
            b"$M<" + bytes([size, command]) + payload + bytes([checksum])
        )

        self.device.reset_input_buffer()
        self.device.write(frame)
        self.device.flush()

        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            data.extend(self.device.read(self.device.in_waiting or 1))

            while True:
                pos = data.find(b"$M")
                if pos < 0:
                    data[:] = data[-2:]
                    break
                if pos:
                    del data[:pos]
                if len(data) < 6:
                    break

                length = data[3]
                total = length + 6
                if len(data) < total:
                    break

                packet = bytes(data[:total])
                del data[:total]

                direction = packet[2]
                response_command = packet[4]
                body = packet[5:5 + length]

                check = length ^ response_command
                for byte in body:
                    check ^= byte

                if check != packet[-1] or response_command != command:
                    continue

                if direction == ord("!"):
                    raise RuntimeError(
                        f"FC не поддерживает MSP-команду {command}."
                    )
                if direction == ord(">"):
                    return body

        raise TimeoutError(f"Нет ответа MSP: команда {command}.")


def fc_information(port):
    msp = MSP(port)
    try:
        variant = msp.request(2, timeout=0.6).decode("ascii", "replace")
        if variant != "BTFL":
            raise RuntimeError(
                f"Обнаружена прошивка {variant}, требуется Betaflight."
            )

        version = msp.request(3)
        board = msp.request(4)
        try:
            craft = msp.request(10).decode("utf-8", "replace").rstrip("\0")
        except Exception:
            craft = "Недоступно"

        target = "Недоступно"
        model = board[:4].decode("ascii", "replace") if len(board) >= 4 else "?"
        # Расширенный MSP_BOARD_INFO:
        # board id(4), revision(2), type(1), capabilities(1),
        # target length(1), target(...), board name length(1), board name(...)
        if len(board) > 8:
            target_length = board[8]
            end = 9 + target_length
            if end <= len(board):
                target = board[9:end].decode("ascii", "replace") or model
                if end < len(board):
                    model_length = board[end]
                    if end + 1 + model_length <= len(board):
                        board_name = board[end + 1:end + 1 + model_length]
                        model = board_name.decode("ascii", "replace") or model

        return {
            "port": port,
            "model": model,
            "target": target,
            "firmware": "Betaflight " + ".".join(map(str, version[:3])),
            "craft": craft or "Не задано",
            "pilot": "Недоступно через MSP",
        }
    finally:
        msp.close()


class BFCLI:
    def __init__(self, port):
        self.device = open_serial(port)
        self.device.reset_input_buffer()
        self.device.write(b"#")
        reply = self.read_prompt(3)
        if "#" not in reply:
            self.close()
            raise RuntimeError("FC не перешёл в Betaflight CLI.")

    def close(self):
        self.device.close()

    def read_prompt(self, timeout=3):
        data = bytearray()
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()

        while time.monotonic() < deadline:
            chunk = self.device.read(self.device.in_waiting or 1)
            if chunk:
                data.extend(chunk)
                last_data = time.monotonic()
            elif (
                data.rstrip().endswith(b"#")
                and time.monotonic() - last_data > 0.15
            ):
                break

        return data.decode("utf-8", "replace")

    def command(self, command, timeout=3):
        self.device.reset_input_buffer()
        self.device.write((command + "\r\n").encode("ascii"))
        return self.read_prompt(timeout)

    def reboot_command(self, command):
        self.device.write((command + "\r\n").encode("ascii"))
        self.device.flush()
        time.sleep(0.15)

    def get_value(self, name):
        reply = self.command("get " + name)
        match = re.search(
            rf"^{re.escape(name)}\s*=\s*(.*?)\r?$",
            reply,
            re.MULTILINE,
        )
        return match.group(1).strip() if match else None

    def set_value(self, name, value):
        reply = self.command(f"set {name} = {value}")
        if re.search(
            r"unknown|invalid|###error|error:", reply, re.IGNORECASE
        ):
            raise RuntimeError(f"FC отклонил настройку {name}: {reply.strip()}")

        actual = self.get_value(name)
        if actual != value:
            raise RuntimeError(
                f"Не подтверждена настройка {name}: получено {actual!r}."
            )


class Engine(QThread):
    log = Signal(str)
    info = Signal(dict)
    connection = Signal(bool, str)
    operation = Signal(str, str)
    progress = Signal(int)
    busy = Signal(bool)
    reset_operations = Signal()
    images_changed = Signal()

    def __init__(self):
        super().__init__()
        self.commands = queue.Queue()
        self.stop_requested = threading.Event()
        self.settings = {}
        self.current = None

        # Автопрошивка запускается только один раз за цикл подключения.
        # Переход COM -> DFU -> COM внутри операции не считается
        # новым физическим подключением.
        self.consumed = False
        self.absent_since = None
        self.last_detection_error = ""

    def submit(self, command, settings=None):
        self.commands.put((command, dict(settings or {})))

    def message(self, text):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.emit(f"[{stamp}] {text}")

    def raw_devices(self):
        dfu = dfu_devices()
        selected = self.settings.get("port", "")
        ports = []
        for item in list_ports.comports():
            if selected:
                if item.device == selected:
                    ports.append(item.device)
            elif item.vid in STM32_VIDS:
                ports.append(item.device)

        return dfu, ports

    def detect(self):
        dfu, ports = self.raw_devices()

        # Скрипты жёстко используют usb1: при неоднозначности не запускаем.
        if len(dfu) + len(ports) > 1:
            raise RuntimeError(
                "Обнаружено несколько подходящих устройств. "
                "Оставьте подключённым один FC."
            )

        if dfu:
            return {"mode": "dfu", "id": dfu[0], "port": None}

        if ports:
            if (
                self.current
                and self.current.get("mode") == "serial"
                and self.current.get("port") == ports[0]
            ):
                return self.current

            information = fc_information(ports[0])
            return {
                "mode": "serial",
                "port": ports[0],
                "id": ports[0],
                "information": information,
            }

        return None

    def read_names(self, port):
        """Чтение имён через встроенный CLI, без Betaflight Configurator."""
        cli = BFCLI(port)
        try:
            craft = cli.get_value("craft_name")
            pilot = cli.get_value("pilot_name")
            return craft, pilot
        finally:
            try:
                # На части версий Betaflight exit перезагружает FC.
                cli.reboot_command("exit")
            finally:
                cli.close()

    def publish_device(self, device, read_names=False):
        if device["mode"] == "dfu":
            self.info.emit({
                "port": "USB / DFU",
                "model": "STM32 в DFU",
                "target": "Недоступно в DFU",
                "firmware": "Недоступно в DFU",
                "craft": "Недоступно в DFU",
                "pilot": "Недоступно в DFU",
            })
            self.connection.emit(True, "FC подключён: DFU")
            return

        information = dict(device["information"])
        self.connection.emit(True, f"FC подключён: {device['port']}")

        if read_names:
            self.message("Чтение имён через CLI.")
            try:
                craft, pilot = self.read_names(device["port"])
                if craft is not None:
                    information["craft"] = craft or "Не задано"
                if pilot is not None:
                    information["pilot"] = pilot or "Не задано"
                else:
                    information["pilot"] = "Не поддерживается этой версией"

                # Дожидаемся возвращения после выхода из CLI.
                self.wait_serial(timeout=20)#ff9b37
            except Exception as exc:
                self.message(f"Дополнительные сведения недоступны: {exc}")

        self.info.emit(information)

    def wait_serial(self, timeout=35):
        deadline = time.monotonic() + timeout
        last_error = ""

        while time.monotonic() < deadline:
            _, ports = self.raw_devices()
            if len(ports) > 1:
                raise RuntimeError("После перезапуска найдено несколько COM-портов.")
            if len(ports) == 1:
                try:
                    information = fc_information(ports[0])
                    self.current = {
                        "mode": "serial",
                        "port": ports[0],
                        "id": ports[0],
                        "information": information,
                    }
                    self.info.emit(information)
                    return ports[0]
                except Exception as exc:
                    last_error = str(exc)
            time.sleep(0.5)

        raise RuntimeError(
            "FC не вернулся в Betaflight по USB. "
            "Проверьте соответствие образа плате. " + last_error
        )

    def wait_dfu(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            devices = dfu_devices()
            if len(devices) > 1:
                raise RuntimeError("Обнаружено несколько STM32 DFU.")
            if len(devices) == 1:
                self.current = {
                    "mode": "dfu",
                    "id": devices[0],
                    "port": None,
                }
                # Небольшая задержка на готовность USB-драйвера.
                time.sleep(0.8)
                return
            time.sleep(0.4)
        raise RuntimeError("STM32 DFU не появился.")

    def enter_dfu(self):
        if self.current["mode"] == "dfu":
            self.message("FC уже находится в DFU.")
            return

        cli = BFCLI(self.current["port"])
        try:
            cli.reboot_command("bl")
        finally:
            cli.close()
        self.wait_dfu()

    def execute(self, args, timeout=300):
        """Вывод процесса в консоль; pause получает EOF через DEVNULL."""
        self.message("> " + subprocess.list2cmdline([str(x) for x in args]))

        process = subprocess.Popen(
            [str(x) for x in args],
            cwd=RUNTIME,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
        )

        lines = queue.Queue()
        output = []

        def reader():
            try:
                for raw in iter(process.stdout.readline, b""):
                    lines.put(raw)
            finally:
                lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.monotonic() + timeout
        completed = False

        while not completed:
            if time.monotonic() > deadline:
                # cmd.exe может иметь дочерний stm32pr.exe.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                )
                process.wait()
                raise TimeoutError(
                    "Превышено время операции. Процесс остановлен; "
                    "состояние записи необходимо проверить."
                )

            try:
                raw = lines.get(timeout=0.15)
            except queue.Empty:
                continue

            if raw is None:
                completed = True
                continue

            text = raw.decode("utf-8", "replace").strip()
            if text:
                output.append(text)
                self.message(text)

        code = process.wait(timeout=5)
        return code, "\n".join(output)

    def run_script(self, name):
        script = next(
            (
                RUNTIME / f"{name}{extension}"
                for extension in (".cmd", ".bat")
                if (RUNTIME / f"{name}{extension}").exists()
            ),
            None,
        )
        if script is None:
            raise RuntimeError(f"Не найден {name}.cmd или {name}.bat.")

        code, output = self.execute([
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d", "/c", script.name,
        ])

        if code != 0:
            raise RuntimeError(f"{script.name}: код завершения {code}.")

        # У исходных bat итоговый ERRORLEVEL ненадёжен:
        # проверяем также их собственное сообщение.
        if re.search(r"Прошивка\s+НЕ", output, re.IGNORECASE):
            raise RuntimeError(f"{script.name} сообщил об ошибке.")

        if "Операция выполнена" not in output:
            raise RuntimeError(
                f"{script.name}: нет ожидаемого сообщения об успехе."
            )

    def leave_dfu(self):
        programmer = RUNTIME / "bin" / "stm32pr.exe"
        if not programmer.exists():
            raise RuntimeError("Не найден bin/stm32pr.exe.")

        code, output = self.execute([
            programmer,
            "-c", "port=usb1",
            "-s", "0x08000000",
        ], timeout=30)
        if code != 0 or re.search(r"\berror\b", output, re.IGNORECASE):
            raise RuntimeError(
                "Программатор не подтвердил запуск приложения из DFU."
            )

    def apply_names(self, options):
        port = self.wait_serial()
        cli = BFCLI(port)
        saved = False

        try:
            # Не пишем случайные адреса в памяти:
            # проверяем, что прошивка поддерживает оба параметра.
            if cli.get_value("craft_name") is None:
                raise RuntimeError(
                    "В этой версии Betaflight нет CLI-параметра craft_name."
                )
            if cli.get_value("pilot_name") is None:
                raise RuntimeError(
                    "В этой версии Betaflight нет CLI-параметра pilot_name."
                )

            cli.set_value("craft_name", options["craft"])
            cli.set_value("pilot_name", options["pilot"])
            cli.reboot_command("save")
            saved = True
        finally:
            if not saved:
                try:
                    # Выходим без save, чтобы не сохранять частичные изменения.
                    cli.reboot_command("exit")
                except Exception:
                    pass
            cli.close()

        time.sleep(1.2)
        port = self.wait_serial()

        # Повторное чтение после перезапуска подтверждает сохранение.
        craft, pilot = self.read_names(port)
        if craft != options["craft"] or pilot != options["pilot"]:
            raise RuntimeError("Имена не прошли проверку после сохранения.")

        self.wait_serial()
        information = dict(self.current["information"])
        information["craft"] = craft or "Не задано"
        information["pilot"] = pilot or "Не задано"
        self.info.emit(information)

    def calibrate(self):
        port = self.wait_serial()
        msp = MSP(port)
        try:
            msp.request(205)  # MSP_ACC_CALIBRATION
            self.message(
                "FC принял команду калибровки. "
                "Ожидание завершения измерений..."
            )
            time.sleep(5)
            msp.request(250)  # MSP_EEPROM_WRITE
        finally:
            msp.close()

        self.message(
            "FC подтвердил сохранение настроек. "
            "Горизонтальность установки программно не проверяется."
        )

    def validate(self, mode, options):
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
                    if not re.fullmatch(r"[A-Za-z0-9 _.\-]{1,16}", value):
                        raise RuntimeError(
                            f"{label}: используйте 1–16 символов: "
                            "латиницу, цифры, пробел, _, . или -."
                        )

    def perform(self, mode, options):
        self.busy.emit(True)
        self.reset_operations.emit()
        self.progress.emit(0)
        current_step = "Подготовка"

        try:
            self.validate(mode, options)

            if not self.current:
                raise RuntimeError(
                    "FC не подключён в программе. "
                    "Включите автоподключение или нажмите «Подключиться»."
                )

            # Новая проверка исключает запуск скриптов с несколькими DFU.
            device = self.detect()
            if not device:
                raise RuntimeError("FC отключён.")
            self.current = device

            self.consumed = True

            if mode == "write":
                shutil.copy2(options["image"], RUNTIME / "fw.bin")

            steps = [("Перевод в DFU", self.enter_dfu)]

            if mode == "read":
                def read_image():
                    # Скрипт сам удаляет старый fw.bin; удаляем также заранее,
                    # чтобы остаток предыдущей операции не считался результатом.
                    (RUNTIME / "fw.bin").unlink(missing_ok=True)
                    self.run_script("read")

                    result = RUNTIME / "fw.bin"
                    if not result.exists() or result.stat().st_size == 0:
                        raise RuntimeError("Скрипт не создал непустой fw.bin.")

                    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                    destination = Path(options["folder"]) / f"read_{stamp}.bin"
                    shutil.copy2(result, destination)
                    self.message(f"Образ сохранён: {destination}")
                    self.message(
                        "Полнота чтения не подтверждена: "
                        "исходный скрипт скрывает результаты двух чтений."
                    )
                    self.images_changed.emit()

                steps.extend([
                    ("Чтение образа", read_image),
                    ("Выход из DFU", self.leave_dfu),
                ])
            else:
                if options["erase"]:
                    steps.append((
                        "Полное стирание чипа",
                        lambda: self.run_script("erase"),
                    ))

                steps.extend([
                    ("Запись и проверка образа", lambda: self.run_script("write")),
                    ("Выход из DFU", self.leave_dfu),
                ])

                if options["names"]:
                    steps.append((
                        "Запись имени борта и пилота",
                        lambda: self.apply_names(options),
                    ))

                if options["calibrate"]:
                    steps.append((
                        "Калибровка акселерометра",
                        self.calibrate,
                    ))

            for title, _ in steps:
                self.operation.emit(title, "Ожидание")

            for index, (title, action) in enumerate(steps):
                current_step = title
                self.operation.emit(title, "Выполняется")
                self.message(title)
                action()

                status = "Готово"
                if title in ("Чтение образа", "Запись и проверка образа"):
                    status = "Готово по отчёту скрипта"
                if title == "Калибровка акселерометра":
                    status = "Команды подтверждены FC"

                self.operation.emit(title, status)
                self.progress.emit(round((index + 1) * 100 / len(steps)))

            self.message("Последовательность операций завершена.")

            # После записи пользовательский образ может быть не Betaflight.
            # Если имена/калибровка отключены, это не делает запись ошибочной.
            try:
                self.wait_serial(timeout=10)
                self.connection.emit(True, "FC подключён")
            except Exception:
                self.current = None
                self.connection.emit(False, "Ожидание FC")
                self.message("Betaflight по USB пока не обнаружен.")

        except Exception as exc:
            self.operation.emit(current_step, "Ошибка")
            self.message(f"ОШИБКА: {exc}")
            self.message("Последующие этапы не выполняются.")
        finally:
            # Не сбрасываем consumed: ошибка не должна запускать
            # бесконечные повторные автопрошивки.
            self.busy.emit(False)
            self.absent_since = None

    def connect_device(self, automatic=False):
        device = self.detect()
        if not device:
            if not automatic:
                raise RuntimeError("Совместимый FC не найден.")
            return

        self.current = device
        auto_write = self.settings.get("auto_write", False)
        self.publish_device(device, read_names=not auto_write)

        if auto_write and not self.consumed:
            self.consumed = True
            self.message("Запуск прошивки при подключении.")
            self.perform("write", dict(self.settings))

    def monitor(self):
        try:
            dfu, ports = self.raw_devices()
            present = bool(dfu or ports)

            if not present:
                if self.absent_since is None:
                    self.absent_since = time.monotonic()

                if time.monotonic() - self.absent_since >= 2:
                    self.current = None
                    self.consumed = False
                    self.connection.emit(False, "FC не подключён")
                    self.last_detection_error = ""
                return

            self.absent_since = None

            if len(dfu) + len(ports) > 1:
                raise RuntimeError(
                    "Подключено несколько устройств. Оставьте один FC."
                )

            if self.current:
                valid = (
                    self.current["mode"] == "dfu"
                    and self.current["id"] in dfu
                ) or (
                    self.current["mode"] == "serial"
                    and self.current["port"] in ports
                )
                if not valid:
                    self.current = None
                    self.connection.emit(False, "Режим подключения FC изменился")

            if not self.current and self.settings.get("auto_connect", True):
                self.busy.emit(True)
                try:
                    self.connect_device(automatic=True)
                finally:
                    self.busy.emit(False)

            self.last_detection_error = ""

        except Exception as exc:
            text = str(exc)
            if text != self.last_detection_error:
                self.message(text)
                self.last_detection_error = text

    def run(self):
        next_scan = 0.0

        while not self.stop_requested.is_set():
            try:
                command, options = self.commands.get(timeout=0.15)

                if command == "settings":
                    self.settings = options

                elif command == "connect":
                    self.settings = options
                    self.busy.emit(True)
                    try:
                        self.connect_device()
                    except Exception as exc:
                        self.message(f"Подключение: {exc}")
                    finally:
                        self.busy.emit(False)

                elif command in ("read", "write"):
                    self.settings = options
                    self.perform(command, options)

            except queue.Empty:
                pass

            if time.monotonic() >= next_scan:
                self.monitor()
                next_scan = time.monotonic() + 1.5


def make_scroll(content):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setWidget(content)
    return scroll

def resource_path(filename):
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return (base_dir / filename).as_posix()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 780)
        self.setMinimumSize(760, 500)
        self.working = False
        self.rows = {}

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        title = QLabel("ezSTM - инструмент прошивки STM32")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("""
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
        """)

        title_glow = QGraphicsDropShadowEffect(title)
        title_glow.setBlurRadius(24)
        title_glow.setOffset(0, 2)
        title_glow.setColor(QColor(125, 200, 255, 75))
        title.setGraphicsEffect(title_glow)
        root.addWidget(title)

        check_box_icon_path = (PAYLOAD / "check.svg").as_posix()

        styles = """
        QMainWindow, QWidget {
            color: #edf5ff;
            font-family: "Segoe UI";
            font-size: 10pt;
        }

        QMainWindow {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 1, y2: 1,
                stop: 0 #101d35,
                stop: 0.5 #203953,
                stop: 1 #17283f
            );
        }

        QWidget {
            background: transparent;
        }

        QGroupBox {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(255, 255, 255, 28),
                stop: 1 rgba(255, 255, 255, 9)
            );

            border: 1px solid rgba(255, 255, 255, 55);
            border-top-color: rgba(255, 255, 255, 100);
            border-radius: 16px;

            /* Место для заголовка над рамкой */
            margin-top: 20px;
            padding: 14px 12px 12px 12px;
            font-weight: bold;
        }

        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;

            left: 16px;
            top: 0px;
            padding: 0px 6px;

            
            background: transparent;
            border: none;
            color: #d7ecff;
        }


        QCheckBox {
            spacing: 10px;
            padding: 5px 0;
            font-size: 13px;
            color: #d9e7f5;
        }

        QCheckBox:hover {
            color: #ffffff;
        }

        QCheckBox::indicator {
            width: 18px;
            height: 18px;
            border: 1px solid rgba(255, 255, 255, 85);
            border-top-color: rgba(255, 255, 255, 140);
            border-radius: 6px;
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(255, 255, 255, 40),
                stop: 1 rgba(255, 255, 255, 12)
            );
        }

        QCheckBox::indicator:hover {
            border-color: rgba(190, 225, 255, 190);
            background: rgba(190, 225, 255, 45);
        }

        QCheckBox::indicator:checked {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(130, 210, 255, 235),
                stop: 1 rgba(55, 135, 235, 220)
            );
            border-color: rgba(210, 240, 255, 200);
            image: url("__CHECK_BOX_ICON_PATH__");
        }

        QCheckBox::indicator:checked:hover {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(165, 225, 255, 250),
                stop: 1 rgba(80, 160, 250, 235)
            );
            border-color: #e2f5ff;
        }

        QCheckBox::indicator:pressed {
            background: rgba(90, 160, 220, 100);
            border-color: rgba(210, 240, 255, 220);
        }

        QCheckBox::indicator:disabled {
            background: rgba(255, 255, 255, 8);
            border-color: rgba(255, 255, 255, 25);
        }

        QCheckBox::indicator:checked:disabled {
            background: rgba(105, 150, 190, 65);
            border-color: rgba(170, 200, 225, 50);
        }

        QCheckBox:disabled {
            color: rgba(220, 235, 250, 90);
        }

        QPushButton {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(255, 255, 255, 48),
                stop: 1 rgba(255, 255, 255, 16)
            );
            border: 1px solid rgba(255, 255, 255, 65);
            border-top-color: rgba(255, 255, 255, 125);
            border-radius: 12px;
            padding: 10px 18px;
            color: #f2f8ff;
            font-weight: 600;
        }

        QPushButton:hover {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(220, 240, 255, 70),
                stop: 1 rgba(180, 220, 255, 30)
            );
            border-color: rgba(220, 240, 255, 140);
        }

        QPushButton:pressed {
            background: rgba(150, 200, 245, 32);
            border-color: rgba(220, 240, 255, 90);
        }

        QPushButton:disabled {
            background: rgba(255, 255, 255, 7);
            color: rgba(220, 235, 250, 80);
            border-color: rgba(255, 255, 255, 20);
        }

        QPushButton[accent="true"] {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(120, 200, 255, 220),
                stop: 1 rgba(55, 125, 230, 200)
            );
            border-color: rgba(190, 230, 255, 170);
            border-top-color: rgba(235, 250, 255, 230);
            color: #ffffff;
        }

        QPushButton[accent="true"]:hover {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(155, 220, 255, 240),
                stop: 1 rgba(75, 150, 250, 225)
            );
            border-color: rgba(235, 250, 255, 230);
        }

        QPushButton[accent="true"]:pressed {
            background: rgba(55, 125, 215, 200);
            border-color: rgba(190, 230, 255, 150);
        }

        QPushButton[accent="true"]:disabled {
            background: rgba(90, 135, 180, 45);
            border-color: rgba(160, 200, 230, 35);
            color: rgba(220, 235, 250, 85);
        }
        QLineEdit, QComboBox, QPlainTextEdit, QTreeWidget {
            background: rgba(8, 20, 38, 100);
            border: 1px solid rgba(255, 255, 255, 45);
            border-top-color: rgba(255, 255, 255, 70);
            border-radius: 10px;
            padding: 8px;
            color: #edf5ff;
            selection-background-color: rgba(95, 170, 245, 160);
            selection-color: #ffffff;
        }

        QLineEdit:hover, QComboBox:hover,
        QPlainTextEdit:hover, QTreeWidget:hover {
            border-color: rgba(200, 230, 255, 90);
        }

        QLineEdit:focus, QComboBox:focus,
        QPlainTextEdit:focus, QTreeWidget:focus {
            border-color: rgba(145, 210, 255, 210);
            background: rgba(12, 30, 52, 150);
        }

        QLineEdit:disabled, QComboBox:disabled,
        QPlainTextEdit:disabled, QTreeWidget:disabled {
            color: rgba(220, 235, 250, 80);
            background: rgba(10, 20, 35, 45);
            border-color: rgba(255, 255, 255, 20);
        }

        QComboBox {
            padding-right: 30px;
        }

        QComboBox::drop-down {
            width: 26px;
            border: none;
            border-left: 1px solid rgba(255, 255, 255, 30);
            border-top-right-radius: 10px;
            border-bottom-right-radius: 10px;
            background: rgba(255, 255, 255, 12);
        }

        QComboBox QAbstractItemView {
            background: #20364e;
            border: 1px solid rgba(200, 230, 255, 100);
            padding: 6px;
            color: #edf5ff;
            selection-background-color: #426e98;
            selection-color: #ffffff;
            outline: none;
        }

        QPlainTextEdit {
            font-family: "Consolas";
            font-size: 10pt;
        }

        QTreeWidget {
            alternate-background-color: rgba(255, 255, 255, 8);
            outline: none;
        }

        QTreeWidget::item {
            padding: 6px;
            border-radius: 6px;
        }

        QTreeWidget::item:hover {
            background: rgba(200, 230, 255, 25);
        }

        QTreeWidget::item:selected {
            background: rgba(100, 175, 245, 100);
            color: #ffffff;
        }

        QTabWidget::pane {
            border: 1px solid rgba(255, 255, 255, 50);
            border-radius: 14px;
            background: rgba(255, 255, 255, 10);
            padding: 6px;
        }

        QTabBar::tab {
            background: rgba(255, 255, 255, 12);
            border: 1px solid rgba(255, 255, 255, 25);
            border-radius: 10px;
            padding: 10px 18px;
            margin: 4px 4px 8px 0;
            color: #b8cde0;
        }

        QTabBar::tab:hover {
            background: rgba(220, 240, 255, 30);
            border-color: rgba(220, 240, 255, 70);
            color: #ffffff;
        }

        QTabBar::tab:selected {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(185, 225, 255, 65),
                stop: 1 rgba(100, 170, 235, 35)
            );
            border-color: rgba(195, 230, 255, 130);
            border-top-color: rgba(235, 250, 255, 180);
            color: #ffffff;
        }

        QProgressBar {
            border: 1px solid rgba(255, 255, 255, 50);
            border-radius: 12px;
            background: rgba(8, 20, 38, 100);
            text-align: center;
            min-height: 24px;
            color: #ffffff;
        }

        QProgressBar::chunk {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(155, 225, 255, 230),
                stop: 0.5 rgba(90, 180, 250, 210),
                stop: 1 rgba(65, 140, 230, 200)
            );
            border-radius: 11px;
        }

        QHeaderView::section {
            background: qlineargradient(
                x1: 0, y1: 0, x2: 0, y2: 1,
                stop: 0 rgba(210, 235, 255, 40),
                stop: 1 rgba(160, 200, 240, 15)
            );
            color: #e6f3ff;
            padding: 9px;
            border: none;
            border-right: 1px solid rgba(255, 255, 255, 20);
            border-bottom: 1px solid rgba(255, 255, 255, 40);
            font-weight: 600;
        }

        QSplitter::handle {
            background: rgba(220, 240, 255, 25);
        }

        QSplitter::handle:hover {
            background: rgba(170, 220, 255, 90);
        }

        QScrollBar:vertical {
            background: transparent;
            width: 12px;
            margin: 2px;
        }

        QScrollBar::handle:vertical {
            background: rgba(205, 230, 255, 75);
            border-radius: 4px;
            min-height: 30px;
        }

        QScrollBar::handle:vertical:hover {
            background: rgba(220, 240, 255, 135);
        }

        QScrollBar:horizontal {
            background: transparent;
            height: 12px;
            margin: 2px;
        }

        QScrollBar::handle:horizontal {
            background: rgba(205, 230, 255, 75);
            border-radius: 4px;
            min-width: 30px;
        }

        QScrollBar::handle:horizontal:hover {
            background: rgba(220, 240, 255, 135);
        }

        QScrollBar::add-line:vertical,


        QScrollBar::sub-line:vertical {
            height: 0px;
            border: none;
            background: transparent;
        }

        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {
            width: 0px;
            border: none;
            background: transparent;
        }

        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical,
        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {
            background: transparent;
        }

        QToolTip {
            background: #263e56;
            color: #edf5ff;
            border: 1px solid rgba(210, 235, 255, 110);
            padding: 6px 10px;
        }
        """

        # Подставляем путь без f-строки: фигурные скобки остаются одинарными.
        styles = styles.replace("__CHECK_BOX_ICON_PATH__", check_box_icon_path)

        # Заменяем старую тему, чтобы её правила не мешали новой.
        app.setStyleSheet(styles)
        

        self.status = QLabel("FC не подключён")
        root.addWidget(self.status)

        # Вся рабочая область прокручивается, если окно слишком узкое.
        workspace = QWidget()
        workspace.setMinimumWidth(1060)
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        workspace_layout.addWidget(splitter)
        root.addWidget(make_scroll(workspace), 1)

        left = QWidget()
        left.setMinimumWidth(380)
        left_layout = QVBoxLayout(left)

        connection_box = QGroupBox("Подключение")
        connection_layout = QVBoxLayout(connection_box)

        port_row = QHBoxLayout()
        self.port = QComboBox()
        self.port.addItem("Автоматически — STM32 USB", "")
        self.refresh_ports_button = QPushButton("Обновить")
        port_row.addWidget(self.port, 1)
        port_row.addWidget(self.refresh_ports_button)
        connection_layout.addLayout(port_row)

        self.auto_connect = QCheckBox("Подключаться автоматически")
        self.auto_connect.setChecked(True)
        self.auto_write = QCheckBox("Прошивать FC при подключении")
        self.connect_button = QPushButton("Подключиться")

        connection_layout.addWidget(self.auto_connect)
        connection_layout.addWidget(self.auto_write)
        connection_layout.addWidget(self.connect_button)
        left_layout.addWidget(connection_box)

        images_box = QGroupBox("Образы")
        images_layout = QVBoxLayout(images_box)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(DEFAULT_IMAGES))
        self.folder.setReadOnly(True)
        self.browse_button = QPushButton("Папка…")
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(self.browse_button)
        images_layout.addLayout(folder_row)

        self.image = QComboBox()
        self.reload_images_button = QPushButton("Обновить список образов")
        images_layout.addWidget(self.image)
        images_layout.addWidget(self.reload_images_button)
        left_layout.addWidget(images_box)

        self.tabs = QTabWidget()

        write_tab = QWidget()
        write_layout = QVBoxLayout(write_tab)

        self.erase = QCheckBox("Полное стирание чипа")
        self.names = QCheckBox("Записать имя")
        self.names.setChecked(True)
        self.calibrate_box = QCheckBox("Калибровка акселерометра")

        write_layout.addWidget(self.erase)
        write_layout.addWidget(self.names)

        self.names_widget = QWidget()
        names_layout = QFormLayout(self.names_widget)
        names_layout.setContentsMargins(0, 0, 0, 0)
        self.craft = QLineEdit("FC")
        self.pilot = QLineEdit("PILOT")
        self.craft.setMaxLength(16)
        self.pilot.setMaxLength(16)
        names_layout.addRow("Имя борта:", self.craft)
        names_layout.addRow("Имя пилота:", self.pilot)

        write_layout.addWidget(self.names_widget)
        write_layout.addWidget(self.calibrate_box)

        self.write_button = QPushButton("Записать образ")
        self.write_button.setProperty("accent", True)
        write_layout.addWidget(self.write_button)

        note = QLabel(
            "Для имён нужна поддержка craft_name и pilot_name.\n"
            "Калибровка: FC неподвижен и установлен горизонтально."
        )
        note.setWordWrap(True)
        write_layout.addWidget(note)
        write_layout.addStretch()

        read_tab = QWidget()
        read_layout = QVBoxLayout(read_tab)

        read_note = QLabel(
            "Результат сохраняется в выбранную папку:\n"
            "read_дата_время.bin\n\n"
            "Исходный read.bat запускается без изменений."
        )
        read_note.setWordWrap(True)
        read_layout.addWidget(read_note)

        self.read_button = QPushButton("Считать прошивку")
        self.read_button.setProperty("accent", True)
        read_layout.addWidget(self.read_button)
        read_layout.addStretch()

        self.tabs.addTab(write_tab, "Прошить")
        self.tabs.addTab(read_tab, "Считать прошивку")
        left_layout.addWidget(self.tabs)

        info_box = QGroupBox("Данные FC")
        info_layout = QFormLayout(info_box)
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
            info_layout.addRow(text, label)

        left_layout.addWidget(info_box)
        left_layout.addStretch()
        splitter.addWidget(make_scroll(left))

        right = QWidget()
        right.setMinimumWidth(620)
        right_layout = QVBoxLayout(right)

        right_layout.addWidget(QLabel("Операции"))
        self.operations = QTreeWidget()
        self.operations.setHeaderLabels(["Операция", "Статус"])
        self.operations.setRootIsDecorated(False)
        self.operations.setColumnWidth(0, 300)
        self.operations.setMinimumHeight(180)
        right_layout.addWidget(self.operations, 2)

        right_layout.addWidget(QLabel("Консоль"))
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(12000)
        right_layout.addWidget(self.console, 5)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p% — выполнение этапов")
        right_layout.addWidget(self.progress)

        splitter.addWidget(right)
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([400, 700])

        self.engine = Engine()
        self.engine.log.connect(self.append_log)
        self.engine.info.connect(self.update_info)
        self.engine.connection.connect(self.update_connection)
        self.engine.operation.connect(self.update_operation)
        self.engine.progress.connect(self.update_progress)
        self.engine.busy.connect(self.set_busy)
        self.engine.reset_operations.connect(self.reset_operations)
        self.engine.images_changed.connect(self.reload_images)

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

        self.refresh_ports()
        self.reload_images()
        self.send_settings()
        self.engine.start()

        self.append_log(f"Рабочая папка: {RUNTIME}")
        self.append_log(
            "Работа со скриптами без изменения их содержимого. "
            "Точный прогресс STM32CLI скрыт самими скриптами."
        )

    def update_progress(self, value):
        self.progress.setValue(value)

        color = "#31c975" if value == 100 else "#e78a30"

        self.progress.setStyleSheet(
            "QProgressBar::chunk {"
            f"background: {color};"
            "border-radius: 4px;"
            "}"
        )
    
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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    APPDIR.mkdir(parents=True, exist_ok=True)

    # Один рабочий каталог и жёсткий usb1 требуют запрета второго экземпляра.
    lock = QLockFile(str(APPDIR / "ezSTM.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        error = QLabel("ezSTM уже запущен.")
        error.setWindowTitle(APP_NAME)
        error.resize(360, 90)
        error.show()
        return app.exec()

    try:
        prepare_files()
    except Exception as exc:
        error = QPlainTextEdit()
        error.setReadOnly(True)
        error.setWindowTitle("ezSTM — ошибка запуска")
        error.setPlainText(str(exc))
        error.resize(700, 220)
        error.show()
        return app.exec()

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    # Помогает Windows корректно идентифицировать приложение
    # и отображать его иконку на панели задач.
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ezSTM.Desktop"
        )

    app = QApplication(sys.argv)

    icon = QIcon(resource_path(PAYLOAD / "icon.ico"))
    app.setWindowIcon(icon)

    window = MainWindow()
    window.setWindowIcon(icon)
    window.show()
    sys.exit(app.exec())
