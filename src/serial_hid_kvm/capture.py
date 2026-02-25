"""HDMI capture device control using OpenCV."""

import json
import logging
import platform
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


import re

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


def list_capture_devices() -> list[dict]:
    """List all available video capture devices with OS-level names.

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
                devices.append({"device": device_path, "name": name, "vidpid": vidpid})

    elif system == "Windows":
        # PnP enumeration order matches DirectShow index order
        pnp_devices = _get_windows_video_device_names()
        for idx, pnp in enumerate(pnp_devices):
            devices.append({
                "device": str(idx),
                "name": pnp["name"],
                "vidpid": pnp.get("vidpid", ""),
            })

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
                 width: int | None = None, height: int | None = None):
        self._device = device
        self._cap: cv2.VideoCapture | None = None
        self._req_width = width
        self._req_height = height

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
            self._cap = cv2.VideoCapture(device, cv2.CAP_DSHOW)
        else:
            self._cap = cv2.VideoCapture(device)

        if not self._cap.isOpened():
            raise RuntimeError(f"Failed to open capture device: {device}")

        if self._req_width is not None and self._req_height is not None:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._req_width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._req_height)

        actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info(f"Opened capture device: {device} ({actual_w}x{actual_h})")

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

            self._latest_frame = frame.copy()
            time.sleep(0.016)  # ~60fps cap

    def get_latest_frame(self) -> np.ndarray | None:
        """Return the latest captured BGR frame, or None if not available."""
        frame = self._latest_frame
        if frame is not None:
            return frame.copy()
        return None

    def get_frame_jpeg(self, quality: int = 85) -> tuple[bytes, int, int] | None:
        """Return the latest frame as JPEG bytes with dimensions.

        Returns:
            (jpeg_bytes, width, height) or None if no frame available.
        """
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
        return {
            "device": str(self._device),
            "width": int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": self._cap.get(cv2.CAP_PROP_FPS),
            "backend": self._cap.getBackendName(),
            "preview": self._running,
        }
