import ctypes
import os

from serial.tools import list_ports

from ezstm.config import DFU_ID_PREFIX, STM32_VIDS


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
        if item.upper().startswith(DFU_ID_PREFIX)
    })


def serial_ports(selected=""):
    """COM-порты: выбранный пользователем или все с VID STM32."""
    ports = []
    for item in list_ports.comports():
        if selected:
            if item.device == selected:
                ports.append(item.device)
        elif item.vid in STM32_VIDS:
            ports.append(item.device)
    return ports
