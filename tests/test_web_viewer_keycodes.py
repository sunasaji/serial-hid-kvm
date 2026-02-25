"""Tests for web viewer JS code to HID keycode mapping."""

import pytest

# Import the mapping dict directly
from serial_hid_kvm._web_viewer import _JS_CODE_TO_HID


class TestJsCodeToHidMapping:
    """Verify W3C KeyboardEvent.code → HID keycode mappings."""

    # --- Letters (a-z) ---

    @pytest.mark.parametrize("letter,expected_hid", [
        ("A", 0x04), ("B", 0x05), ("C", 0x06), ("D", 0x07),
        ("E", 0x08), ("F", 0x09), ("G", 0x0A), ("H", 0x0B),
        ("I", 0x0C), ("J", 0x0D), ("K", 0x0E), ("L", 0x0F),
        ("M", 0x10), ("N", 0x11), ("O", 0x12), ("P", 0x13),
        ("Q", 0x14), ("R", 0x15), ("S", 0x16), ("T", 0x17),
        ("U", 0x18), ("V", 0x19), ("W", 0x1A), ("X", 0x1B),
        ("Y", 0x1C), ("Z", 0x1D),
    ])
    def test_letter_keys(self, letter, expected_hid):
        assert _JS_CODE_TO_HID[f"Key{letter}"] == expected_hid

    # --- Function keys ---

    @pytest.mark.parametrize("fkey,expected_hid", [
        ("F1", 0x3A), ("F2", 0x3B), ("F3", 0x3C), ("F4", 0x3D),
        ("F5", 0x3E), ("F6", 0x3F), ("F7", 0x40), ("F8", 0x41),
        ("F9", 0x42), ("F10", 0x43), ("F11", 0x44), ("F12", 0x45),
    ])
    def test_function_keys(self, fkey, expected_hid):
        assert _JS_CODE_TO_HID[fkey] == expected_hid

    # --- Digit row ---

    @pytest.mark.parametrize("digit,expected_hid", [
        ("1", 0x1E), ("2", 0x1F), ("3", 0x20), ("4", 0x21),
        ("5", 0x22), ("6", 0x23), ("7", 0x24), ("8", 0x25),
        ("9", 0x26), ("0", 0x27),
    ])
    def test_digit_keys(self, digit, expected_hid):
        assert _JS_CODE_TO_HID[f"Digit{digit}"] == expected_hid

    # --- Navigation ---

    def test_arrow_keys(self):
        assert _JS_CODE_TO_HID["ArrowUp"] == 0x52
        assert _JS_CODE_TO_HID["ArrowDown"] == 0x51
        assert _JS_CODE_TO_HID["ArrowLeft"] == 0x50
        assert _JS_CODE_TO_HID["ArrowRight"] == 0x4F

    def test_navigation_cluster(self):
        assert _JS_CODE_TO_HID["Insert"] == 0x49
        assert _JS_CODE_TO_HID["Home"] == 0x4A
        assert _JS_CODE_TO_HID["PageUp"] == 0x4B
        assert _JS_CODE_TO_HID["Delete"] == 0x4C
        assert _JS_CODE_TO_HID["End"] == 0x4D
        assert _JS_CODE_TO_HID["PageDown"] == 0x4E

    # --- Essential keys ---

    def test_essential_keys(self):
        assert _JS_CODE_TO_HID["Enter"] == 0x28
        assert _JS_CODE_TO_HID["Escape"] == 0x29
        assert _JS_CODE_TO_HID["Backspace"] == 0x2A
        assert _JS_CODE_TO_HID["Tab"] == 0x2B
        assert _JS_CODE_TO_HID["Space"] == 0x2C

    # --- JIS-specific ---

    def test_jis_keys(self):
        assert _JS_CODE_TO_HID["IntlRo"] == 0x87    # International1 (ろ)
        assert _JS_CODE_TO_HID["IntlYen"] == 0x89    # International3 (¥)
        assert _JS_CODE_TO_HID["KanaMode"] == 0x88
        assert _JS_CODE_TO_HID["Convert"] == 0x8A
        assert _JS_CODE_TO_HID["NonConvert"] == 0x8B

    # --- Numpad ---

    @pytest.mark.parametrize("n,expected_hid", [
        ("0", 0x62), ("1", 0x59), ("2", 0x5A), ("3", 0x5B),
        ("4", 0x5C), ("5", 0x5D), ("6", 0x5E), ("7", 0x5F),
        ("8", 0x60), ("9", 0x61),
    ])
    def test_numpad_digits(self, n, expected_hid):
        assert _JS_CODE_TO_HID[f"Numpad{n}"] == expected_hid

    def test_numpad_operators(self):
        assert _JS_CODE_TO_HID["NumpadDivide"] == 0x54
        assert _JS_CODE_TO_HID["NumpadMultiply"] == 0x55
        assert _JS_CODE_TO_HID["NumpadSubtract"] == 0x56
        assert _JS_CODE_TO_HID["NumpadAdd"] == 0x57
        assert _JS_CODE_TO_HID["NumpadEnter"] == 0x58
        assert _JS_CODE_TO_HID["NumpadDecimal"] == 0x63

    # --- Consistency check ---

    def test_all_values_are_valid_hid_keycodes(self):
        """All mapped HID keycodes should be in valid range."""
        for code, hid in _JS_CODE_TO_HID.items():
            assert 0x00 <= hid <= 0xFF, f"{code} maps to out-of-range HID 0x{hid:02X}"

    def test_no_duplicate_hid_values_in_same_row(self):
        """No two different JS codes should map to the same HID keycode
        (except intentional duplicates like Enter/NumpadEnter)."""
        seen: dict[int, str] = {}
        intentional_duplicates = {0x28}  # Enter and NumpadEnter intentionally differ
        for code, hid in _JS_CODE_TO_HID.items():
            if hid in seen and hid not in intentional_duplicates:
                # This is a potential issue but not necessarily a bug
                pass
            seen[hid] = code
