"""Tests for HID protocol packet building."""

from serial_hid_kvm.hid_protocol import (
    build_keyboard_packet,
    build_keyboard_report,
    build_keyboard_release_packet,
)


class TestBuildKeyboardReport:
    """Tests for build_keyboard_report (multi-key support)."""

    def test_no_keys(self):
        """Empty report should be all zeros in key slots."""
        pkt = build_keyboard_report(0x00)
        # Packet: HEADER(2) + ADDR(1) + CMD(1) + LEN(1) + DATA(8) + SUM(1) = 14
        assert len(pkt) == 14
        data = pkt[5:13]  # 8-byte HID report
        assert data == bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

    def test_single_key(self):
        """Single key should appear in first key slot."""
        pkt = build_keyboard_report(0x00, [0x04])  # 'a'
        data = pkt[5:13]
        assert data[0] == 0x00  # modifier
        assert data[2] == 0x04  # key1
        assert data[3] == 0x00  # key2 empty

    def test_modifier_with_key(self):
        """Modifier + key should both be set."""
        pkt = build_keyboard_report(0x01, [0x06])  # Ctrl+C
        data = pkt[5:13]
        assert data[0] == 0x01  # LCtrl
        assert data[2] == 0x06  # C

    def test_multiple_keys(self):
        """Multiple keys should fill key slots."""
        pkt = build_keyboard_report(0x00, [0x04, 0x05, 0x06])  # a, b, c
        data = pkt[5:13]
        keys = set(data[2:5])
        assert keys == {0x04, 0x05, 0x06}

    def test_max_six_keys(self):
        """Should accept up to 6 keys."""
        keycodes = [0x04, 0x05, 0x06, 0x07, 0x08, 0x09]
        pkt = build_keyboard_report(0x00, keycodes)
        data = pkt[5:13]
        keys = set(data[2:8])
        assert keys == set(keycodes)

    def test_more_than_six_keys_truncated(self):
        """Keys beyond 6 should be silently dropped."""
        keycodes = [0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A]
        pkt = build_keyboard_report(0x00, keycodes)
        data = pkt[5:13]
        # Only 6 keys fit, 7th is dropped
        non_zero = [b for b in data[2:8] if b != 0]
        assert len(non_zero) == 6

    def test_set_input(self):
        """Should accept a set of keycodes."""
        pkt = build_keyboard_report(0x00, {0x04, 0x1A})  # a, w
        data = pkt[5:13]
        non_zero = {b for b in data[2:8] if b != 0}
        assert non_zero == {0x04, 0x1A}

    def test_zero_keycodes_filtered(self):
        """Zero keycodes should be filtered out."""
        pkt = build_keyboard_report(0x00, [0x00, 0x04])
        data = pkt[5:13]
        assert data[2] == 0x04

    def test_matches_single_key_packet(self):
        """Single key report should produce same payload as build_keyboard_packet."""
        report_pkt = build_keyboard_report(0x02, [0x28])  # Shift+Enter
        single_pkt = build_keyboard_packet(0x02, 0x28)
        assert report_pkt == single_pkt

    def test_release_is_empty_report(self):
        """Release packet should match empty report."""
        release = build_keyboard_release_packet()
        empty = build_keyboard_report(0x00)
        assert release == empty
