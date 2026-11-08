import queue
import subprocess
import threading
import time

from ezstm.config import NO_WINDOW


def run_process(args, cwd, on_line, timeout=300):
    """Запускает процесс, построчно передаёт вывод в on_line.

    stdin закрыт (DEVNULL), поэтому pause в bat-скриптах получает EOF.
    Возвращает (код завершения, весь вывод).
    """
    process = subprocess.Popen(
        [str(x) for x in args],
        cwd=cwd,
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
            on_line(text)

    code = process.wait(timeout=5)
    return code, "\n".join(output)
