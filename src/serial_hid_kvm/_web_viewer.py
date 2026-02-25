"""Web-based remote desktop viewer served over HTTP + WebSocket.

Provides a browser-based KVM interface: JPEG video stream over WebSocket
(binary frames) and keyboard/mouse input as JSON text messages.  HTML/JS
is embedded directly — no npm or build step required.

Usage (integrated into server.py):
    serial-hid-kvm --headless --web
    serial-hid-kvm --web --web-port 9330 --web-fps 20 --web-quality 60
"""

import asyncio
import json
import logging
import queue

import websockets
from websockets.http11 import Response

from ._audio import AudioCapture
from .hid_protocol import (
    build_keyboard_packet,
    build_keyboard_release_packet,
    build_keyboard_report,
    build_mouse_abs_packet,
    build_mouse_rel_packet,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# W3C KeyboardEvent.code → HID keycode mapping
# ---------------------------------------------------------------------------

_JS_CODE_TO_HID: dict[str, int] = {
    # Row 0: Escape + Function keys
    "Escape": 0x29,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D,
    "F5": 0x3E, "F6": 0x3F, "F7": 0x40, "F8": 0x41,
    "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "PrintScreen": 0x46, "ScrollLock": 0x47, "Pause": 0x48,

    # Row 1: Digits
    "Backquote": 0x35,
    "Digit1": 0x1E, "Digit2": 0x1F, "Digit3": 0x20, "Digit4": 0x21,
    "Digit5": 0x22, "Digit6": 0x23, "Digit7": 0x24, "Digit8": 0x25,
    "Digit9": 0x26, "Digit0": 0x27,
    "Minus": 0x2D, "Equal": 0x2E, "Backspace": 0x2A,

    # Row 2: QWERTY
    "Tab": 0x2B,
    "KeyQ": 0x14, "KeyW": 0x1A, "KeyE": 0x08, "KeyR": 0x15,
    "KeyT": 0x17, "KeyY": 0x1C, "KeyU": 0x18, "KeyI": 0x0C,
    "KeyO": 0x12, "KeyP": 0x13,
    "BracketLeft": 0x2F, "BracketRight": 0x30, "Backslash": 0x31,

    # Row 3: ASDF
    "CapsLock": 0x39,
    "KeyA": 0x04, "KeyS": 0x16, "KeyD": 0x07, "KeyF": 0x09,
    "KeyG": 0x0A, "KeyH": 0x0B, "KeyJ": 0x0D, "KeyK": 0x0E,
    "KeyL": 0x0F,
    "Semicolon": 0x33, "Quote": 0x34, "Enter": 0x28,

    # Row 4: ZXCV
    "KeyZ": 0x1D, "KeyX": 0x1B, "KeyC": 0x06, "KeyV": 0x19,
    "KeyB": 0x05, "KeyN": 0x11, "KeyM": 0x10,
    "Comma": 0x36, "Period": 0x37, "Slash": 0x38,

    # Row 5
    "Space": 0x2C,

    # Navigation cluster
    "Insert": 0x49, "Home": 0x4A, "PageUp": 0x4B,
    "Delete": 0x4C, "End": 0x4D, "PageDown": 0x4E,

    # Arrow keys
    "ArrowUp": 0x52, "ArrowDown": 0x51,
    "ArrowLeft": 0x50, "ArrowRight": 0x4F,

    # Numpad
    "NumLock": 0x53,
    "NumpadDivide": 0x54, "NumpadMultiply": 0x55,
    "NumpadSubtract": 0x56, "NumpadAdd": 0x57,
    "NumpadEnter": 0x58, "NumpadDecimal": 0x63,
    "Numpad0": 0x62, "Numpad1": 0x59, "Numpad2": 0x5A,
    "Numpad3": 0x5B, "Numpad4": 0x5C, "Numpad5": 0x5D,
    "Numpad6": 0x5E, "Numpad7": 0x5F, "Numpad8": 0x60,
    "Numpad9": 0x61,

    # ISO extra key (left of Z on non-US keyboards)
    "IntlBackslash": 0x64,

    # JIS-specific
    "IntlRo": 0x87,       # International1 (ろ / _\)
    "IntlYen": 0x89,      # International3 (¥|)
    "KanaMode": 0x88,     # Katakana/Hiragana
    "Convert": 0x8A,      # 変換
    "NonConvert": 0x8B,    # 無変換
    "Lang1": 0x90,
    "Lang2": 0x91,

    # Context menu
    "ContextMenu": 0x65,
}

# Modifier codes → bitmask
_JS_MOD_BITS: dict[str, int] = {
    "ShiftLeft": 0x02,    "ShiftRight": 0x20,
    "ControlLeft": 0x01,  "ControlRight": 0x10,
    "AltLeft": 0x04,      "AltRight": 0x40,
    "MetaLeft": 0x08,     "MetaRight": 0x80,
}


# ---------------------------------------------------------------------------
# Embedded HTML/JS viewer
# ---------------------------------------------------------------------------

_VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>serial-hid-kvm</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#1a1a2e;font-family:system-ui,sans-serif;color:#e0e0e0}
#toolbar{display:flex;align-items:center;gap:8px;padding:4px 12px;background:#16213e;height:36px;user-select:none}
#toolbar button{background:#0f3460;border:1px solid #1a1a5e;color:#e0e0e0;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px}
#toolbar button:hover{background:#1a4a8a}
#toolbar button:active{background:#0a2a50}
#status{margin-left:auto;font-size:12px;color:#8888aa}
#fps{font-size:12px;color:#8888aa;min-width:70px;text-align:right}
#container{display:flex;align-items:center;justify-content:center;width:100%;height:calc(100% - 36px);background:#0a0a1a;overflow:auto}
canvas{display:block;image-rendering:auto;cursor:none}
#toolbar button.active{background:#2a6a2a;border-color:#3a8a3a}
</style>
</head>
<body>
<div id="toolbar">
  <button id="btnCad" title="Send Ctrl+Alt+Delete to target">Ctrl+Alt+Del</button>
  <button id="btnAltTab" title="Send Alt+Tab to target">Alt+Tab</button>
  <button id="btnViewOnly" title="Toggle view-only mode (no input sent)">View Only</button>
  <button id="btnAudio" title="Toggle audio playback" style="display:none">&#x1f507; Audio</button>
  <button id="btnCursor" title="Toggle local cursor visibility">Cursor</button>
  <button id="btnScale" title="Toggle 1:1 / Fit scaling">1:1</button>
  <button id="btnFs" title="Toggle fullscreen">Fullscreen</button>
  <span id="status">Connecting…</span>
  <span id="fps"></span>
</div>
<div id="container"><canvas id="screen"></canvas></div>
<script>
"use strict";
const canvas = document.getElementById("screen");
const ctx = canvas.getContext("2d");
const statusEl = document.getElementById("status");
const fpsEl = document.getElementById("fps");
const container = document.getElementById("container");

let ws = null;
let frameCount = 0;
let lastFpsTime = performance.now();

// --- WebSocket ---
function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(proto + "//" + location.host + "/ws");
  ws.binaryType = "arraybuffer";

  ws.onopen = () => { statusEl.textContent = "Connected"; };
  ws.onclose = () => {
    statusEl.textContent = "Disconnected — reconnecting…";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => { ws.close(); };
  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      const view = new Uint8Array(ev.data);
      const type = view[0];
      const payload = ev.data.slice(1);
      if (type === 1) {
        // Video frame (JPEG)
        const blob = new Blob([payload], {type: "image/jpeg"});
        const url = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width = img.width;
            canvas.height = img.height;
            updateCanvasSize();
          }
          ctx.drawImage(img, 0, 0);
          URL.revokeObjectURL(url);
          frameCount++;
        };
        img.src = url;
      } else if (type === 2) {
        // Audio chunk (PCM int16 LE)
        feedAudio(payload);
      }
    } else {
      // Text message (JSON)
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "audio_config") { setupAudioConfig(msg); }
      } catch(e) {}
    }
  };
}

// --- FPS counter ---
setInterval(() => {
  const now = performance.now();
  const elapsed = (now - lastFpsTime) / 1000;
  fpsEl.textContent = (frameCount / elapsed).toFixed(1) + " fps";
  frameCount = 0;
  lastFpsTime = now;
}, 2000);

// --- Canvas sizing ---
let scaleMode = "native";  // "native" = 1:1 pixel, "fit" = fit to window

function updateCanvasSize() {
  if (canvas.width === 0 || canvas.height === 0) return;
  if (scaleMode === "fit") {
    const cw = container.clientWidth;
    const ch = container.clientHeight;
    const ar = canvas.width / canvas.height;
    let dw, dh;
    if (cw / ch > ar) { dh = ch; dw = ch * ar; }
    else { dw = cw; dh = cw / ar; }
    canvas.style.width = dw + "px";
    canvas.style.height = dh + "px";
  } else {
    canvas.style.width = canvas.width + "px";
    canvas.style.height = canvas.height + "px";
  }
}
window.addEventListener("resize", updateCanvasSize);

// --- Mouse coordinate normalisation (0-4095) ---
function mouseCoords(e) {
  const rect = canvas.getBoundingClientRect();
  const sx = canvas.width / rect.width;
  const sy = canvas.height / rect.height;
  const cx = (e.clientX - rect.left) * sx;
  const cy = (e.clientY - rect.top) * sy;
  const x = Math.max(0, Math.min(4095, Math.round(cx * 4096 / canvas.width)));
  const y = Math.max(0, Math.min(4095, Math.round(cy * 4096 / canvas.height)));
  return {x, y};
}

let viewOnly = false;
let showCursor = false;

function send(obj) {
  if (viewOnly) return;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- Mouse events ---
let lastMouseSend = 0;
canvas.addEventListener("mousemove", (e) => {
  const now = performance.now();
  if (now - lastMouseSend < 16) return;  // throttle ~60Hz
  lastMouseSend = now;
  const {x, y} = mouseCoords(e);
  send({type:"mousemove", x, y, buttons: e.buttons});
});
canvas.addEventListener("mousedown", (e) => {
  e.preventDefault();
  canvas.focus();
  const {x, y} = mouseCoords(e);
  send({type:"mousedown", x, y, buttons: e.buttons});
});
canvas.addEventListener("mouseup", (e) => {
  e.preventDefault();
  const {x, y} = mouseCoords(e);
  send({type:"mouseup", x, y, buttons: e.buttons});
});
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const dy = e.deltaY > 0 ? -3 : 3;
  send({type:"scroll", deltaY: dy});
}, {passive: false});
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

// --- Keyboard events ---
canvas.setAttribute("tabindex", "0");
canvas.addEventListener("keydown", (e) => {
  if (viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.repeat) return;
  send({type:"keydown", code: e.code});
});
canvas.addEventListener("keyup", (e) => {
  if (viewOnly) return;
  e.preventDefault();
  e.stopPropagation();
  send({type:"keyup", code: e.code});
});
canvas.addEventListener("blur", () => { send({type:"release_all"}); });

// --- Toolbar ---
document.getElementById("btnCad").addEventListener("click", () => {
  send({type:"keydown", code:"ControlLeft"});
  send({type:"keydown", code:"AltLeft"});
  send({type:"keydown", code:"Delete"});
  setTimeout(() => {
    send({type:"keyup", code:"Delete"});
    send({type:"keyup", code:"AltLeft"});
    send({type:"keyup", code:"ControlLeft"});
  }, 100);
  canvas.focus();
});
document.getElementById("btnViewOnly").addEventListener("click", () => {
  const btn = document.getElementById("btnViewOnly");
  viewOnly = !viewOnly;
  btn.classList.toggle("active", viewOnly);
  if (viewOnly) showCursor = true;
  updateCursor();
});
document.getElementById("btnCursor").addEventListener("click", () => {
  const btn = document.getElementById("btnCursor");
  showCursor = !showCursor;
  btn.classList.toggle("active", showCursor);
  updateCursor();
  canvas.focus();
});
function updateCursor() {
  canvas.style.cursor = showCursor ? "default" : "none";
  document.getElementById("btnCursor").classList.toggle("active", showCursor);
}
document.getElementById("btnScale").addEventListener("click", () => {
  const btn = document.getElementById("btnScale");
  if (scaleMode === "native") { scaleMode = "fit"; btn.textContent = "Fit"; }
  else { scaleMode = "native"; btn.textContent = "1:1"; }
  updateCanvasSize();
  canvas.focus();
});
document.getElementById("btnAltTab").addEventListener("click", () => {
  send({type:"keydown", code:"AltLeft"});
  send({type:"keydown", code:"Tab"});
  setTimeout(() => {
    send({type:"keyup", code:"Tab"});
    send({type:"keyup", code:"AltLeft"});
  }, 100);
  canvas.focus();
});
document.getElementById("btnFs").addEventListener("click", () => {
  if (!document.fullscreenElement) document.documentElement.requestFullscreen();
  else document.exitFullscreen();
  canvas.focus();
});
// Keyboard Lock API — capture system keys (Alt+Tab, Win, etc.) in fullscreen
document.addEventListener("fullscreenchange", () => {
  if (document.fullscreenElement && navigator.keyboard && navigator.keyboard.lock) {
    navigator.keyboard.lock(["AltLeft","AltRight","Tab","MetaLeft","MetaRight","Escape"]).catch(() => {});
  }
});

// --- Audio playback ---
let audioCtx = null;
let audioNode = null;
let audioCfg = null;

const WORKLET_SRC = `
class P extends AudioWorkletProcessor {
  constructor() {
    super();
    this.b = new Float32Array(0);
    this.port.onmessage = e => {
      const o = this.b, n = e.data;
      const m = new Float32Array(o.length + n.length);
      m.set(o); m.set(n, o.length);
      this.b = m;
    };
  }
  process(ins, outs) {
    const out = outs[0], ch = out.length, fr = out[0].length;
    const need = fr * ch;
    if (this.b.length >= need) {
      for (let i = 0; i < fr; i++)
        for (let c = 0; c < ch; c++)
          out[c][i] = this.b[i * ch + c];
      this.b = this.b.slice(need);
    }
    return true;
  }
}
registerProcessor("p", P);
`;

function setupAudioConfig(cfg) {
  audioCfg = cfg;
  document.getElementById("btnAudio").style.display = "";
}

async function startAudio() {
  if (!audioCfg || audioCtx) return;
  audioCtx = new AudioContext({sampleRate: audioCfg.sampleRate});
  const blob = new Blob([WORKLET_SRC], {type: "application/javascript"});
  const url = URL.createObjectURL(blob);
  await audioCtx.audioWorklet.addModule(url);
  URL.revokeObjectURL(url);
  audioNode = new AudioWorkletNode(audioCtx, "p",
    {outputChannelCount: [audioCfg.channels]});
  audioNode.connect(audioCtx.destination);
}

function feedAudio(buf) {
  if (!audioNode) return;
  const i16 = new Int16Array(buf);
  const f32 = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768.0;
  audioNode.port.postMessage(f32, [f32.buffer]);
}

document.getElementById("btnAudio").addEventListener("click", async () => {
  const btn = document.getElementById("btnAudio");
  if (!audioCtx) {
    await startAudio();
    btn.textContent = "\u{1f50a} Audio";
    btn.classList.add("active");
  } else if (audioCtx.state === "running") {
    audioCtx.suspend();
    btn.textContent = "\u{1f507} Audio";
    btn.classList.remove("active");
  } else {
    audioCtx.resume();
    btn.textContent = "\u{1f50a} Audio";
    btn.classList.add("active");
  }
  canvas.focus();
});

// --- Start ---
canvas.focus();
connect();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebViewerServer
# ---------------------------------------------------------------------------

class WebViewerServer:
    """WebSocket server that streams JPEG frames and accepts input events.

    Uses a single port for both HTTP (HTML delivery via process_request)
    and WebSocket (video + input).
    """

    def __init__(self, hardware, config, audio: AudioCapture | None = None):
        """
        Args:
            hardware: KvmHardware instance.
            config: Config instance with web_port, web_fps, web_quality.
            audio: Optional shared AudioCapture instance.
        """
        self._hw = hardware
        self._config = config
        self._server = None
        self._clients: set = set()
        self._audio = audio

    async def start(self):
        host = self._config.web_host
        port = self._config.web_port
        self._server = await websockets.serve(
            self._handle_client,
            host,
            port,
            process_request=self._process_http,
        )
        logger.info(f"Web viewer listening on http://{host}:{port}")

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _process_http(self, connection, request):
        """Serve the HTML viewer for non-WebSocket HTTP requests."""
        if request.path == "/ws":
            # Stash User-Agent for logging when _handle_client runs
            ua = request.headers.get("User-Agent", "")
            connection._kvm_user_agent = ua
            return None  # let WebSocket handshake proceed
        html_bytes = _VIEWER_HTML.encode("utf-8")
        return Response(
            200, "OK",
            websockets.Headers({
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(html_bytes)),
                "Cache-Control": "no-cache",
            }),
            html_bytes,
        )

    async def _handle_client(self, ws):
        """Handle a single WebSocket client: stream frames + process input."""
        self._clients.add(ws)
        addr = ws.remote_address
        ip = f"{addr[0]}:{addr[1]}" if addr else "unknown"
        ua = getattr(ws, "_kvm_user_agent", "")
        ua_short = ua[:120] + "…" if len(ua) > 120 else ua
        logger.info(f"Web client connected from {ip} ({len(self._clients)} total)"
                     + (f"  UA: {ua_short}" if ua_short else ""))
        audio_queue = None
        try:
            # Notify client about audio availability
            if self._audio is not None:
                audio_queue = self._audio.subscribe()
                await ws.send(json.dumps({
                    "type": "audio_config",
                    "sampleRate": self._audio.samplerate,
                    "channels": self._audio.channels,
                }))

            tasks = [
                asyncio.create_task(self._send_frames(ws)),
                asyncio.create_task(self._recv_input(ws)),
            ]
            if audio_queue is not None:
                tasks.append(asyncio.create_task(
                    self._send_audio(ws, audio_queue)))
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            if audio_queue is not None and self._audio is not None:
                self._audio.unsubscribe(audio_queue)
            self._clients.discard(ws)
            logger.info(f"Web client disconnected from {ip} ({len(self._clients)} total)")

    async def _send_frames(self, ws):
        """Stream JPEG frames to the client at configured FPS."""
        fps = self._config.web_fps
        quality = self._config.web_quality
        interval = 1.0 / fps
        capture = self._hw.get_capture()
        loop = asyncio.get_running_loop()

        while True:
            result = await loop.run_in_executor(
                None, capture.get_frame_jpeg, quality
            )
            if result is not None:
                jpeg_bytes, _w, _h = result
                try:
                    await ws.send(b"\x01" + jpeg_bytes)
                except websockets.ConnectionClosed:
                    break
            await asyncio.sleep(interval)

    async def _send_audio(self, ws, audio_queue: queue.Queue):
        """Stream PCM audio chunks to the client."""
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(
                None, self._get_audio_chunk, audio_queue
            )
            if chunk is not None:
                try:
                    await ws.send(b"\x02" + chunk)
                except websockets.ConnectionClosed:
                    break

    @staticmethod
    def _get_audio_chunk(q: queue.Queue) -> bytes | None:
        try:
            return q.get(timeout=0.1)
        except queue.Empty:
            return None

    async def _recv_input(self, ws):
        """Receive and process input events from the client."""
        held_modifiers = 0
        held_keys: set[int] = set()  # currently pressed non-modifier HID keycodes
        ch9329 = self._hw.get_ch9329()
        loop = asyncio.get_running_loop()

        async for message in ws:
            if not isinstance(message, str):
                continue
            try:
                ev = json.loads(message)
            except json.JSONDecodeError:
                continue

            ev_type = ev.get("type")

            if ev_type == "keydown":
                code = ev.get("code", "")
                # Modifier key?
                mod_bit = _JS_MOD_BITS.get(code)
                if mod_bit is not None:
                    held_modifiers |= mod_bit
                    pkt = build_keyboard_report(held_modifiers, held_keys)
                    await loop.run_in_executor(None, ch9329.send, pkt)
                    continue
                # Regular key
                hid = _JS_CODE_TO_HID.get(code)
                if hid is not None:
                    held_keys.add(hid)
                    pkt = build_keyboard_report(held_modifiers, held_keys)
                    await loop.run_in_executor(None, ch9329.send, pkt)

            elif ev_type == "keyup":
                code = ev.get("code", "")
                mod_bit = _JS_MOD_BITS.get(code)
                if mod_bit is not None:
                    held_modifiers &= ~mod_bit
                    pkt = build_keyboard_report(held_modifiers, held_keys)
                    await loop.run_in_executor(None, ch9329.send, pkt)
                    continue
                # Release key
                hid = _JS_CODE_TO_HID.get(code)
                if hid is not None:
                    held_keys.discard(hid)
                pkt = build_keyboard_report(held_modifiers, held_keys)
                await loop.run_in_executor(None, ch9329.send, pkt)

            elif ev_type == "mousemove":
                x = ev.get("x", 0)
                y = ev.get("y", 0)
                buttons = ev.get("buttons", 0)
                pkt = build_mouse_abs_packet(buttons, x, y)
                await loop.run_in_executor(None, ch9329.send, pkt)

            elif ev_type == "mousedown":
                x = ev.get("x", 0)
                y = ev.get("y", 0)
                buttons = ev.get("buttons", 0)
                pkt = build_mouse_abs_packet(buttons, x, y)
                await loop.run_in_executor(None, ch9329.send, pkt)

            elif ev_type == "mouseup":
                x = ev.get("x", 0)
                y = ev.get("y", 0)
                buttons = ev.get("buttons", 0)
                pkt = build_mouse_abs_packet(buttons, x, y)
                await loop.run_in_executor(None, ch9329.send, pkt)

            elif ev_type == "scroll":
                dy = ev.get("deltaY", 0)
                scroll = max(-127, min(127, int(dy)))
                pkt = build_mouse_rel_packet(0, 0, 0, scroll=scroll)
                await loop.run_in_executor(None, ch9329.send, pkt)

            elif ev_type == "release_all":
                await loop.run_in_executor(None, ch9329.release_all)
                held_modifiers = 0
                held_keys.clear()
