"""Tests for HID keycode mappings and lookup functions."""

import pytest

from serial_hid_kvm.hid_keycodes import (
    char_to_hid,
    special_key_to_hid,
    modifier_name_to_bit,
    set_layout,
    build_char_map,
    MOD_NONE,
    MOD_LSHIFT,
    MOD_LCTRL,
    MOD_LALT,
    MOD_LWIN,
    MOD_RCTRL,
    MOD_RSHIFT,
    MOD_RALT,
)


class TestCharToHid:
    """Tests for char_to_hid (US layout by default)."""

    def test_lowercase_a(self):
        assert char_to_hid("a") == (MOD_NONE, 0x04)

    def test_lowercase_z(self):
        assert char_to_hid("z") == (MOD_NONE, 0x1D)

    def test_uppercase_A(self):
        assert char_to_hid("A") == (MOD_LSHIFT, 0x04)

    def test_digit_1(self):
        assert char_to_hid("1") == (MOD_NONE, 0x1E)

    def test_digit_0(self):
        assert char_to_hid("0") == (MOD_NONE, 0x27)

    def test_shifted_symbol_exclamation(self):
        assert char_to_hid("!") == (MOD_LSHIFT, 0x1E)

    def test_shifted_symbol_at(self):
        assert char_to_hid("@") == (MOD_LSHIFT, 0x1F)

    def test_space(self):
        assert char_to_hid(" ") == (MOD_NONE, 0x2C)

    def test_newline_maps_to_enter(self):
        assert char_to_hid("\n") == (MOD_NONE, 0x28)

    def test_tab_maps_to_tab(self):
        assert char_to_hid("\t") == (MOD_NONE, 0x2B)

    def test_unmapped_char(self):
        assert char_to_hid("\x00") is None


class TestSpecialKeyToHid:
    """Tests for special_key_to_hid."""

    def test_enter(self):
        assert special_key_to_hid("enter") == 0x28

    def test_enter_case_insensitive(self):
        assert special_key_to_hid("Enter") == 0x28

    def test_escape(self):
        assert special_key_to_hid("escape") == 0x29

    def test_esc_alias(self):
        assert special_key_to_hid("esc") == 0x29

    def test_return_alias(self):
        assert special_key_to_hid("return") == 0x28

    def test_f1(self):
        assert special_key_to_hid("f1") == 0x3A

    def test_f12(self):
        assert special_key_to_hid("f12") == 0x45

    def test_hex_keycode(self):
        assert special_key_to_hid("0x87") == 0x87

    def test_hex_keycode_uppercase(self):
        assert special_key_to_hid("0xFF") == 0xFF

    def test_unknown_key(self):
        assert special_key_to_hid("nonexistent") is None

    def test_invalid_hex(self):
        assert special_key_to_hid("0xZZ") is None


class TestModifierNameToBit:
    """Tests for modifier_name_to_bit."""

    def test_ctrl(self):
        assert modifier_name_to_bit("ctrl") == MOD_LCTRL

    def test_shift(self):
        assert modifier_name_to_bit("shift") == MOD_LSHIFT

    def test_alt(self):
        assert modifier_name_to_bit("alt") == MOD_LALT

    def test_win(self):
        assert modifier_name_to_bit("win") == MOD_LWIN

    def test_gui_alias(self):
        assert modifier_name_to_bit("gui") == MOD_LWIN

    def test_super_alias(self):
        assert modifier_name_to_bit("super") == MOD_LWIN

    def test_meta_alias(self):
        assert modifier_name_to_bit("meta") == MOD_LWIN

    def test_rctrl(self):
        assert modifier_name_to_bit("rctrl") == MOD_RCTRL

    def test_rshift(self):
        assert modifier_name_to_bit("rshift") == MOD_RSHIFT

    def test_ralt(self):
        assert modifier_name_to_bit("ralt") == MOD_RALT

    def test_unknown(self):
        assert modifier_name_to_bit("unknown") is None


class TestLayoutMapping:
    """Tests for layout loading and character mapping."""

    def test_jp106_at_sign_differs_from_us(self):
        """JP layout has @ on a different key than US."""
        jp_map = build_char_map("jp106")
        us_at = (MOD_LSHIFT, 0x1F)  # US: Shift+2
        jp_at = jp_map.get("@")
        assert jp_at is not None
        assert jp_at != us_at
        assert jp_at == (MOD_NONE, 0x2F)  # JP: unshifted [

    def test_jp106_double_quote_differs(self):
        """JP layout has " on Shift+2 instead of @."""
        jp_map = build_char_map("jp106")
        assert jp_map['"'] == (MOD_LSHIFT, 0x1F)

    def test_jp106_colon_unshifted(self):
        """JP layout has : as unshifted."""
        jp_map = build_char_map("jp106")
        assert jp_map[":"] == (MOD_NONE, 0x34)

    def test_jp106_preserves_us_letters(self):
        """JP layout should preserve basic US letter mappings."""
        jp_map = build_char_map("jp106")
        assert jp_map["a"] == (MOD_NONE, 0x04)
        assert jp_map["z"] == (MOD_NONE, 0x1D)

    def test_us104_is_base(self):
        """US104 layout should have no overrides (identical to base)."""
        us_map = build_char_map("us104")
        assert us_map["@"] == (MOD_LSHIFT, 0x1F)
        assert us_map['"'] == (MOD_LSHIFT, 0x34)

    def test_unknown_layout_raises(self):
        with pytest.raises(ValueError, match="Unknown layout"):
            build_char_map("nonexistent_layout")
