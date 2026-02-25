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
