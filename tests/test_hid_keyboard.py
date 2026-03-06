"""Tests for Keyboard._tokenize() and tag parsing."""

import pytest

from serial_hid_kvm.hid_keyboard import Keyboard


class TestTokenize:
    """Tests for the _tokenize static method."""

    def test_plain_text(self):
        assert Keyboard._tokenize("hello") == list("hello")

    def test_single_tag(self):
        tokens = Keyboard._tokenize("{enter}")
        assert tokens == ["\x01enter"]

    def test_tag_with_modifiers(self):
        tokens = Keyboard._tokenize("{ctrl+c}")
        assert tokens == ["\x01ctrl+c"]

    def test_text_with_tag(self):
        tokens = Keyboard._tokenize("ls -la{enter}")
        expected = list("ls -la") + ["\x01enter"]
        assert tokens == expected

    def test_escaped_open_brace(self):
        tokens = Keyboard._tokenize("{{")
        assert tokens == ["{"]

    def test_escaped_close_brace(self):
        tokens = Keyboard._tokenize("}}")
        assert tokens == ["}"]

    def test_escaped_braces_in_text(self):
        tokens = Keyboard._tokenize("hello{{world}}")
        assert tokens == list("hello") + ["{"] + list("world") + ["}"]

    def test_hex_keycode_tag(self):
        tokens = Keyboard._tokenize("{0x87}")
        assert tokens == ["\x010x87"]

    def test_modifier_hex_tag(self):
        tokens = Keyboard._tokenize("{shift+0x87}")
        assert tokens == ["\x01shift+0x87"]

    def test_multiple_tags(self):
        tokens = Keyboard._tokenize("{ctrl+a}{ctrl+c}")
        assert tokens == ["\x01ctrl+a", "\x01ctrl+c"]

    def test_unclosed_brace(self):
        """Unclosed brace is treated as literal."""
        tokens = Keyboard._tokenize("{unclosed")
        assert tokens == list("{unclosed")

    def test_empty_string(self):
        assert Keyboard._tokenize("") == []

    def test_complex_mixed(self):
        tokens = Keyboard._tokenize("path{0x87}file{enter}")
        expected = list("path") + ["\x010x87"] + list("file") + ["\x01enter"]
        assert tokens == expected

    def test_unknown_tag_passes_through(self):
        """Unknown {content} is treated as literal text (whitelist-based)."""
        tokens = Keyboard._tokenize("{print $1}")
        assert tokens == list("{print $1}")

    def test_awk_command(self):
        """awk code with braces passes through literally, {enter} is a tag."""
        tokens = Keyboard._tokenize("awk '{print $1}' file.txt{enter}")
        expected = list("awk '{print $1}' file.txt") + ["\x01enter"]
        assert tokens == expected

    def test_python_dict_literal(self):
        """Python-style dict braces pass through literally."""
        tokens = Keyboard._tokenize('{"key": "value"}')
        assert tokens == list('{"key": "value"}')

    def test_escaped_recognized_tag(self):
        """Escaped braces around a recognized tag produce literal text."""
        tokens = Keyboard._tokenize("{{enter}}")
        assert tokens == ["{"] + list("enter") + ["}"]


class TestIsValidTag:
    """Tests for _is_valid_tag."""

    def test_recognized_special_keys(self):
        assert Keyboard._is_valid_tag("enter") is True
        assert Keyboard._is_valid_tag("tab") is True
        assert Keyboard._is_valid_tag("escape") is True
        assert Keyboard._is_valid_tag("f1") is True
        assert Keyboard._is_valid_tag("backspace") is True

    def test_hex_keycodes(self):
        assert Keyboard._is_valid_tag("0x87") is True
        assert Keyboard._is_valid_tag("0x2C") is True

    def test_modifier_combos(self):
        assert Keyboard._is_valid_tag("ctrl+c") is True
        assert Keyboard._is_valid_tag("shift+0x87") is True
        assert Keyboard._is_valid_tag("ctrl+alt+delete") is True

    def test_unknown_content(self):
        assert Keyboard._is_valid_tag("print $1") is False
        assert Keyboard._is_valid_tag("unknown") is False
        assert Keyboard._is_valid_tag("key: value") is False

    def test_single_char_key(self):
        assert Keyboard._is_valid_tag("a") is True
        assert Keyboard._is_valid_tag("ctrl+a") is True
