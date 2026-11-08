import serial


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
