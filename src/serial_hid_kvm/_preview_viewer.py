"""KVM preview viewer with interactive input forwarding (in-process).

Runs as part of the KVM server process.  Reads frames directly from the
ScreenCapture instance and sends keyboard/mouse input directly to CH9329
via callback functions — no shared memory IPC required.

On Windows, a low-level keyboard hook (WH_KEYBOARD_LL) is used so that keys
like Win and Zenkaku/Hankaku are captured and suppressed on the host — they
only reach the remote target.  Scan codes are translated to HID keycodes
directly, which is layout-independent and correct for KVM pass-through.

On Linux, pynput is used (without host-side suppression).
Falls back to OpenCV waitKeyEx when neither is available.
"""

import logging
import platform
import subprocess
import threading
import time
from typing import Callable

import cv2
import numpy as np

from .config import Config
from .hid_keycodes import char_to_hid, set_layout, MOD_LCTRL, MOD_LSHIFT, MOD_LALT, MOD_LWIN
from .hid_protocol import (
    build_keyboard_packet, build_keyboard_release_packet,
    build_mouse_abs_packet, build_mouse_rel_packet,
)

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"
_IS_LINUX = platform.system() == "Linux"


# ---------------------------------------------------------------------------
# PS/2 scan code → HID keycode tables  (layout-independent)
# Reference: USB HID Usage Tables §10, Table A-1
# ---------------------------------------------------------------------------

# Non-extended scan codes → HID keycodes (one entry per line!)
_SCAN_TO_HID: dict[int, int] = {
    0x01: 0x29,  # Esc
    0x02: 0x1E,  # 1
    0x03: 0x1F,  # 2
    0x04: 0x20,  # 3
    0x05: 0x21,  # 4
    0x06: 0x22,  # 5
    0x07: 0x23,  # 6
    0x08: 0x24,  # 7
    0x09: 0x25,  # 8
    0x0A: 0x26,  # 9
    0x0B: 0x27,  # 0
    0x0C: 0x2D,  # -
    0x0D: 0x2E,  # =
    0x0E: 0x2A,  # Backspace
    0x0F: 0x2B,  # Tab
    0x10: 0x14,  # Q
    0x11: 0x1A,  # W
    0x12: 0x08,  # E
    0x13: 0x15,  # R
    0x14: 0x17,  # T
    0x15: 0x1C,  # Y
    0x16: 0x18,  # U
    0x17: 0x0C,  # I
    0x18: 0x12,  # O
    0x19: 0x13,  # P
    0x1A: 0x2F,  # [
    0x1B: 0x30,  # ]
    0x1C: 0x28,  # Enter
    0x1E: 0x04,  # A
    0x1F: 0x16,  # S
    0x20: 0x07,  # D
    0x21: 0x09,  # F
    0x22: 0x0A,  # G
    0x23: 0x0B,  # H
    0x24: 0x0D,  # J
    0x25: 0x0E,  # K
    0x26: 0x0F,  # L
    0x27: 0x33,  # ;
    0x28: 0x34,  # '
    0x29: 0x35,  # ` (Grave / Zenkaku-Hankaku on JIS)
    0x2B: 0x31,  # \ (ANSI backslash key / JIS ])
    0x2C: 0x1D,  # Z
    0x2D: 0x1B,  # X
    0x2E: 0x06,  # C
    0x2F: 0x19,  # V
    0x30: 0x05,  # B
    0x31: 0x11,  # N
    0x32: 0x10,  # M
    0x33: 0x36,  # ,
    0x34: 0x37,  # .
    0x35: 0x38,  # /
    0x37: 0x55,  # Numpad *
    0x39: 0x2C,  # Space
    0x3A: 0x39,  # CapsLock
    0x3B: 0x3A,  # F1
    0x3C: 0x3B,  # F2
    0x3D: 0x3C,  # F3
    0x3E: 0x3D,  # F4
    0x3F: 0x3E,  # F5
    0x40: 0x3F,  # F6
    0x41: 0x40,  # F7
    0x42: 0x41,  # F8
    0x43: 0x42,  # F9
    0x44: 0x43,  # F10
    0x45: 0x53,  # NumLock
    0x46: 0x47,  # ScrollLock
    0x47: 0x5F,  # Num7
    0x48: 0x60,  # Num8
    0x49: 0x61,  # Num9
    0x4A: 0x56,  # Num-
    0x4B: 0x5C,  # Num4
    0x4C: 0x5D,  # Num5
    0x4D: 0x5E,  # Num6
    0x4E: 0x57,  # Num+
    0x4F: 0x59,  # Num1
    0x50: 0x5A,  # Num2
    0x51: 0x5B,  # Num3
    0x52: 0x62,  # Num0
    0x53: 0x63,  # Num.
    0x56: 0x64,  # Non-US \ (ISO extra key, left of Z)
    0x57: 0x44,  # F11
    0x58: 0x45,  # F12
    # JIS-specific scan codes
    0x70: 0x88,  # Katakana/Hiragana (International2)
    0x73: 0x87,  # International1 (JIS ろ / _\)
    0x79: 0x8A,  # Henkan / 変換 (International4)
    0x7B: 0x8B,  # Muhenkan / 無変換 (International5)
    0x7D: 0x89,  # International3 (JIS ¥|)
}

# Extended scan codes (E0 prefix)
_SCAN_EXT_TO_HID: dict[int, int] = {
    0x1C: 0x58,  # Numpad Enter
    0x35: 0x54,  # Numpad /
    0x37: 0x46,  # PrintScreen
    0x46: 0x48,  # Pause/Break
    0x47: 0x4A,  # Home
    0x48: 0x52,  # Up
    0x49: 0x4B,  # PageUp
    0x4B: 0x50,  # Left
    0x4D: 0x4F,  # Right
    0x4F: 0x4D,  # End
    0x50: 0x51,  # Down
    0x51: 0x4E,  # PageDown
    0x52: 0x49,  # Insert
    0x53: 0x4C,  # Delete
    0x5D: 0x65,  # Application / Menu
}
# fmt: on

# VK codes for modifier keys → HID modifier bitmask
_VK_MOD_BITS: dict[int, int] = {
    0xA0: 0x02,  # VK_LSHIFT   → MOD_LSHIFT
    0xA1: 0x20,  # VK_RSHIFT   → MOD_RSHIFT
    0xA2: 0x01,  # VK_LCONTROL → MOD_LCTRL
    0xA3: 0x10,  # VK_RCONTROL → MOD_RCTRL
    0xA4: 0x04,  # VK_LMENU    → MOD_LALT
    0xA5: 0x40,  # VK_RMENU    → MOD_RALT
    0x5B: 0x08,  # VK_LWIN     → MOD_LWIN
    0x5C: 0x80,  # VK_RWIN     → MOD_RWIN
}

# VK modifier scan codes (non-extended) — skip HID lookup for these
_MOD_SCANS = {0x2A, 0x36, 0x1D, 0x38}       # LShift, RShift, LCtrl, LAlt
_MOD_SCANS_EXT = {0x1D, 0x38, 0x5B, 0x5C}   # RCtrl, RAlt, LWin, RWin


# ---------------------------------------------------------------------------
# Callback type aliases
# ---------------------------------------------------------------------------

# on_key(modifier, keycode) — press + release
OnKeyCallback = Callable[[int, int], None]
# on_key_down(modifier, keycode) — key down only (modifier state update)
OnKeyDownCallback = Callable[[int, int], None]
# on_mouse(buttons, abs_x, abs_y) — absolute mouse position
OnMouseCallback = Callable[[int, int, int], None]
# on_scroll(amount) — scroll wheel
OnScrollCallback = Callable[[int], None]


# ---------------------------------------------------------------------------
# Window focus detection
# ---------------------------------------------------------------------------

class _FocusDetector:
    """Detect whether our OpenCV window currently has keyboard focus."""

    def __init__(self, window_name: str):
        self._window_name = window_name
        self._hwnd = None
        self._xdotool_warned = False

        # Check xdotool availability on Linux at init time
        if _IS_LINUX:
            try:
                subprocess.run(
                    ["xdotool", "--version"],
                    capture_output=True, timeout=2,
                )
            except FileNotFoundError:
                logger.warning(
                    "xdotool is not installed. Window focus detection is "
                    "disabled — keyboard input will NOT be forwarded to the "
                    "target while the preview window is open. "
                    "Install it with: sudo apt install xdotool"
                )
            except Exception:
                pass

    def has_focus(self) -> bool:
        try:
            if _IS_WINDOWS:
                return self._has_focus_windows()
            elif _IS_LINUX:
                return self._has_focus_linux()
        except FileNotFoundError:
            if not self._xdotool_warned:
                logger.warning("xdotool not found — focus detection disabled, "
                               "keys will not be forwarded")
                self._xdotool_warned = True
            return False
        except Exception:
            return False
        return True

    def _has_focus_windows(self) -> bool:
        import ctypes
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        if fg == 0:
            return False
        if self._hwnd is None:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(fg, buf, 256)
            if self._window_name in buf.value:
                self._hwnd = fg
                return True
            return False
        return fg == self._hwnd

    def _has_focus_linux(self) -> bool:
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=1,
        )
        return self._window_name in result.stdout.strip()


# ---------------------------------------------------------------------------
# Win32 low-level keyboard hook (WH_KEYBOARD_LL)
# ---------------------------------------------------------------------------

class _Win32KeyboardGrab:
    """Capture keyboard via WH_KEYBOARD_LL with focus-gated host suppression.

    When the viewer window has focus, all keyboard events are:
      - Converted from PS/2 scan codes to HID keycodes
      - Forwarded to the KVM target via callback
      - Suppressed on the host OS  (Win key, Zenkaku/Hankaku, etc.)

    When the viewer does not have focus, events pass through untouched.
    """

    # Windows constants
    _WH_KEYBOARD_LL = 13
    _WM_KEYDOWN    = 0x0100
    _WM_KEYUP      = 0x0101
    _WM_SYSKEYDOWN = 0x0104
    _WM_SYSKEYUP   = 0x0105
    _WM_QUIT       = 0x0012
    _LLKHF_EXTENDED = 0x01
    _LLKHF_INJECTED = 0x10

    def __init__(self, on_key: OnKeyCallback, on_key_down: OnKeyDownCallback,
                 focus: _FocusDetector, debug: bool = False):
        self._on_key = on_key
        self._on_key_down = on_key_down
        self._focus = focus
        self._debug = debug
        self._held_modifiers: int = 0
        self._hook = None
        self._hook_proc_ref = None   # prevent GC of the C callback
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0     # Windows thread ID for PostThreadMessage
        self.quit_requested = False
        self._quit_keys: set[str] = set()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="win32-kb-hook")
        self._thread.start()
        # Wait for the hook to be installed
        for _ in range(50):
            if self._hook:
                break
            time.sleep(0.02)

    def stop(self):
        if self._thread_id:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id, self._WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        import ctypes
        from ctypes import wintypes, POINTER, WINFUNCTYPE, cast, byref

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._thread_id = kernel32.GetCurrentThreadId()

        # Set argtypes/restype for 64-bit safety (lParam is pointer-sized)
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode",      wintypes.DWORD),
                ("scanCode",    wintypes.DWORD),
                ("flags",       wintypes.DWORD),
                ("time",        wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        HOOKPROC = WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int,
                               wintypes.WPARAM, wintypes.LPARAM)

        @HOOKPROC
        def hook_proc(nCode, wParam, lParam):
            if nCode < 0:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            kb = cast(lParam, POINTER(KBDLLHOOKSTRUCT)).contents
            vk = kb.vkCode
            scan = kb.scanCode
            flags = kb.flags
            extended = bool(flags & self._LLKHF_EXTENDED)
            injected = bool(flags & self._LLKHF_INJECTED)
            is_up = wParam in (self._WM_KEYUP, self._WM_SYSKEYUP)

            # Always pass through injected events (from other software)
            if injected:
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            # Check Ctrl+Alt+Q quit combo (always, regardless of focus)
            if self._check_quit(vk, is_up):
                # Don't forward to remote, don't suppress
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            # If viewer doesn't have focus, pass through but track modifiers
            if not self._focus.has_focus():
                mod_bit = _VK_MOD_BITS.get(vk)
                if mod_bit:
                    if is_up:
                        self._held_modifiers &= ~mod_bit
                    else:
                        self._held_modifiers |= mod_bit
                return user32.CallNextHookEx(None, nCode, wParam, lParam)

            # --- Viewer has focus: capture, forward, suppress ---

            # Modifier key?
            mod_bit = _VK_MOD_BITS.get(vk)
            if mod_bit:
                if is_up:
                    self._held_modifiers &= ~mod_bit
                else:
                    self._held_modifiers |= mod_bit
                # Send modifier state update to remote
                try:
                    self._on_key_down(self._held_modifiers, 0)
                except Exception as e:
                    logger.warning(f"on_key_down error: {e}")
                if self._debug:
                    print(f"[HOOK {'UP' if is_up else 'DN'}] vk=0x{vk:02X} "
                          f"scan=0x{scan:02X} ext={extended} → MOD 0x{self._held_modifiers:02X}")
                return 1  # suppress

            # Regular key — look up HID keycode via scan code
            if extended:
                hid = _SCAN_EXT_TO_HID.get(scan)
            else:
                hid = _SCAN_TO_HID.get(scan)

            if hid is None:
                if self._debug:
                    print(f"[HOOK {'UP' if is_up else 'DN'}] vk=0x{vk:02X} "
                          f"scan=0x{scan:02X} ext={extended} → UNMAPPED")
                return 1  # suppress unknown keys while focused

            # Ctrl+Alt+End → Ctrl+Alt+Delete (like RDP/VNC)
            has_ctrl = self._held_modifiers & 0x11  # LCtrl or RCtrl
            has_alt = self._held_modifiers & 0x44   # LAlt or RAlt
            if hid == 0x4D and has_ctrl and has_alt:  # End → Delete
                hid = 0x4C
                if self._debug:
                    print(f"[HOOK] Ctrl+Alt+End → Ctrl+Alt+Delete")

            if not is_up:
                try:
                    self._on_key(self._held_modifiers, hid)
                except Exception as e:
                    logger.warning(f"on_key error: {e}")

            if self._debug:
                print(f"[HOOK {'UP' if is_up else 'DN'}] vk=0x{vk:02X} "
                      f"scan=0x{scan:02X} ext={extended} → HID 0x{hid:02X}")
            return 1  # suppress

        # prevent garbage collection of the C callback
        self._hook_proc_ref = hook_proc

        self._hook = user32.SetWindowsHookExW(
            self._WH_KEYBOARD_LL, hook_proc, None, 0)
        if not self._hook:
            print("Warning: Failed to install keyboard hook")
            return

        # Message pump — required for the hook to receive events
        msg = wintypes.MSG()
        while user32.GetMessageW(byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

        # Cleanup
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def _check_quit(self, vk: int, is_up: bool) -> bool:
        """Track Ctrl+Alt+Q quit combo. Returns True if Q is the quit trigger."""
        if vk in (0xA2, 0xA3):     # VK_LCONTROL / VK_RCONTROL
            if is_up:
                self._quit_keys.discard("ctrl")
            else:
                self._quit_keys.add("ctrl")
        elif vk in (0xA4, 0xA5):   # VK_LMENU / VK_RMENU
            if is_up:
                self._quit_keys.discard("alt")
            else:
                self._quit_keys.add("alt")
        elif vk == 0x51 and not is_up:  # VK_Q key-down
            if "ctrl" in self._quit_keys and "alt" in self._quit_keys:
                self.quit_requested = True
                return True
        return False


# ---------------------------------------------------------------------------
# pynput keyboard handler  (Linux / fallback)
# ---------------------------------------------------------------------------

def _build_pynput_keycode_map() -> dict:
    """Build mapping from pynput Key enum members to HID keycodes."""
    try:
        from pynput.keyboard import Key
    except ImportError:
        return {}

    return {
        Key.backspace: 0x2A,
        Key.tab: 0x2B,
        Key.enter: 0x28,
        Key.esc: 0x29,
        Key.space: 0x2C,
        Key.caps_lock: 0x39,
        Key.f1: 0x3A, Key.f2: 0x3B, Key.f3: 0x3C, Key.f4: 0x3D,
        Key.f5: 0x3E, Key.f6: 0x3F, Key.f7: 0x40, Key.f8: 0x41,
        Key.f9: 0x42, Key.f10: 0x43, Key.f11: 0x44, Key.f12: 0x45,
        Key.print_screen: 0x46,
        Key.scroll_lock: 0x47,
        Key.pause: 0x48,
        Key.insert: 0x49,
        Key.home: 0x4A,
        Key.page_up: 0x4B,
        Key.delete: 0x4C,
        Key.end: 0x4D,
        Key.page_down: 0x4E,
        Key.right: 0x4F,
        Key.left: 0x50,
        Key.down: 0x51,
        Key.up: 0x52,
        Key.num_lock: 0x53,
        # Modifier keys (sent as key-down / key-up, not as modifiers)
        Key.shift: 0xE1,       # Left Shift
        Key.shift_r: 0xE5,     # Right Shift
        Key.ctrl_l: 0xE0,      # Left Ctrl
        Key.ctrl_r: 0xE4,      # Right Ctrl
        Key.alt_l: 0xE2,       # Left Alt
        Key.alt_r: 0xE6,       # Right Alt
        Key.cmd: 0xE3,         # Left GUI / Win
        Key.cmd_r: 0xE7,       # Right GUI / Win
        Key.menu: 0x65,        # Menu / Application key
    }


def _build_pynput_modifier_bits() -> dict:
    try:
        from pynput.keyboard import Key
    except ImportError:
        return {}
    return {
        Key.ctrl_l: MOD_LCTRL,
        Key.ctrl_r: 0x10,
        Key.shift: MOD_LSHIFT,
        Key.shift_r: 0x20,
        Key.alt_l: MOD_LALT,
        Key.alt_r: 0x40,
        Key.cmd: MOD_LWIN,
        Key.cmd_r: 0x80,
    }


_MODIFIER_HID_KEYCODES = {0xE0, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7}


class _PynputKeyboardHandler:
    """Capture keyboard via pynput with window-focus gating (Linux / fallback)."""

    def __init__(self, on_key: OnKeyCallback, on_key_down: OnKeyDownCallback,
                 focus: _FocusDetector, debug: bool = False,
                 host_char_map: dict[str, tuple[int, int]] | None = None,
                 wayland_hybrid: bool = False):
        self._on_key = on_key
        self._on_key_down = on_key_down
        self._focus = focus
        self._debug = debug
        self._host_char_map = host_char_map
        self._wayland_hybrid = wayland_hybrid
        self._listener = None
        self._held_modifiers: int = 0
        self._pynput_key_map = _build_pynput_keycode_map()
        self._pynput_mod_bits = _build_pynput_modifier_bits()
        self._active = True
        self.quit_requested = False
        self._quit_keys: set[str] = set()

    def start(self):
        from pynput.keyboard import Listener
        self._listener = Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        self._active = False
        if self._listener:
            self._listener.stop()

    def _on_press(self, key):
        if not self._active:
            return

        # Check quit combo regardless of focus
        if self._check_quit_press(key):
            return

        if not self._focus.has_focus():
            return

        if self._debug:
            vk = getattr(key, 'vk', None)
            char = getattr(key, 'char', None)
            print(f"[KEY DOWN] key={key!r}  type={type(key).__name__}  "
                  f"vk={f'0x{vk:02X}' if vk is not None else None}  char={char!r}")

        # Update modifier state
        mod_bit = self._pynput_mod_bits.get(key)
        if mod_bit:
            self._held_modifiers |= mod_bit

        hid = self._pynput_key_map.get(key)
        if hid is not None:
            # Ctrl+Alt+End → Ctrl+Alt+Delete (like RDP/VNC)
            has_ctrl = self._held_modifiers & 0x11
            has_alt = self._held_modifiers & 0x44
            if hid == 0x4D and has_ctrl and has_alt:
                hid = 0x4C
                if self._debug:
                    print(f"[KEY] Ctrl+Alt+End → Ctrl+Alt+Delete")
            try:
                if hid in _MODIFIER_HID_KEYCODES:
                    self._on_key_down(self._held_modifiers, 0)
                else:
                    self._on_key(self._held_modifiers, hid)
            except Exception as e:
                logger.warning(f"Key callback error: {e}")
            return

        # Try vk first — needed for JIS keys that also have .char
        vk = getattr(key, 'vk', None)
        if vk is not None:
            hid_code = self._vk_to_hid(vk)
            if hid_code is not None:
                try:
                    self._on_key(self._held_modifiers, hid_code)
                except Exception as e:
                    logger.warning(f"Key callback error: {e}")
                return

        # Character key (pynput gives KeyCode with .char)
        char = getattr(key, 'char', None)
        if char and len(char) == 1:
            if self._host_char_map is not None:
                if self._wayland_hybrid:
                    # Wayland hybrid: ALL shifted chars come from the
                    # Xwayland layout (host_char_map) while unshifted
                    # chars come from the display layout (≈ target).
                    # When Shift is physically held, reverse-lookup via
                    # host_char_map; otherwise fall through to char_to_hid.
                    if self._held_modifiers & 0x22:  # any Shift held
                        host_mapping = self._host_char_map.get(char)
                        if host_mapping:
                            _host_mod, physical_keycode = host_mapping
                            if self._debug:
                                print(f"  -> WAYLAND-REVERSE char={char!r} → "
                                      f"phys=0x{physical_keycode:02X} "
                                      f"held=0x{self._held_modifiers:02X}")
                            try:
                                self._on_key(self._held_modifiers, physical_keycode)
                            except Exception as e:
                                logger.warning(f"Key callback error: {e}")
                            return
                    # No Shift: fall through to char_to_hid below
                else:
                    # Pure X11: all chars match the host layout → always
                    # reverse-lookup to get the physical keycode.
                    host_mapping = self._host_char_map.get(char)
                    if host_mapping:
                        _host_mod, physical_keycode = host_mapping
                        if self._debug:
                            print(f"  -> HOST REVERSE char={char!r} → "
                                  f"phys=0x{physical_keycode:02X} "
                                  f"mod=0x{self._held_modifiers:02X}")
                        try:
                            self._on_key(self._held_modifiers, physical_keycode)
                        except Exception as e:
                            logger.warning(f"Key callback error: {e}")
                        return
            # Fallback: use target layout char_to_hid (original behaviour)
            mapping = char_to_hid(char)
            if mapping:
                char_mod, keycode = mapping
                # pynput delivers the shifted character (e.g. '@' for
                # Shift+2 on a US host).  char_to_hid already knows
                # whether Shift is needed on the *target* layout, so
                # use its Shift bits instead of the physically-held
                # ones.  Keep Ctrl/Alt/Win from _held_modifiers.
                mod = (self._held_modifiers & ~0x22) | (char_mod & 0x22)
                if self._debug:
                    print(f"  -> CHAR char={char!r} → "
                          f"char_to_hid=(0x{char_mod:02X}, 0x{keycode:02X}) "
                          f"held=0x{self._held_modifiers:02X} → "
                          f"send=(0x{mod:02X}, 0x{keycode:02X})")
                try:
                    self._on_key(mod, keycode)
                except Exception as e:
                    logger.warning(f"Key callback error: {e}")
                return

            if self._debug:
                print(f"  -> CHAR UNMAPPED char={char!r} (0x{ord(char):04X})")

        if self._debug and vk is not None:
            print(f"  -> UNMAPPED vk=0x{vk:02X}")

    def _on_release(self, key):
        if not self._active:
            return

        self._check_quit_release(key)

        mod_bit = self._pynput_mod_bits.get(key)
        if mod_bit:
            self._held_modifiers &= ~mod_bit

        if not self._focus.has_focus():
            return

        hid = self._pynput_key_map.get(key)
        if hid is not None and hid in _MODIFIER_HID_KEYCODES:
            try:
                self._on_key_down(self._held_modifiers, 0)
            except Exception as e:
                logger.warning(f"Key callback error: {e}")

    def _check_quit_press(self, key) -> bool:
        from pynput.keyboard import Key
        if key in (Key.ctrl_l, Key.ctrl_r):
            self._quit_keys.add("ctrl")
        elif key in (Key.alt_l, Key.alt_r):
            self._quit_keys.add("alt")
        elif (getattr(key, 'char', None) == 'q'
              and "ctrl" in self._quit_keys
              and "alt" in self._quit_keys):
            self.quit_requested = True
            return True
        return False

    def _check_quit_release(self, key):
        from pynput.keyboard import Key
        if key in (Key.ctrl_l, Key.ctrl_r):
            self._quit_keys.discard("ctrl")
        elif key in (Key.alt_l, Key.alt_r):
            self._quit_keys.discard("alt")

    @staticmethod
    def _vk_to_hid(vk: int) -> int | None:
        """Map OS virtual key code to HID keycode for special keys."""
        if _IS_WINDOWS:
            vk_map = {
                0xF3: 0x35,  # VK_OEM_AUTO  → Hankaku/Zenkaku
                0xF4: 0x35,  # VK_OEM_ENLW  → Hankaku/Zenkaku
                0xE2: 0x87,  # VK_OEM_102   → International1 (ろ)
                0xDC: 0x89,  # VK_OEM_5     → International3 (¥|)
                0xF2: 0x8A,  # VK_OEM_COPY  → 変換
                0xF1: 0x8B,  # VK_OEM_BACKTAB → 無変換
                0x19: 0x90,  # VK_KANJI → Lang1
                0x15: 0x91,  # VK_KANA  → Lang2
                0x1C: 0x8A,  # VK_CONVERT → 変換
                0x1D: 0x8B,  # VK_NONCONVERT → 無変換
            }
            return vk_map.get(vk)
        else:
            vk_map = {
                0xFF22: 0x8A,  # Muhenkan
                0xFF23: 0x8B,  # Henkan
                0xFF21: 0x90,  # Kana_Lock
                0xFF2F: 0x91,  # Eisu_toggle
                0xFF7E: 0x35,  # Zenkaku_Hankaku
            }
            return vk_map.get(vk)


# ---------------------------------------------------------------------------
# OpenCV keycode -> HID keycode mapping (fallback when nothing else works)
# ---------------------------------------------------------------------------

def _build_cv2_special_key_map() -> dict[int, int]:
    m: dict[int, int] = {}
    if _IS_LINUX:
        m[0xFF08] = 0x2A; m[0xFF09] = 0x2B; m[0xFF0D] = 0x28; m[0xFF1B] = 0x29
        m[0xFF50] = 0x4A; m[0xFF51] = 0x50; m[0xFF52] = 0x52; m[0xFF53] = 0x4F
        m[0xFF54] = 0x51; m[0xFF55] = 0x4B; m[0xFF56] = 0x4E; m[0xFF57] = 0x4D
        m[0xFF63] = 0x49; m[0xFFFF] = 0x4C
        for i in range(12):
            m[0xFFBE + i] = 0x3A + i
    else:
        m[0x250000] = 0x50; m[0x260000] = 0x52; m[0x270000] = 0x4F; m[0x280000] = 0x51
        m[0x240000] = 0x4A; m[0x230000] = 0x4D; m[0x210000] = 0x4B; m[0x220000] = 0x4E
        m[0x2D0000] = 0x49; m[0x2E0000] = 0x4C
        for i in range(12):
            m[(0x70 + i) << 16] = 0x3A + i
    m.setdefault(8, 0x2A); m.setdefault(9, 0x2B); m.setdefault(13, 0x28)
    return m


_CV2_SPECIAL_KEYS = _build_cv2_special_key_map()


def _cv2_key_to_hid(key: int,
                    host_char_map: dict[str, tuple[int, int]] | None = None,
                    ) -> tuple[int, int] | None:
    hid = _CV2_SPECIAL_KEYS.get(key)
    if hid is not None:
        return (0x00, hid)
    if 1 <= key <= 26:
        return (MOD_LCTRL, 0x04 + key - 1)
    if 32 <= key <= 126:
        char = chr(key)
        if host_char_map is not None:
            mapping = host_char_map.get(char)
            if mapping:
                return mapping
        return char_to_hid(char)
    return None


# ---------------------------------------------------------------------------
# In-process preview (called from kvm_server.py)
# ---------------------------------------------------------------------------

def run_preview_inprocess(hardware, config: Config,
                          host_char_map: dict[str, tuple[int, int]] | None = None,
                          wayland_hybrid: bool = False):
    """Run the interactive preview window in the current (main) thread.

    Args:
        hardware: KvmHardware instance (from kvm_server).
        config: Config instance.
        host_char_map: If provided, maps characters to (modifier, keycode)
            on the *host* layout for reverse-lookup in pynput / CV2 handlers.
        wayland_hybrid: When True, pynput receives a hybrid of display-layout
            unshifted chars and Xwayland shifted chars.  The handler uses a
            Shift-consistency test to choose between char_to_hid and reverse
            lookup.
    """
    debug_keys = config.debug_keys

    ch9329 = hardware.get_ch9329()
    capture = hardware.get_capture()

    screen_w = config.screen_width
    screen_h = config.screen_height

    # --- Build callback functions that talk directly to CH9329 ---

    def on_key(modifier: int, keycode: int):
        """Key press + release."""
        ch9329.send_keyboard(modifier, keycode)

    def on_key_down(modifier: int, keycode: int):
        """Key down only (modifier state update)."""
        ch9329.send(build_keyboard_packet(modifier, keycode))

    def on_mouse(buttons: int, abs_x: int, abs_y: int):
        """Absolute mouse position."""
        ch9329.send(build_mouse_abs_packet(buttons, abs_x, abs_y))

    def on_scroll(amount: int):
        """Scroll wheel."""
        ch9329.send(build_mouse_rel_packet(0, 0, 0, scroll=amount))

    # --- Set up window ---

    window_name = "KVM Preview (Interactive)"
    frame_w = 0
    frame_h = 0
    mouse_buttons = 0

    print("KVM Preview Viewer (Interactive)")

    if _IS_WINDOWS:
        print("Keyboard: Win32 low-level hook (all keys captured when focused)")
    else:
        try:
            import pynput  # noqa: F401
            print("Keyboard: pynput (host keys NOT suppressed)")
        except ImportError:
            print("Keyboard: OpenCV fallback (install pynput for modifier support)")

    if debug_keys:
        print("Key debug: ON (--debug-keys)")
    print("Ctrl+Alt+End → Ctrl+Alt+Del (sent to target)")
    print("Ctrl+Alt+Q   → Quit the viewer")

    # Pre-load blank cursor for hiding crosshair (target OS draws its own)
    _blank_cursor = None
    if not config.show_cursor and _IS_WINDOWS:
        try:
            import ctypes
            # Create 1x1 transparent cursor
            and_mask = (ctypes.c_ubyte * 1)(0xFF)
            xor_mask = (ctypes.c_ubyte * 1)(0x00)
            _blank_cursor = ctypes.windll.user32.CreateCursor(
                None, 0, 0, 1, 1, and_mask, xor_mask)
        except Exception:
            pass

    def mouse_callback(event, x, y, flags, _):
        nonlocal mouse_buttons
        # Hide cursor on every mouse event (OpenCV resets it)
        if _blank_cursor is not None:
            import ctypes
            ctypes.windll.user32.SetCursor(_blank_cursor)
        if frame_w == 0 or frame_h == 0:
            return
        # Scale window coords to CH9329 absolute coords (0-4095)
        abs_x = max(0, min(4095, int(x * 4096 / frame_w)))
        abs_y = max(0, min(4095, int(y * 4096 / frame_h)))

        if event == cv2.EVENT_LBUTTONDOWN:
            mouse_buttons |= 0x01
        elif event == cv2.EVENT_LBUTTONUP:
            mouse_buttons &= ~0x01
        elif event == cv2.EVENT_RBUTTONDOWN:
            mouse_buttons |= 0x02
        elif event == cv2.EVENT_RBUTTONUP:
            mouse_buttons &= ~0x02
        elif event == cv2.EVENT_MBUTTONDOWN:
            mouse_buttons |= 0x04
        elif event == cv2.EVENT_MBUTTONUP:
            mouse_buttons &= ~0x04
        elif event == cv2.EVENT_MOUSEWHEEL:
            amount = 3 if flags > 0 else -3
            try:
                on_scroll(amount)
            except Exception as e:
                logger.warning(f"Scroll error: {e}")
            return

        if event in (cv2.EVENT_MOUSEMOVE,
                     cv2.EVENT_LBUTTONDOWN, cv2.EVENT_LBUTTONUP,
                     cv2.EVENT_RBUTTONDOWN, cv2.EVENT_RBUTTONUP,
                     cv2.EVENT_MBUTTONDOWN, cv2.EVENT_MBUTTONUP):
            try:
                on_mouse(mouse_buttons, abs_x, abs_y)
            except Exception as e:
                logger.warning(f"Mouse error: {e}")

    # WINDOW_GUI_NORMAL suppresses Qt toolbar/statusbar/context-menu when
    # OpenCV is built with the Qt backend (common on Linux).
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE | cv2.WINDOW_GUI_NORMAL)
    cv2.setMouseCallback(window_name, mouse_callback)

    # Set up keyboard handler based on platform
    focus = _FocusDetector(window_name)
    kb_handler = None

    if _IS_WINDOWS:
        kb_handler = _Win32KeyboardGrab(on_key, on_key_down, focus,
                                        debug=debug_keys)
        kb_handler.start()
    else:
        try:
            import pynput  # noqa: F401
            kb_handler = _PynputKeyboardHandler(on_key, on_key_down, focus,
                                                debug=debug_keys,
                                                host_char_map=host_char_map,
                                                wayland_hybrid=wayland_hybrid)
            kb_handler.start()
        except ImportError:
            pass  # will use OpenCV fallback

    try:
        while True:
            if kb_handler and kb_handler.quit_requested:
                break

            frame = capture.get_latest_frame()
            if frame is not None:
                h, w = frame.shape[:2]
                if w > 0 and h > 0:
                    frame_w, frame_h = w, h
                    cv2.imshow(window_name, frame)

            key = cv2.waitKeyEx(16)

            # OpenCV fallback keyboard (when no other handler is available)
            if kb_handler is None:
                if key == 27:
                    break
                if key != -1:
                    result = _cv2_key_to_hid(key, host_char_map=host_char_map)
                    if result is not None:
                        modifier, keycode = result
                        try:
                            on_key(modifier, keycode)
                        except Exception as e:
                            logger.warning(f"Key error: {e}")

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if kb_handler:
            kb_handler.stop()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    # Standalone mode is no longer supported — use serial-hid-kvm instead
    print("This module is now part of serial-hid-kvm.")
    print("Run: serial-hid-kvm")
    print("Or:  serial-hid-kvm --headless")
