"""HID keycode mappings for US keyboard layout."""

# Modifier key bitmasks
MOD_NONE = 0x00
MOD_LCTRL = 0x01
MOD_LSHIFT = 0x02
MOD_LALT = 0x04
MOD_LWIN = 0x08
MOD_RCTRL = 0x10
MOD_RSHIFT = 0x20
MOD_RALT = 0x40
MOD_RWIN = 0x80

# Modifier name -> bitmask
MODIFIER_MAP: dict[str, int] = {
    "ctrl": MOD_LCTRL,
    "lctrl": MOD_LCTRL,
    "rctrl": MOD_RCTRL,
    "shift": MOD_LSHIFT,
    "lshift": MOD_LSHIFT,
    "rshift": MOD_RSHIFT,
    "alt": MOD_LALT,
    "lalt": MOD_LALT,
    "ralt": MOD_RALT,
    "win": MOD_LWIN,
    "lwin": MOD_LWIN,
    "rwin": MOD_RWIN,
    "gui": MOD_LWIN,
    "super": MOD_LWIN,
    "meta": MOD_LWIN,
}

# Special key name -> HID keycode
SPECIAL_KEY_MAP: dict[str, int] = {
    "enter": 0x28,
    "return": 0x28,
    "escape": 0x29,
    "esc": 0x29,
    "backspace": 0x2A,
    "tab": 0x2B,
    "space": 0x2C,
    "capslock": 0x39,
    "f1": 0x3A,
    "f2": 0x3B,
    "f3": 0x3C,
    "f4": 0x3D,
    "f5": 0x3E,
    "f6": 0x3F,
    "f7": 0x40,
    "f8": 0x41,
    "f9": 0x42,
    "f10": 0x43,
    "f11": 0x44,
    "f12": 0x45,
    "printscreen": 0x46,
    "scrolllock": 0x47,
    "pause": 0x48,
    "insert": 0x49,
    "home": 0x4A,
    "pageup": 0x4B,
    "delete": 0x4C,
    "end": 0x4D,
    "pagedown": 0x4E,
    "right": 0x4F,
    "left": 0x50,
    "down": 0x51,
    "up": 0x52,
    "numlock": 0x53,
}

# Character -> (modifier, keycode) mapping for US layout
# Lowercase letters: a=0x04, b=0x05, ... z=0x1D
# Numbers: 1=0x1E, 2=0x1F, ... 9=0x26, 0=0x27
_CHAR_MAP: dict[str, tuple[int, int]] = {}

# a-z (no modifier)
for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _CHAR_MAP[c] = (MOD_NONE, 0x04 + i)

# A-Z (shift)
for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    _CHAR_MAP[c] = (MOD_LSHIFT, 0x04 + i)

# 1-9, 0
for i, c in enumerate("123456789"):
    _CHAR_MAP[c] = (MOD_NONE, 0x1E + i)
_CHAR_MAP["0"] = (MOD_NONE, 0x27)

# Symbols on number row (shifted)
_SHIFT_NUMBER_SYMBOLS = {
    "!": 0x1E,  # Shift+1
    "@": 0x1F,  # Shift+2
    "#": 0x20,  # Shift+3
    "$": 0x21,  # Shift+4
    "%": 0x22,  # Shift+5
    "^": 0x23,  # Shift+6
    "&": 0x24,  # Shift+7
    "*": 0x25,  # Shift+8
    "(": 0x26,  # Shift+9
    ")": 0x27,  # Shift+0
}
for char, keycode in _SHIFT_NUMBER_SYMBOLS.items():
    _CHAR_MAP[char] = (MOD_LSHIFT, keycode)

# Other printable characters (unshifted)
_OTHER_CHARS = {
    " ": (MOD_NONE, 0x2C),
    "-": (MOD_NONE, 0x2D),
    "=": (MOD_NONE, 0x2E),
    "[": (MOD_NONE, 0x2F),
    "]": (MOD_NONE, 0x30),
    "\\": (MOD_NONE, 0x31),
    ";": (MOD_NONE, 0x33),
    "'": (MOD_NONE, 0x34),
    "`": (MOD_NONE, 0x35),
    ",": (MOD_NONE, 0x36),
    ".": (MOD_NONE, 0x37),
    "/": (MOD_NONE, 0x38),
}
_CHAR_MAP.update(_OTHER_CHARS)

# Shifted symbols
_SHIFTED_CHARS = {
    "_": (MOD_LSHIFT, 0x2D),
    "+": (MOD_LSHIFT, 0x2E),
    "{": (MOD_LSHIFT, 0x2F),
    "}": (MOD_LSHIFT, 0x30),
    "|": (MOD_LSHIFT, 0x31),
    ":": (MOD_LSHIFT, 0x33),
    '"': (MOD_LSHIFT, 0x34),
    "~": (MOD_LSHIFT, 0x35),
    "<": (MOD_LSHIFT, 0x36),
    ">": (MOD_LSHIFT, 0x37),
    "?": (MOD_LSHIFT, 0x38),
}
_CHAR_MAP.update(_SHIFTED_CHARS)

# Tab and Enter as characters
_CHAR_MAP["\t"] = (MOD_NONE, 0x2B)
_CHAR_MAP["\n"] = (MOD_NONE, 0x28)


# Active character map (starts as US104, updated by set_layout)
_active_char_map: dict[str, tuple[int, int]] = _CHAR_MAP
_active_layout: str = "us104"


def set_layout(layout_name: str, layouts_dir: str | None = None) -> str:
    """Apply a keyboard layout's overrides to the character map.

    Call this once at startup based on SHKVM_TARGET_LAYOUT / --layout.
    Returns the source description string (e.g. "built-in (jp106.yaml)").
    """
    global _active_char_map, _active_layout
    from .hid_layouts import get_overrides

    overrides, source = get_overrides(layout_name, layouts_dir=layouts_dir)
    merged = dict(_CHAR_MAP)  # fresh copy of base US104
    merged.update(overrides)
    _active_char_map = merged
    _active_layout = layout_name
    return source


def get_layout() -> str:
    """Return the name of the currently active layout."""
    return _active_layout


def build_char_map(layout_name: str, layouts_dir: str | None = None) -> dict[str, tuple[int, int]]:
    """Build a character map for a layout without modifying global state.

    Returns a dict mapping characters to (modifier, keycode) tuples,
    starting from the base US104 map and applying the layout's overrides.
    """
    from .hid_layouts import get_overrides

    overrides, _source = get_overrides(layout_name, layouts_dir=layouts_dir)
    merged = dict(_CHAR_MAP)
    merged.update(overrides)
    return merged


def char_to_hid(char: str) -> tuple[int, int] | None:
    """Convert a character to (modifier, keycode) tuple.

    Uses the active layout set by set_layout(). Defaults to US104.
    Returns None if character has no mapping.
    """
    return _active_char_map.get(char)


def special_key_to_hid(key_name: str) -> int | None:
    """Convert a special key name or hex keycode string to HID keycode.

    Accepts:
        - Named keys: "enter", "tab", "f1", etc.
        - Hex keycode strings: "0x87", "0x2C", etc. (values 0x00-0xFF)

    Returns None if key name is unknown and not a valid hex keycode.
    """
    result = SPECIAL_KEY_MAP.get(key_name.lower())
    if result is not None:
        return result

    # Try parsing as hex keycode (e.g., "0x87", "0x2C")
    lower = key_name.lower()
    if lower.startswith("0x"):
        try:
            value = int(lower, 16)
            if 0x00 <= value <= 0xFF:
                return value
        except ValueError:
            pass

    return None


def modifier_name_to_bit(name: str) -> int | None:
    """Convert a modifier name to its bitmask.

    Returns None if name is unknown.
    """
    return MODIFIER_MAP.get(name.lower())


# Characters that can potentially be typed via HID (any layout).
# ASCII printable (0x20-0x7E) plus tab and line endings.
_TYPABLE_CHARS = set(chr(c) for c in range(0x20, 0x7F)) | {"\t", "\n", "\r"}


def validate_chars(text: str) -> None:
    """Raise ValueError if text contains characters that cannot be typed via HID.

    This is a pre-flight check usable on the client side before sending to the
    KVM server.  It validates against the set of characters that any keyboard
    layout can produce (ASCII printable + tab + line endings).  The server
    performs its own check against the active layout, which may be stricter or
    broader, but this catches obviously unsupported characters (Unicode,
    control chars) early.
    """
    for ch in text:
        if ch not in _TYPABLE_CHARS:
            raise ValueError(
                f"Unsupported character: {ch!r} (U+{ord(ch):04X}). "
                "Only ASCII printable characters are supported. "
                "For unsupported characters or binary data, use base64 encoding."
            )
