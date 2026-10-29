"""Input compatibility settings preserve defaults and config precedence."""

import os

import pytest

from serial_hid_kvm import config
from serial_hid_kvm.server import _build_parser


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for name in os.environ:
        if name.startswith("SHKVM_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(config, "_default_config_paths", lambda: [])


def test_defaults_preserve_existing_input_modes():
    loaded = config.load_config(_build_parser().parse_args([]))
    assert loaded.mouse_button_mode == "absolute"
    assert loaded.preview_keyboard == "auto"


def test_file_env_and_cli_precedence(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text("mouse_button_mode: relative\npreview_keyboard: opencv\n")
    parser = _build_parser()
    args = parser.parse_args(["--config", str(path)])
    loaded = config.load_config(args)
    assert (loaded.mouse_button_mode, loaded.preview_keyboard) == ("relative", "opencv")

    monkeypatch.setenv("SHKVM_MOUSE_BUTTON_MODE", "absolute")
    monkeypatch.setenv("SHKVM_PREVIEW_KEYBOARD", "pynput")
    loaded = config.load_config(args)
    assert (loaded.mouse_button_mode, loaded.preview_keyboard) == ("absolute", "pynput")

    loaded = config.load_config(parser.parse_args([
        "--config", str(path), "--mouse-button-mode", "relative",
        "--preview-keyboard", "opencv",
    ]))
    assert (loaded.mouse_button_mode, loaded.preview_keyboard) == ("relative", "opencv")


@pytest.mark.parametrize("attr,env", [
    ("mouse_button_mode", "SHKVM_MOUSE_BUTTON_MODE"),
    ("preview_keyboard", "SHKVM_PREVIEW_KEYBOARD"),
])
@pytest.mark.parametrize("source", ["env", "file", "cli"])
def test_invalid_input_modes_are_rejected(tmp_path, monkeypatch, attr, env, source):
    parser = _build_parser()
    if source == "cli":
        with pytest.raises(SystemExit):
            parser.parse_args(["--" + attr.replace("_", "-"), "invalid"])
        return
    if source == "file":
        path = tmp_path / "settings.yaml"
        path.write_text(f"{attr}: invalid\n")
        monkeypatch.setenv("SHKVM_CONFIG", str(path))
    else:
        monkeypatch.setenv(env, "invalid")
    with pytest.raises(ValueError, match=attr):
        config.load_config()
