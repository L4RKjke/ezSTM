import datetime
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ezstm.config import RUNTIME
from ezstm.engine.process import run_process
from ezstm.engine.validation import validate
from ezstm.hardware.cli import BFCLI
from ezstm.hardware.msp import MSP, fc_information
from ezstm.hardware.usb import dfu_devices, serial_ports


class Engine(QThread):
    log = Signal(str)
    info = Signal(dict)
    connection = Signal(bool, str)
    operation = Signal(str, str)
    progress = Signal(int)
    busy = Signal(bool)
    reset_operations = Signal()
    images_changed = Signal()
    written = Signal()

    def __init__(self):
        super().__init__()
        self.commands = queue.Queue() if False else __import__("queue").Queue()
        self.stop_requested = threading.Event()
        self.settings = {}
        self.current = None
        
        # Автопрошивка запускается только один раз за цикл подключения.
        # Переход COM -> DFU -> COM внутри операции не считается
        # новым физическим подключением.
        self.consumed = False
        self.absent_since = None
        self.last_detection_error = ""

    # ----------------------------------------------------------- utilities

    def submit(self, command, settings=None):
        self.commands.put((command, dict(settings or {})))

    def message(self, text):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.emit(f"[{stamp}] {text}")

    def execute(self, args, timeout=300):
        self.message("> " + subprocess.list2cmdline([str(x) for x in args]))
        return run_process(args, RUNTIME, self.message, timeout)

    def raw_devices(self):
        return dfu_devices(), serial_ports(self.settings.get("port", ""))

    # ----------------------------------------------------------- detection

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
                self.wait_serial(timeout=20)
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

    # ----------------------------------------------------------- device actions

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

    # ----------------------------------------------------------- operations

    def _read_image(self, options):
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

    def _read_steps(self, options):
        return [
            ("Чтение образа", lambda: self._read_image(options)),
            ("Выход из DFU", self.leave_dfu),
        ]

    def _write_steps(self, options):
        steps = []

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
            steps.append(("Калибровка акселерометра", self.calibrate))

        return steps

    def _build_steps(self, mode, options):
        steps = [("Перевод в DFU", self.enter_dfu)]
        if mode == "read":
            steps += self._read_steps(options)
        else:
            steps += self._write_steps(options)
        return steps

    @staticmethod
    def _final_status(title):
        if title in ("Чтение образа", "Запись и проверка образа"):
            return "Готово по отчёту скрипта"
        if title == "Калибровка акселерометра":
            return "Команды подтверждены FC"
        
        return "Готово"

    def perform(self, mode, options):
        self.busy.emit(True)
        self.reset_operations.emit()
        self.progress.emit(0)
        current_step = "Подготовка"

        try:
            validate(mode, options)

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

            steps = self._build_steps(mode, options)

            for title, _ in steps:
                self.operation.emit(title, "Ожидание")

            for index, (title, action) in enumerate(steps):
                current_step = title
                self.operation.emit(title, "Выполняется")
                self.message(title)
                action()

                self.operation.emit(title, self._final_status(title))
                self.progress.emit(round((index + 1) * 100 / len(steps)))

            self.message("Последовательность операций завершена.")
            # После записи пользовательский образ может быть не Betaflight.
            if mode == "write":
                self.written.emit()
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

    # ----------------------------------------------------------- automation

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