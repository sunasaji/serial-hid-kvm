"""TCP client for KVM Server.

Provides a synchronous, thread-safe interface for sending commands to the
KVM server via JSON Lines over TCP.  Auto-reconnects on connection loss.
"""

import base64
import json
import logging
import socket
import threading
import time
import uuid

logger = logging.getLogger(__name__)


class KvmClientError(Exception):
    """Raised when the KVM server returns an error response."""


class KvmClient:
    """Synchronous TCP client for the KVM server JSON Lines API.

    Thread-safe: all socket I/O is serialised with a lock.
    Auto-reconnects on send failure (one retry).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9329,
                 timeout: float = 30.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._rfile = None

    # -- connection management -----------------------------------------------

    def connect(self):
        """Establish TCP connection to the KVM server."""
        with self._lock:
            self._connect_unlocked()

    def _connect_unlocked(self):
        self._close_unlocked()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        sock.connect((self._host, self._port))
        self._sock = sock
        self._rfile = sock.makefile("r", encoding="utf-8")
        logger.info(f"Connected to KVM server at {self._host}:{self._port}")

    def close(self):
        """Close the TCP connection."""
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self):
        if self._rfile is not None:
            try:
                self._rfile.close()
            except Exception:
                pass
            self._rfile = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- RPC -----------------------------------------------------------------

    def call(self, method: str, params: dict | None = None) -> dict:
        """Send a request and return the result dict.

        Raises KvmClientError on server-side errors.
        Auto-reconnects once on connection failure.
        """
        params = params or {}
        req_id = uuid.uuid4().hex[:8]
        payload = json.dumps({
            "id": req_id,
            "method": method,
            "params": params,
        }, ensure_ascii=False) + "\n"

        with self._lock:
            for attempt in range(2):
                try:
                    if self._sock is None:
                        self._connect_unlocked()
                    assert self._sock is not None
                    assert self._rfile is not None
                    self._sock.sendall(payload.encode("utf-8"))
                    line = self._rfile.readline()
                    if not line:
                        raise ConnectionError("Server closed connection")
                    resp = json.loads(line)
                    if not resp.get("ok"):
                        raise KvmClientError(resp.get("error", "Unknown error"))
                    return resp.get("result", {})
                except (ConnectionError, OSError, json.JSONDecodeError) as e:
                    self._close_unlocked()
                    if attempt == 0:
                        logger.warning(f"KVM connection lost, reconnecting: {e}")
                        time.sleep(0.5)
                        continue
                    raise KvmClientError(f"Failed to communicate with KVM server: {e}")
        raise KvmClientError("Failed to communicate with KVM server")

    # -- convenience methods -------------------------------------------------

    def ping(self) -> dict:
        return self.call("ping")

    def type_text(self, text: str, char_delay_ms: int | None = None,
                  raw: bool = False) -> dict:
        params: dict = {"text": text}
        if char_delay_ms is not None:
            params["char_delay_ms"] = char_delay_ms
        if raw:
            params["raw"] = True
        return self.call("type_text", params)

    def send_key(self, key: str, modifiers: list[str] | None = None) -> dict:
        params: dict = {"key": key}
        if modifiers:
            params["modifiers"] = modifiers
        return self.call("send_key", params)

    def send_key_sequence(self, steps: list[dict],
                          default_delay_ms: int | None = None) -> dict:
        params: dict = {"steps": steps}
        if default_delay_ms is not None:
            params["default_delay_ms"] = default_delay_ms
        return self.call("send_key_sequence", params)

    def mouse_move(self, x: int, y: int, relative: bool = False) -> dict:
        return self.call("mouse_move", {"x": x, "y": y, "relative": relative})

    def mouse_click(self, button: str = "left",
                    x: int | None = None, y: int | None = None) -> dict:
        params: dict = {"button": button}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return self.call("mouse_click", params)

    def mouse_down(self, button: str = "left",
                   x: int | None = None, y: int | None = None) -> dict:
        params: dict = {"button": button}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return self.call("mouse_down", params)

    def mouse_up(self, button: str = "left",
                 x: int | None = None, y: int | None = None) -> dict:
        params: dict = {"button": button}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return self.call("mouse_up", params)

    def mouse_scroll(self, amount: int) -> dict:
        return self.call("mouse_scroll", {"amount": amount})

    def capture_frame(self, quality: int = 85) -> dict:
        """Capture a frame. Returns dict with jpeg_b64, width, height."""
        return self.call("capture_frame", {"quality": quality})

    def capture_frame_jpeg(self, quality: int = 85) -> tuple[bytes, int, int]:
        """Capture a frame and return decoded JPEG bytes with dimensions."""
        result = self.capture_frame(quality)
        jpeg_bytes = base64.b64decode(result["jpeg_b64"])
        return jpeg_bytes, result["width"], result["height"]

    def get_device_info(self) -> dict:
        return self.call("get_device_info")

    def list_capture_devices(self) -> dict:
        return self.call("list_capture_devices")

    def set_capture_device(self, device: str) -> dict:
        return self.call("set_capture_device", {"device": device})

    def set_capture_resolution(self, width: int, height: int) -> dict:
        return self.call("set_capture_resolution", {"width": width, "height": height})
