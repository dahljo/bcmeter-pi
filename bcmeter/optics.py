"""LED optical control via pigpiod.

Manages the 3 measurement wavelength LEDs (880nm, 520nm, 370nm)
and the status indicator LED.
"""

import logging
import math
import time
import threading

logger = logging.getLogger("bcmeter.optics")

# Pin assignments (BCM GPIO)
LED_PINS = {
    0: 26,  # 880nm IR
    1: 25,  # 520nm Green
    2: 24,  # 370nm UV
}

WAVELENGTH_NAMES = ["880nm", "520nm", "370nm"]
SIGMA = [7.77e-8, 13.14e-8, 18.47e-8]  # m²/g absorption cross-sections

STATUS_LED_PIN = 1  # Mono LED for status

LED_PWM_FREQ = 8000
LED_PWM_RANGE = 1000
LED_PUBLIC_DUTY_MAX = 255


def led_duty_to_pwm(duty: int) -> int:
    """Convert public 0..255 LED duty to the configured pigpio PWM range."""
    bounded = max(0, min(LED_PUBLIC_DUTY_MAX, int(duty)))
    return int(round(bounded * LED_PWM_RANGE / LED_PUBLIC_DUTY_MAX))


class Optics:
    """LED control wrapper around pigpiod."""

    def __init__(self, pi=None):
        self._pi = pi
        self._duties = [255, 255, 255]  # Per-channel duty cycles
        self._lock = threading.Lock()

    def init(self, pi):
        """Initialize LED PWM channels."""
        self._pi = pi
        if not pi or not pi.connected:
            logger.error("pigpiod not connected, cannot init optics")
            return False

        for ch in range(3):
            pin = LED_PINS[ch]
            pi.set_mode(pin, 1)  # OUTPUT
            pi.set_PWM_range(pin, LED_PWM_RANGE)
            actual_freq = pi.set_PWM_frequency(pin, LED_PWM_FREQ)
            pi.set_PWM_dutycycle(pin, 0)
            try:
                real_range = int(pi.get_PWM_real_range(pin))
            except Exception:
                real_range = 0
            if real_range > 0 and real_range < LED_PWM_RANGE:
                logger.warning(
                    "LED CH%d PWM quantized on GPIO%d: requested %d Hz range=%d, actual %d Hz real_range=%d",
                    ch, pin, LED_PWM_FREQ, LED_PWM_RANGE, actual_freq, real_range,
                )
            else:
                logger.info(
                    "LED CH%d PWM on GPIO%d: requested %d Hz range=%d, actual %d Hz real_range=%d",
                    ch, pin, LED_PWM_FREQ, LED_PWM_RANGE, actual_freq, real_range,
                )

        logger.info("Optics initialized (3 LED channels)")
        return True

    def set_led_duty(self, channel: int, duty: int):
        """Set stored duty for a channel (0-255). Does not turn on."""
        if 0 <= channel < 3:
            self._duties[channel] = max(0, min(LED_PUBLIC_DUTY_MAX, duty))

    def get_led_duty(self, channel: int) -> int:
        if 0 <= channel < 3:
            return self._duties[channel]
        return 0

    def led_on(self, channel: int):
        """Turn on LED at stored duty cycle."""
        if 0 <= channel < 3 and self._pi and self._pi.connected:
            with self._lock:
                self._pi.set_PWM_dutycycle(
                    LED_PINS[channel], led_duty_to_pwm(self._duties[channel])
                )

    def led_on_duty(self, channel: int, duty: int):
        """Turn on LED at specific duty cycle."""
        if 0 <= channel < 3 and self._pi and self._pi.connected:
            self._duties[channel] = max(0, min(LED_PUBLIC_DUTY_MAX, duty))
            with self._lock:
                self._pi.set_PWM_dutycycle(
                    LED_PINS[channel], led_duty_to_pwm(self._duties[channel])
                )

    def led_off(self, channel: int):
        """Turn off a specific LED channel."""
        if 0 <= channel < 3 and self._pi and self._pi.connected:
            with self._lock:
                self._pi.set_PWM_dutycycle(LED_PINS[channel], 0)

    def all_off(self):
        """Turn off all measurement LEDs."""
        if self._pi and self._pi.connected:
            with self._lock:
                for ch in range(3):
                    self._pi.set_PWM_dutycycle(LED_PINS[ch], 0)


_MORSE_TABLE = {
    'a': '.-',    'b': '-...',  'c': '-.-.',  'd': '-..',
    'e': '.',     'f': '..-.',  'g': '--.',   'h': '....',
    'i': '..',    'j': '.---',  'k': '-.-',   'l': '.-..',
    'm': '--',    'n': '-.',    'o': '---',   'p': '.--.',
    'q': '--.-',  'r': '.-.',   's': '...',   't': '-',
    'u': '..-',   'v': '...-',  'w': '.--',   'x': '-..-',
    'y': '-.--',  'z': '--..',
}
_DOT_MS = 0.12   # 120 ms dot, matching ESP32


class StatusLed:
    """Status indicator LED with breathing, SOS, and Morse identify patterns."""

    def __init__(self):
        self._pi = None
        self._pin = STATUS_LED_PIN
        self._mode = "idle"  # "idle", "sampling", "error", "identify"
        self._stop = threading.Event()

    def init(self, pi):
        self._pi = pi
        if pi and pi.connected:
            pi.set_mode(self._pin, 1)  # OUTPUT

    def set_mode(self, mode: str):
        self._mode = mode

    def _effective_mode(self) -> str:
        """Return the active LED mode, deriving runtime state when available."""
        if self._mode == "identify":
            return self._mode
        try:
            from .state import state
            snap = state.snapshot()
            if int(snap.get("error", 0)) != 0:
                return "error"
            if bool(snap.get("sampling", False)):
                return "sampling"
        except Exception:
            pass
        return self._mode

    def start_identify(self):
        """Switch to identify mode (Morse blink for ~30s, then revert)."""
        if self._mode == "identify":
            return
        self._prev_mode = self._mode
        self._mode = "identify"

    def task(self, stop_event: threading.Event):
        """Background thread for LED animation."""
        self._stop = stop_event
        while not self._stop.is_set():
            try:
                mode = self._effective_mode()
                if mode == "identify":
                    self._identify_pattern()
                elif mode == "error":
                    self._sos_pattern()
                elif mode == "sampling":
                    self._breathe(period=1.3, step_ms=30)
                else:
                    self._breathe(period=4.0, step_ms=50)
            except Exception:
                time.sleep(0.1)

    def _identify_pattern(self):
        """Blink 'bcmeter' in Morse code for ~30 seconds, then revert."""
        if not self._pi or not self._pi.connected:
            time.sleep(0.1)
            return
        word = "bcmeter"
        start = time.monotonic()
        while time.monotonic() - start < 30 and self._mode == "identify":
            for ch in word:
                code = _MORSE_TABLE.get(ch, '')
                for symbol in code:
                    if self._mode != "identify" or self._stop.is_set():
                        break
                    self._pi.set_PWM_dutycycle(self._pin, 220)
                    time.sleep(_DOT_MS if symbol == '.' else _DOT_MS * 3)
                    self._pi.set_PWM_dutycycle(self._pin, 0)
                    time.sleep(_DOT_MS)  # intra-char gap
                if self._mode != "identify" or self._stop.is_set():
                    break
                time.sleep(_DOT_MS * 2)  # inter-char gap (1 dot already waited)
            if self._mode != "identify" or self._stop.is_set():
                break
            time.sleep(_DOT_MS * 4)  # word gap remainder
        self._pi.set_PWM_dutycycle(self._pin, 0)
        self._mode = getattr(self, '_prev_mode', 'idle')

    def _breathe(self, period=4.0, step_ms=50):
        """Breathing LED effect."""
        if not self._pi or not self._pi.connected:
            time.sleep(0.1)
            return
        t = time.time()
        phase = (t % period) / period * 2 * math.pi
        brightness = (math.exp(math.sin(phase)) - 0.368) * 108.0
        brightness = max(0, min(255, int(brightness)))
        try:
            self._pi.set_PWM_dutycycle(self._pin, brightness)
        except Exception:
            pass
        time.sleep(step_ms / 1000.0)

    def _sos_pattern(self):
        """SOS blink pattern for error state."""
        if not self._pi or not self._pi.connected:
            time.sleep(1.0)
            return
        pattern = [1, 1, 1, 0, 2, 2, 2, 0, 1, 1, 1]
        for p in pattern:
            if self._stop.is_set():
                return
            if p == 0:
                self._pi.set_PWM_dutycycle(self._pin, 0)
                time.sleep(0.5)
            else:
                self._pi.set_PWM_dutycycle(self._pin, 200)
                time.sleep(0.15 if p == 1 else 0.4)
                self._pi.set_PWM_dutycycle(self._pin, 0)
                time.sleep(0.15)
        time.sleep(1.0)
