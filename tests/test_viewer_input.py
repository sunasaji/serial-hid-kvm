"""Input forwarding regressions; no display or serial hardware required."""

import asyncio
import json
import sys
from unittest.mock import Mock

import numpy as np
import pytest

from serial_hid_kvm import _preview_viewer as preview
from serial_hid_kvm._web_viewer import WebViewerServer
from serial_hid_kvm.config import Config
from serial_hid_kvm.hid_protocol import (
    build_keyboard_packet,
    build_mouse_abs_packet,
    build_mouse_rel_packet,
)


@pytest.mark.parametrize("windows,mode,expected", [
    (True, "auto", "win32"),
    (False, "auto", "pynput"),
    (True, "opencv", "opencv"),
    (False, "opencv", "opencv"),
    (True, "pynput", "pynput"),
    (False, "pynput", "pynput"),
])
def test_keyboard_backend_preserves_auto_selection(monkeypatch, windows, mode, expected):
    monkeypatch.setattr(preview, "_IS_WINDOWS", windows)
    monkeypatch.setattr(preview.shutil, "which", lambda _: "/usr/bin/xdotool")
    # Working Xwayland setups must not be silently switched away from pynput.
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert preview._keyboard_backend(mode) == expected


@pytest.mark.parametrize("windows,linux,mode,expected", [
    (False, True, "auto", "opencv"),
    (False, True, "pynput", "pynput"),
    (False, True, "opencv", "opencv"),
    (True, False, "auto", "win32"),
    (False, False, "auto", "pynput"),
])
def test_missing_xdotool_only_changes_linux_auto(monkeypatch, windows, linux, mode, expected):
    monkeypatch.setattr(preview, "_IS_WINDOWS", windows)
    monkeypatch.setattr(preview, "_IS_LINUX", linux)
    monkeypatch.setattr(preview.shutil, "which", lambda _: None)
    assert preview._keyboard_backend(mode) == expected


@pytest.mark.parametrize("windows", [False, True])
def test_preview_auto_starts_existing_platform_handler(monkeypatch, windows):
    monkeypatch.setattr(preview, "_IS_WINDOWS", windows)
    monkeypatch.setattr(preview.shutil, "which", lambda _: "/usr/bin/xdotool")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setitem(sys.modules, "pynput", Mock())
    focus = Mock()
    monkeypatch.setattr(preview, "_FocusDetector", focus)
    win32 = Mock()
    pynput = Mock()
    monkeypatch.setattr(preview, "_Win32KeyboardGrab", win32)
    monkeypatch.setattr(preview, "_PynputKeyboardHandler", pynput)
    selected = win32 if windows else pynput
    selected.return_value.quit_requested = True
    for name in ("namedWindow", "setMouseCallback", "destroyAllWindows"):
        monkeypatch.setattr(preview.cv2, name, Mock())
    config = Config()
    config.show_cursor = True  # Avoid native cursor APIs in the Windows case.

    preview.run_preview_inprocess(Mock(), config)

    selected.return_value.start.assert_called_once_with()
    selected.return_value.stop.assert_called_once_with()
    (pynput if windows else win32).assert_not_called()
    focus.assert_called_once()


@pytest.mark.parametrize("mode", ["absolute", "relative"])
@pytest.mark.parametrize("button", [1, 2, 4])
def test_web_click_reports_use_only_selected_button_mode(mode, button):
    hw = Mock()
    config = Config()
    config.mouse_button_mode = mode
    viewer = WebViewerServer(hw, config)

    async def messages():
        for event, buttons in (("mousedown", button), ("mousemove", button),
                               ("mouseup", 0)):
            yield json.dumps(dict(type=event, x=2048, y=1024, buttons=buttons))

    asyncio.run(viewer._recv_input(messages()))
    packets = [call.args[0] for call in hw.get_ch9329().send.call_args_list]
    if mode == "absolute":
        assert packets == [
            build_mouse_abs_packet(button, 2048, 1024),
            build_mouse_abs_packet(button, 2048, 1024),
            build_mouse_abs_packet(0, 2048, 1024),
        ]
    else:
        assert packets == [
            build_mouse_abs_packet(0, 2048, 1024),
            build_mouse_rel_packet(button, 0, 0),
            build_mouse_abs_packet(0, 2048, 1024),
            build_mouse_abs_packet(0, 2048, 1024),
            build_mouse_rel_packet(0, 0, 0),
        ]


def test_web_keyboard_still_forwards_press_and_release():
    hw = Mock()
    viewer = WebViewerServer(hw, Config())

    async def messages():
        for event in ("keydown", "keyup"):
            yield json.dumps(dict(type=event, code="KeyA"))

    asyncio.run(viewer._recv_input(messages()))
    assert [c.args[0] for c in hw.get_ch9329().send.call_args_list] == [
        build_keyboard_packet(0, 4), build_keyboard_packet(0, 0),
    ]


@pytest.mark.parametrize("mode", ["absolute", "relative"])
@pytest.mark.parametrize("keyboard_mode", ["auto", "opencv"])
def test_preview_forwards_window_keys_and_selected_button_mode(monkeypatch, mode, keyboard_mode):
    monkeypatch.setattr(preview, "_IS_WINDOWS", False)
    monkeypatch.setattr(preview, "_IS_LINUX", True)
    monkeypatch.setattr(preview.shutil, "which", lambda _: None)
    focus_detector = Mock(side_effect=AssertionError("X11 focus must not be used"))
    monkeypatch.setattr(preview, "_FocusDetector", focus_detector)
    hw = Mock()
    hw.get_capture().get_latest_frame.return_value = np.zeros((100, 200, 3), dtype=np.uint8)
    callbacks = {}
    for name in ("namedWindow", "resizeWindow", "imshow", "destroyAllWindows"):
        monkeypatch.setattr(preview.cv2, name, Mock())
    monkeypatch.setattr(preview.cv2, "setMouseCallback",
                        lambda name, cb: callbacks.update(mouse=cb))
    events = iter([
        preview.cv2.EVENT_LBUTTONDOWN, preview.cv2.EVENT_MOUSEMOVE,
        preview.cv2.EVENT_RBUTTONDOWN, preview.cv2.EVENT_LBUTTONUP,
        preview.cv2.EVENT_RBUTTONUP,
    ])

    def wait_key(_):
        event = next(events, None)
        if event is None:
            return 27  # Close the fallback viewer.
        callbacks["mouse"](event, 100, 50, 0, None)
        return ord("a") if event == preview.cv2.EVENT_LBUTTONDOWN else -1

    monkeypatch.setattr(preview.cv2, "waitKeyEx", wait_key)
    config = Config()
    config.preview_keyboard = keyboard_mode
    config.mouse_button_mode = mode
    preview.run_preview_inprocess(hw, config)
    hw.get_ch9329().send_keyboard.assert_called_once_with(0, 4)
    packets = [c.args[0] for c in hw.get_ch9329().send.call_args_list]
    if mode == "relative":
        assert [pkt[6] for pkt in packets if pkt[3] == 5] == [1, 3, 2, 0]
        assert [pkt[6] for pkt in packets if pkt[3] == 4] == [0, 0, 0, 0, 0]
    else:
        assert all(pkt[3] == 4 for pkt in packets)
        assert [pkt[6] for pkt in packets] == [1, 1, 3, 2, 0]


def test_relative_click_wire_bytes():
    assert build_mouse_rel_packet(1, 0, 0) == bytes.fromhex("57 ab 00 05 05 01 01 00 00 00 0e")
    assert build_mouse_rel_packet(0, 0, 0) == bytes.fromhex("57 ab 00 05 05 01 00 00 00 00 0d")


def test_web_relative_buttons_preserve_scroll_and_clear_on_release_all():
    hw = Mock()
    config = Config()
    config.mouse_button_mode = "relative"
    viewer = WebViewerServer(hw, config)

    async def messages():
        for event in (
            dict(type="mousedown", buttons=3),
            dict(type="scroll", deltaY=-3),
            dict(type="release_all"),
            dict(type="scroll", deltaY=3),
        ):
            yield json.dumps(event)

    asyncio.run(viewer._recv_input(messages()))
    hw.get_ch9329().release_all.assert_called_once_with()
    packets = [c.args[0] for c in hw.get_ch9329().send.call_args_list]
    assert packets == [
        build_mouse_abs_packet(0, 0, 0),
        build_mouse_rel_packet(3, 0, 0),
        build_mouse_rel_packet(3, 0, 0, scroll=-3),
        build_mouse_rel_packet(0, 0, 0, scroll=3),
    ]
