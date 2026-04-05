"""CH9329 HID protocol packet construction and serial communication."""

import logging
import threading
import time

import serial

logger = logging.getLogger(__name__)

# CH9329 packet constants
HEADER = bytes([0x57, 0xAB])
DEFAULT_ADDR = 0x00

# Commands
CMD_KEYBOARD = 0x02
CMD_MOUSE_ABS = 0x04
CMD_MOUSE_REL = 0x05
CMD_GET_INFO = 0x01


def _checksum(data: bytes) -> int:
    """Calculate checksum: sum of all bytes & 0xFF."""
    return sum(data) & 0xFF


def build_packet(cmd: int, data: bytes, addr: int = DEFAULT_ADDR) -> bytes:
    """Build a CH9329 protocol packet.

    Format: [0x57] [0xAB] [ADDR] [CMD] [LEN] [DATA...] [SUM]
    """
    length = len(data)
    packet = HEADER + bytes([addr, cmd, length]) + data
    packet += bytes([_checksum(packet)])
    return packet


def build_keyboard_packet(modifier: int, keycode: int) -> bytes:
    """Build a keyboard HID report packet.

    Args:
        modifier: Modifier key bitmask (ctrl, shift, alt, win)
        keycode: HID keycode
    """
    # HID keyboard report: [modifier, 0x00, keycode, 0, 0, 0, 0, 0]
    data = bytes([modifier, 0x00, keycode, 0x00, 0x00, 0x00, 0x00, 0x00])
    return build_packet(CMD_KEYBOARD, data)


def build_keyboard_report(modifier: int, keycodes: list[int] | set[int] = ()) -> bytes:  # type: ignore[assignment]
    """Build a keyboard HID report with multiple simultaneous keys.

    Args:
        modifier: Modifier key bitmask (ctrl, shift, alt, win)
        keycodes: Up to 6 HID keycodes currently pressed
    """
    keys = [k for k in keycodes if k][:6]
    keys.extend([0x00] * (6 - len(keys)))
    data = bytes([modifier, 0x00] + keys)
    return build_packet(CMD_KEYBOARD, data)


def build_keyboard_release_packet() -> bytes:
    """Build a keyboard release (all keys up) packet."""
    return build_keyboard_packet(0x00, 0x00)


def build_mouse_abs_packet(
    buttons: int, x: int, y: int, scroll: int = 0
) -> bytes:
    """Build an absolute mouse position packet.

    Args:
        buttons: Button bitmask (bit0=left, bit1=right, bit2=middle)
        x: Absolute X in 0-4095 range
        y: Absolute Y in 0-4095 range
        scroll: Scroll wheel value (-127 to 127, signed)
    """
    x = max(0, min(4095, x))
    y = max(0, min(4095, y))
    scroll_byte = scroll & 0xFF  # Convert signed to unsigned byte
    data = bytes([
        0x02,  # Absolute mode flag
        buttons,
        x & 0xFF, (x >> 8) & 0xFF,
        y & 0xFF, (y >> 8) & 0xFF,
        scroll_byte,
    ])
    return build_packet(CMD_MOUSE_ABS, data)


def build_mouse_rel_packet(
    buttons: int, dx: int, dy: int, scroll: int = 0
) -> bytes:
    """Build a relative mouse movement packet.

    Args:
        buttons: Button bitmask
        dx: Relative X movement (-127 to 127)
        dy: Relative Y movement (-127 to 127)
        scroll: Scroll wheel value (-127 to 127)
    """
    dx = max(-127, min(127, dx))
    dy = max(-127, min(127, dy))
    data = bytes([
        0x01,  # Relative mode flag
        buttons,
        dx & 0xFF,
        dy & 0xFF,
        scroll & 0xFF,
    ])
    return build_packet(CMD_MOUSE_REL, data)


class CH9329:
    """CH9329 HID emulator serial connection."""

    def __init__(self, port: str, baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._send_lock = threading.Lock()

    def open(self):
        """Open serial connection."""
        if self._serial and self._serial.is_open:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            timeout=1,
        )
        logger.info(f"Opened CH9329 on {self.port} at {self.baudrate} baud")

    def close(self):
        """Close serial connection."""
        if self._serial and self._serial.is_open:
            self._serial.close()
            self._serial = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def send(self, packet: bytes):
        """Send a packet to the CH9329."""
        with self._send_lock:
            if not self.is_open:
                self.open()
            self._serial.write(packet)  # type: ignore[union-attr]
            self._serial.flush()  # type: ignore[union-attr]

    def send_keyboard(self, modifier: int, keycode: int, release_delay: float = 0.01):
        """Send a key press and release."""
        self.send(build_keyboard_packet(modifier, keycode))
        time.sleep(release_delay)
        self.send(build_keyboard_release_packet())

    def release_all(self):
        """Release all keys and mouse buttons."""
        self.send(build_keyboard_release_packet())
        self.send(build_mouse_rel_packet(0, 0, 0))
