"""Keyboard control via CH9329 HID emulator."""

import atexit
import logging
import time

from .hid_keycodes import char_to_hid, special_key_to_hid, modifier_name_to_bit
from .hid_protocol import CH9329

logger = logging.getLogger(__name__)


class Keyboard:
    """Keyboard controller using CH9329."""

    def __init__(self, ch9329: CH9329, char_delay: float = 0.02):
        self._dev = ch9329
        self._char_delay = char_delay
        atexit.register(self._release_all)

    def _release_all(self):
        try:
            if self._dev.is_open:
                self._dev.release_all()
        except Exception:
            pass

    def type_text(self, text: str, char_delay: float | None = None,
                  raw: bool = False):
        """Type a string with optional inline key tags.

        Plain characters are sent as HID key presses. Special keys and raw
        HID keycodes can be embedded using ``{tag}`` syntax:

        - Named keys: ``{enter}``, ``{tab}``, ``{f1}``, ``{escape}``, ...
        - Hex keycodes: ``{0x87}``, ``{0x89}``, ...
        - With modifiers: ``{shift+0x87}``, ``{ctrl+alt+delete}``, ...
        - Literal brace: ``{{`` produces ``{``, ``}}`` produces ``}``

        **Whitelist-based tag parsing:** Only recognized special key names
        inside {braces} are interpreted as tags. Unknown ``{content}``
        (e.g. ``{print $1}``) is passed through as literal text including
        the braces. This means code with curly braces (awk, Python, shell)
        can be sent without escaping in most cases.

        **Raw mode (raw=True):** Disables all tag interpretation. Newline
        characters (``\\n``) in the string are sent as Enter key presses.
        Use literal ``\\n`` (escaped as ``\\\\n`` in JSON) to type a
        backslash + n.

        Examples::

            type_text("ls -la{enter}")
            type_text("path{0x87}file")          # 0x87 = international1
            type_text("{ctrl+c}")
            type_text("hello{{world}}")           # types hello{world}
            type_text("awk '{print $1}'{enter}")  # {print $1} passes through literally
            type_text("echo hello\\necho world\\n", raw=True)  # raw mode

        Args:
            text: Text to type, with optional ``{tag}`` sequences.
            char_delay: Delay between keystrokes (seconds). Uses default if None.
            raw: If True, disable all tag interpretation. Newlines become Enter.
        """
        delay = char_delay if char_delay is not None else self._char_delay

        if raw:
            segments = text.split('\n')
            for idx, segment in enumerate(segments):
                for ch in segment:
                    mapping = char_to_hid(ch)
                    if mapping is None:
                        logger.warning(f"No HID mapping for character: {ch!r}, skipping")
                        continue
                    modifier, keycode = mapping
                    self._dev.send_keyboard(modifier, keycode)
                    if delay > 0:
                        time.sleep(delay)
                if idx < len(segments) - 1:
                    self._send_tag("enter", delay)
            return

        for token in self._tokenize(text):
            if token.startswith("\x01"):
                # Tag token — strip sentinel and dispatch
                self._send_tag(token[1:], delay)
            else:
                # Plain character
                mapping = char_to_hid(token)
                if mapping is None:
                    logger.warning(f"No HID mapping for character: {token!r}, skipping")
                    continue
                modifier, keycode = mapping
                self._dev.send_keyboard(modifier, keycode)
                if delay > 0:
                    time.sleep(delay)

    @staticmethod
    def _is_valid_tag(tag: str) -> bool:
        """Check if a tag string represents a valid special key (with optional modifiers).

        Args:
            tag: Tag content, e.g. 'enter', 'ctrl+c', 'shift+0x87'

        Returns:
            True if tag is a recognized key combination
        """
        parts = [p.strip() for p in tag.split("+")]
        key_part = parts[-1]
        mod_parts = parts[:-1]

        # All prefixes must be valid modifiers
        for mod_name in mod_parts:
            if modifier_name_to_bit(mod_name) is None:
                return False

        # Key part must be a recognized special key, hex keycode, or single char
        if special_key_to_hid(key_part) is not None:
            return True
        if len(key_part) == 1:
            return True
        return False

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Parse text into a list of single-char strings and tag tokens.

        Tags are ``{name}`` sequences and are returned prefixed with ``\\x01``.
        Only recognized special key names are treated as tags; unknown
        ``{content}`` is passed through as literal braces + content.
        Escaped braces ``{{`` / ``}}`` become literal ``{`` / ``}``.
        """
        tokens: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "{":
                if i + 1 < n and text[i + 1] == "{":
                    # Escaped {{ -> literal {
                    tokens.append("{")
                    i += 2
                    continue
                # Find closing brace
                end = text.find("}", i + 1)
                if end == -1:
                    # No closing brace — treat as literal
                    tokens.append(ch)
                    i += 1
                else:
                    tag_content = text[i + 1:end]
                    if tag_content and Keyboard._is_valid_tag(tag_content):
                        tokens.append("\x01" + tag_content)
                        i = end + 1
                    else:
                        # Not a recognized tag — emit literal { and continue
                        tokens.append("{")
                        i += 1
            elif ch == "}":
                if i + 1 < n and text[i + 1] == "}":
                    # Escaped }} -> literal }
                    tokens.append("}")
                    i += 2
                else:
                    tokens.append(ch)
                    i += 1
            else:
                tokens.append(ch)
                i += 1
        return tokens

    def _send_tag(self, tag: str, delay: float):
        """Parse and send a single {tag} expression.

        Supports modifier prefixes separated by ``+``:
        ``ctrl+alt+delete``, ``shift+0x87``, etc.
        """
        parts = [p.strip() for p in tag.split("+")]
        key_part = parts[-1]
        mod_parts = parts[:-1]

        # Build modifier bitmask
        mod_bits = 0
        for mod_name in mod_parts:
            bit = modifier_name_to_bit(mod_name)
            if bit is None:
                raise ValueError(f"Unknown modifier in tag {{{tag}}}: {mod_name}")
            mod_bits |= bit

        # Resolve keycode
        keycode = special_key_to_hid(key_part)
        if keycode is not None:
            self._dev.send_keyboard(mod_bits, keycode)
        elif len(key_part) == 1:
            mapping = char_to_hid(key_part)
            if mapping is not None:
                char_mod, kc = mapping
                self._dev.send_keyboard(mod_bits | char_mod, kc)
            else:
                raise ValueError(f"Unknown key in tag {{{tag}}}: {key_part}")
        else:
            raise ValueError(f"Unknown key in tag {{{tag}}}: {key_part}")

        if delay > 0:
            time.sleep(delay)

    def send_key(self, key: str, modifiers: list[str] | None = None):
        """Send a single key press with optional modifiers.

        Args:
            key: Key name (e.g., 'a', 'enter', 'f1') or single character
            modifiers: List of modifier names (e.g., ['ctrl', 'shift'])
        """
        # Build modifier bitmask
        mod_bits = 0
        if modifiers:
            for mod_name in modifiers:
                bit = modifier_name_to_bit(mod_name)
                if bit is None:
                    raise ValueError(f"Unknown modifier: {mod_name}")
                mod_bits |= bit

        # Try as special key first
        keycode = special_key_to_hid(key)
        if keycode is not None:
            self._dev.send_keyboard(mod_bits, keycode)
            return

        # Try as single character
        if len(key) == 1:
            mapping = char_to_hid(key)
            if mapping is not None:
                char_mod, keycode = mapping
                self._dev.send_keyboard(mod_bits | char_mod, keycode)
                return

        raise ValueError(f"Unknown key: {key}")

    def send_key_sequence(self, steps: list[dict], default_delay_ms: int = 100):
        """Send a sequence of key steps with delays.

        Args:
            steps: List of step dicts with keys: key, modifiers (optional), delay_ms (optional)
            default_delay_ms: Default delay between steps in milliseconds
        """
        for step in steps:
            key = step["key"]
            modifiers = step.get("modifiers", [])
            delay_ms = step.get("delay_ms", default_delay_ms)
            self.send_key(key, modifiers)
            time.sleep(delay_ms / 1000.0)
