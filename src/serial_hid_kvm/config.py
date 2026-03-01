"""Configuration with priority: CLI args > environment variables > config file > defaults.

Environment variables use the ``SHKVM_`` prefix.  A YAML config file is
searched in these locations (first match wins):

1. ``--config FILE`` CLI argument
2. ``SHKVM_CONFIG`` environment variable
3. ``./serial-hid-kvm.yaml`` (current directory)
4. ``~/.config/serial-hid-kvm/config.yaml`` (Linux)
   ``%APPDATA%\\serial-hid-kvm\\config.yaml`` (Windows)
"""

import logging
import os
import platform
from pathlib import Path

logger = logging.getLogger(__name__)


class Config:
    """All configuration for serial-hid-kvm."""

    def __init__(self):
        # Serial
        self.serial_port: str | None = None
        self.serial_baud: int = 9600

        # Target screen (for mouse coordinate mapping)
        self.screen_width: int = 1920
        self.screen_height: int = 1080

        # Capture device
        self.capture_device: str | None = None
        self.capture_width: int = 1920
        self.capture_height: int = 1080
        self.capture_fourcc: str = "MJPG"

        # Keyboard layout
        self.target_layout: str = "us104"
        self.host_layout: str = "auto"
        self.layouts_dir: str | None = None

        # API server (JSON Lines over TCP socket)
        self.api_enabled: bool = False
        self.api_host: str = "127.0.0.1"
        self.api_port: int = 9329

        # Web viewer
        self.web_enabled: bool = False
        self.web_host: str = "127.0.0.1"
        self.web_port: int = 9330
        self.web_fps: int = 20
        self.web_quality: int = 85

        # Audio (web viewer only; None = disabled)
        self.audio_device: str | None = None

        # Runtime options
        self.headless: bool = False
        self.debug_keys: bool = False
        self.show_cursor: bool = False


# ---------------------------------------------------------------------------
# Config file loading (YAML)
# ---------------------------------------------------------------------------

_FILE_KEYS = {
    "serial_port", "serial_baud",
    "screen_width", "screen_height",
    "capture_device", "capture_width", "capture_height", "capture_fourcc",
    "target_layout", "host_layout", "layouts_dir",
    "api_enabled", "api_host", "api_port",
    "web_enabled", "web_host", "web_port", "web_fps", "web_quality",
    "audio_device",
    "debug_keys", "headless", "show_cursor",
}


def _default_config_paths() -> list[Path]:
    """Return platform-specific default config file search paths."""
    paths = [Path("serial-hid-kvm.yaml")]
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "serial-hid-kvm" / "config.yaml")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        paths.append(Path(xdg) / "serial-hid-kvm" / "config.yaml")
    return paths


def _apply_file(config: Config, path: Path):
    """Apply config file values to a Config instance."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return
    except Exception as e:
        logger.warning(f"Failed to load config file {path}: {e}")
        return

    for key in _FILE_KEYS:
        if key in data and data[key] is not None:
            value = data[key]
            current = getattr(config, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, str) or current is None:
                value = str(value) if value is not None else None
            setattr(config, key, value)

    logger.info(f"Loaded config from {path}")


# ---------------------------------------------------------------------------
# Environment variable loading
# ---------------------------------------------------------------------------

_ENV_MAP = {
    "SHKVM_SERIAL_PORT":   "serial_port",
    "SHKVM_SERIAL_BAUD":   "serial_baud",
    "SHKVM_SCREEN_WIDTH":  "screen_width",
    "SHKVM_SCREEN_HEIGHT": "screen_height",
    "SHKVM_CAPTURE_DEVICE": "capture_device",
    "SHKVM_CAPTURE_WIDTH": "capture_width",
    "SHKVM_CAPTURE_HEIGHT": "capture_height",
    "SHKVM_CAPTURE_FOURCC": "capture_fourcc",
    "SHKVM_TARGET_LAYOUT": "target_layout",
    "SHKVM_HOST_LAYOUT":   "host_layout",
    "SHKVM_LAYOUTS_DIR":   "layouts_dir",
    "SHKVM_API":           "api_enabled",
    "SHKVM_API_HOST":      "api_host",
    "SHKVM_API_PORT":      "api_port",
    "SHKVM_AUDIO_DEVICE":  "audio_device",
    "SHKVM_WEB":           "web_enabled",
    "SHKVM_WEB_HOST":      "web_host",
    "SHKVM_WEB_PORT":      "web_port",
    "SHKVM_WEB_FPS":       "web_fps",
    "SHKVM_WEB_QUALITY":   "web_quality",
    "SHKVM_DEBUG_KEYS":    "debug_keys",
    "SHKVM_SHOW_CURSOR":   "show_cursor",
}

_BOOL_FALSE = {"0", "false", "no", ""}


def _apply_env(config: Config):
    """Apply environment variables to a Config instance."""
    for env_key, attr in _ENV_MAP.items():
        value = os.environ.get(env_key)
        if value is None:
            continue
        current = getattr(config, attr)
        if isinstance(current, bool):
            setattr(config, attr, value.lower() not in _BOOL_FALSE)
        elif isinstance(current, int):
            setattr(config, attr, int(value))
        elif isinstance(current, str) or current is None:
            setattr(config, attr, value)


# ---------------------------------------------------------------------------
# CLI argument application
# ---------------------------------------------------------------------------

def _apply_args(config: Config, args):
    """Apply parsed argparse Namespace to a Config instance.

    Only overrides values that were explicitly provided (not None).
    """
    arg_to_attr = {
        "serial_port":    "serial_port",
        "serial_baud":    "serial_baud",
        "screen_width":   "screen_width",
        "screen_height":  "screen_height",
        "capture_device": "capture_device",
        "capture_width":  "capture_width",
        "capture_height": "capture_height",
        "capture_fourcc": "capture_fourcc",
        "target_layout":  "target_layout",
        "host_layout":    "host_layout",
        "layouts_dir":    "layouts_dir",
        "api_host":       "api_host",
        "api_port":       "api_port",
        "audio_device":   "audio_device",
        "web_host":       "web_host",
        "web_port":       "web_port",
        "web_fps":        "web_fps",
        "web_quality":    "web_quality",
    }
    for arg_name, attr in arg_to_attr.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            setattr(config, attr, value)

    # Boolean flags (store_true: only set when True)
    if getattr(args, "headless", False):
        config.headless = True
    if getattr(args, "api", False):
        config.api_enabled = True
    if getattr(args, "web", False):
        config.web_enabled = True
    if getattr(args, "debug_keys", False):
        config.debug_keys = True
    if getattr(args, "show_cursor", False):
        config.show_cursor = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(args=None) -> Config:
    """Load configuration with priority: CLI > env > config file > defaults.

    Args:
        args: Parsed argparse Namespace (optional).

    Returns:
        Fully resolved Config instance.
    """
    config = Config()

    # 1. Find and load config file
    config_path = None

    # CLI --config flag (highest priority for file location)
    if args and getattr(args, "config", None):
        config_path = Path(args.config)
        if not config_path.exists():
            logger.warning(f"Config file not found: {config_path}")
            config_path = None
    # SHKVM_CONFIG env var
    elif os.environ.get("SHKVM_CONFIG"):
        config_path = Path(os.environ["SHKVM_CONFIG"])
        if not config_path.exists():
            logger.warning(f"Config file not found (SHKVM_CONFIG): {config_path}")
            config_path = None
    # Default search paths
    else:
        for candidate in _default_config_paths():
            if candidate.exists():
                config_path = candidate
                break

    if config_path:
        _apply_file(config, config_path)

    # 2. Override with environment variables
    _apply_env(config)

    # 3. Override with CLI args
    if args:
        _apply_args(config, args)

    return config
