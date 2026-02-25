# Creating Custom Keyboard Layouts

This guide explains how to create a custom keyboard layout file for serial-hid-kvm.

## Overview

Layout files define how **characters** map to **physical key presses** on the target machine. They are used by the API (`type_text`, `send_key`) and the preview window's character-to-keycode fallback path. The web viewer forwards physical key positions via `event.code` and does not use layout files.

The base layout is US ANSI 104-key (US104). Custom layouts only need to specify characters that **differ from US104**. For example, `jp106.yaml` overrides `"` from Shift+`'` (US) to Shift+`2` (JIS).

## File Format

Layout files are YAML with a single top-level key `overrides`:

```yaml
# mylayout.yaml - Description of your layout
# Only characters that differ from US104 need to be listed.

overrides:
  'character': [modifier, hid_keycode]
```

Each entry maps a character to a `[modifier, keycode]` pair:

- **character**: The character you want to type (e.g., `'"'`, `'@'`, `'\'`)
- **modifier**: Which modifier key(s) to hold — `none`, `shift`, `ralt`, `ctrl`, `shift+ralt`, etc.
- **hid_keycode**: The physical key's USB HID keycode in hex (e.g., `0x1F`)

### Modifier Names

| Name | Bitmask | Notes |
|------|---------|-------|
| `none` | 0x00 | No modifier |
| `shift` / `lshift` | 0x02 | Left Shift |
| `rshift` | 0x20 | Right Shift |
| `ctrl` / `lctrl` | 0x01 | Left Control |
| `rctrl` | 0x10 | Right Control |
| `alt` / `lalt` | 0x04 | Left Alt |
| `ralt` | 0x40 | Right Alt (AltGr on ISO keyboards) |
| `win` / `gui` / `super` / `meta` | 0x08 | Left Windows/Command |

Combine modifiers with `+`: `shift+ralt`, `ctrl+shift`, etc.

### HID Keycodes (Physical Key Positions)

HID keycodes identify **physical key positions**, not the characters printed on them. Common keycodes:

```
Row 0 (Function):
  Esc=0x29  F1=0x3A  F2=0x3B  F3=0x3C  F4=0x3D
  F5=0x3E   F6=0x3F  F7=0x40  F8=0x41
  F9=0x42   F10=0x43 F11=0x44 F12=0x45

Row 1 (Number):
  `/~=0x35
  1=0x1E  2=0x1F  3=0x20  4=0x21  5=0x22
  6=0x23  7=0x24  8=0x25  9=0x26  0=0x27
  -/_=0x2D  =/+=0x2E  Backspace=0x2A

Row 2 (QWERTY):
  Tab=0x2B
  Q=0x14  W=0x1A  E=0x08  R=0x15  T=0x17
  Y=0x1C  U=0x18  I=0x0C  O=0x12  P=0x13
  [/{=0x2F  ]/}=0x30  \/|=0x31

Row 3 (Home):
  CapsLock=0x39
  A=0x04  S=0x16  D=0x07  F=0x09  G=0x0A
  H=0x0B  J=0x0D  K=0x0E  L=0x0F
  ;/:=0x33  '/\"=0x34  Enter=0x28

Row 4 (Bottom):
  Z=0x1D  X=0x1B  C=0x06  V=0x19  B=0x05
  N=0x11  M=0x10
  ,/<=0x36  ./>=0x37  //?=0x38
  Space=0x2C

ISO/JIS extra keys:
  Non-US #~=0x32       (key right of ' on ISO keyboards)
  Non-US \/|=0x64      (key left of Z on ISO keyboards)
  International1=0x87  (JIS: ろ / backslash)
  International3=0x89  (JIS: ¥|)
```

> The labels above (e.g., `'/\"=0x34`) show the US characters on that physical key. On your layout, the same physical key may produce different characters — that's exactly what your override file defines.

## US104 Base Mapping

Characters that are **already correct** in US104 do not need overrides. The base mapping includes:

- `a`-`z` → keys 0x04-0x1D (no modifier)
- `A`-`Z` → keys 0x04-0x1D (shift)
- `0`-`9` → keys 0x27, 0x1E-0x26 (no modifier)
- `!@#$%^&*()` → keys 0x1E-0x27 (shift)
- Common punctuation: `` ` `` `-` `=` `[` `]` `\` `;` `'` `,` `.` `/` (no modifier)
- Shifted punctuation: `~` `_` `+` `{` `}` `|` `:` `"` `<` `>` `?` (shift)
- `\n` → Enter, `\t` → Tab, ` ` → Space

## Example: Creating a Layout

Suppose you need a layout where `@` is typed with `AltGr+2` instead of `Shift+2`:

```yaml
# myiso.yaml - My custom ISO layout

overrides:
  '@': [ralt, 0x1F]    # AltGr+2 produces @
```

## How to Use

1. Save your file as `<name>.yaml` (e.g., `myiso.yaml`)
2. Place it in a directory and specify with `--layouts-dir`:
   ```bash
   serial-hid-kvm --target-layout myiso --layouts-dir /path/to/layouts/
   ```
   Or set the environment variable:
   ```bash
   export SHKVM_LAYOUTS_DIR=/path/to/layouts
   serial-hid-kvm --target-layout myiso
   ```

## Reference: Existing Layouts

Study the built-in layouts in this directory for examples:

| File | Description | Key Differences from US104 |
|------|-------------|---------------------------|
| `us104.yaml` | US ANSI (base) | No overrides (empty) |
| `jp106.yaml` | Japanese JIS | Number row symbols, `@`/`[`/`]` positions, JIS ro/yen keys |
| `uk105.yaml` | UK ISO | `"` and `@` swapped, `#` on Non-US key |
| `de105.yaml` | German QWERTZ | Y/Z swapped, umlauts, AltGr symbols |
| `fr105.yaml` | French AZERTY | Extensively remapped letter and symbol positions |

## Testing and Debugging via the API

The most reliable way to verify a layout is to send test text via the API and check the output on the target screen. Start the server with `--debug-keys` to include HID-level trace information in API responses.

### 1. Start the server with your layout and debug mode

```bash
serial-hid-kvm --headless --api --target-layout mylayout --debug-keys
```

### 2. Send test text covering all overridden characters

Open a text editor (e.g., Notepad) on the target, then send text via the API:

```bash
echo '{"id":"1","method":"type_text","params":{"text":"!\"#$%&'"'"'() @ [ ] \\\\ ` | ~ _"}}' \
  | nc localhost 9329
```

Or using the Python client:

```python
from serial_hid_kvm.client import KvmClient
c = KvmClient(); c.connect()
c.type_text('!"#$%&\'() @ [ ] \\ ` | ~ _')
```

### 3. Check the `hid_trace` in the response

When `--debug-keys` is enabled, `type_text` and `send_key` responses include an `hid_trace` field showing the exact modifier and keycode sent for each character:

```json
{
  "id": "1", "ok": true,
  "result": {
    "chars_typed": 5,
    "hid_trace": [
      {"char": "@", "modifier": "0x00", "keycode": "0x2F"},
      {"char": "[", "modifier": "0x00", "keycode": "0x30"}
    ]
  }
}
```

This lets you verify that each character resolves to the correct physical key without needing to look at the target screen.

### 4. Capture the screen to verify output

```bash
echo '{"id":"2","method":"capture_frame","params":{"quality":80}}' | nc localhost 9329
```

The response contains a base64-encoded JPEG of the target screen. Compare the visible text against what you sent.

### 5. Common issues

- **Wrong characters appear**: The target OS keyboard layout does not match your layout file. For example, sending JP106 keycodes to a target with US keyboard layout active will produce US characters. Make sure the target OS keyboard layout matches (e.g., Japanese 106/109-key for jp106.yaml).
- **Some characters are missing**: The keycode may not exist on the target's keyboard driver. Check that JIS-specific keys (0x87, 0x89) are recognised by the target OS.
- **`{` and `}` not typed**: These are tag delimiters in `type_text`. Use `{{` and `}}` to type literal braces.
- **Layout-specific physical keys in preview window (e.g. JIS ろ/¥)**: Some keyboard layouts have physical keys that don't exist on other layouts. On Linux, pynput delivers characters — not physical key positions — so keys that produce the same character as a standard key cannot be distinguished. For example, JIS `_` (Shift+ろ, 0x87) and US `_` (Shift+`-`, 0x2D) both produce `_` on Xwayland. The preview window sends the standard key position in these cases. **Use the web viewer** for layout-specific keys — it uses `event.code` (physical key positions) and handles all keys correctly regardless of layout.

## Debug Output

Enable `--debug-keys` to see keycode resolution on the console. In the preview window on Linux, each key press shows one of these labels:

| Label | Meaning |
|-------|---------|
| `WAYLAND-REVERSE` | Shifted character reverse-looked up via the Xwayland host layout (Wayland hybrid mode) |
| `HOST REVERSE` | Character reverse-looked up via the host layout (pure X11, host ≠ target) |
| `CHAR` | Character mapped via `char_to_hid` using the target layout (default/fallback path) |
| `CHAR UNMAPPED` | Character not found in any layout map |

Example output (JP host, JP target on GNOME Wayland):
```
  -> WAYLAND-REVERSE char='!' → phys=0x1E held=0x02
  -> CHAR char='@' → char_to_hid=(0x00, 0x2F) held=0x00 → send=(0x00, 0x2F)
```

The first line shows `!` (Shift held) was reverse-looked up from the Xwayland US layout to physical key 0x1E (the `1` key), then sent with Shift (0x02). The second line shows `@` (no Shift) was mapped via `char_to_hid` to the JP106 target position (0x2F, the `@` key on JIS).

When `--debug-keys` is also used with `--api`, the API responses include `hid_trace` fields showing the resolved modifier and keycode for each character (see the API section above).

## Tips

- **Start small**: Only override what differs. Test with `type_text` via the API.
- **Use `--debug-keys`**: Shows keycodes being sent, helpful for debugging.
- **Physical keyboard reference**: If unsure which HID keycode a physical key has, press it in the web viewer with `--debug-keys` to see the keycode logged. The web viewer uses `event.code` and always shows the correct physical key position.
- **Web viewer for all keys**: If the preview window doesn't handle a layout-specific key correctly, use the web viewer instead — it handles all physical keys regardless of layout.

## Asking an AI to Create a Layout

If you want an AI assistant to generate a layout file, use a prompt like:

> Create a serial-hid-kvm keyboard layout YAML file for [your layout name] (e.g., "Brazilian ABNT2", "Spanish ISO", "Korean 106").
>
> The file format is:
> ```yaml
> overrides:
>   'character': [modifier, hid_keycode]
> ```
>
> - Only characters that differ from US ANSI 104 need to be listed.
> - `modifier` is one of: `none`, `shift`, `ralt`, `ctrl`, `shift+ralt`, etc.
> - `hid_keycode` is the USB HID Usage ID of the physical key position in hex (e.g., `0x1F` for the `2` key).
> - Reference the HID keycode table: a=0x04, 1=0x1E, 2=0x1F, ..., 0=0x27, -=0x2D, ==0x2E, [=0x2F, ]=0x30, \=0x31, Non-US#=0x32, ;=0x33, '=0x34, `=0x35, ,=0x36, .=0x37, /=0x38, International1=0x87, International3=0x89, Non-US\|=0x64.
> - See jp106.yaml and uk105.yaml for examples of the format.
