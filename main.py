import sys
import os
import math
import time
import traceback
from dataclasses import dataclass

# Make Windows report physical desktop coordinates correctly when display scaling
# is enabled (e.g. 125% or 150%). This must run before querying screen dimensions.
if sys.platform == "win32":
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import cv2
import mediapipe as mp
import pyautogui

from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer, QSize, QRectF, QPointF, QUrl, QSettings
from PySide6.QtGui import (QImage, QPixmap, QFont, QPainter, QColor, QBrush, QLinearGradient,
                           QRadialGradient, QPen, QPainterPath, QIcon, QDesktopServices)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QComboBox,
    QSlider, QCheckBox, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame,
    QFormLayout, QTabWidget, QScrollArea, QSizePolicy, QMessageBox
)

APP_NAME = "GesturePilot"

# Вставьте сюда ссылку на свою страницу донатов (например, Boosty).
DONATION_URL = ""


def _normalise_camera_frame(frame):
    """Return a contiguous 8-bit BGR frame suitable for preview and MediaPipe."""
    if frame is None or getattr(frame, "size", 0) == 0:
        return None
    if frame.dtype.name != "uint8":
        frame = cv2.convertScaleAbs(frame)
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    elif frame.ndim == 3 and frame.shape[2] == 4:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    elif frame.ndim != 3 or frame.shape[2] != 3:
        return None
    return frame.copy(order="C")


def _open_camera_capture(index, width, height, fps=30):
    """Try Windows Media Foundation first, then DirectShow. Return (capture, backend).

    The Windows Camera app normally uses Media Foundation. Preferring MSMF avoids
    forcing a different DirectShow driver path, which can produce corrupted frames
    on some UVC cameras. Lower-resolution and MJPG fallbacks cover devices that do
    not support the requested mode. A short probe rejects streams that cannot read.
    """
    if sys.platform == "win32":
        backends = [(cv2.CAP_MSMF, "MSMF"), (cv2.CAP_DSHOW, "DSHOW")]
    else:
        backends = [(cv2.CAP_ANY, "AUTO")]

    sizes = [(int(width), int(height))]
    if (int(width), int(height)) != (640, 480):
        sizes.append((640, 480))

    attempts = []
    for backend, backend_name in backends:
        for w, h in sizes:
            attempts.append((backend, backend_name, w, h, None))
            # MJPG can be more reliable than raw YUY2 on bandwidth-limited USB cameras.
            if sys.platform == "win32" and backend == cv2.CAP_DSHOW:
                attempts.append((backend, backend_name, w, h, cv2.VideoWriter_fourcc(*"MJPG")))

    errors = []
    for backend, backend_name, w, h, fourcc in attempts:
        cap = None
        try:
            cap = cv2.VideoCapture(int(index), backend)
            if cap is None or not cap.isOpened():
                errors.append(f"{backend_name} {w}x{h}: не открылся")
                if cap is not None:
                    cap.release()
                continue
            if fourcc is not None:
                cap.set(cv2.CAP_PROP_FOURCC, fourcc)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, int(fps))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            # Warm up the driver and verify that it returns usable frames.
            good_frame = None
            for _ in range(4):
                ok, raw = cap.read()
                frame = _normalise_camera_frame(raw) if ok else None
                if frame is not None and frame.shape[0] >= 120 and frame.shape[1] >= 160:
                    good_frame = frame
                    break
                time.sleep(0.025)
            if good_frame is None:
                errors.append(f"{backend_name} {w}x{h}: кадры не читаются")
                cap.release()
                continue

            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or good_frame.shape[1])
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or good_frame.shape[0])
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            actual_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
            try:
                fourcc_text = "".join(chr((actual_fourcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")
            except Exception:
                fourcc_text = "unknown"
            info = f"{backend_name}, {actual_w}x{actual_h}, {actual_fps:.1f} FPS, {fourcc_text or 'формат по умолчанию'}"
            return cap, info
        except Exception as exc:
            errors.append(f"{backend_name} {w}x{h}: {str(exc)[:100]}")
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
    return None, "; ".join(errors[-6:])
pyautogui.PAUSE = 0.005
# PyAutoGUI's emergency corner failsafe remains enabled.
pyautogui.FAILSAFE = True


@dataclass
class Settings:
    camera_index: int = 0
    secondary_camera_index: int = 1
    camera_mode: str = "single"
    sensitivity: float = 1.0
    smoothing: float = 0.28
    show_landmarks: bool = True
    control_enabled: bool = False
    active_profile: str = "Universal"
    smart_dual: bool = False
    adaptive_smoothing: bool = True
    false_click_protection: bool = True
    window_actions_enabled: bool = False
    magnetic_cursor_enabled: bool = True
    magnetic_radius: int = 40
    magnetic_strength: int = 70
    magnetic_highlight: bool = True
    control_source: str = "primary"
    gesture_actions: dict = None


def distance(a, b):
    """3D landmark distance. Using MediaPipe's z coordinate helps when a hand turns."""
    dx = a.x - b.x
    dy = a.y - b.y
    dz = getattr(a, "z", 0.0) - getattr(b, "z", 0.0)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def palm_center(landmarks):
    # Average of wrist and the four palm knuckles; more stable than a fingertip.
    ids = (0, 5, 9, 13, 17)
    return (
        sum(landmarks[i].x for i in ids) / len(ids),
        sum(landmarks[i].y for i in ids) / len(ids),
    )


def pinch_ratio(landmarks, finger_tip):
    # Normalize pinch distance against palm size to work at different camera distances.
    palm = max(distance(landmarks[0], landmarks[9]), 0.001)
    return distance(landmarks[4], landmarks[finger_tip]) / palm


class HandWorker(QThread):
    preview_ready = Signal(QImage)
    status = Signal(str)
    hand_info = Signal(str)
    camera_list = Signal(list)
    error = Signal(str)
    magnetic_target = Signal(object)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self._running = True
        self._control = settings.control_enabled
        self._show_landmarks = settings.show_landmarks
        self._camera_index = settings.camera_index  # requested index; changed by the UI
        self._camera_mode = settings.camera_mode
        self._control_source = getattr(settings, "control_source", "primary")
        self._secondary_data = None
        self._depth_cue = 0.0
        self._active_camera_index = None
        self._camera_failed_index = None
        self._sensitivity = settings.sensitivity
        self._smoothing = settings.smoothing
        self._adaptive_smoothing = bool(getattr(settings, "adaptive_smoothing", True))
        self._profile = settings.active_profile
        self._last_cursor = None
        self._last_cursor_int = None
        self._left_down = False
        self._right_down = False
        # Press/release debouncing timers prevent transient landmark jumps from clicking.
        self._left_candidate_since = None
        self._left_release_since = None
        self._right_candidate_since = None
        self._right_release_since = None
        self._click_confirm_seconds = 0.10
        # A release must be continuously visible for this long. Turning the wrist
        # or one bad landmark frame should not cancel a held action.
        self._release_confirm_seconds = 0.14
        self._pinch_on_threshold = 0.30
        self._pinch_off_threshold = 0.49
        self._tracking_lost_since = None
        self._tracking_grace_seconds = 0.70
        self._zoom_candidate_since = None
        self._zoom_active = False
        self._zoom_lost_since = None
        self._last_zoom_distance = None
        self._last_zoom_time = 0.0
        self._scroll_candidate_since = None
        self._scroll_active = False
        self._scroll_last_y = None
        self._scroll_accumulator = 0.0
        self._last_status_emit = 0.0
        self._screen_w, self._screen_h = pyautogui.size()
        self._cap = None
        self._false_click_protection = bool(getattr(settings, "false_click_protection", True))
        self._window_actions_enabled = bool(getattr(settings, "window_actions_enabled", False))
        self._magnetic_enabled = bool(getattr(settings, "magnetic_cursor_enabled", True))
        self._magnetic_radius = int(getattr(settings, "magnetic_radius", 40))
        self._magnetic_strength = int(getattr(settings, "magnetic_strength", 70)) / 100.0
        self._magnetic_highlight = bool(getattr(settings, "magnetic_highlight", True))
        self._uia_cache = []
        self._uia_cache_at = 0.0
        self._magnetic_last_target = None
        self._gesture_actions = dict(getattr(settings, "gesture_actions", None) or {})
        self._gesture_candidate = None
        self._gesture_candidate_since = None
        self._gesture_latched = None
        self._gesture_cooldown_until = 0.0
        self._tracking_source = "primary"
        self._source_candidate = None
        self._source_candidate_since = 0.0
        self.set_false_click_protection(self._false_click_protection)

    def stop(self):
        self._running = False

    def set_control(self, enabled):
        self._control = enabled
        if not enabled:
            self._release_buttons()
            self._last_zoom_distance = None
            self._zoom_candidate_since = None
            self._zoom_active = False
            self._zoom_lost_since = None
            self._scroll_candidate_since = None
            self._scroll_active = False
            self._scroll_last_y = None
            self._scroll_accumulator = 0.0
            self._tracking_lost_since = None
        self.status.emit("Управление включено" if enabled else "Предпросмотр — управление выключено")

    def set_landmarks(self, enabled):
        self._show_landmarks = enabled

    def set_sensitivity(self, value):
        self._sensitivity = max(0.3, min(2.5, value))

    def set_smoothing(self, value):
        self._smoothing = max(0.05, min(0.95, value))

    def set_adaptive_smoothing(self, enabled):
        self._adaptive_smoothing = bool(enabled)

    def set_false_click_protection(self, enabled):
        self._false_click_protection = bool(enabled)
        self._click_confirm_seconds = 0.075 if enabled else 0.045
        # A short release debounce allows two quick pinches to produce two
        # separate mouse clicks instead of keeping the button held between them.
        self._release_confirm_seconds = 0.065 if enabled else 0.045
        self._pinch_on_threshold = 0.30 if enabled else 0.335
        if not enabled:
            self._release_buttons()

    def set_magnetic_settings(self, enabled=None, radius=None, strength=None, highlight=None):
        if enabled is not None:
            self._magnetic_enabled = bool(enabled)
        if radius is not None:
            self._magnetic_radius = max(10, min(250, int(radius)))
        if strength is not None:
            self._magnetic_strength = max(0.1, min(1.0, int(strength) / 100.0))
        if highlight is not None:
            self._magnetic_highlight = bool(highlight)
        if not self._magnetic_enabled:
            self._magnetic_last_target = None
            self.magnetic_target.emit(None)

    def _get_uia_targets(self, now):
        """Get cached clickable UI Automation rectangles. Optional pywinauto dependency."""
        if sys.platform != "win32":
            return []
        if now - self._uia_cache_at < 0.50:
            return self._uia_cache
        self._uia_cache_at = now
        targets = []
        try:
            from pywinauto import Desktop
            # Enumerate only visible, enabled, interactive controls; never invoke them.
            desktop = Desktop(backend="uia")
            for win in desktop.windows(visible_only=True, enabled_only=True):
                try:
                    rect = win.rectangle()
                    if rect.width() < 2 or rect.height() < 2:
                        continue
                    for el in win.descendants():
                        try:
                            info = el.element_info
                            if not info.enabled or not info.visible:
                                continue
                            ctype = str(getattr(info, "control_type", ""))
                            if ctype not in {"Button", "SplitButton", "MenuItem", "TabItem", "ListItem", "CheckBox", "RadioButton", "Hyperlink", "TitleBar"}:
                                continue
                            r = el.rectangle()
                            w, h = r.width(), r.height()
                            if w < 5 or h < 5 or w > 600 or h > 180:
                                continue
                            # Exclude this app's own window to avoid attracting to its settings.
                            title = str(getattr(win.element_info, "name", ""))
                            if "GesturePilot" in title:
                                continue
                            targets.append((int(r.left), int(r.top), int(r.right), int(r.bottom), ctype))
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception:
            # UIA is best-effort: if unavailable, cursor control keeps working normally.
            targets = []
        self._uia_cache = targets[:900]
        return self._uia_cache

    def _magnet_target(self, x, y, now):
        if not self._magnetic_enabled or self._left_down or self._right_down:
            if self._magnetic_last_target is not None:
                self._magnetic_last_target = None
                self.magnetic_target.emit(None)
            return (x, y)
        candidates = self._get_uia_targets(now)
        nearest = None
        nearest_d = float(self._magnetic_radius) + 1.0
        for left, top, right, bottom, ctype in candidates:
            # Closest point on the control rectangle, rather than always its center.
            px = min(max(x, left), right)
            py = min(max(y, top), bottom)
            d = math.hypot(px - x, py - y)
            if d < nearest_d:
                nearest_d, nearest = d, (left, top, right, bottom, px, py, ctype)
        if nearest is None:
            if self._magnetic_last_target is not None:
                self._magnetic_last_target = None
                self.magnetic_target.emit(None)
            return (x, y)
        left, top, right, bottom, px, py, ctype = nearest
        # Smoothly blend toward the closest point on the target. No click is issued.
        amount = self._magnetic_strength * (1.0 - nearest_d / max(1.0, self._magnetic_radius))
        result = (x + (px - x) * amount, y + (py - y) * amount)
        key = (left, top, right, bottom, ctype)
        if key != self._magnetic_last_target:
            self._magnetic_last_target = key
            self.magnetic_target.emit((left, top, right-left, bottom-top) if self._magnetic_highlight else None)
        return result

    def set_window_actions_enabled(self, enabled):
        self._window_actions_enabled = bool(enabled)
        self._gesture_candidate = None
        self._gesture_candidate_since = None
        self._gesture_latched = None

    def set_gesture_actions(self, mapping):
        self._gesture_actions = dict(mapping or {})
        self._gesture_candidate = None
        self._gesture_candidate_since = None
        self._gesture_latched = None

    def set_profile(self, profile):
        self._profile = profile
        self._last_zoom_distance = None
        self.status.emit(f"Профиль: {profile}")

    def set_camera_mode(self, mode):
        self._camera_mode = "dual" if mode == "dual" else "single"
        if self._camera_mode == "single":
            self._control_source = "primary"
            self._tracking_source = "primary"
        self._last_cursor = None
        self._last_cursor_int = None
        self._release_buttons()
        self.status.emit("Две камеры: ручной выбор источника" if self._camera_mode == "dual" else "Одна камера: основной трекинг")

    def set_control_source(self, source):
        source = "auxiliary" if source == "auxiliary" else "primary"
        self._control_source = source
        self._tracking_source = source
        self._last_cursor = None
        self._last_cursor_int = None
        self._release_buttons()
        self.status.emit("Курсором управляет боковая камера" if source == "auxiliary" else "Курсором управляет фронтальная камера")

    def set_secondary_data(self, data):
        self._secondary_data = data
        # Side view is used only as a depth/stability cue when front camera drives
        # the cursor. It never supplies cursor coordinates in this mode.
        if self._control_source == "primary" and data and data.get("hands"):
            hands = data.get("hands", {})
            hand = hands.get("Right") or next(iter(hands.values()), None)
            if hand and len(hand) > 17:
                palm_width = math.hypot(hand[5].x - hand[17].x, hand[5].y - hand[17].y)
                # Normalized and clamped cue: larger side-view palm generally means
                # the hand is nearer. This is a relative cue, not calibrated 3D depth.
                self._depth_cue = max(0.0, min(1.0, (palm_width - 0.08) / 0.22))
        else:
            self._depth_cue = 0.0

    def set_camera(self, index):
        # Never touch VideoCapture from the GUI thread. The worker owns the camera
        # and will reopen it safely at the top of its next loop iteration.
        self._camera_index = int(index)

    def _release_buttons(self):
        try:
            if self._left_down:
                pyautogui.mouseUp(button="left")
            if self._right_down:
                pyautogui.mouseUp(button="right")
        except Exception:
            pass
        self._left_down = False
        self._right_down = False
        self._left_candidate_since = None
        self._left_release_since = None
        self._right_candidate_since = None
        self._right_release_since = None

    def _open_camera(self, index=None):
        """Open one camera in the worker thread; never retry a failed index forever."""
        if index is None:
            index = self._camera_index
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._active_camera_index = int(index)
        self.status.emit(f"Подключение к камере {index}…")
        try:
            cap, capture_info = _open_camera_capture(index, 1280, 720, 30)
            if cap is None:
                self._cap = None
                self._camera_failed_index = int(index)
                self.status.emit(f"Камера {index} недоступна: {capture_info}. Выберите другой источник.")
                return False
            self._cap = cap
            self._camera_failed_index = None
            self.status.emit(f"Камера {index} подключена — {capture_info}")
            return True
        except Exception as exc:
            self._cap = None
            self._camera_failed_index = int(index)
            self.status.emit(f"Не удалось открыть камеру {index}: {exc}")
            return False

    def _move_cursor(self, cx, cy, frame_w, frame_h):
        # The frame is mirrored once for a selfie-style preview. MediaPipe x is
        # already in that mirrored frame, so no second inversion is applied.
        nx = cx
        ny = cy

        # Map the central 70% of the camera image to the full desktop.
        active_min = 0.15
        active_max = 0.85
        active_span = active_max - active_min
        nx = max(0.0, min(1.0, (nx - active_min) / active_span))
        ny = max(0.0, min(1.0, (ny - active_min) / active_span))
        nx = max(0.0, min(1.0, 0.5 + (nx - 0.5) * self._sensitivity))
        ny = max(0.0, min(1.0, 0.5 + (ny - 0.5) * self._sensitivity))
        target = (nx * (self._screen_w - 1), ny * (self._screen_h - 1))
        # Magnetic assistance is deliberately applied only to pointer movement.
        # It never clicks/activates controls and is disabled while a mouse button is held.
        if self._control and self._magnetic_enabled:
            mx, my = self._magnet_target(int(target[0]), int(target[1]), time.monotonic())
            target = (mx, my)
        elif self._magnetic_last_target is not None:
            self._magnetic_last_target = None
            self.magnetic_target.emit(None)

        if self._last_cursor is None:
            self._last_cursor = target
            self._last_cursor_int = (int(target[0]), int(target[1]))
            pyautogui.moveTo(*self._last_cursor_int)
            return

        dx = target[0] - self._last_cursor[0]
        dy = target[1] - self._last_cursor[1]
        movement = math.hypot(dx, dy)

        # Screen-space dead zone: small camera landmark tremors do not move the
        # pointer. The target must pass the dead zone before the cursor follows.
        dead_zone_px = max(6.0, min(10.0, min(self._screen_w, self._screen_h) * 0.006))
        if movement <= dead_zone_px:
            return
        effective_movement = movement - dead_zone_px
        adjusted_target = (
            self._last_cursor[0] + dx * (effective_movement / movement),
            self._last_cursor[1] + dy * (effective_movement / movement),
        )

        # Smooth small movements strongly; boost responsiveness on fast deliberate moves.
        base_alpha = max(0.12, min(0.70, 0.70 - self._smoothing * 0.58))
        speed_boost = min(0.25, movement / 1200.0) if self._adaptive_smoothing else 0.0
        # The side camera adjusts responsiveness only; it never pulls the pointer
        # toward its own image coordinates. Keep this influence intentionally mild.
        depth_boost = (self._depth_cue * 0.06) if self._camera_mode == "dual" and self._control_source == "primary" else 0.0
        alpha = min(0.88, base_alpha + speed_boost + depth_boost)
        self._last_cursor = (
            self._last_cursor[0] + (adjusted_target[0] - self._last_cursor[0]) * alpha,
            self._last_cursor[1] + (adjusted_target[1] - self._last_cursor[1]) * alpha,
        )
        point = (int(round(self._last_cursor[0])), int(round(self._last_cursor[1])))
        if point != self._last_cursor_int:
            pyautogui.moveTo(point[0], point[1])
            self._last_cursor_int = point

    def _process_zoom(self, hands_by_label, hand_scores=None):
        """Debounced two-hand pinch zoom; suppresses single-hand clicks while arming."""
        hand_scores = hand_scores or {}
        if "Left" not in hands_by_label or "Right" not in hands_by_label:
            # If a two-hand zoom was already active, don't cancel it on a single
            # missed frame. Keep the gesture latched during brief tracking loss.
            if self._zoom_active:
                now = time.monotonic()
                if self._zoom_lost_since is None:
                    self._zoom_lost_since = now
                self._last_zoom_distance = None
                if now - self._zoom_lost_since < self._tracking_grace_seconds:
                    return True
            self._zoom_candidate_since = None
            self._zoom_active = False
            self._zoom_lost_since = None
            self._last_zoom_distance = None
            return False
        if min(hand_scores.get("Left", 1.0), hand_scores.get("Right", 1.0)) < 0.60:
            if self._zoom_active:
                now = time.monotonic()
                if self._zoom_lost_since is None:
                    self._zoom_lost_since = now
                self._last_zoom_distance = None
                if now - self._zoom_lost_since < self._tracking_grace_seconds:
                    return True
            self._zoom_candidate_since = None
            self._zoom_active = False
            self._zoom_lost_since = None
            self._last_zoom_distance = None
            return False
        self._zoom_lost_since = None

        left = hands_by_label["Left"]
        right = hands_by_label["Right"]
        left_ratio = pinch_ratio(left, 8)
        right_ratio = pinch_ratio(right, 8)
        now = time.monotonic()
        both_near = left_ratio < 0.34 and right_ratio < 0.34
        both_pinched = left_ratio < 0.29 and right_ratio < 0.29

        if self._zoom_active:
            # Keep zoom active until both pinches have been clearly released
            # for a sustained interval; transient wrist rotations are ignored.
            if left_ratio > self._pinch_off_threshold and right_ratio > self._pinch_off_threshold:
                if self._zoom_candidate_since is None:
                    self._zoom_candidate_since = now
                elif now - self._zoom_candidate_since >= self._release_confirm_seconds:
                    self._zoom_active = False
                    self._zoom_candidate_since = None
                    self._last_zoom_distance = None
                    return False
            else:
                self._zoom_candidate_since = None
        else:
            if not both_near:
                self._zoom_candidate_since = None
                self._last_zoom_distance = None
                return False
            if both_pinched:
                if self._zoom_candidate_since is None:
                    self._zoom_candidate_since = now
                elif now - self._zoom_candidate_since >= 0.14:
                    self._zoom_active = True
                    self._last_zoom_distance = None
            else:
                self._zoom_candidate_since = None
            # Reserve this two-hand pinch while it is being confirmed. This keeps
            # it from turning into an accidental left click on the right hand.
            if not self._zoom_active:
                return True

        lc = palm_center(left)
        rc = palm_center(right)
        current = math.hypot(lc[0] - rc[0], lc[1] - rc[1])
        if self._last_zoom_distance is not None and now - self._last_zoom_time >= 0.045:
            delta = current - self._last_zoom_distance
            if abs(delta) > 0.008:
                amount = int(max(-6, min(6, delta * 220)))
                if amount:
                    pyautogui.scroll(amount)
                self._last_zoom_time = now
        self._last_zoom_distance = current
        return True

    def _process_two_finger_scroll(self, hands_by_label, hand_scores=None):
        """Scroll with index+middle fingers extended; vertical hand motion is inverted."""
        scores = hand_scores or {}
        hand = hands_by_label.get("Right")
        if hand is None or scores.get("Right", 1.0) < 0.54:
            self._scroll_candidate_since = None
            self._scroll_active = False
            self._scroll_last_y = None
            self._scroll_accumulator = 0.0
            return False

        # MediaPipe y grows downward. Index/middle must be straight; ring/pinky folded.
        def extended(tip, pip):
            return hand[tip].y < hand[pip].y - 0.025
        two_fingers = (extended(8, 6) and extended(12, 10)
                       and not extended(16, 14) and not extended(20, 18))
        now = time.monotonic()
        if not two_fingers:
            self._scroll_candidate_since = None
            self._scroll_active = False
            self._scroll_last_y = None
            self._scroll_accumulator = 0.0
            return False

        if not self._scroll_active:
            if self._scroll_candidate_since is None:
                self._scroll_candidate_since = now
                return True  # reserve the pose while it is being confirmed
            if now - self._scroll_candidate_since < 0.18:
                return True
            self._scroll_active = True
            self._scroll_last_y = palm_center(hand)[1]
            self._scroll_accumulator = 0.0
            return True

        current_y = palm_center(hand)[1]
        if self._scroll_last_y is not None:
            delta_y = current_y - self._scroll_last_y
            # Ignore tiny camera jitter. Moving the hand up (delta_y < 0)
            # intentionally scrolls DOWN; moving it down scrolls UP.
            if abs(delta_y) >= 0.004:
                self._scroll_accumulator += delta_y * 180.0
                steps = int(self._scroll_accumulator)
                if steps:
                    pyautogui.scroll(steps)
                    self._scroll_accumulator -= steps
        self._scroll_last_y = current_y
        return True

    def _update_mouse_button(self, which, candidate, ratio, now):
        """Require a stable pinch before pressing and a stable release before lifting."""
        if which == "left":
            down_attr = "_left_down"
            candidate_attr = "_left_candidate_since"
            release_attr = "_left_release_since"
        else:
            down_attr = "_right_down"
            candidate_attr = "_right_candidate_since"
            release_attr = "_right_release_since"

        is_down = getattr(self, down_attr)
        if not is_down:
            setattr(self, release_attr, None)
            if not candidate:
                setattr(self, candidate_attr, None)
                return
            started = getattr(self, candidate_attr)
            if started is None:
                setattr(self, candidate_attr, now)
                return
            if now - started >= self._click_confirm_seconds:
                # Never allow two mouse buttons to be pressed by the pinch system at once.
                other_down = self._right_down if which == "left" else self._left_down
                if not other_down:
                    pyautogui.mouseDown(button=which)
                    setattr(self, down_attr, True)
                setattr(self, candidate_attr, None)
            return

        # Once pressed, ignore brief tracking/pose glitches; require a sustained
        # opening of the thumb-finger gap to release the mouse button.
        setattr(self, candidate_attr, None)
        if ratio > self._pinch_off_threshold:
            started = getattr(self, release_attr)
            if started is None:
                setattr(self, release_attr, now)
            elif now - started >= self._release_confirm_seconds:
                pyautogui.mouseUp(button=which)
                setattr(self, down_attr, False)
                setattr(self, release_attr, None)
        else:
            setattr(self, release_attr, None)

    @staticmethod
    def _recognise_pose(landmarks):
        """Coarse, deliberately conservative whole-hand pose recognition."""
        if not landmarks or len(landmarks) < 21:
            return None
        # A finger is considered extended only when the tip is clearly above its
        # proximal joint. Require margin to reduce pose changes from jitter.
        extended = [landmarks[tip].y < landmarks[joint].y - 0.025
                    for tip, joint in ((8, 6), (12, 10), (16, 14), (20, 18))]
        count = sum(extended)
        if count >= 4:
            return "Открытая ладонь"
        if count == 0:
            return "Кулак"
        if extended == [True, True, False, False]:
            return "V-жест"
        if extended == [True, False, False, False]:
            return "Указательный палец"
        if extended == [True, True, True, False]:
            return "Три пальца"
        return None

    @staticmethod
    def _send_window_action(action):
        """Execute an explicit user-mapped Windows shortcut; no action by default."""
        shortcuts = {
            "Показать рабочий стол (Win+D)": ("win", "d"),
            "Свернуть все окна (Win+M)": ("win", "m"),
            "Обзор окон — выбрать вручную (Win+Tab)": ("win", "tab"),
            "Переключить окно (Alt+Tab)": ("alt", "tab"),
            "Закрепить окно слева": ("win", "left"),
            "Закрепить окно справа": ("win", "right"),
            "Развернуть окно": ("win", "up"),
            "Свернуть активное окно (Win+Down)": ("win", "down"),
            "Escape": ("esc",),
            "Диспетчер задач": ("ctrl", "shift", "esc"),
        }
        keys = shortcuts.get(action)
        if keys:
            pyautogui.hotkey(*keys)

    def _process_mapped_gesture(self, hands_by_label, hand_scores, now):
        if not self._control or not self._window_actions_enabled:
            self._gesture_candidate = None
            self._gesture_candidate_since = None
            self._gesture_latched = None
            return
        right = hands_by_label.get("Right")
        if right is None or (hand_scores or {}).get("Right", 1.0) < (0.66 if self._false_click_protection else 0.58):
            self._gesture_candidate = None
            self._gesture_candidate_since = None
            self._gesture_latched = None
            return
        pose = self._recognise_pose(right)
        action = self._gesture_actions.get(pose or "", "Нет действия")
        if not pose or not action or action == "Нет действия":
            self._gesture_candidate = None
            self._gesture_candidate_since = None
            self._gesture_latched = None
            return
        if pose != self._gesture_candidate:
            self._gesture_candidate = pose
            self._gesture_candidate_since = now
            return
        if self._gesture_latched == pose or now < self._gesture_cooldown_until:
            return
        if self._gesture_candidate_since is not None and now - self._gesture_candidate_since >= 0.48:
            try:
                self._release_buttons()
                self._send_window_action(action)
                self.status.emit(f"Жест «{pose}»: {action}")
                self._gesture_latched = pose
                self._gesture_cooldown_until = now + 1.1
            except Exception as exc:
                self.status.emit(f"Не удалось выполнить действие жеста: {str(exc)[:120]}")

    def _choose_smart_source(self, primary_hands, primary_scores, aux, wall_now):
        """Prefer the primary view, borrow the auxiliary view when needed, and
        require a short stable decision before switching to avoid camera flicker."""
        aux_fresh = bool(aux and wall_now - aux.get("time", 0) < 0.28 and aux.get("hands"))
        aux_hands = aux.get("hands", {}) if aux_fresh else {}
        aux_scores = aux.get("scores", {}) if aux_fresh else {}
        pscore = max(primary_scores.values(), default=0.0) if primary_hands else 0.0
        ascore = max(aux_scores.values(), default=0.0) if aux_hands else 0.0
        primary_ok = bool(primary_hands and pscore >= 0.55)
        aux_ok = bool(aux_hands and ascore >= 0.55)
        # The app's cursor/click anchor is the user's right hand. If the main
        # camera sees only the left hand while the auxiliary sees the right one,
        # the latter is the useful control source even though both streams detect hands.
        primary_right = bool(primary_hands.get("Right") is not None and primary_scores.get("Right", 0.0) >= 0.55)
        aux_right = bool(aux_hands.get("Right") is not None and aux_scores.get("Right", 0.0) >= 0.55)

        desired = self._tracking_source
        if self._tracking_source == "primary":
            if aux_right and not primary_right:
                desired = "auxiliary"
            elif primary_right:
                desired = "primary"
            elif not primary_ok and aux_ok:
                desired = "auxiliary"
            else:
                desired = "primary"
        else:
            if primary_right:
                # Return to the primary source when its right-hand track recovers;
                # the switch hysteresis below filters brief detection flicker.
                desired = "primary"
            elif aux_right:
                desired = "auxiliary"
            elif not aux_ok and primary_ok:
                desired = "primary"
            elif aux_ok:
                desired = "auxiliary"
            else:
                desired = "primary" if primary_hands else "auxiliary"

        if desired == self._tracking_source:
            self._source_candidate = None
            self._source_candidate_since = 0.0
        else:
            if self._source_candidate != desired:
                self._source_candidate = desired
                self._source_candidate_since = time.monotonic()
            elif time.monotonic() - self._source_candidate_since >= 0.24:
                self._tracking_source = desired
                self._source_candidate = None
                self._source_candidate_since = 0.0
                self._last_zoom_distance = None
                self._zoom_candidate_since = None
                self.status.emit("Умный трекинг: " + ("основная камера" if desired == "primary" else "резервная камера"))

        if self._tracking_source == "auxiliary" and aux_ok:
            return aux_hands, aux_scores, aux.get("shape", (480, 640, 3)), True
        return primary_hands, primary_scores, None, False

    def _handle_hand_controls(self, hands_by_label, frame_shape, hand_scores=None):
        if not self._control:
            return
        hand_scores = hand_scores or {}
        # Zoom and scrolling have priority over mapped window shortcuts so a V-pose
        # used for the wheel cannot accidentally trigger a user-mapped V action.
        # Zoom takes precedence and is debounced before any mouse click is permitted.
        if self._process_zoom(hands_by_label, hand_scores):
            self._release_buttons()
            return
        self._last_zoom_distance = None

        # Two-finger vertical movement controls the wheel and temporarily parks the cursor.
        if self._process_two_finger_scroll(hands_by_label, hand_scores):
            self._process_mapped_gesture({}, {}, time.monotonic())
            self._release_buttons()
            return

        self._process_mapped_gesture(hands_by_label, hand_scores, time.monotonic())
        right = hands_by_label.get("Right")
        if right is None:
            # Don't release a held mouse button just because MediaPipe briefly
            # lost the hand while the user turns the wrist. Release only after
            # tracking has been absent for a sustained grace period.
            now = time.monotonic()
            if self._tracking_lost_since is None:
                self._tracking_lost_since = now
            self._last_zoom_distance = None
            if now - self._tracking_lost_since >= self._tracking_grace_seconds:
                self._release_buttons()
                self._left_candidate_since = None
                self._right_candidate_since = None
            return
        self._tracking_lost_since = None

        # Track the midpoint between the base joints of the right index finger
        # and middle finger (MediaPipe landmarks 5 and 9). These are the two
        # points at the beginning of the fingers near the palm, as clarified by
        # the user; do not use the fingertips (landmarks 8 and 12) as the cursor anchor.
        index_base = right[5]
        middle_base = right[9]
        cx = (index_base.x + middle_base.x) / 2.0
        cy = (index_base.y + middle_base.y) / 2.0
        self._move_cursor(cx, cy, frame_shape[1], frame_shape[0])

        index_ratio = pinch_ratio(right, 8)
        middle_ratio = pinch_ratio(right, 12)
        # Don't arm clicks when MediaPipe is uncertain which hand it is tracking.
        right_hand_confident = hand_scores.get("Right", 1.0) >= (0.60 if self._false_click_protection else 0.54)

        # Distinguish the intended finger from a folded/overlapping neighbouring finger.
        # If both tips are similarly close to the thumb, treat it as ambiguous, not a click.
        left_candidate = (
            right_hand_confident
            and index_ratio < self._pinch_on_threshold
            and middle_ratio > index_ratio + 0.035
        )
        right_candidate = (
            right_hand_confident
            and middle_ratio < self._pinch_on_threshold
            and index_ratio > middle_ratio + 0.035
        )

        now = time.monotonic()
        if self._left_down:
            self._update_mouse_button("left", False, index_ratio, now)
            self._update_mouse_button("right", False, middle_ratio, now)
        elif self._right_down:
            self._update_mouse_button("right", False, middle_ratio, now)
            self._update_mouse_button("left", False, index_ratio, now)
        elif left_candidate:
            self._update_mouse_button("left", True, index_ratio, now)
            self._update_mouse_button("right", False, middle_ratio, now)
        elif right_candidate:
            self._update_mouse_button("right", True, middle_ratio, now)
            self._update_mouse_button("left", False, index_ratio, now)
        else:
            self._update_mouse_button("left", False, index_ratio, now)
            self._update_mouse_button("right", False, middle_ratio, now)

    def run(self):
        try:
            mp_hands = mp.solutions.hands
            mp_draw = mp.solutions.drawing_utils
            self._open_camera(self._camera_index)
            with mp_hands.Hands(
                static_image_mode=False,
                max_num_hands=2,
                model_complexity=1,
                min_detection_confidence=0.58,
                min_tracking_confidence=0.62,
            ) as hands:
                while self._running:
                    # Camera switching and release happen only in this worker thread.
                    if self._camera_index != self._active_camera_index:
                        self._release_buttons()
                        self._last_cursor = None
                        self._last_cursor_int = None
                        self._zoom_active = False
                        self._zoom_candidate_since = None
                        self._last_zoom_distance = None
                        self._open_camera(self._camera_index)

                    if self._cap is None:
                        # Stay responsive while waiting for the user to choose another
                        # camera; don't flood the UI with repeated errors/popups.
                        time.sleep(0.08)
                        continue
                    try:
                        ok, frame = self._cap.read()
                    except Exception as exc:
                        self.status.emit(f"Ошибка чтения камеры: {exc}")
                        try:
                            self._cap.release()
                        except Exception:
                            pass
                        self._cap = None
                        time.sleep(0.08)
                        continue
                    if not ok or frame is None:
                        if self._camera_failed_index != self._active_camera_index:
                            self._camera_failed_index = self._active_camera_index
                            self.status.emit(f"Нет изображения с камеры {self._active_camera_index}. Выберите другой источник.")
                        time.sleep(0.08)
                        continue
                    elif self._camera_failed_index == self._active_camera_index:
                        self._camera_failed_index = None
                        self.status.emit(f"Камера {self._active_camera_index} снова передаёт изображение")

                    frame = _normalise_camera_frame(frame)
                    if frame is None:
                        self.status.emit("Камера вернула кадр неизвестного формата; повторное чтение…")
                        time.sleep(0.03)
                        continue
                    frame = cv2.flip(frame, 1)
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    result = hands.process(rgb)
                    hands_by_label = {}
                    hand_scores = {}
                    labels = []
                    if result.multi_hand_landmarks:
                        for i, hand_lms in enumerate(result.multi_hand_landmarks):
                            label = "Unknown"
                            score = 0.0
                            if result.multi_handedness and i < len(result.multi_handedness):
                                classification = result.multi_handedness[i].classification[0]
                                label = classification.label
                                score = float(classification.score)
                            hands_by_label[label] = hand_lms.landmark
                            hand_scores[label] = score
                            labels.append("Правая" if label == "Right" else "Левая" if label == "Left" else "Рука")
                            if self._show_landmarks:
                                mp_draw.draw_landmarks(frame, hand_lms, mp_hands.HAND_CONNECTIONS)

                    # Manual source selection prevents the two different camera
                    # viewpoints from fighting over cursor coordinates.
                    source_shape = frame.shape
                    if self._camera_mode == "dual" and self._control_source == "auxiliary":
                        aux = self._secondary_data
                        fresh = bool(aux and time.time() - aux.get("time", 0) < 0.30)
                        if fresh and aux.get("hands"):
                            hands_by_label = aux.get("hands", {})
                            hand_scores = aux.get("scores", {})
                            source_shape = aux.get("shape") or frame.shape
                            labels = ["Правая (боковая)" if x == "Right" else "Левая (боковая)" for x in hands_by_label]
                        else:
                            # Do not silently fall back to the front camera: that
                            # would create the exact unpredictable source jump being fixed.
                            hands_by_label, hand_scores, labels = {}, {}, []
                    else:
                        self._tracking_source = "primary"
                        self._source_candidate = None
                    if self._control:
                        try:
                            self._handle_hand_controls(hands_by_label, source_shape, hand_scores)
                        except pyautogui.FailSafeException:
                            self._control = False
                            self._release_buttons()
                            self.status.emit("Аварийная остановка: курсор в верхнем левом углу")
                        except Exception as exc:
                            self.status.emit(f"Ошибка управления: {exc}")

                    self.hand_info.emit("Обнаружено: " + (", ".join(labels) if labels else "руки не найдены"))
                    if time.time() - self._last_status_emit > 1.0:
                        self._last_status_emit = time.time()
                        self.status.emit("Управление активно" if self._control else "Предпросмотр — управление выключено")

                    # Overlay labels and current mode.
                    cv2.putText(frame, "GesturePilot", (18, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (235, 240, 255), 2)
                    mode_text = "CONTROL ON" if self._control else "PREVIEW ONLY"
                    cv2.putText(frame, mode_text, (18, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.58,
                                (80, 230, 150) if self._control else (190, 190, 205), 2)
                    if "Right" in hands_by_label:
                        ir = pinch_ratio(hands_by_label["Right"], 8)
                        mr = pinch_ratio(hands_by_label["Right"], 12)
                        debug_text = f"Index pinch: {ir:.2f}   Middle pinch: {mr:.2f}"
                        cv2.putText(frame, debug_text, (18, 88), cv2.FONT_HERSHEY_SIMPLEX, 0.48,
                                    (200, 210, 230), 1, cv2.LINE_AA)
                    h, w, ch = frame.shape
                    image = QImage(frame.data, w, h, ch * w, QImage.Format.Format_BGR888).copy()
                    self.preview_ready.emit(image)
                    self.msleep(1)
        except Exception as exc:
            self.error.emit(f"{exc}\n\n{traceback.format_exc()}")
        finally:
            self._release_buttons()
            if self._cap is not None:
                self._cap.release()


class AuxiliaryCameraWorker(QThread):
    """Independent secondary-camera tracker. It never controls the mouse directly."""
    preview_ready = Signal(QImage)
    status = Signal(str)
    data_ready = Signal(object)

    def __init__(self, camera_index=1, show_landmarks=True):
        super().__init__()
        self._running = True
        self._camera_index = int(camera_index)
        self._requested_index = int(camera_index)
        self._show_landmarks = show_landmarks
        self._enabled = False

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)

    def set_camera(self, index):
        self._requested_index = int(index)

    def set_landmarks(self, enabled):
        self._show_landmarks = bool(enabled)

    def stop(self):
        self._running = False

    def run(self):
        cap = None
        active = None
        try:
            mp_hands = mp.solutions.hands
            mp_draw = mp.solutions.drawing_utils
            with mp_hands.Hands(static_image_mode=False, max_num_hands=2,
                    model_complexity=0, min_detection_confidence=0.55,
                    min_tracking_confidence=0.58) as hands:
                while self._running:
                    if not self._enabled:
                        if cap is not None:
                            cap.release(); cap = None; active = None
                        self.msleep(100)
                        continue
                    if active != self._requested_index:
                        if cap is not None:
                            cap.release()
                            cap = None
                        active = self._requested_index
                        try:
                            cap, capture_info = _open_camera_capture(active, 640, 480, 30)
                            if cap is not None:
                                self.status.emit(f"Доп. камера {active} подключена — {capture_info}")
                            else:
                                self.status.emit(f"Доп. камера {active} недоступна: {capture_info}")
                        except Exception as exc:
                            cap = None
                            self.status.emit(f"Доп. камера: {str(exc)[:140]}")
                    if cap is None:
                        self.msleep(250)
                        continue
                    try:
                        ok, frame = cap.read()
                    except Exception as exc:
                        self.status.emit(f"Ошибка дополнительной камеры: {str(exc)[:140]}")
                        cap.release(); cap = None
                        self.msleep(200)
                        continue
                    if not ok or frame is None:
                        self.msleep(80)
                        continue
                    frame = _normalise_camera_frame(frame)
                    if frame is None:
                        self.msleep(30)
                        continue
                    frame = cv2.flip(frame, 1)
                    result = hands.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    by_label, scores, labels = {}, {}, []
                    if result.multi_hand_landmarks:
                        for i, lms in enumerate(result.multi_hand_landmarks):
                            label, score = "Unknown", 0.0
                            if result.multi_handedness and i < len(result.multi_handedness):
                                c = result.multi_handedness[i].classification[0]
                                label, score = c.label, float(c.score)
                            by_label[label] = lms.landmark
                            scores[label] = score
                            labels.append(label)
                            if self._show_landmarks:
                                mp_draw.draw_landmarks(frame, lms, mp_hands.HAND_CONNECTIONS)
                    # The primary worker only borrows this result if its own camera loses the hand.
                    self.data_ready.emit({"hands": by_label, "scores": scores,
                                          "shape": frame.shape, "time": time.time(),
                                          "labels": labels})
                    cv2.putText(frame, "AUXILIARY CAMERA", (14, 27), cv2.FONT_HERSHEY_SIMPLEX,
                                0.55, (220, 225, 238), 2, cv2.LINE_AA)
                    h, w, ch = frame.shape
                    self.preview_ready.emit(QImage(frame.data, w, h, ch*w,
                        QImage.Format.Format_BGR888).copy())
                    self.msleep(1)
        except Exception as exc:
            self.status.emit(f"Доп. камера остановлена: {str(exc)[:160]}")
        finally:
            if cap is not None:
                try: cap.release()
                except Exception: pass


class GlassPanel(QFrame):
    """Animated translucent panel with a soft specular sweep and glass rim."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("glassPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(65)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def _advance(self):
        self._phase = (self._phase + 0.012) % 1.4
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.7, 0.7, -0.7, -0.7)
        path = QPainterPath()
        path.addRoundedRect(rect, 20, 20)
        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor(92, 102, 122, 124))
        base.setColorAt(0.22, QColor(54, 61, 76, 158))
        base.setColorAt(0.62, QColor(32, 37, 49, 178))
        base.setColorAt(1.0, QColor(19, 23, 32, 190))
        p.fillPath(path, QBrush(base))
        p.save()
        p.setClipPath(path)
        x = rect.left() + self._phase * (rect.width() + 240) - 120
        sheen_rect = QRectF(x, rect.top() - 70, 115, rect.height() + 140)
        sheen = QLinearGradient(sheen_rect.topLeft(), sheen_rect.topRight())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.46, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.62, QColor(225, 237, 255, 17))
        sheen.setColorAt(0.74, QColor(255, 255, 255, 35))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(sheen_rect, QBrush(sheen))
        top_glow = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + 70))
        top_glow.setColorAt(0, QColor(255, 255, 255, 32))
        top_glow.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 70), QBrush(top_glow))
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 74), 1.0))
        p.drawPath(path)
        rim = QLinearGradient(rect.topLeft(), rect.topRight())
        rim.setColorAt(0, QColor(255, 255, 255, 5))
        rim.setColorAt(0.45, QColor(255, 255, 255, 94))
        rim.setColorAt(0.75, QColor(255, 255, 255, 31))
        rim.setColorAt(1, QColor(255, 255, 255, 7))
        p.setPen(QPen(QBrush(rim), 1.2))
        p.drawLine(QPointF(rect.left() + 20, rect.top() + 1), QPointF(rect.right() - 20, rect.top() + 1))
        p.end()
        super().paintEvent(event)


class GlassSwitch(QCheckBox):
    """Compact iOS-like toggle drawn in Qt; clickable and keyboard accessible."""
    def __init__(self, text="", checked=False, parent=None):
        super().__init__(text, parent)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        return QSize(250, 36)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        track_w, track_h = 46.0, 26.0
        tx = self.width() - track_w - 3
        ty = (h - track_h) / 2
        track = QRectF(tx, ty, track_w, track_h)
        if self.isChecked():
            g = QLinearGradient(track.topLeft(), track.bottomRight())
            g.setColorAt(0, QColor(101, 242, 183, 242))
            g.setColorAt(1, QColor(38, 177, 130, 235))
            p.setPen(QPen(QColor(190, 255, 226, 170), 1))
            p.setBrush(QBrush(g))
        else:
            p.setPen(QPen(QColor(255, 255, 255, 65), 1))
            p.setBrush(QColor(18, 22, 30, 168))
        p.drawRoundedRect(track, track_h / 2, track_h / 2)
        # Reflective streak across the upper edge of the toggle track.
        top = QLinearGradient(track.topLeft(), QPointF(track.left(), track.top() + 10))
        top.setColorAt(0, QColor(255, 255, 255, 120 if self.isChecked() else 53))
        top.setColorAt(1, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(top))
        p.drawRoundedRect(QRectF(track.left() + 2, track.top() + 1, track.width() - 4, 10), 7, 7)
        knob_d = 20.0
        kx = track.right() - knob_d - 3 if self.isChecked() else track.left() + 3
        knob = QRectF(kx, track.top() + 3, knob_d, knob_d)
        glow = QRadialGradient(knob.center(), knob_d)
        glow.setColorAt(0, QColor(255, 255, 255, 255))
        glow.setColorAt(0.72, QColor(239, 244, 250, 255))
        glow.setColorAt(1, QColor(169, 179, 194, 240))
        p.setPen(QPen(QColor(255, 255, 255, 205), 0.8))
        p.setBrush(QBrush(glow))
        p.drawEllipse(knob)
        p.setPen(QColor(230, 235, 244, 255))
        p.setFont(self.font())
        p.drawText(QRectF(1, 0, max(0, tx - 16), h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.hasFocus():
            p.setPen(QPen(QColor(173, 202, 255, 165), 1.0, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(0.5, 1, self.width() - 1, h - 2), 8, 8)
        p.end()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.setChecked(not self.isChecked())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.setChecked(not self.isChecked())
            event.accept()
            return
        super().keyPressEvent(event)


class GlassIcon(QLabel):
    """Small translucent glyph tile with an animated highlight reflection."""
    def __init__(self, glyph="✧", size=42, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self._phase = 0.0
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._timer = QTimer(self)
        self._timer.setInterval(72)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def _advance(self):
        self._phase = (self._phase + 0.025) % 1.6
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.6, 0.6, -0.6, -0.6)
        path = QPainterPath()
        path.addRoundedRect(r, min(13.0, r.height() * 0.28), min(13.0, r.height() * 0.28))
        base = QLinearGradient(r.topLeft(), r.bottomRight())
        base.setColorAt(0, QColor(218, 230, 255, 68))
        base.setColorAt(0.48, QColor(116, 137, 172, 42))
        base.setColorAt(1, QColor(34, 45, 66, 86))
        p.fillPath(path, QBrush(base))
        p.save()
        p.setClipPath(path)
        x = r.left() + self._phase * (r.width() + 45) - 22
        streak = QRectF(x, r.top() - 10, max(8, r.width() * 0.24), r.height() + 20)
        shine = QLinearGradient(streak.topLeft(), streak.topRight())
        shine.setColorAt(0, QColor(255, 255, 255, 0))
        shine.setColorAt(0.45, QColor(255, 255, 255, 0))
        shine.setColorAt(0.72, QColor(238, 247, 255, 75))
        shine.setColorAt(1, QColor(255, 255, 255, 0))
        p.fillRect(streak, QBrush(shine))
        p.restore()
        p.setPen(QPen(QColor(235, 243, 255, 124), 1))
        p.drawPath(path)
        font = QFont("Segoe UI Symbol", max(13, int(r.height() * 0.42)))
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(247, 250, 255, 245))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.glyph)
        p.end()


class StatCard(GlassPanel):
    def __init__(self, title, value="—", parent=None):
        super().__init__(parent)
        glyphs = {"Отслеживание": "⌖", "Курсор": "↖", "Профиль": "✧"}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(11)
        layout.addWidget(GlassIcon(glyphs.get(title, "◉"), 36))
        labels = QVBoxLayout()
        labels.setSpacing(4)
        self.title = QLabel(title.upper())
        self.title.setObjectName("muted")
        self.value = QLabel(value)
        self.value.setObjectName("statValue")
        labels.addWidget(self.title)
        labels.addWidget(self.value)
        labels.addStretch(1)
        layout.addLayout(labels, 1)

    def set_value(self, value):
        self.value.setText(value)


class MagneticHighlight(QWidget):
    """Thin, click-through highlight around the current magnetic target."""
    def __init__(self):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                 Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowTransparentForInput |
                 Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._target = None
        self.hide()

    @Slot(object)
    def set_target(self, target):
        if not target:
            self._target = None
            self.hide()
            return
        try:
            x, y, w, h = [int(v) for v in target]
            self._target = (x, y, w, h)
            self.setGeometry(x - 3, y - 3, max(8, w + 6), max(8, h + 6))
            self.show()
            self.update()
        except Exception:
            self.hide()

    def paintEvent(self, event):
        if not self._target:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(112, 231, 255, 235), 2)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(112, 231, 255, 24)))
        painter.drawRoundedRect(QRectF(2, 2, self.width()-4, self.height()-4), 7, 7)
        painter.end()


class MainWindow(QMainWindow):
    ACTIONS = [
        "Нет действия",
        "Показать рабочий стол (Win+D)",
        "Свернуть все окна (Win+M)",
        "Обзор окон — выбрать вручную (Win+Tab)",
        "Переключить окно (Alt+Tab)",
        "Закрепить окно слева", "Закрепить окно справа", "Развернуть окно",
        "Свернуть активное окно (Win+Down)", "Escape", "Диспетчер задач"
    ]
    GESTURES = ["Открытая ладонь", "Кулак", "V-жест", "Указательный палец", "Три пальца"]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("GesturePilot — управление жестами")
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "gesturepilot_icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1320, 850)
        self.setMinimumSize(980, 680)
        self.settings_store = QSettings("GesturePilot", "GesturePilot")
        saved_actions = self.settings_store.value("window/gesture_actions", {}, type=dict)
        if not isinstance(saved_actions, dict):
            saved_actions = {}
        self.settings = Settings(
            window_actions_enabled=self.settings_store.value("window/actions_enabled", False, type=bool),
            gesture_actions={g: str(saved_actions.get(g, "Нет действия")) for g in self.GESTURES},
        )
        self.worker = None
        self.aux_worker = None
        self._build_ui()
        self._apply_style()
        self._start_worker()

    def open_donation_page(self):
        url = DONATION_URL.strip()
        if url.startswith("https://") or url.startswith("http://"):
            QDesktopServices.openUrl(QUrl(url))
        else:
            QMessageBox.information(
                self, "Поддержать GesturePilot",
                "Сначала укажите ссылку на свою страницу донатов в переменной DONATION_URL в начале файла main.py."
            )

    @staticmethod
    def make_panel_layout(panel, margins=(16, 15, 16, 15), spacing=12):
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(*margins)
        layout.setSpacing(spacing)
        return layout

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 17, 22, 18)
        outer.setSpacing(14)

        header = QHBoxLayout()
        brand_icon = GlassIcon("✧", 52)
        header.addWidget(brand_icon)
        title_col = QVBoxLayout()
        title = QLabel("GesturePilot")
        title.setObjectName("appTitle")
        subtitle = QLabel("ЖЕСТЫ · WINDOWS · MAYA · BLENDER · UNREAL ENGINE")
        subtitle.setObjectName("muted")
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch(1)
        self.state_badge = QLabel("● ПРЕДПРОСМОТР")
        self.state_badge.setObjectName("badgeOff")
        header.addWidget(self.state_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        outer.addWidget(self.tabs, 1)

        # Добровольная поддержка разработчика. Кнопка активируется после настройки ссылки.
        support_row = QHBoxLayout()
        support_row.addStretch(1)
        support_note = QLabel("GesturePilot бесплатен · поддержка добровольная")
        support_note.setObjectName("muted")
        support_row.addWidget(support_note)
        self.support_btn = QPushButton("♥  Поддержать разработчика")
        self.support_btn.setObjectName("supportButton")
        self.support_btn.setToolTip("Чтобы включить кнопку, укажите DONATION_URL в начале main.py")
        self.support_btn.setEnabled(bool(DONATION_URL.strip()))
        self.support_btn.clicked.connect(self.open_donation_page)
        support_row.addWidget(self.support_btn)
        outer.addLayout(support_row)

        # Dashboard: live preview and everyday controls.
        dash_scroll = QScrollArea()
        dash_scroll.setWidgetResizable(True)
        dash_scroll.setFrameShape(QFrame.Shape.NoFrame)
        dash = QWidget()
        dash_layout = QVBoxLayout(dash)
        dash_layout.setContentsMargins(2, 8, 5, 8)
        dash_layout.setSpacing(14)
        split = QHBoxLayout()
        split.setSpacing(14)
        dash_layout.addLayout(split, 1)

        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        split.addLayout(left_col, 3)
        camera_card = GlassPanel()
        cam_layout = self.make_panel_layout(camera_card, (15, 14, 15, 15), 10)
        cam_header = QHBoxLayout()
        cam_title = QLabel("◉  Живое изображение")
        cam_title.setObjectName("sectionTitle")
        self.hand_label = QLabel("Ожидание камеры…")
        self.hand_label.setObjectName("muted")
        cam_header.addWidget(cam_title)
        cam_header.addStretch()
        cam_header.addWidget(self.hand_label)
        cam_layout.addLayout(cam_header)
        self.preview = QLabel("Подключение камеры…")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(460, 300)
        self.preview.setObjectName("preview")
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        cam_layout.addWidget(self.preview, 1)
        self.secondary_preview = QLabel("Резервная камера не активна")
        self.secondary_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.secondary_preview.setMinimumHeight(88)
        self.secondary_preview.setMaximumHeight(145)
        self.secondary_preview.setObjectName("previewSecondary")
        self.secondary_preview.hide()
        cam_layout.addWidget(self.secondary_preview)
        left_col.addWidget(camera_card, 1)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_hands = StatCard("Отслеживание", "Ожидание")
        self.card_cursor = StatCard("Курсор", "Выключен")
        self.card_profile = StatCard("Профиль", "Universal")
        cards.addWidget(self.card_hands)
        cards.addWidget(self.card_cursor)
        cards.addWidget(self.card_profile)
        left_col.addLayout(cards)

        right_col = QVBoxLayout()
        right_col.setSpacing(12)
        split.addLayout(right_col, 2)
        control_panel = GlassPanel()
        cl = self.make_panel_layout(control_panel)
        cl.addWidget(self.heading("Управление"))
        self.toggle_btn = QPushButton("▶  Включить управление")
        self.toggle_btn.setObjectName("primaryButton")
        self.toggle_btn.clicked.connect(self.toggle_control)
        cl.addWidget(self.toggle_btn)
        self.dual_switch = GlassSwitch("Режим двух камер", False)
        self.dual_switch.setToolTip("Включает обе камеры. Источник управления выбирается вручную, без автоматического переключения.")
        self.dual_switch.toggled.connect(self.toggle_smart_dual)
        cl.addWidget(self.dual_switch)
        form = QFormLayout()
        form.setSpacing(9)
        self.camera_combo = QComboBox()
        self.camera_combo.addItems([f"Камера {i}" for i in range(8)])
        self.camera_combo.currentIndexChanged.connect(self.change_camera)
        form.addRow("Основная камера", self.camera_combo)
        self.secondary_combo = QComboBox()
        self.secondary_combo.addItems([f"Камера {i}" for i in range(8)])
        self.secondary_combo.setCurrentIndex(1)
        self.secondary_combo.currentIndexChanged.connect(self.change_secondary_camera)
        form.addRow("Боковая камера", self.secondary_combo)
        self.control_source_combo = QComboBox()
        self.control_source_combo.addItem("Фронтальная — курсор", "primary")
        self.control_source_combo.addItem("Боковая — курсор", "auxiliary")
        self.control_source_combo.currentIndexChanged.connect(self.change_control_source)
        form.addRow("Камерой управляет", self.control_source_combo)
        self.control_source_combo.setEnabled(False)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(["Universal", "Blender", "Maya", "Unreal Engine"])
        self.profile_combo.currentTextChanged.connect(self.change_profile)
        form.addRow("Профиль", self.profile_combo)
        cl.addLayout(form)
        self.sensitivity_label = QLabel("Чувствительность курсора  ·  100%")
        self.sensitivity_label.setObjectName("muted")
        cl.addWidget(self.sensitivity_label)
        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity_slider.setRange(30, 200)
        self.sensitivity_slider.setValue(100)
        self.sensitivity_slider.valueChanged.connect(self.change_sensitivity)
        cl.addWidget(self.sensitivity_slider)
        self.smoothing_label = QLabel("Плавность  ·  28%")
        self.smoothing_label.setObjectName("muted")
        cl.addWidget(self.smoothing_label)
        self.smoothing_slider = QSlider(Qt.Orientation.Horizontal)
        self.smoothing_slider.setRange(5, 95)
        self.smoothing_slider.setValue(28)
        self.smoothing_slider.valueChanged.connect(self.change_smoothing)
        cl.addWidget(self.smoothing_slider)
        self.landmark_switch = GlassSwitch("Показывать скелет рук", True)
        self.landmark_switch.toggled.connect(self.toggle_landmarks)
        cl.addWidget(self.landmark_switch)
        self.status_label = QLabel("Подготовка камеры…")
        self.status_label.setObjectName("statusText")
        self.status_label.setWordWrap(True)
        cl.addWidget(self.status_label)
        right_col.addWidget(control_panel, 1)

        quick_panel = GlassPanel()
        ql = self.make_panel_layout(quick_panel, (15, 13, 15, 13), 8)
        ql.addWidget(self.heading("Жесты по умолчанию"))
        guide = QLabel("• Большой + указательный — левая кнопка\n• Большой + средний — правая кнопка\n• Две руки в щипке — прокрутка / зум\n• Отпускание кнопки подтверждается с задержкой")
        guide.setObjectName("helpText")
        guide.setWordWrap(True)
        ql.addWidget(guide)
        right_col.addWidget(quick_panel)
        dash_scroll.setWidget(dash)
        self.tabs.addTab(dash_scroll, "◉  Главная")

        # Feature studio: safety, camera strategy, gestures and window shortcuts.
        feature_scroll = QScrollArea()
        feature_scroll.setWidgetResizable(True)
        feature_scroll.setFrameShape(QFrame.Shape.NoFrame)
        features = QWidget()
        fl = QVBoxLayout(features)
        fl.setContentsMargins(4, 9, 8, 10)
        fl.setSpacing(13)
        top_grid = QGridLayout()
        top_grid.setHorizontalSpacing(13)
        top_grid.setVerticalSpacing(13)
        fl.addLayout(top_grid)

        smooth_panel = GlassPanel()
        sl = self.make_panel_layout(smooth_panel)
        sl.addWidget(self.heading("Плавность и точность"))
        sl.addWidget(QLabel("Настройте поведение курсора под скорость движения руки."))
        self.filter_switch = GlassSwitch("Адаптивное сглаживание", True)
        self.filter_switch.toggled.connect(self.toggle_adaptive_smoothing)
        sl.addWidget(self.filter_switch)
        sl.addWidget(QLabel("Сглаживание"))
        self.smooth_detail = QSlider(Qt.Orientation.Horizontal)
        self.smooth_detail.setRange(5, 95)
        self.smooth_detail.setValue(28)
        self.smooth_detail.valueChanged.connect(self.change_smoothing)
        sl.addWidget(self.smooth_detail)
        self.smooth_detail_value = QLabel("28% · баланс скорости и устойчивости")
        self.smooth_detail_value.setObjectName("muted")
        sl.addWidget(self.smooth_detail_value)
        top_grid.addWidget(smooth_panel, 0, 0)

        safe_panel = GlassPanel()
        safe = self.make_panel_layout(safe_panel)
        safe.addWidget(self.heading("Защита от ложных кликов"))
        self.safety_switch = GlassSwitch("Строго подтверждать щипок", True)
        self.safety_switch.toggled.connect(self.toggle_safety)
        safe.addWidget(self.safety_switch)
        safe_desc = QLabel("При включении GesturePilot требует более уверенного распознавания руки, дольше подтверждает зажатие и отпускание и игнорирует неоднозначное положение пальцев.")
        safe_desc.setObjectName("helpText")
        safe_desc.setWordWrap(True)
        safe.addWidget(safe_desc)
        self.safety_status = QLabel("Режим: усиленная защита")
        self.safety_status.setObjectName("muted")
        safe.addWidget(self.safety_status)
        top_grid.addWidget(safe_panel, 0, 1)

        gesture_panel = GlassPanel()
        gl = self.make_panel_layout(gesture_panel)
        gl.addWidget(self.heading("Конструктор жестов"))
        gesture_intro = QLabel("Выберите жест и действие. Действия запускаются только при включённом управлении и после удержания жеста около половины секунды.")
        gesture_intro.setObjectName("helpText")
        gesture_intro.setWordWrap(True)
        gl.addWidget(gesture_intro)
        self.window_actions_switch = GlassSwitch(
            "Разрешить жесты для окон Windows", self.settings.window_actions_enabled
        )
        self.window_actions_switch.toggled.connect(self.toggle_window_actions)
        gl.addWidget(self.window_actions_switch)
        self.gesture_combos = {}
        gesture_grid = QGridLayout()
        gesture_grid.setHorizontalSpacing(12)
        gesture_grid.setVerticalSpacing(8)
        gesture_grid.addWidget(self.heading("Распознанный жест"), 0, 0)
        gesture_grid.addWidget(self.heading("Выполнить действие"), 0, 1)
        default_mapping = {
            "Открытая ладонь": "Нет действия",
            "Кулак": "Нет действия",
            "V-жест": "Нет действия",
            "Указательный палец": "Нет действия",
            "Три пальца": "Нет действия",
        }
        for row, gesture in enumerate(self.GESTURES, start=1):
            label = QLabel(gesture)
            label.setObjectName("gestureName")
            combo = QComboBox()
            combo.addItems(self.ACTIONS)
            combo.setCurrentText(self.settings.gesture_actions.get(gesture, default_mapping.get(gesture, "Нет действия")))
            combo.currentTextChanged.connect(lambda action, g=gesture: self.change_gesture_action(g, action))
            self.gesture_combos[gesture] = combo
            gesture_grid.addWidget(label, row, 0)
            gesture_grid.addWidget(combo, row, 1)
        gl.addLayout(gesture_grid)
        note = QLabel("Назначения и разрешение жестов сохраняются между запусками. «Обзор окон» открывает системный экран Windows — нужное окно выберите вручную. Все действия изначально отключены.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        gl.addWidget(note)
        fl.addWidget(gesture_panel)

        magnetic_panel = GlassPanel()
        ml = self.make_panel_layout(magnetic_panel)
        ml.addWidget(self.heading("Магнитный курсор"))
        magnetic_desc = QLabel("Плавно помогает попасть по кнопкам, иконкам и другим интерактивным элементам Windows. Сам по себе не выполняет клики.")
        magnetic_desc.setObjectName("helpText")
        magnetic_desc.setWordWrap(True)
        ml.addWidget(magnetic_desc)
        self.magnetic_switch = GlassSwitch("Включить притягивание", self.settings.magnetic_cursor_enabled)
        self.magnetic_switch.toggled.connect(self.toggle_magnetic_enabled)
        ml.addWidget(self.magnetic_switch)
        ml.addWidget(QLabel("Радиус притягивания"))
        self.magnetic_radius_slider = QSlider(Qt.Orientation.Horizontal)
        self.magnetic_radius_slider.setRange(10, 200)
        self.magnetic_radius_slider.setValue(self.settings.magnetic_radius)
        self.magnetic_radius_slider.valueChanged.connect(self.change_magnetic_radius)
        ml.addWidget(self.magnetic_radius_slider)
        self.magnetic_radius_value = QLabel(f"{self.settings.magnetic_radius} px")
        self.magnetic_radius_value.setObjectName("muted")
        ml.addWidget(self.magnetic_radius_value)
        ml.addWidget(QLabel("Сила притягивания"))
        self.magnetic_strength_slider = QSlider(Qt.Orientation.Horizontal)
        self.magnetic_strength_slider.setRange(10, 100)
        self.magnetic_strength_slider.setValue(self.settings.magnetic_strength)
        self.magnetic_strength_slider.valueChanged.connect(self.change_magnetic_strength)
        ml.addWidget(self.magnetic_strength_slider)
        self.magnetic_strength_value = QLabel(f"{self.settings.magnetic_strength}%")
        self.magnetic_strength_value.setObjectName("muted")
        ml.addWidget(self.magnetic_strength_value)
        self.magnetic_highlight_switch = GlassSwitch("Подсвечивать цель", self.settings.magnetic_highlight)
        self.magnetic_highlight_switch.toggled.connect(self.toggle_magnetic_highlight)
        ml.addWidget(self.magnetic_highlight_switch)
        uia_note = QLabel("Для обнаружения элементов используется Windows UI Automation (pywinauto). Если элемент недоступен через UIA, обычное управление продолжает работать без притягивания к нему.")
        uia_note.setObjectName("helpText")
        uia_note.setWordWrap(True)
        ml.addWidget(uia_note)
        fl.addWidget(magnetic_panel)

        actions_panel = GlassPanel()
        al = self.make_panel_layout(actions_panel)
        al.addWidget(self.heading("Управление окнами"))
        action_desc = QLabel("Доступные команды: рабочий стол, переключение окна, привязка к краю, разворачивание, сворачивание, Escape и диспетчер задач. Команда срабатывает один раз, пока распознанный жест не будет отпущен.")
        action_desc.setObjectName("helpText")
        action_desc.setWordWrap(True)
        al.addWidget(action_desc)
        self.window_action_state = QLabel("Управление окнами разрешено" if self.settings.window_actions_enabled else "Управление окнами выключено")
        self.window_action_state.setObjectName("muted")
        al.addWidget(self.window_action_state)
        fl.addWidget(actions_panel)

        diagnostic_panel = GlassPanel()
        dl = self.make_panel_layout(diagnostic_panel)
        dl.addWidget(self.heading("Умный режим двух камер"))
        dual_desc = QLabel("Источник управления выбирается вручную. Фронтальная камера обычно управляет курсором; боковую можно назначить основной в выпадающем списке. Если выбрана фронтальная камера, боковой поток используется только как дополнительная оценка относительной близости руки и слегка корректирует плавность. Координаты двух ракурсов не смешиваются, а автоматического переключения нет.")
        dual_desc.setObjectName("helpText")
        dual_desc.setWordWrap(True)
        dl.addWidget(dual_desc)
        fl.addWidget(diagnostic_panel)
        fl.addStretch(1)
        feature_scroll.setWidget(features)
        self.tabs.addTab(feature_scroll, "✦  Функции")

        footer = QHBoxLayout()
        self.stop_btn = QPushButton("■  Аварийная остановка")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.clicked.connect(self.emergency_stop)
        footer.addWidget(self.stop_btn)
        footer.addStretch(1)
        footer_note = QLabel("v20 · локальная обработка · видео не отправляется в интернет")
        footer_note.setObjectName("muted")
        footer.addWidget(footer_note)
        outer.addLayout(footer)
        self.change_camera_mode(False)

    @staticmethod
    def heading(text):
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0d1017,stop:0.35 #202634,stop:0.68 #121823,stop:1 #090c12); }
            QWidget { color: #f0f3f9; font-family: "Segoe UI"; font-size: 10pt; background: transparent; }
            #root { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(22,28,40,90),stop:0.5 rgba(10,14,22,30),stop:1 rgba(32,39,53,65)); }
            #appTitle { font-size: 27pt; font-weight: 750; color: #ffffff; letter-spacing: 0.4px; }
            #sectionTitle { font-size: 12pt; font-weight: 700; color: #ffffff; padding-bottom: 2px; }
            #muted { color: #bac4d5; }
            #statusText { color: #d7e2f2; background: rgba(10,15,24,90); border: 1px solid rgba(255,255,255,30); border-radius: 10px; padding: 9px; }
            #preview { background: rgba(6,10,17,215); border: 1px solid rgba(255,255,255,58); border-radius: 15px; color: #bec9d9; padding: 4px; }
            #previewSecondary { background: rgba(6,10,17,170); border: 1px solid rgba(255,255,255,36); border-radius: 12px; color: #bec9d9; padding: 4px; }
            #statValue { font-size: 13pt; font-weight: 750; color: #ffffff; }
            #badgeOff, #badgeOn { border-radius: 12px; padding: 9px 13px; font-size: 9pt; font-weight: 750; }
            #badgeOff { background: rgba(255,255,255,19); color: #e1e6ef; border: 1px solid rgba(255,255,255,48); }
            #badgeOn { background: rgba(40,204,143,38); color: #b5ffe0; border: 1px solid rgba(128,255,205,115); }
            QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(255,255,255,32),stop:1 rgba(255,255,255,10)); color: #f5f7fb; border: 1px solid rgba(255,255,255,62); border-radius: 12px; padding: 10px 13px; font-weight: 650; min-height: 22px; }
            QPushButton:hover { background: rgba(255,255,255,50); border-color: rgba(255,255,255,125); }
            QPushButton#supportButton { background: rgba(42, 183, 210, 30); border: 1px solid rgba(83, 217, 255, 100); color: #c7f6ff; }
            QPushButton#supportButton:disabled { color: #8493a7; border-color: rgba(255,255,255,25); background: rgba(255,255,255,7); }
            QPushButton:pressed { background: rgba(255,255,255,13); }
            #primaryButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #fcfdff,stop:0.5 #dce4f1,stop:1 #aebed4); border: 1px solid rgba(255,255,255,235); color: #18202d; padding: 13px; font-size: 10.5pt; font-weight: 800; }
            #primaryButton:hover { background: #ffffff; }
            #dangerButton { background: rgba(182,53,75,85); border-color: rgba(255,143,160,135); color: #ffe1e8; }
            #dangerButton:hover { background: rgba(208,62,85,120); }
            #helpText { color: #e0e7f1; }
            #gestureName { color: #e4eaf4; padding: 5px; }
            QComboBox { background: rgba(7,12,22,145); border: 1px solid rgba(255,255,255,56); border-radius: 10px; padding: 8px 10px; min-height: 20px; selection-background-color: #54647e; }
            QComboBox:hover { border-color: rgba(255,255,255,130); background: rgba(28,37,53,220); }
            QComboBox QAbstractItemView { background: #202938; color: #f4f7fb; border: 1px solid #65718a; selection-background-color: #4c5d78; padding: 4px; }
            QComboBox:focus, QPushButton:focus, QSlider:focus { border: 1px solid #9ec9ff; }
            QSlider::groove:horizontal { height: 5px; background: rgba(255,255,255,48); border-radius: 3px; }
            QSlider::sub-page:horizontal { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6cb1ee,stop:1 #e6f7ff); border-radius: 3px; }
            QSlider::handle:horizontal { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ffffff,stop:1 #bdcee4); border: 1px solid rgba(255,255,255,200); width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }
            QTabWidget::pane { border: 1px solid rgba(255,255,255,28); border-radius: 16px; top: -1px; background: rgba(7,11,18,28); }
            QTabBar::tab { background: rgba(255,255,255,10); border: 1px solid rgba(255,255,255,28); color: #c4cede; padding: 10px 18px; margin-right: 5px; border-radius: 10px; }
            QTabBar::tab:selected { background: rgba(207,226,255,32); color: #ffffff; border-color: rgba(219,237,255,95); }
            QTabBar::tab:hover { background: rgba(255,255,255,24); }
            QScrollArea { background: transparent; }
            QToolTip { background: #242d3c; color: #f5f7fb; border: 1px solid #73839e; padding: 6px; }
        """)

    def _start_worker(self):
        self.magnetic_overlay = MagneticHighlight()
        self.worker = HandWorker(self.settings)
        self.worker.magnetic_target.connect(self.magnetic_overlay.set_target)
        self.worker.preview_ready.connect(self.update_preview)
        self.worker.status.connect(self.update_status)
        self.worker.hand_info.connect(self.update_hands)
        self.worker.error.connect(self.show_worker_error)
        self.worker.start()
        self.aux_worker = AuxiliaryCameraWorker(self.secondary_combo.currentIndex(), True)
        self.aux_worker.data_ready.connect(self.worker.set_secondary_data)
        self.aux_worker.preview_ready.connect(self.update_secondary_preview)
        self.aux_worker.status.connect(self.update_status)
        self.aux_worker.start()
        self.aux_worker.set_camera(self.secondary_combo.currentIndex())
        self.aux_worker.set_enabled(False)

    @Slot(QImage)
    def update_preview(self, image):
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    @Slot(str)
    def update_status(self, text):
        self.status_label.setText(text)

    @Slot(str)
    def update_hands(self, text):
        self.hand_label.setText(text)
        self.card_hands.set_value("Активно" if "Обнаружено:" in text and "не найдены" not in text else "Нет рук")

    @Slot(str)
    def show_worker_error(self, text):
        first_line = str(text).splitlines()[0][:220]
        self.status_label.setText(first_line)

    def toggle_control(self):
        if self.worker is None:
            return
        enabled = self.toggle_btn.text().startswith("▶")
        self.worker.set_control(enabled)
        if enabled:
            self.toggle_btn.setText("Ⅱ  Выключить управление")
            self.state_badge.setText("● УПРАВЛЕНИЕ АКТИВНО")
            self.state_badge.setObjectName("badgeOn")
            self.card_cursor.set_value("Активен")
        else:
            self.toggle_btn.setText("▶  Включить управление")
            self.state_badge.setText("● ПРЕДПРОСМОТР")
            self.state_badge.setObjectName("badgeOff")
            self.card_cursor.set_value("Выключен")
        self._repolish(self.state_badge)

    @staticmethod
    def _repolish(widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    @Slot(QImage)
    def update_secondary_preview(self, image):
        self.secondary_preview.setPixmap(QPixmap.fromImage(image).scaled(self.secondary_preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def toggle_smart_dual(self, enabled):
        self.settings.smart_dual = bool(enabled)
        self.change_camera_mode(enabled)

    def change_camera_mode(self, _enabled=False):
        dual = self.dual_switch.isChecked()
        if not dual:
            self.control_source_combo.blockSignals(True)
            self.control_source_combo.setCurrentIndex(0)
            self.control_source_combo.blockSignals(False)
            self.settings.control_source = "primary"
        if self.worker:
            self.worker.set_camera_mode("dual" if dual else "single")
            self.worker.set_control_source(self.settings.control_source)
        self.secondary_combo.setEnabled(dual)
        self.control_source_combo.setEnabled(dual)
        if self.aux_worker:
            self.aux_worker.set_enabled(dual)
        self.secondary_preview.setVisible(dual)
        if not dual:
            self.secondary_preview.setText("Боковая камера отключена")
        self.status_label.setText("Две камеры: ручной выбор источника" if dual else "Одна камера: основной трекинг")

    def change_control_source(self, _index):
        source = self.control_source_combo.currentData() or "primary"
        self.settings.control_source = source
        if self.worker:
            self.worker.set_control_source(source)
        self.status_label.setText("Источник курсора: боковая камера" if source == "auxiliary" else "Источник курсора: фронтальная камера")

    def change_secondary_camera(self, index):
        if self.dual_switch.isChecked() and index == self.camera_combo.currentIndex():
            replacement = next((i for i in range(self.secondary_combo.count()) if i != self.camera_combo.currentIndex()), None)
            if replacement is not None:
                self.secondary_combo.blockSignals(True)
                self.secondary_combo.setCurrentIndex(replacement)
                self.secondary_combo.blockSignals(False)
                index = replacement
                self.status_label.setText("Обе камеры не могут использовать один индекс; боковой источник изменён.")
        if self.aux_worker:
            self.aux_worker.set_camera(index)

    def change_camera(self, index):
        if self.dual_switch.isChecked() and index == self.secondary_combo.currentIndex():
            replacement = next((i for i in range(self.secondary_combo.count()) if i != index), None)
            if replacement is not None:
                self.secondary_combo.blockSignals(True)
                self.secondary_combo.setCurrentIndex(replacement)
                self.secondary_combo.blockSignals(False)
                if self.aux_worker:
                    self.aux_worker.set_camera(replacement)
                self.status_label.setText("Обе камеры не могут использовать один индекс; боковой источник изменён.")
        if self.worker:
            self.worker.set_camera(index)
            self.status_label.setText(f"Переключение на камеру {index}…")

    def change_profile(self, profile):
        self.settings.active_profile = profile
        self.card_profile.set_value(profile)
        if self.worker:
            self.worker.set_profile(profile)

    def toggle_landmarks(self, enabled):
        if self.worker:
            self.worker.set_landmarks(enabled)
        if self.aux_worker:
            self.aux_worker.set_landmarks(enabled)

    def change_sensitivity(self, value):
        self.settings.sensitivity = value / 100.0
        self.sensitivity_label.setText(f"Чувствительность курсора  ·  {value}%")
        if self.worker:
            self.worker.set_sensitivity(self.settings.sensitivity)

    def change_smoothing(self, value):
        self.settings.smoothing = value / 100.0
        self.smoothing_label.setText(f"Плавность  ·  {value}%")
        if hasattr(self, "smooth_detail_value"):
            self.smooth_detail_value.setText(f"{value}% · " + ("мягкое движение" if value >= 60 else "быстрый отклик" if value <= 20 else "баланс скорости и устойчивости"))
        if hasattr(self, "smooth_detail") and self.smooth_detail.value() != value:
            self.smooth_detail.blockSignals(True)
            self.smooth_detail.setValue(value)
            self.smooth_detail.blockSignals(False)
        if hasattr(self, "smoothing_slider") and self.smoothing_slider.value() != value:
            self.smoothing_slider.blockSignals(True)
            self.smoothing_slider.setValue(value)
            self.smoothing_slider.blockSignals(False)
        if self.worker:
            self.worker.set_smoothing(self.settings.smoothing)

    def toggle_adaptive_smoothing(self, enabled):
        # Adaptive response uses the worker's speed-dependent alpha boost. When disabled,
        # keep the selected base smoothing but remove the acceleration boost.
        self.settings.adaptive_smoothing = bool(enabled)
        if self.worker:
            self.worker.set_adaptive_smoothing(enabled)

    def toggle_safety(self, enabled):
        self.settings.false_click_protection = bool(enabled)
        self.safety_status.setText("Режим: усиленная защита" if enabled else "Режим: стандартная защита")
        if self.worker:
            self.worker.set_false_click_protection(enabled)

    def toggle_magnetic_enabled(self, enabled):
        self.settings.magnetic_cursor_enabled = bool(enabled)
        if self.worker:
            self.worker.set_magnetic_settings(enabled=enabled)

    def change_magnetic_radius(self, value):
        self.settings.magnetic_radius = int(value)
        self.magnetic_radius_value.setText(f"{value} px")
        if self.worker:
            self.worker.set_magnetic_settings(radius=value)

    def change_magnetic_strength(self, value):
        self.settings.magnetic_strength = int(value)
        self.magnetic_strength_value.setText(f"{value}%")
        if self.worker:
            self.worker.set_magnetic_settings(strength=value)

    def toggle_magnetic_highlight(self, enabled):
        self.settings.magnetic_highlight = bool(enabled)
        if self.worker:
            self.worker.set_magnetic_settings(highlight=enabled)

    def toggle_window_actions(self, enabled):
        self.settings.window_actions_enabled = bool(enabled)
        self.settings_store.setValue("window/actions_enabled", bool(enabled))
        self.window_action_state.setText("Управление окнами разрешено" if enabled else "Управление окнами выключено")
        if self.worker:
            self.worker.set_window_actions_enabled(enabled)

    def change_gesture_action(self, gesture, action):
        self.settings.gesture_actions[gesture] = action
        self.settings_store.setValue("window/gesture_actions", self.settings.gesture_actions)
        if self.worker:
            self.worker.set_gesture_actions(self.settings.gesture_actions)

    def emergency_stop(self):
        if self.worker:
            self.worker.set_control(False)
        self.toggle_btn.setText("▶  Включить управление")
        self.state_badge.setText("● ПРЕДПРОСМОТР")
        self.state_badge.setObjectName("badgeOff")
        self._repolish(self.state_badge)
        self.card_cursor.set_value("Выключен")
        self.status_label.setText("Аварийная остановка: ввод мыши отключён")

    def closeEvent(self, event):
        if self.aux_worker:
            self.aux_worker.stop()
            self.aux_worker.wait(1800)
            if self.aux_worker.isRunning():
                event.ignore()
                self.status_label.setText("Остановка резервной камеры… повторите закрытие через секунду")
                return
        if self.worker:
            self.worker.stop()
            self.worker.wait(2200)
            if self.worker.isRunning():
                event.ignore()
                self.status_label.setText("Остановка основной камеры… повторите закрытие через секунду")
                return
        if hasattr(self, "magnetic_overlay"):
            self.magnetic_overlay.hide()
        event.accept()

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
