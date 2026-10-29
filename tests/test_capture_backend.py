"""Capture backend selection without opening cameras or display windows."""

from unittest.mock import Mock, call

import pytest

from serial_hid_kvm import capture


@pytest.mark.parametrize("system,device,opened,expected", [
    ("Linux", "/dev/video0", [True],
     [call("/dev/video0", capture.cv2.CAP_V4L2)]),
    ("Linux", "2", [True], [call(2, capture.cv2.CAP_V4L2)]),
    ("Linux", "/dev/video0", [False, True],
     [call("/dev/video0", capture.cv2.CAP_V4L2), call("/dev/video0")]),
    ("Darwin", 0, [True], [call(0)]),
    ("Windows", "0", [True], [call(0, capture.cv2.CAP_MSMF)]),
    ("Windows", 0, [False, True],
     [call(0, capture.cv2.CAP_MSMF), call(0, capture.cv2.CAP_DSHOW)]),
])
def test_capture_backend_selection(monkeypatch, system, device, opened, expected):
    caps = [_mock_capture(state) for state in opened]
    factory = Mock(side_effect=caps)
    monkeypatch.setattr(capture.platform, "system", lambda: system)
    monkeypatch.setattr(capture.cv2, "VideoCapture", factory)

    capture.ScreenCapture(device=device, fourcc="YUYV")._open_device()

    assert factory.call_args_list == expected
    if system == "Linux" and not opened[0]:
        caps[0].release.assert_called_once_with()
        caps[1].set.assert_any_call(capture.cv2.CAP_PROP_FOURCC,
                                   capture.cv2.VideoWriter.fourcc(*"YUYV"))
    else:
        caps[0].release.assert_not_called()


def test_linux_capture_fails_when_both_backends_fail(monkeypatch):
    caps = [_mock_capture(False), _mock_capture(False)]
    factory = Mock(side_effect=caps)
    monkeypatch.setattr(capture.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capture.cv2, "VideoCapture", factory)

    with pytest.raises(RuntimeError, match="Failed to open capture device"):
        capture.ScreenCapture(device="/dev/video0")._open_device()

    caps[0].release.assert_called_once_with()
    assert factory.call_args_list == [
        call("/dev/video0", capture.cv2.CAP_V4L2), call("/dev/video0"),
    ]


def _mock_capture(opened):
    cap = Mock()
    cap.isOpened.return_value = opened
    cap.get.return_value = 0
    cap.getBackendName.return_value = "TEST"
    return cap
