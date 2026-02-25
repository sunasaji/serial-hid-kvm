"""Audio capture and playback (optional — requires sounddevice).

AudioCapture grabs PCM from an input device and broadcasts to subscribers.
AudioPlayback consumes from a subscriber queue and plays locally.
"""

import logging
import queue
import threading

logger = logging.getLogger(__name__)


class AudioCapture:
    """Capture PCM audio from an input device and broadcast to subscribers."""

    def __init__(self, device):
        import sounddevice as sd
        dev_value = int(device) if device.isdigit() else device
        info = sd.query_devices(dev_value, kind="input")
        self.samplerate = int(info["default_samplerate"])
        self.channels = min(2, info["max_input_channels"])
        self.device_name = info["name"]
        self._device = dev_value
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._stream = None

    def start(self):
        import sounddevice as sd
        self._stream = sd.InputStream(
            device=self._device,
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="int16",
            blocksize=960,  # 20ms at 48kHz
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            f"Audio capture started: {self.device_name}"
            f" ({self.samplerate} Hz, {self.channels} ch)"
        )

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _callback(self, indata, frames, time_info, status):
        chunk = bytes(indata)
        with self._lock:
            for q in self._subscribers:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    pass  # drop if consumer is too slow


class AudioPlayback:
    """Play PCM audio from an AudioCapture subscriber queue."""

    def __init__(self, capture: AudioCapture):
        self._capture = capture
        self._queue: queue.Queue | None = None
        self._stream = None

    def start(self):
        import sounddevice as sd
        self._queue = self._capture.subscribe()
        self._stream = sd.OutputStream(
            samplerate=self._capture.samplerate,
            channels=self._capture.channels,
            dtype="int16",
            blocksize=960,
        )
        self._stream.start()
        self._thread = threading.Thread(
            target=self._feed_loop, daemon=True, name="audio-playback",
        )
        self._thread.start()
        logger.info("Audio playback started")

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._queue is not None:
            self._capture.unsubscribe(self._queue)
            self._queue = None

    def _feed_loop(self):
        import numpy as np
        channels = self._capture.channels
        while self._stream is not None and self._queue is not None:
            try:
                chunk = self._queue.get(timeout=0.1)
                data = np.frombuffer(chunk, dtype="int16").reshape(-1, channels)
                self._stream.write(data)
            except queue.Empty:
                continue
            except Exception as e:
                logger.warning(f"Audio playback error: {e}")
                break
