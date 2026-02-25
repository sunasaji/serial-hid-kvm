"""Auto-detection of CH340/CH341 USB-serial adapters."""

import logging

import serial.tools.list_ports

logger = logging.getLogger(__name__)

# Known VID:PID pairs for CH340/CH341
CH340_VIDPID = {
    (0x1A86, 0x7523),  # CH340
    (0x1A86, 0x7522),  # CH341
}


def list_ch340_ports() -> list[dict]:
    """List all connected CH340/CH341 serial ports.

    Returns:
        List of dicts with port info: device, description, hwid
    """
    results = []
    for port in serial.tools.list_ports.comports():
        if port.vid is not None and port.pid is not None:
            if (port.vid, port.pid) in CH340_VIDPID:
                results.append({
                    "device": port.device,
                    "description": port.description,
                    "hwid": port.hwid,
                    "vid": f"0x{port.vid:04X}",
                    "pid": f"0x{port.pid:04X}",
                })
    return results


def auto_detect_port() -> str:
    """Auto-detect a single CH340/CH341 serial port.

    Returns:
        The device path (e.g., /dev/ttyUSB0 or COM3)

    Raises:
        RuntimeError: If no port found or multiple ports found.
    """
    ports = list_ch340_ports()
    if len(ports) == 0:
        raise RuntimeError(
            "No CH340/CH341 device found. "
            "Check USB connection or set SHKVM_SERIAL_PORT / --serial-port."
        )
    if len(ports) > 1:
        port_list = "\n".join(f"  - {p['device']} ({p['description']})" for p in ports)
        raise RuntimeError(
            f"Multiple CH340/CH341 devices found:\n{port_list}\n"
            "Set SHKVM_SERIAL_PORT / --serial-port to select one."
        )
    port = ports[0]["device"]
    logger.info(f"Auto-detected CH340 on {port}")
    return port
