import re
import time

from ezstm.hardware.serial_port import open_serial


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
