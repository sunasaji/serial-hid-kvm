"""HDMI capture device control using OpenCV."""

import json
import logging
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# Auto-crop constants
_AUTOCROP_THRESHOLD = 20      # pixel brightness threshold for "black"
_AUTOCROP_MIN_BORDER = 4      # minimum border pixels to trigger crop
_AUTOCROP_MIN_CONTENT = 64    # minimum content pixels in each dimension


def _detect_crop_rect(frame: np.ndarray) -> tuple[int, int, int, int] | None:
    """Detect black borders around the content area of a BGR frame.

    Returns (y1, y2, x1, x2) crop coordinates, or None if no significant
    borders are detected.
    """
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Max brightness per row/column
    row_max = np.max(gray, axis=1)  # shape (h,)
    col_max = np.max(gray, axis=0)  # shape (w,)

    # Find first/last non-black row
    active_rows = np.where(row_max > _AUTOCROP_THRESHOLD)[0]
    active_cols = np.where(col_max > _AUTOCROP_THRESHOLD)[0]

    if len(active_rows) < _AUTOCROP_MIN_CONTENT or len(active_cols) < _AUTOCROP_MIN_CONTENT:
        return None

    y1, y2 = int(active_rows[0]), int(active_rows[-1]) + 1
    x1, x2 = int(active_cols[0]), int(active_cols[-1]) + 1

    top = y1
    bottom = h - y2
    left = x1
    right = w - x2

    if (top < _AUTOCROP_MIN_BORDER and bottom < _AUTOCROP_MIN_BORDER  # noqa: W503
            and left < _AUTOCROP_MIN_BORDER and right < _AUTOCROP_MIN_BORDER):  # noqa: W503
        return None

    return (y1, y2, x1, x2)


def _fourcc_int_to_str(fourcc_int: int) -> str:
    """Convert an integer FOURCC code to its 4-character string representation.

    Returns hex notation if any byte is outside printable ASCII range
    (e.g. DirectShow may return media-type GUIDs instead of FOURCC).
    """
    fourcc_int = fourcc_int & 0xFFFFFFFF
    chars = []
    for i in range(4):
        b = (fourcc_int >> (8 * i)) & 0xFF
        if 0x20 <= b <= 0x7E:
            chars.append(chr(b))
        else:
            return f"0x{fourcc_int:08X}"
    return "".join(chars)


_VIDPID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})")


def _parse_vidpid(device_id: str) -> str:
    """Extract VID:PID from a PnP DeviceID string like USB\\VID_1A86&PID_7523\\..."""
    m = _VIDPID_RE.search(device_id)
    if m:
        return f"{m.group(1).upper()}:{m.group(2).upper()}"
    return ""


def _get_windows_video_device_names() -> list[dict]:
    """Query Windows PnP for video capture device names."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity"
             " | Where-Object { $_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image' }"
             " | Select-Object Name, DeviceID"
             " | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            data = [data]
        return [{
            "name": d["Name"],
            "device_id": d["DeviceID"],
            "vidpid": _parse_vidpid(d.get("DeviceID", "")),
        } for d in data]
    except Exception as e:
        logger.warning(f"Failed to query Windows PnP devices: {e}")
        return []


_WEBCAM_KEYWORDS = ["webcam", "camera", "ir camera", "front", "rear", "facetime"]


def _is_webcam_name(name: str) -> bool:
    """Check if a device name looks like a webcam rather than HDMI capture."""
    name_lower = name.lower()
    return any(kw in name_lower for kw in _WEBCAM_KEYWORDS)


def _enumerate_formats_linux(device_path: str) -> list[str]:
    """List supported pixel formats for a V4L2 device using v4l2-ctl."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--device", device_path, "--list-formats-ext"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        formats = []
        for line in result.stdout.splitlines():
            # Lines like "	[0]: 'MJPG' (Motion-JPEG, compressed)"
            m = re.match(r"\s*\[\d+]:\s*'(\w+)'", line)
            if m:
                formats.append(m.group(1))
        return formats
    except FileNotFoundError:
        logger.debug("v4l2-ctl not found, skipping format enumeration")
        return []
    except Exception as e:
        logger.debug(f"v4l2-ctl failed for {device_path}: {e}")
        return []


def _enumerate_formats_windows(device_index: int) -> list[str]:
    """Probe common FOURCC codes on a Windows device using OpenCV."""
    probe_fourccs = ["MJPG", "YUY2", "NV12", "H264"]
    supported = []
    try:
        cap = cv2.VideoCapture(device_index, cv2.CAP_MSMF)
        if not cap.isOpened():
            cap = cv2.VideoCapture(device_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return []
        for code in probe_fourccs:
            fourcc_int = cv2.VideoWriter.fourcc(*code)
            cap.set(cv2.CAP_PROP_FOURCC, fourcc_int)
            actual = int(cap.get(cv2.CAP_PROP_FOURCC))
            if _fourcc_int_to_str(actual) == code:
                supported.append(code)
        cap.release()
    except Exception as e:
        logger.debug(f"Format probe failed for device {device_index}: {e}")
    return supported


def list_capture_devices(enumerate_formats: bool = False) -> list[dict]:
    """List all available video capture devices with OS-level names.

    Args:
        enumerate_formats: If True, also list supported pixel formats per device.

    On Windows, uses PnP device names only (no OpenCV probe) so this works
    even when another process already has a device open.
    """
    devices = []
    system = platform.system()

    if system == "Linux":
        v4l_path = Path("/sys/class/video4linux")
        if v4l_path.exists():
            for dev_dir in sorted(v4l_path.iterdir()):
                name_file = dev_dir / "name"
                name = name_file.read_text().strip() if name_file.exists() else "Unknown"
                device_path = f"/dev/{dev_dir.name}"
                vidpid = ""
                device_link = dev_dir / "device"
                if device_link.exists():
                    try:
                        real = device_link.resolve()
                        for parent in [real] + list(real.parents):
                            vid_f = parent / "idVendor"
                            pid_f = parent / "idProduct"
                            if vid_f.exists() and pid_f.exists():
                                vid = vid_f.read_text().strip().upper()
                                pid = pid_f.read_text().strip().upper()
                                vidpid = f"{vid}:{pid}"
                                break
                    except Exception:
                        pass
                entry: dict = {"device": device_path, "name": name, "vidpid": vidpid}
                if enumerate_formats:
                    entry["formats"] = _enumerate_formats_linux(device_path)
                devices.append(entry)

    elif system == "Windows":
        # PnP enumeration order matches DirectShow index order
        pnp_devices = _get_windows_video_device_names()
        for idx, pnp in enumerate(pnp_devices):
            entry = {
                "device": str(idx),
                "name": pnp["name"],
                "vidpid": pnp.get("vidpid", ""),
            }
            if enumerate_formats:
                entry["formats"] = _enumerate_formats_windows(idx)
            devices.append(entry)

    return devices


def detect_capture_device() -> int | str:
    """Auto-detect an HDMI capture device by name, preferring non-webcam devices.

    Does not open any device, so it works even when devices are already in use.
    """
    devices = list_capture_devices()

    # First pass: pick the first non-webcam device
    for d in devices:
        if not _is_webcam_name(d["name"]):
            device = d["device"]
            if device.isdigit():
                device = int(device)
            logger.info(f"Auto-detected capture device: {device} ({d['name']})")
            return device

    # Fallback: pick the first device
    if devices:
        device = devices[0]["device"]
        if device.isdigit():
            device = int(device)
        logger.info(f"Falling back to first device: {device} ({devices[0]['name']})")
        return device

    raise RuntimeError(
        "No HDMI capture device found. "
        "Set SHKVM_CAPTURE_DEVICE / --capture-device to specify the device path or index."
    )


class ScreenCapture:
    """HDMI capture device controller with background capture thread."""

    def __init__(self, device: int | str | None = None, preview: bool = False,
                 width: int | None = None, height: int | None = None,
                 fourcc: str = "MJPG", autocrop: bool = True):
        self._device = device
        self._cap: cv2.VideoCapture | None = None
        self._req_width = width
        self._req_height = height
        self._req_fourcc = fourcc.upper()

        # MJPEG passthrough state
        self._mjpeg_passthrough = False
        self._latest_jpeg: bytes | None = None

        # Auto-crop state
        self._autocrop = autocrop
        self._crop_rect: tuple[int, int, int, int] | None = None
        self._crop_frame_counter: int = 0

        # Capture thread state
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest_frame: np.ndarray | None = None
        self._running = False

    def _open_device(self):
        """Open the capture device and apply requested resolution."""
        device = self._device
        if device is None:
            device = detect_capture_device()
            self._device = device

        if isinstance(device, str) and device.isdigit():
            device = int(device)

        if platform.system() == "Windows" and isinstance(device, int):
            # Prefer MSMF (Media Foundation) — handles FOURCC/MJPEG properly.
            # DirectShow's CAP_PROP_FOURCC set is broken in OpenCV.
            self._cap = cv2.VideoCapture(device, cv2.CAP_MSMF)
            if not self._cap.isOpened():
                logger.info("MSMF backend failed, falling back to DirectShow")
                self._cap = cv2.VideoCapture(device, cv2.CAP_DSHOW)
        else:
            self._cap = cv2.VideoCapture(device)

        if not self._cap.isOpened():
            raise RuntimeError(f"Failed to open capture device: {device}")

        # Set FOURCC, then resolution, then re-read FOURCC
        # (resolution change can reset FOURCC on some backends)
        req_fourcc_int = cv2.VideoWriter.fourcc(*self._req_fourcc)
        self._cap.set(cv2.CAP_PROP_FOURCC, req_fourcc_int)

        if self._req_width is not None and self._req_height is not None:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._req_width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._req_height)

        actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fourcc_int = int(self._cap.get(cv2.CAP_PROP_FOURCC))
        actual_fourcc = _fourcc_int_to_str(actual_fourcc_int)
        backend_name = self._cap.getBackendName()

        # MSMF returns Media Foundation subtype IDs (not FOURCC) via
        # CAP_PROP_FOURCC, so the readback is unreliable on that backend.
        if actual_fourcc == self._req_fourcc:
            logger.info(f"FOURCC: {actual_fourcc}")
        elif backend_name == "MSMF":
            logger.info(f"FOURCC: requested {self._req_fourcc} "
                        f"(MSMF reports {actual_fourcc} — normal)")
        else:
            logger.warning(f"FOURCC: requested {self._req_fourcc} but got "
                           f"{actual_fourcc} — backend may not support it")

        # Attempt MJPEG passthrough: set CONVERT_RGB=0 so OpenCV delivers
        # raw JPEG bytes instead of decoded BGR frames.
        # MSMF/DSHOW on Windows always decode internally and CONVERT_RGB=0
        # either breaks the stream (MSMF) or is ignored (DSHOW).
        # Passthrough works on V4L2 (Linux).
        self._mjpeg_passthrough = False
        if self._req_fourcc == "MJPG" and backend_name not in ("MSMF", "DSHOW"):
            self._cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
            ret, test_frame = self._cap.read()
            if ret and test_frame is not None and test_frame.ndim == 1:
                self._mjpeg_passthrough = True
                logger.info("MJPEG passthrough enabled (raw JPEG from device)")
            else:
                self._cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
                logger.info("MJPEG passthrough not available, using decode path")

        logger.info(
            f"Opened capture device: {device} ({actual_w}x{actual_h}, "
            f"fourcc={actual_fourcc})")

    def _ensure_open(self):
        """Open capture device if not already open."""
        if self._cap is not None and self._cap.isOpened():
            return
        self._open_device()

    def start_capture_thread(self):
        """Start the background capture thread.

        Use get_latest_frame() or get_frame_jpeg() to retrieve frames.
        """
        if self._running:
            return
        self._ensure_open()
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info("Capture thread started")

    def stop_capture_thread(self):
        """Stop the background capture thread."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _capture_loop(self):
        """Background thread: continuously capture frames."""
        while self._running:
            if self._cap is None or not self._cap.isOpened():
                time.sleep(0.1)
                continue

            with self._lock:
                ret, frame = self._cap.read()

            if not ret or frame is None:
                time.sleep(0.01)
                continue

            if self._mjpeg_passthrough and frame.ndim == 1:
                # Raw JPEG bytes from device — decode for crop detection and preview
                decoded = cv2.imdecode(frame, cv2.IMREAD_COLOR)
                if decoded is None:
                    time.sleep(0.016)
                    continue
                frame_bgr = decoded
            else:
                frame_bgr = frame

            # Auto-crop: detect black borders periodically
            if self._autocrop:
                self._crop_frame_counter += 1
                if self._crop_frame_counter >= 30:
                    self._crop_frame_counter = 0
                    new_rect = _detect_crop_rect(frame_bgr)
                    if new_rect != self._crop_rect:
                        if new_rect is not None:
                            y1, y2, x1, x2 = new_rect
                            logger.info(f"Auto-crop: {x2 - x1}x{y2 - y1} "
                                        f"(borders: T={y1} B={frame_bgr.shape[0] - y2} "
                                        f"L={x1} R={frame_bgr.shape[1] - x2})")
                        else:
                            logger.info("Auto-crop: disabled (no borders detected)")
                        self._crop_rect = new_rect

            # Apply crop
            if self._crop_rect is not None:
                y1, y2, x1, x2 = self._crop_rect
                frame_bgr = frame_bgr[y1:y2, x1:x2]
                # Invalidate passthrough JPEG — dimensions changed
                self._latest_jpeg = None
                self._latest_frame = frame_bgr.copy()
            elif self._mjpeg_passthrough and frame.ndim == 1:
                self._latest_jpeg = frame.tobytes()
                self._latest_frame = frame_bgr
            else:
                self._latest_frame = frame_bgr.copy()

            time.sleep(0.016)  # ~60fps cap

    def get_latest_frame(self) -> np.ndarray | None:
        """Return the latest captured BGR frame, or None if not available."""
        frame = self._latest_frame
        if frame is not None:
            return frame.copy()
        return None

    def get_frame_jpeg(self, quality: int = 85) -> tuple[bytes, int, int] | None:
        """Return the latest frame as JPEG bytes with dimensions.

        When MJPEG passthrough is active, returns the raw JPEG from the
        device without re-encoding (quality parameter is ignored).

        Returns:
            (jpeg_bytes, width, height) or None if no frame available.
        """
        if self._mjpeg_passthrough and self._latest_jpeg is not None:
            frame = self._latest_frame
            if frame is None:
                return None
            h, w = frame.shape[:2]
            return (self._latest_jpeg, w, h)

        frame = self._latest_frame
        if frame is None:
            return None
        h, w = frame.shape[:2]
        ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ret:
            return None
        return (buf.tobytes(), w, h)

    def capture(self) -> Image.Image:
        """Capture a single frame.

        If the capture thread is running, returns the latest frame.
        Otherwise reads directly from the device.
        """
        if self._running and self._latest_frame is not None:
            frame = self._latest_frame.copy()
        else:
            self._ensure_open()
            assert self._cap is not None
            with self._lock:
                ret, frame = self._cap.read()
            if not ret or frame is None:
                raise RuntimeError("Failed to capture frame from device")

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(frame_rgb)

    def close(self):
        """Release the capture device and stop capture thread."""
        self.stop_capture_thread()
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def switch_device(self, device: int | str):
        """Switch to a different capture device, restarting capture if active."""
        was_running = self._running
        self.close()
        self._device = device
        self._open_device()
        if was_running:
            self.start_capture_thread()

    def set_resolution(self, width: int, height: int):
        """Change the capture resolution, reopening the device."""
        self._req_width = width
        self._req_height = height
        was_running = self._running
        self.close()
        self._open_device()
        if was_running:
            self.start_capture_thread()

    def get_info(self) -> dict:
        """Get capture device information."""
        self._ensure_open()
        assert self._cap is not None
        fourcc_int = int(self._cap.get(cv2.CAP_PROP_FOURCC))
        fourcc_str = _fourcc_int_to_str(fourcc_int)
        backend = self._cap.getBackendName()
        # MSMF readback is unreliable; show the requested FOURCC instead
        if backend == "MSMF" and fourcc_str != self._req_fourcc:
            fourcc_str = self._req_fourcc
        info = {
            "device": str(self._device),
            "width": int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": self._cap.get(cv2.CAP_PROP_FPS),
            "backend": backend,
            "fourcc": fourcc_str,
            "mjpeg_passthrough": self._mjpeg_passthrough,
            "autocrop": self._autocrop,
            "preview": self._running,
        }
        if self._crop_rect is not None:
            y1, y2, x1, x2 = self._crop_rect
            info["crop_rect"] = {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                 "width": x2 - x1, "height": y2 - y1}
        return info
