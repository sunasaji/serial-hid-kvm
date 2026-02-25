"""KVM Server — standalone process owning CH9329 serial and HDMI capture.

Provides three interfaces to the KVM hardware (all opt-in except preview):
  - Preview window (default, disable with --headless)
  - Web viewer (--web): browser-based remote desktop over WebSocket
  - API server (--api): JSON Lines protocol over TCP socket

Usage:
    serial-hid-kvm                          # preview window only
    serial-hid-kvm --api                    # preview + API server
    serial-hid-kvm --headless --api         # API server only
    serial-hid-kvm --headless --web         # web viewer only
    serial-hid-kvm --headless --api --web   # API + web viewer
    serial-hid-kvm --debug-keys             # show keycode debug output
    serial-hid-kvm list-devices             # list capture devices and exit
    serial-hid-kvm --config my.yaml         # use config file
"""

import argparse
import asyncio
import base64
import json
import logging
import platform
import signal
import subprocess
import sys
import threading

from .config import Config, load_config
from .serial_detect import auto_detect_port
from .hid_protocol import CH9329
from .hid_keyboard import Keyboard
from .hid_mouse import Mouse
from .hid_keycodes import (
    set_layout, get_layout, build_char_map,
    char_to_hid, special_key_to_hid, modifier_name_to_bit,
)
from .capture import ScreenCapture, list_capture_devices

logger = logging.getLogger(__name__)


def _setup_logging(log_file: str | None = None):
    """Configure logging to stderr, and optionally to a file."""
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


# ---------------------------------------------------------------------------
# KvmHardware — single owner of all hardware resources
# ---------------------------------------------------------------------------

class KvmHardware:
    """Lazy-initialised container for all KVM hardware resources."""

    def __init__(self, config: Config):
        self._config = config
        self._ch9329: CH9329 | None = None
        self._keyboard: Keyboard | None = None
        self._mouse: Mouse | None = None
        self._capture: ScreenCapture | None = None

    def get_ch9329(self) -> CH9329:
        if self._ch9329 is None:
            port = self._config.serial_port or auto_detect_port()
            self._ch9329 = CH9329(port, self._config.serial_baud)
            self._ch9329.open()
        return self._ch9329

    def get_keyboard(self) -> Keyboard:
        if self._keyboard is None:
            self._keyboard = Keyboard(self.get_ch9329())
        return self._keyboard

    def get_mouse(self) -> Mouse:
        if self._mouse is None:
            self._mouse = Mouse(
                self.get_ch9329(),
                self._config.screen_width,
                self._config.screen_height,
            )
        return self._mouse

    def get_capture(self) -> ScreenCapture:
        if self._capture is None:
            self._capture = ScreenCapture(
                self._config.capture_device,
                preview=False,
                width=self._config.capture_width,
                height=self._config.capture_height,
            )
            self._capture.start_capture_thread()
        return self._capture

    def close(self):
        if self._capture is not None:
            self._capture.close()
            self._capture = None
        if self._ch9329 is not None:
            try:
                self._ch9329.release_all()
            except Exception:
                pass
            self._ch9329.close()
            self._ch9329 = None
        self._keyboard = None
        self._mouse = None


# ---------------------------------------------------------------------------
# ApiDispatcher — JSON request → hardware call
# ---------------------------------------------------------------------------

class ApiDispatcher:
    """Dispatch JSON RPC-style requests to KvmHardware methods."""

    def __init__(self, hardware: KvmHardware, config: Config):
        self._hw = hardware
        self._config = config

    async def dispatch(self, method: str, params: dict) -> dict:
        """Execute *method* with *params* and return a result dict.

        Blocking hardware calls are run in the default executor.
        """
        loop = asyncio.get_running_loop()
        handler = getattr(self, f"_do_{method}", None)
        if handler is None:
            raise ValueError(f"Unknown method: {method}")
        return await loop.run_in_executor(None, handler, params)

    # -- method handlers (run in thread pool) --------------------------------

    def _do_ping(self, params: dict) -> dict:
        return {"pong": True}

    def _do_type_text(self, params: dict) -> dict:
        text = params["text"]
        delay_ms = params.get("char_delay_ms")
        delay = delay_ms / 1000.0 if delay_ms is not None else None
        self._hw.get_keyboard().type_text(text, char_delay=delay)
        result: dict = {"chars_typed": len(text)}
        if self._config.debug_keys:
            result["hid_trace"] = self._trace_text(text)
        return result

    def _do_send_key(self, params: dict) -> dict:
        key = params["key"]
        modifiers = params.get("modifiers", [])
        self._hw.get_keyboard().send_key(key, modifiers)
        result: dict = {"sent": True}
        if self._config.debug_keys:
            result["hid_trace"] = self._trace_key(key, modifiers)
        return result

    def _do_send_key_sequence(self, params: dict) -> dict:
        steps = params["steps"]
        default_delay = params.get("default_delay_ms", 100)
        self._hw.get_keyboard().send_key_sequence(steps, default_delay_ms=default_delay)
        result: dict = {"steps_sent": len(steps)}
        if self._config.debug_keys:
            result["hid_trace"] = [
                self._trace_key(s["key"], s.get("modifiers", []))
                for s in steps
            ]
        return result

    # -- debug trace helpers ---------------------------------------------------

    @staticmethod
    def _trace_key(key: str, modifiers: list[str] | None = None) -> dict:
        """Resolve a key + modifiers to HID debug info."""
        mod_bits = 0
        if modifiers:
            for m in modifiers:
                bit = modifier_name_to_bit(m)
                if bit is not None:
                    mod_bits |= bit

        keycode = special_key_to_hid(key)
        if keycode is not None:
            return {"key": key, "modifier": f"0x{mod_bits:02X}",
                    "keycode": f"0x{keycode:02X}"}

        if len(key) == 1:
            mapping = char_to_hid(key)
            if mapping is not None:
                char_mod, kc = mapping
                return {"key": key, "modifier": f"0x{mod_bits | char_mod:02X}",
                        "keycode": f"0x{kc:02X}"}

        return {"key": key, "modifier": f"0x{mod_bits:02X}",
                "keycode": None, "error": "unmapped"}

    def _trace_text(self, text: str) -> list[dict]:
        """Resolve each character/tag in text to HID debug info."""
        from .hid_keyboard import Keyboard
        tokens = Keyboard._tokenize(text)
        trace = []
        for token in tokens:
            if token.startswith("\x01"):
                tag = token[1:]
                parts = [p.strip() for p in tag.split("+")]
                key_part = parts[-1]
                mod_parts = parts[:-1]
                trace.append(self._trace_key(key_part, mod_parts))
            else:
                mapping = char_to_hid(token)
                if mapping is not None:
                    mod, kc = mapping
                    trace.append({"char": token,
                                  "modifier": f"0x{mod:02X}",
                                  "keycode": f"0x{kc:02X}"})
                else:
                    trace.append({"char": token, "keycode": None,
                                  "error": "unmapped"})
        return trace

    def _do_mouse_move(self, params: dict) -> dict:
        x = params["x"]
        y = params["y"]
        relative = params.get("relative", False)
        mouse = self._hw.get_mouse()
        if relative:
            mouse.move_relative(x, y)
        else:
            mouse.move_absolute(x, y)
        return {"position": {"x": x, "y": y, "relative": relative}}

    def _do_mouse_click(self, params: dict) -> dict:
        button = params.get("button", "left")
        x = params.get("x")
        y = params.get("y")
        self._hw.get_mouse().click(button, x, y)
        return {"clicked": True}

    def _do_mouse_down(self, params: dict) -> dict:
        button = params.get("button", "left")
        x = params.get("x")
        y = params.get("y")
        self._hw.get_mouse().mouse_down(button, x, y)
        return {"pressed": True, "button": button}

    def _do_mouse_up(self, params: dict) -> dict:
        button = params.get("button", "left")
        x = params.get("x")
        y = params.get("y")
        self._hw.get_mouse().mouse_up(button, x, y)
        return {"released": True, "button": button}

    def _do_mouse_scroll(self, params: dict) -> dict:
        amount = params["amount"]
        self._hw.get_mouse().scroll(amount)
        return {"scrolled": True}

    def _do_capture_frame(self, params: dict) -> dict:
        quality = params.get("quality", 85)
        cap = self._hw.get_capture()
        result = cap.get_frame_jpeg(quality)
        if result is None:
            # Fall back to single-shot capture
            image = cap.capture()
            import io
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=quality)
            jpeg_bytes = buf.getvalue()
            w, h = image.size
        else:
            jpeg_bytes, w, h = result
        b64 = base64.b64encode(jpeg_bytes).decode("ascii")
        return {"jpeg_b64": b64, "width": w, "height": h}

    def _do_get_device_info(self, params: dict) -> dict:
        from .serial_detect import list_ch340_ports
        info: dict = {}
        info["ch340_ports"] = list_ch340_ports()
        try:
            dev = self._hw.get_ch9329()
            info["serial"] = {
                "port": dev.port,
                "baudrate": dev.baudrate,
                "connected": dev.is_open,
            }
        except Exception as e:
            info["serial"] = {"error": str(e)}
        try:
            info["capture"] = self._hw.get_capture().get_info()
        except Exception as e:
            info["capture"] = {"error": str(e)}
        info["config"] = {
            "screen_width": self._config.screen_width,
            "screen_height": self._config.screen_height,
            "serial_baud": self._config.serial_baud,
            "keyboard_layout": get_layout(),
        }
        return info

    def _do_list_capture_devices(self, params: dict) -> dict:
        return {"devices": list_capture_devices()}

    def _do_set_capture_device(self, params: dict) -> dict:
        device = params["device"]
        dev_value: int | str = int(device) if device.isdigit() else device
        cap = self._hw.get_capture()
        cap.switch_device(dev_value)
        return {"info": cap.get_info()}

    def _do_set_capture_resolution(self, params: dict) -> dict:
        width = params["width"]
        height = params["height"]
        cap = self._hw.get_capture()
        cap.set_resolution(width, height)
        return {"info": cap.get_info()}


# ---------------------------------------------------------------------------
# TcpServer — asyncio-based JSON Lines TCP server
# ---------------------------------------------------------------------------

class TcpServer:
    """TCP server accepting JSON Lines requests, dispatching via ApiDispatcher."""

    def __init__(self, dispatcher: ApiDispatcher, host: str, port: int):
        self._dispatcher = dispatcher
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None
        self._client_count = 0

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        logger.info(f"API server listening on {self._host}:{self._port}")

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        self._client_count += 1
        peer = writer.get_extra_info("peername")
        cid = self._client_count
        logger.info(f"Client #{cid} connected from {peer}")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError as e:
                    resp = {"id": None, "ok": False, "error": f"Invalid JSON: {e}"}
                    writer.write((json.dumps(resp) + "\n").encode())
                    await writer.drain()
                    continue

                req_id = req.get("id")
                method = req.get("method", "")
                params = req.get("params", {})

                try:
                    result = await self._dispatcher.dispatch(method, params)
                    resp = {"id": req_id, "ok": True, "result": result}
                except Exception as e:
                    logger.warning(f"Client #{cid} method {method} error: {e}")
                    resp = {"id": req_id, "ok": False, "error": str(e)}

                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            logger.warning(f"Client #{cid} error: {e}")
        finally:
            logger.info(f"Client #{cid} disconnected")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serial-hid-kvm",
        description="KVM control via CH9329 USB HID emulator + HDMI capture",
    )

    # Subcommands
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list-devices", help="List capture devices and exit")

    # Config file
    parser.add_argument("-c", "--config", metavar="FILE",
                        help="Path to YAML config file")

    # Server options
    parser.add_argument("--headless", action="store_true",
                        help="Run without preview window")
    parser.add_argument("--log-file", type=str, metavar="PATH",
                        help="Also write logs to file (in addition to stderr)")
    parser.add_argument("--debug-keys", action="store_true",
                        help="Print keycode debug output to console")
    parser.add_argument("--show-cursor", action="store_true",
                        help="Show crosshair cursor on preview window")

    # API server (JSON Lines over TCP socket)
    parser.add_argument("--api", action="store_true",
                        help="Enable API server (JSON Lines over TCP socket)")
    parser.add_argument("--api-host", type=str, metavar="ADDR",
                        help="API server bind address (default: 127.0.0.1)")
    parser.add_argument("--api-port", "-p", type=int, metavar="PORT",
                        help="API server port (default: 9329)")

    # Serial
    parser.add_argument("--serial-port", "-s", type=str, metavar="PORT",
                        help="Serial port (e.g. COM3, /dev/ttyUSB0)")
    parser.add_argument("--serial-baud", type=int, metavar="BAUD",
                        help="Serial baud rate (default: 9600)")

    # Screen
    parser.add_argument("--screen-width", type=int, metavar="PX",
                        help="Target screen width (default: 1920)")
    parser.add_argument("--screen-height", type=int, metavar="PX",
                        help="Target screen height (default: 1080)")

    # Capture
    parser.add_argument("--capture-device", type=str, metavar="DEV",
                        help="Capture device index or path")
    parser.add_argument("--capture-width", type=int, metavar="PX",
                        help="Capture resolution width (default: 1920)")
    parser.add_argument("--capture-height", type=int, metavar="PX",
                        help="Capture resolution height (default: 1080)")

    # Audio (for web viewer)
    parser.add_argument("--audio-device", type=str, metavar="DEV",
                        help="Audio input device name or index for web viewer"
                             " (requires: pip install serial-hid-kvm[audio])")

    # Web viewer
    parser.add_argument("--web", action="store_true",
                        help="Enable web-based remote desktop viewer")
    parser.add_argument("--web-host", type=str, metavar="ADDR",
                        help="Web viewer bind address (default: 127.0.0.1)")
    parser.add_argument("--web-port", type=int, metavar="PORT",
                        help="Web viewer port (default: 9330)")
    parser.add_argument("--web-fps", type=int, metavar="FPS",
                        help="Web viewer frame rate (default: 20)")
    parser.add_argument("--web-quality", type=int, metavar="Q",
                        help="Web viewer JPEG quality 1-100 (default: 60)")

    # Keyboard
    parser.add_argument("--target-layout", type=str, metavar="NAME",
                        help="Target keyboard layout: us104, jp106, uk105, de105, fr105")
    parser.add_argument("--host-layout", type=str, metavar="NAME",
                        help="Host keyboard layout (default: auto-detect, 'none' to disable)")
    parser.add_argument("--layouts-dir", type=str, metavar="DIR",
                        help="Directory for custom layout YAML files")

    return parser


# ---------------------------------------------------------------------------
# Host layout auto-detection
# ---------------------------------------------------------------------------

# XKB layout name → serial-hid-kvm layout name
_XKB_TO_LAYOUT: dict[str, str] = {
    "us": "us104",
    "jp": "jp106",
    "gb": "uk105",
    "de": "de105",
    "fr": "fr105",
}


def _detect_host_layout() -> tuple[str, bool]:
    """Auto-detect the host keyboard layout for pynput reverse-lookup.

    On Wayland, pynput uses Xwayland whose keymap may differ from the
    GNOME compositor.  When a mismatch is detected (``wayland_hybrid``),
    unshifted characters from pynput match the display layout while
    shifted characters match the Xwayland layout.

    Returns:
        ``(pynput_layout, wayland_hybrid)`` where *pynput_layout* is the
        layout name that best describes what pynput receives, and
        *wayland_hybrid* is ``True`` when the display and Xwayland
        layouts disagree.
    """
    if platform.system() != "Linux":
        return ("us104", False)

    import re

    display_layout: str | None = None
    xwayland_layout: str | None = None

    # 1. GNOME gsettings → display layout (what the compositor uses)
    try:
        proc = subprocess.run(
            ["gsettings", "get",
             "org.gnome.desktop.input-sources", "sources"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            match = re.search(r"\('xkb',\s*'([^']+)'\)", proc.stdout)
            if match:
                xkb_name = match.group(1).split("+")[0]
                layout = _XKB_TO_LAYOUT.get(xkb_name)
                if layout:
                    display_layout = layout
                    logger.debug(f"gsettings layout: {xkb_name!r} → {layout}")
    except Exception as e:
        logger.debug(f"gsettings detection failed: {e}")

    # 2. setxkbmap -query → Xwayland / X11 layout (what pynput sees)
    try:
        proc = subprocess.run(
            ["setxkbmap", "-query"],
            capture_output=True, text=True, timeout=5,
        )
        for line in proc.stdout.splitlines():
            if line.strip().startswith("layout:"):
                xkb_name = line.split(":", 1)[1].strip().split(",")[0]
                xwayland_layout = _XKB_TO_LAYOUT.get(xkb_name, "us104")
                logger.debug(f"setxkbmap layout: {xkb_name!r} "
                             f"→ {xwayland_layout}")
                break
    except Exception as e:
        logger.debug(f"setxkbmap detection failed: {e}")

    # Determine effective pynput layout and Wayland hybrid flag
    if display_layout and xwayland_layout:
        if display_layout != xwayland_layout:
            logger.info(f"Detected Wayland layout split: "
                        f"display={display_layout}, "
                        f"Xwayland={xwayland_layout}")
            return (xwayland_layout, True)
        logger.info(f"Detected host layout: {display_layout}")
        return (display_layout, False)

    if xwayland_layout:
        logger.info(f"Detected host layout (setxkbmap): {xwayland_layout}")
        return (xwayland_layout, False)

    if display_layout:
        logger.info(f"Detected host layout (gsettings): {display_layout}")
        return (display_layout, False)

    return ("us104", False)


# ---------------------------------------------------------------------------
# Subcommand: list-devices
# ---------------------------------------------------------------------------

def _audio_vidpid_lookup() -> dict[str, str]:
    """Build a mapping of audio device name → VID:PID (best-effort).

    Windows: queries PnP for media devices and matches by name substring.
    Linux: scans ALSA card sysfs for USB parent VID/PID.
    """
    result: dict[str, str] = {}
    system = platform.system()

    if system == "Windows":
        try:
            from .capture import _parse_vidpid
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_PnPEntity"
                 " | Where-Object { $_.PNPClass -eq 'Media'"
                 " -or $_.PNPClass -eq 'AudioEndpoint' }"
                 " | Select-Object Name, DeviceID"
                 " | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                import json as _json
                data = _json.loads(proc.stdout)
                if isinstance(data, dict):
                    data = [data]
                # Build PnP name → vidpid lookup
                pnp_map: list[tuple[str, str]] = []
                for d in data:
                    vp = _parse_vidpid(d.get("DeviceID", ""))
                    if vp:
                        pnp_map.append((d.get("Name", ""), vp))
                # Match sounddevice names against PnP names by substring
                import sounddevice as sd
                for dev in sd.query_devices():
                    name = dev["name"]
                    for pnp_name, vp in pnp_map:
                        if pnp_name in name or name in pnp_name:
                            result[name] = vp
                            break
        except Exception:
            pass

    elif system == "Linux":
        from pathlib import Path
        snd_path = Path("/sys/class/sound")
        if snd_path.exists():
            for card_dir in sorted(snd_path.glob("card*")):
                device_link = card_dir / "device"
                if not device_link.exists():
                    continue
                try:
                    real = device_link.resolve()
                    vid = pid = ""
                    for parent in [real] + list(real.parents):
                        vid_f = parent / "idVendor"
                        pid_f = parent / "idProduct"
                        if vid_f.exists() and pid_f.exists():
                            vid = vid_f.read_text().strip().upper()
                            pid = pid_f.read_text().strip().upper()
                            break
                    if not vid:
                        continue
                    vp = f"{vid}:{pid}"
                    # Read card name and map it
                    id_file = card_dir / "id"
                    if id_file.exists():
                        card_id = id_file.read_text().strip()
                        result[card_id] = vp
                except Exception:
                    pass

    return result


def _cmd_list_devices():
    """List devices and exit."""
    import serial.tools.list_ports
    print("Serial ports:")
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("  (none found)")
    else:
        from .serial_detect import CH340_VIDPID
        for p in ports:
            tag = ""
            vidpid = ""
            if p.vid is not None and p.pid is not None:
                vidpid = f"  [{p.vid:04X}:{p.pid:04X}]"
                if (p.vid, p.pid) in CH340_VIDPID:
                    tag = "  [CH340 - auto-detect target]"
            print(f"  {p.device:20s}  {p.description}{vidpid}{tag}")

    print("\nVideo capture devices:")
    devices = list_capture_devices()
    if not devices:
        print("  (none found)")
    else:
        for d in devices:
            vp = f"  [{d['vidpid']}]" if d.get("vidpid") else ""
            print(f"  {d['device']:20s}  {d['name']}{vp}")

    print("\nAudio input devices:")
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        # Prefer WASAPI on Windows (matches OS sound settings),
        # fall back to default host API on other platforms.
        hostapis = sd.query_hostapis()
        api_idx = None
        for idx, api in enumerate(hostapis):
            if "WASAPI" in api["name"]:
                api_idx = idx
                break
        if api_idx is None:
            api_idx = sd.default.hostapi

        # Build VID:PID lookup from PnP (Windows) or sysfs (Linux)
        audio_vidpid = _audio_vidpid_lookup()

        found = False
        for i, d in enumerate(devs):
            if d["max_input_channels"] > 0 and d["hostapi"] == api_idx:
                sr = int(d["default_samplerate"])
                ch = d["max_input_channels"]
                name = d["name"]
                vp = audio_vidpid.get(name, "")
                vp_str = f"  [{vp}]" if vp else ""
                print(f"  {i:<4d}  {name}  [{sr} Hz, {ch} ch]{vp_str}")
                found = True
        if not found:
            print("  (none found)")
    except ImportError:
        print("  (sounddevice not installed — pip install serial-hid-kvm[audio])")
    except Exception as e:
        print(f"  (error: {e})")


# ---------------------------------------------------------------------------
# main — entry point
# ---------------------------------------------------------------------------

def main():
    parser = _build_parser()
    args = parser.parse_args()

    # Set up logging (before anything else that logs)
    import os
    log_file = getattr(args, "log_file", None) or os.environ.get("SHKVM_LOG_FILE")
    _setup_logging(log_file)

    # Handle subcommands
    if args.command == "list-devices":
        _cmd_list_devices()
        return

    # Load config: CLI > env > file > defaults
    config = load_config(args)

    # Apply keyboard layout
    source = set_layout(config.target_layout, layouts_dir=config.layouts_dir)
    logger.info(f"Keyboard layout: {config.target_layout} ({source})")

    # Resolve host layout for preview reverse-lookup
    host_char_map: dict[str, tuple[int, int]] | None = None
    wayland_hybrid = False
    host_layout = config.host_layout
    if host_layout == "auto":
        host_layout, wayland_hybrid = _detect_host_layout()
    if host_layout != "none" and host_layout != config.target_layout:
        host_char_map = build_char_map(host_layout, layouts_dir=config.layouts_dir)
        extra = ", wayland-hybrid" if wayland_hybrid else ""
        logger.info(f"Host layout: {host_layout} (reverse-lookup enabled{extra})")
    elif host_layout == "none":
        logger.info("Host layout: disabled (--host-layout none)")
    else:
        logger.info(f"Host layout: {host_layout} (same as target, no reverse-lookup)")

    if config.headless and not config.api_enabled and not config.web_enabled:
        logger.error("--headless requires at least one of --api or --web")
        sys.exit(1)

    hardware = KvmHardware(config)
    dispatcher = ApiDispatcher(hardware, config)

    # Create shared audio capture if configured
    audio_capture = None
    if config.audio_device is not None:
        try:
            from ._audio import AudioCapture
            audio_capture = AudioCapture(config.audio_device)
            audio_capture.start()
        except ImportError:
            logger.error(
                "Audio requires sounddevice: pip install serial-hid-kvm[audio]"
            )
        except Exception as e:
            logger.error(f"Failed to open audio device: {e}")

    if config.headless:
        _run_headless(hardware, config, dispatcher, audio_capture)
    else:
        _run_with_preview(hardware, config, dispatcher, audio_capture,
                          host_char_map=host_char_map,
                          wayland_hybrid=wayland_hybrid)


def _run_headless(hardware: KvmHardware, config: Config,
                  dispatcher: ApiDispatcher, audio_capture=None):
    """Run without preview window.  Starts API / web servers as configured."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    tcp = None
    if config.api_enabled:
        tcp = TcpServer(dispatcher, config.api_host, config.api_port)
    web = None
    if config.web_enabled:
        from ._web_viewer import WebViewerServer
        web = WebViewerServer(hardware, config, audio=audio_capture)

    async def serve():
        if tcp is not None:
            await tcp.start()
        if web is not None:
            await web.start()
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                pass  # Windows

        await stop_event.wait()
        if web is not None:
            await web.stop()
        if tcp is not None:
            await tcp.stop()

    try:
        loop.run_until_complete(serve())
    except KeyboardInterrupt:
        pass
    finally:
        # Cancel remaining tasks (e.g. IOCP accept on Windows)
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(loop.shutdown_asyncgens())
        if audio_capture is not None:
            audio_capture.stop()
        hardware.close()
        loop.close()
        logger.info("KVM server stopped")


def _run_with_preview(hardware: KvmHardware, config: Config,
                      dispatcher: ApiDispatcher, audio_capture=None,
                      host_char_map: dict[str, tuple[int, int]] | None = None,
                      wayland_hybrid: bool = False):
    """Run preview window on main thread, API/web servers on a daemon thread."""
    loop = asyncio.new_event_loop()
    tcp = None
    if config.api_enabled:
        tcp = TcpServer(dispatcher, config.api_host, config.api_port)
    web = None
    if config.web_enabled:
        from ._web_viewer import WebViewerServer
        web = WebViewerServer(hardware, config, audio=audio_capture)

    # Audio playback for preview window
    audio_playback = None
    if audio_capture is not None:
        try:
            from ._audio import AudioPlayback
            audio_playback = AudioPlayback(audio_capture)
            audio_playback.start()
        except Exception as e:
            logger.warning(f"Audio playback failed: {e}")

    need_async = tcp is not None or web is not None

    def server_thread():
        asyncio.set_event_loop(loop)
        if tcp is not None:
            loop.run_until_complete(tcp.start())
        if web is not None:
            loop.run_until_complete(web.start())
        loop.run_forever()

    t = None
    if need_async:
        t = threading.Thread(target=server_thread, daemon=True, name="servers")
        t.start()

    # Run the preview window on the main thread (required for GUI + Win32 hooks)
    try:
        from ._preview_viewer import run_preview_inprocess
        run_preview_inprocess(hardware, config, host_char_map=host_char_map,
                              wayland_hybrid=wayland_hybrid)
    except KeyboardInterrupt:
        pass
    finally:
        if audio_playback is not None:
            audio_playback.stop()
        if t is not None:
            async def _shutdown():
                if web is not None:
                    await web.stop()
                if tcp is not None:
                    await tcp.stop()
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                loop.stop()
            asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            t.join(timeout=3)
        if audio_capture is not None:
            audio_capture.stop()
        hardware.close()
        logger.info("KVM server stopped")


if __name__ == "__main__":
    main()
