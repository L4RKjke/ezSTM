import time

from ezstm.hardware.serial_port import open_serial


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
