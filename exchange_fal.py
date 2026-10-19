# -*- coding: utf-8 -*-
"""在《暗区突围无限》里兑换并上架 FAL 四导轨标准护木。

悬浮窗有三个用法：
- 自动：先把仓库里的护木上架完，没有了再兑换，满了再上架
- 兑换：只兑换设定的次数
- 上架：只把扩容仓库里的护木挂到市场

上架不再点右边那一排容器图标：直接进底部「市场」的出售页，滚右侧那条连续的仓库列表，
用 templates/handguard_cell.png 在里面找护木，鼠标放上去按 F。兑换满了直接接着上架。

游戏需要开窗口化或无边框。悬浮窗上的「停止」、按 Esc，
或把鼠标甩到屏幕左上角，都会立刻停下。

用法：
  python exchange_fal.py              打开悬浮窗
  python exchange_fal.py --cli -n 3   不打开窗口，只兑换
  python exchange_fal.py --cli --skip-nav
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import random
import sys

# 用 python exchange_fal.py 启动时，这个文件的名字是 __main__。
# sell_fal 再 import exchange_fal 会重新加载一份。停止信号写在这一份，
# 上架看的是另一份，所以悬浮窗已经显示已停止，上架还在继续点。
if __name__ == "__main__":
    sys.modules["exchange_fal"] = sys.modules[__name__]

import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ctypes.windll.user32.SetProcessDPIAware()

import cv2
import numpy as np
import pyautogui
import win32gui
from PIL import ImageGrab
from rapidocr_onnxruntime import RapidOCR

from falbot import vision
from falbot import (
    AutoWorkflow,
    ExchangeLimitReached,
    FAL_HANDGUARD,
    ListingFull,
    MerchantSlotsFull,
    StopReason,
    WarehouseFull,
    parse_market_limit,
)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.012

ROOT = Path(__file__).resolve().parent
TEMPLATE_OFF = cv2.imread(str(ROOT / "templates" / "core_parts.png"))
TEMPLATE_ON = cv2.imread(str(ROOT / "templates" / "core_parts_on.png"))
# 仓库格子里的护木图标（灰度）。上架靠它在右侧列表里找货，不再认格子上的缩写文字。
HANDGUARD_TEMPLATE = cv2.imread(str(ROOT / "templates" / "handguard_cell.png"), cv2.IMREAD_GRAYSCALE)
VK_ESCAPE = 0x1B
ITEM_CLICK_DY = 0.08  # 录屏里点在名称下方的护木图片上
WHEEL_NOTCH = 120  # Windows 里拨一格滚轮的幅度。小于这个数，游戏列表几乎不动。

OCR = RapidOCR()


class _OcrWorker:
    """识别放在旁边的线程里。点停止后，操作线程不用等这一轮认完才退出。"""

    def __init__(self) -> None:
        self._in: queue.Queue = queue.Queue()
        self._out: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, name="ocr", daemon=True).start()

    def _loop(self) -> None:
        while True:
            token, image = self._in.get()
            try:
                payload: tuple[str, object] = ("ok", OCR(image))
            except Exception as exc:
                payload = ("err", exc)
            self._out.put((token, payload))

    def call(self, image: np.ndarray):
        check_abort()
        token = object()
        self._in.put((token, image))
        while True:
            try:
                got, payload = self._out.get(timeout=0.05)
            except queue.Empty:
                check_abort()
                continue
            if got is not token:
                continue
            kind, value = payload
            if kind == "err":
                raise value
            check_abort()
            return value


_ocr_worker: _OcrWorker | None = None


def release_mouse() -> None:
    """直接发鼠标松开，不走 pyautogui，避免和正在点击的线程卡在一起。"""
    try:
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)
    except Exception:
        pass


def hover_at(screen_x: int, screen_y: int) -> bool:
    """SetCursorPos 不会通知游戏光标动了。补一次很小的移动，按钮才会处于悬停。

    相对移动会被鼠标加速放大，容易从护木格子上滑走。每次挪完立刻钉回目标点，
    并且要求停得更近，避免右键落在旁边空格或详情。
    """
    user32 = ctypes.windll.user32
    x, y = int(screen_x), int(screen_y)
    for _ in range(4):
        user32.SetCursorPos(x, y)
        time.sleep(0.008)
        user32.mouse_event(0x0001, 1, 0, 0, 0)
        time.sleep(0.012)
        user32.SetCursorPos(x, y)
        time.sleep(0.018)
        user32.SetCursorPos(x, y)
        if cursor_near(x, y, tolerance=12):
            return True
    user32.SetCursorPos(x, y)
    return cursor_near(x, y, tolerance=16)


def press_mouse(button: str = "left") -> None:
    """按下和松开都不带位移。pyautogui 会把坐标当成相对移动，点下去时光标会飘出按钮。"""
    if button == "right":
        down, up = 0x0008, 0x0010
    else:
        down, up = 0x0002, 0x0004
    user32 = ctypes.windll.user32
    hold_script_mouse(True)
    try:
        user32.mouse_event(down, 0, 0, 0, 0)
        pause(random.uniform(0.08, 0.15))
    finally:
        user32.mouse_event(up, 0, 0, 0, 0)
        hold_script_mouse(False)


def avoid_panel(screen_x: int, screen_y: int) -> None:
    """悬浮窗盖住目标时，先把它挪走，否则这一下点在窗口上，游戏毫无反应。"""
    rect = PANEL_RECT
    if rect is None:
        return
    if not (rect[0] - 4 <= screen_x <= rect[2] + 4 and rect[1] - 4 <= screen_y <= rect[3] + 4):
        return
    log("  悬浮窗挡住了这个按钮，先挪到右上角")
    PARK_PANEL.set()
    deadline = time.perf_counter() + 1.2
    while time.perf_counter() < deadline:
        check_abort()
        rect = PANEL_RECT
        if rect is None or not (rect[0] - 4 <= screen_x <= rect[2] + 4 and rect[1] - 4 <= screen_y <= rect[3] + 4):
            return
        time.sleep(0.05)


@dataclass
class Hit:
    text: str
    cx: float
    cy: float
    score: float
    x1: float
    y1: float
    x2: float
    y2: float


STOP = threading.Event()
SCRIPT_MOUSE = False
STOP_BUTTON_RECT: tuple[int, int, int, int] | None = None
PANEL_RECT: tuple[int, int, int, int] | None = None
# 点击点落在悬浮窗上时，让窗口自己挪到右上角。挪窗口只能在界面线程做。
PARK_PANEL = threading.Event()


def cursor_near(x: int, y: int, tolerance: int = 12) -> bool:
    point = pyautogui.position()
    return abs(point.x - x) <= tolerance and abs(point.y - y) <= tolerance


def ensure_admin() -> None:
    """游戏用管理员身份运行时，普通权限的脚本移动不了鼠标。"""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    script = str(Path(sys.argv[0]).resolve())
    params = " ".join([f'"{script}"', *[f'"{arg}"' for arg in sys.argv[1:]]])
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    code = shell32.ShellExecuteW(None, "runas", sys.executable, params, str(Path(script).parent), 1)
    if not code or code <= 32:
        print("游戏是用管理员身份打开的，脚本权限不够，鼠标不会动。请在弹出的窗口里点「是」。", flush=True)
        raise SystemExit(1)
    raise SystemExit(0)


def log(message: str) -> None:
    print(message, flush=True)
    for listener in list(_log_listeners):
        try:
            listener(message)
        except Exception:
            pass


_log_listeners: list = []


class _KeyHook(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MouseHook(ctypes.Structure):
    _fields_ = [
        ("pt", ctypes.wintypes.POINT),
        ("mouseData", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def install_esc_hook() -> None:
    """游戏在前台时，普通按键查询经常看不到 Esc。用系统钩子，按下就停。"""
    if getattr(install_esc_hook, "started", False):
        return
    install_esc_hook.started = True

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hook_proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)

    def on_key(n_code, w_param, l_param):
        if n_code >= 0 and w_param in (0x0100, 0x0104) and l_param:
            vk = ctypes.cast(l_param, ctypes.POINTER(_KeyHook)).contents.vkCode
            if vk == VK_ESCAPE:
                STOP.set()
                release_mouse()
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def on_mouse(n_code, w_param, l_param):
        # 脚本用 SendInput 点上架，事件带注入标记，不能当成按下了停止。
        # 人的按下没有这个标记，哪怕脚本正按着鼠标，也要停。
        if n_code >= 0 and w_param == 0x0201 and l_param:
            info = ctypes.cast(l_param, ctypes.POINTER(_MouseHook)).contents
            if not (info.flags & 1):
                point = info.pt
                rect = STOP_BUTTON_RECT
                if rect is not None and rect[0] <= point.x <= rect[2] and rect[1] <= point.y <= rect[3]:
                    STOP.set()
                    release_mouse()
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    key_callback = hook_proc(on_key)
    mouse_callback = hook_proc(on_mouse)
    install_esc_hook.callback = key_callback
    install_esc_hook.mouse_callback = mouse_callback
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, hook_proc, ctypes.c_void_p, ctypes.c_uint]

    def loop() -> None:
        key_hook = user32.SetWindowsHookExW(13, key_callback, None, 0)
        mouse_hook = user32.SetWindowsHookExW(14, mouse_callback, None, 0)
        install_esc_hook.hook = key_hook
        install_esc_hook.mouse_hook = mouse_hook
        if not key_hook:
            log("Esc 监听没有装上，按 Esc 可能停不下来")
        if not mouse_hook:
            log("停止按钮的监听没有装上")
        if not key_hook and not mouse_hook:
            return
        message = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    threading.Thread(target=loop, name="esc-hook", daemon=True).start()


def aborted() -> bool:
    if STOP.is_set():
        return True
    state = ctypes.c_short(ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE)).value
    return bool(state & 0x8000)


def hold_script_mouse(holding: bool) -> None:
    """脚本自己按下鼠标时为真。"""
    global SCRIPT_MOUSE
    SCRIPT_MOUSE = holding


def point_on_panel(x: float, y: float) -> bool:
    rect = PANEL_RECT
    if rect is None:
        return False
    return rect[0] - 16 <= x <= rect[2] + 16 and rect[1] - 16 <= y <= rect[3] + 16


def cursor_on_panel() -> bool:
    x, y = pyautogui.position()
    return point_on_panel(x, y)


def yield_to_panel() -> None:
    """鼠标在悬浮窗上时先让一下。半秒内没有按停止，就把窗口挪开，不能一直卡着。"""
    if not cursor_on_panel():
        return
    release_mouse()
    hold_script_mouse(False)
    deadline = time.perf_counter() + 0.45
    while cursor_on_panel() and time.perf_counter() < deadline:
        check_abort()
        time.sleep(0.05)
    if not cursor_on_panel() or STOP.is_set():
        return
    PARK_PANEL.set()
    deadline = time.perf_counter() + 0.7
    while cursor_on_panel() and time.perf_counter() < deadline:
        check_abort()
        time.sleep(0.05)
    if not cursor_on_panel():
        return
    rect = PANEL_RECT
    if rect is None:
        return
    ctypes.windll.user32.SetCursorPos(max(0, rect[0] - 28), max(0, rect[1] + 8))


def check_abort() -> None:
    install_esc_hook()
    if STOP.is_set():
        raise KeyboardInterrupt("已停止")
    if aborted():
        raise KeyboardInterrupt("已按 Esc，停止")


def pause(seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while True:
        check_abort()
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def dwell(low: float, high: float) -> None:
    """在这段时间里随机停一下。同一步有时快、有时慢，不写死一个秒数。"""
    pause(random.uniform(low, high))


class GameWindow:
    def __init__(self, hwnd: int, title: str):
        self.hwnd = hwnd
        self.title = title

    @staticmethod
    def find() -> "GameWindow | None":
        found: list[tuple[int, str, int]] = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd) or ""
            if any(key in title for key in ("暗区", "Arena Breakout", "ArenaBreakout")):
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
                if area > 400 * 300:
                    found.append((hwnd, title, area))

        win32gui.EnumWindows(callback, None)
        if not found:
            return None
        hwnd, title, _ = max(found, key=lambda item: item[2])
        return GameWindow(hwnd, title)

    def focus(self) -> None:
        hwnd = self.hwnd
        user32 = ctypes.windll.user32
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
        foreground = user32.GetForegroundWindow()
        fore_thread = user32.GetWindowThreadProcessId(foreground, None)
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        current = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = []
        for thread in (fore_thread, target_thread):
            if thread and thread != current and thread not in attached:
                if user32.AttachThreadInput(thread, current, True):
                    attached.append(thread)
        try:
            user32.ShowWindow(hwnd, 5)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            # 先置顶再取消置顶，把游戏从编辑器下面拉出来，悬浮窗仍能盖在上面
            flags = 0x0001 | 0x0002 | 0x0040
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)
        finally:
            for thread in attached:
                user32.AttachThreadInput(thread, current, False)
        time.sleep(random.uniform(0.04, 0.13))

    def rect(self) -> tuple[int, int, int, int]:
        left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
        return left, top, right, bottom

    def grab(self) -> tuple[np.ndarray, int, int]:
        left, top, right, bottom = self.rect()
        shot = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        image = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
        return image, left, top


def read_text(image: np.ndarray, min_score: float = 0.45) -> list[Hit]:
    global _ocr_worker
    check_abort()
    height, width = image.shape[:2]
    scale = 1280 / width if width > 1280 else 1.0
    small = cv2.resize(image, (int(width * scale), int(height * scale))) if scale != 1 else image
    if _ocr_worker is None:
        _ocr_worker = _OcrWorker()
    result, _ = _ocr_worker.call(small)
    hits: list[Hit] = []
    if not result:
        check_abort()
        return hits
    for box, text, score in result:
        if score < min_score:
            continue
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        hits.append(
            Hit(
                text=text.replace(" ", ""),
                cx=(min(xs) + max(xs)) / 2 / scale,
                cy=(min(ys) + max(ys)) / 2 / scale,
                score=float(score),
                x1=min(xs) / scale,
                y1=min(ys) / scale,
                x2=max(xs) / scale,
                y2=max(ys) / scale,
            )
        )
    check_abort()
    return hits


# 模板是在 1600 像素高的窗口里截的。别的分辨率按窗口高度缩放后再对。
TEMPLATE_REF_HEIGHT = 1600


def match_template(
    image: np.ndarray,
    template: np.ndarray,
    scales: tuple[float, ...] = (0.75, 0.85, 1.0, 1.15, 1.3),
) -> tuple[float, tuple[int, int] | None]:
    if template is None:
        return 0.0, None
    best_score = 0.0
    best_center = None
    for scale in scales:
        resized = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        th, tw = resized.shape[:2]
        if th >= image.shape[0] or tw >= image.shape[1]:
            continue
        result = cv2.matchTemplate(image, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            best_center = (loc[0] + tw // 2, loc[1] + th // 2)
    return best_score, best_center


def orange_buttons(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 0] < 18) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 70)).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    buttons = []
    height, width = image.shape[:2]
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < width * 0.08 or h < 20 or h > height * 0.08:
            continue
        if not (width * 0.40 < x + w / 2 < width * 0.90):
            continue
        buttons.append((x, y, w, h))
    buttons.sort(key=lambda item: item[1])
    return buttons


def is_contact_tab(text: str) -> bool:
    return text.startswith("联") and text.endswith("人") and 2 <= len(text) <= 4


def is_handguard(text: str) -> bool:
    # 商品名必须是「FAL四导轨标准护木」。枪管/枪托/加长护木都不能点。
    t = text.replace(" ", "")
    if any(k in t for k in (*FAL_HANDGUARD.excluded_name_parts, "，", "。", "消音", "机匣")):
        return False
    if len(t) > 16:
        return False
    # 必须同时像护木：四导轨 + 标准 + 护/木，避免点到「四导轨标准枪管」
    if "四导轨" in t and "标准" in t and ("护木" in t or t.endswith("护") or "标准护" in t):
        return True
    return "四导轨标准护木" in t or ("FAL" in t and "四导轨" in t and "护木" in t and "加长" not in t)


def pick(hits: list[Hit], predicate, *, x_min=None, x_max=None, y_min=None, y_max=None) -> Hit | None:
    chosen = None
    for hit in hits:
        if x_min is not None and hit.cx < x_min:
            continue
        if x_max is not None and hit.cx > x_max:
            continue
        if y_min is not None and hit.cy < y_min:
            continue
        if y_max is not None and hit.cy > y_max:
            continue
        if not predicate(hit.text):
            continue
        if chosen is None or hit.score > chosen.score:
            chosen = hit
    return chosen


class Trader:
    def __init__(self, window: GameWindow, debug: bool = False):
        self.window = window
        self.debug = debug
        self.image: np.ndarray | None = None
        self.origin = (0, 0)
        self.hits: list[Hit] = []
        self.item_at: tuple[float, float, float, float] | None = None

    def refresh(self) -> None:
        check_abort()
        self.grab()
        self.hits = read_text(self.image)

    def grab(self) -> None:
        self.image, left, top = self.window.grab()
        self.origin = (left, top)

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        """把截图像素换算成屏幕坐标。窗口和截图大小不一致时按比例缩放，换分辨率也能点准。"""
        image_h, image_w = self.image.shape[:2]
        left, top, right, bottom = self.window.rect()
        window_w = max(1, right - left)
        window_h = max(1, bottom - top)
        return (
            int(left + x * window_w / image_w),
            int(top + y * window_h / image_h),
        )

    def point_inside(self, x: float, y: float, w: float, h: float, tight: bool = False) -> tuple[float, float]:
        """真正能点的区域比看到的按钮小一圈，而且比 20 像素更宽。落点只在中心附近随机。"""
        if tight:
            span_x = min(max(w * 0.08, 1.0), 8.0)
            span_y = min(max(h * 0.08, 1.0), 5.0)
            return x + w / 2 + random.uniform(-span_x, span_x), y + h / 2 + random.uniform(-span_y, span_y)
        height = self.image.shape[0] if self.image is not None else 1600
        inset = height / 1600 * random.uniform(44, 60)
        inset_x = min(max(inset, w * 0.3), max(1.0, (w - 8) / 2))
        inset_y = min(max(inset, h * 0.3), max(1.0, (h - 8) / 2))
        inner_w = max(2.0, w - inset_x * 2)
        inner_h = max(2.0, h - inset_y * 2)
        span_x = inner_w / 2 * random.uniform(0.15, 0.55)
        span_y = inner_h / 2 * random.uniform(0.15, 0.55)
        return x + w / 2 + random.uniform(-span_x, span_x), y + h / 2 + random.uniform(-span_y, span_y)

    def _glide(self, start_x: float, start_y: float, end_x: float, end_y: float, duration: float) -> bool:
        """把光标滑到终点。路径穿过悬浮窗时跳过去，不要停在窗口上干等。

        人自己把光标移进悬浮窗时返回 False，调用方不要再把光标拽走。
        """
        steps = max(1, int(duration / 0.02))
        for index in range(1, steps + 1):
            check_abort()
            t = index / steps
            nx = int(start_x + (end_x - start_x) * t)
            ny = int(start_y + (end_y - start_y) * t)
            if point_on_panel(nx, ny):
                continue
            ctypes.windll.user32.SetCursorPos(nx, ny)
            if cursor_on_panel():
                yield_to_panel()
                check_abort()
                return False
            pause(duration / steps)
        return True

    def move_to(self, screen_x: int, screen_y: int, quick: bool) -> None:
        check_abort()
        avoid_panel(screen_x, screen_y)
        yield_to_panel()
        check_abort()
        start_x, start_y = pyautogui.position()
        distance = ((screen_x - start_x) ** 2 + (screen_y - start_y) ** 2) ** 0.5
        if distance > 220:
            bend = min(42.0, distance * 0.07)
            side = random.choice((-1.0, 1.0))
            mid_x = (start_x + screen_x) / 2 + side * random.uniform(10, bend)
            mid_y = (start_y + screen_y) / 2 + random.uniform(-bend * 0.35, bend * 0.35)
            if not self._glide(start_x, start_y, mid_x, mid_y, random.uniform(0.02, 0.07)):
                return
            start_x, start_y = pyautogui.position()
        self._glide(start_x, start_y, screen_x, screen_y, random.uniform(0.05, 0.16))

    def aim(self, screen_x: int, screen_y: int, quick: bool) -> None:
        """把光标放到按钮上。游戏刚关窗口时会把光标拉回去，多放几次再点。"""
        for attempt in range(4):
            check_abort()
            avoid_panel(screen_x, screen_y)
            self.move_to(screen_x, screen_y, quick)
            check_abort()
            if cursor_on_panel():
                yield_to_panel()
                check_abort()
            if hover_at(screen_x, screen_y):
                return
            if attempt == 0:
                log("  鼠标没停住，再放一次")
            pause(0.12)
        if ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError("鼠标没有停在按钮上，这一下没点下去")
        raise RuntimeError("鼠标没有移动。游戏是管理员身份运行的，请允许这个脚本也用管理员身份打开。")

    def click(
        self,
        x: float,
        y: float,
        label: str,
        quick: bool = False,
        box: tuple[float, float, float, float] | None = None,
        tight: bool = False,
    ) -> None:
        check_abort()
        if box is not None:
            x, y = self.point_inside(*box, tight=tight)
        screen_x, screen_y = self.to_screen(x, y)
        log(f"  点击 {label}  ({screen_x}, {screen_y})")
        if self.debug and self.image is not None:
            marked = self.image.copy()
            cv2.circle(marked, (int(x), int(y)), 18, (0, 0, 255), 2)
            cv2.imwrite(str(ROOT / "debug_last.jpg"), marked)
        self.aim(screen_x, screen_y, quick)
        press_mouse("left")
        dwell(0.22, 0.52) if quick else dwell(0.09, 0.30)

    def wheel(self, notches: int) -> None:
        """按真实滚轮格数滚动。notches 为负是向下。"""
        if notches == 0:
            return
        step = WHEEL_NOTCH if notches > 0 else -WHEEL_NOTCH
        for _ in range(abs(notches)):
            check_abort()
            ctypes.windll.user32.mouse_event(0x0800, 0, 0, step, 0)
            time.sleep(random.uniform(0.004, 0.013))

    def wait_until(self, predicate, timeout: float, description: str) -> bool:
        # 先等界面停稳再截图。识别要几秒，不能因为超时把这次结果丢掉。
        deadline = time.time() + timeout
        while True:
            dwell(0.07, 0.20)
            check_abort()
            self.refresh()
            if predicate():
                return True
            if time.time() >= deadline:
                log(f"  等待超时：{description}")
                return False

    def wait_visual(self, before: np.ndarray, timeout: float = 2.5, threshold: float = 18.0) -> bool:
        """只看画面变没变，不跑文字识别。用来确认这一下有没有点进新页面。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            check_abort()
            self.grab()
            if self.center_changed(before, threshold):
                return True
            pause(random.uniform(0.08, 0.22))
        return False

    def click_contact_nav(self, lift: float = 0) -> None:
        height = self.image.shape[0]
        hit = pick(self.hits, is_contact_tab, y_min=height * 0.78)
        if hit is None:
            raise RuntimeError("底部没有找到「联络人」。")
        text_h = max(1.0, hit.y2 - hit.y1)
        self.click(
            hit.cx,
            hit.cy,
            "联络人",
            box=(hit.x1, hit.y1 - lift, hit.x2 - hit.x1, text_h + lift),
        )

    def on_market_screen(self) -> bool:
        """市场的出售页。顶栏「购买」是页签，不是联络人商店里的购买按钮。"""
        assert self.image is not None
        height = self.image.shape[0]
        blob = "".join(hit.text for hit in self.hits)
        if any(key in blob for key in ("我的上架", "上架物品", "可上架", "上架槽", "批量出售", "我的交易")):
            return True
        top_buy = pick(self.hits, lambda text: text == "购买", y_max=height * 0.16)
        top_sell = pick(self.hits, lambda text: "出售" in text, y_max=height * 0.16)
        return top_buy is not None and top_sell is not None

    def leave_market(self) -> None:
        """出售页底部点「联络人」，离开市场后再找兰德尔。"""
        if not self.on_market_screen():
            return
        log("离开出售页，点击底部「联络人」")
        height = self.image.shape[0]
        for lift_ratio in (0.0, 0.03, 0.055):
            before = self.center_signature()
            self.window.focus()
            self.click_contact_nav(height * lift_ratio)
            self.wait_visual(before, timeout=2.2, threshold=8)
            self.refresh()
            if not self.on_market_screen():
                log("已回到联络人页面")
                return
            log("还在出售页，再点一次「联络人」")
        raise RuntimeError("没有从出售页回到联络人。请确认底部能看到「联络人」。")

    def find_randall(self) -> Hit | None:
        width = self.image.shape[1]
        return pick(
            self.hits,
            lambda text: "兰德尔" in text or "费舍" in text,
            x_max=width * 0.62,
        )

    def on_home(self) -> bool:
        height = self.image.shape[0]
        has_nav = pick(self.hits, is_contact_tab, y_min=height * 0.78) is not None
        has_home = any(
            any(key in hit.text for key in ("每日任务", "进入暗", "限时商品", "感染区"))
            for hit in self.hits
        )
        return has_nav and has_home

    def center_signature(self) -> np.ndarray:
        height, width = self.image.shape[:2]
        crop = self.image[int(height * 0.18) : int(height * 0.78), int(width * 0.12) : int(width * 0.88)]
        return cv2.resize(crop, (48, 27))

    def center_changed(self, before: np.ndarray, threshold: float = 18.0) -> bool:
        now = self.center_signature()
        if before.shape != now.shape:
            return True
        return float(cv2.absdiff(before, now).mean()) > threshold

    def left_contact_page(self, before: np.ndarray) -> bool:
        if self.find_randall() is not None:
            return True
        # 首页特征还在，说明这一下没点进去。角色动画不算离开首页。
        return not self.on_home() and self.center_changed(before)

    def enter_contact_list(self) -> None:
        self.window.focus()
        self.refresh()
        if self.on_market_screen():
            self.leave_market()
        if self.find_randall() is not None:
            return
        if not self.on_home() and not self.on_market_screen():
            return
        height = self.image.shape[0]
        # 按窗口高度的比例上移，不写死像素，1080p / 2K / 4K 都点在按钮上
        for lift_ratio in (0.0, 0.03, 0.055):
            before = self.center_signature()
            self.window.focus()
            self.click_contact_nav(height * lift_ratio)
            if not self.wait_visual(before):
                log("  画面没有变化，换一个位置再点「联络人」")
                continue
            log("  页面已切换")
            self.hits = read_text(self.image)
            if self.on_market_screen():
                log("  还在出售页")
                continue
            if self.find_randall() is not None or not self.on_home():
                return
            dwell(0.16, 0.42)
            self.refresh()
            if self.on_market_screen():
                continue
            if self.find_randall() is not None or not self.on_home():
                return
            log("  还在首页，换一个位置再点「联络人」")
        raise RuntimeError("没有进入联络人列表。请确认游戏停在首页，并且没有别的窗口挡在上面。")

    def reveal_randall(self) -> Hit:
        self.refresh()
        if self.on_market_screen():
            self.leave_market()
        hit = self.find_randall()
        if hit is not None:
            return hit
        for _ in range(8):
            log("  联络人列表里还没有兰德尔，向下翻")
            self.scroll_list(-8)
            self.refresh()
            hit = self.find_randall()
            if hit is not None:
                return hit
        for _ in range(8):
            log("  往回翻联络人列表")
            self.scroll_list(8)
            self.refresh()
            hit = self.find_randall()
            if hit is not None:
                return hit
        raise RuntimeError("联络人列表里没有找到「兰德尔·费舍」")

    def click_randall(self) -> None:
        hit = self.reveal_randall()
        self.click(hit.cx, hit.cy, "兰德尔·费舍", box=(hit.x1, hit.y1, hit.x2 - hit.x1, hit.y2 - hit.y1))

    def contact_buy_box(self) -> tuple[float, float, float, float] | None:
        """联络人卡片底部的购买。字贴在按钮右侧，点在字上游戏不认，要点左边的空处。"""
        assert self.image is not None
        height, width = self.image.shape[:2]
        hit = pick(self.hits, lambda text: text == "购买", y_min=height * 0.4)
        if hit is None:
            return None
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        y = int(min(max(hit.cy, 0), height - 1))
        runs: list[tuple[int, int]] = []
        start: int | None = None
        for index, value in enumerate(gray[y]):
            if value > 120 and start is None:
                start = index
            elif value <= 120 and start is not None:
                if index - start >= 36:
                    runs.append((start, index))
                start = None
        if start is not None and width - start >= 36:
            runs.append((start, width))
        candidates = [run for run in runs if run[1] <= hit.cx + 8 and hit.cx - run[1] < 90 and hit.cx - run[0] < 460]
        if not candidates:
            candidates = [run for run in runs if run[0] <= hit.cx <= run[1]]
        if not candidates:
            return None
        left, right = max(candidates, key=lambda run: run[1])
        pad_x = (left + right) // 2
        top = bottom = y
        while top > 0 and gray[top - 1, pad_x] > 120:
            top -= 1
        while bottom < height - 1 and gray[bottom + 1, pad_x] > 120:
            bottom += 1
        if bottom - top < 16 or right - left < 36:
            return None
        return (float(left), float(top), float(right - left), float(bottom - top))

    def click_buy(self) -> None:
        for attempt in range(3):
            check_abort()
            if attempt:
                self.grab()
                if self.shop_open():
                    return
                self.hits = read_text(self.image)
            box = self.contact_buy_box()
            if box is None:
                height = self.image.shape[0] if self.image is not None else 0
                hit = pick(self.hits, lambda text: text == "购买", y_min=height * 0.4)
                if hit is None:
                    if attempt:
                        return
                    raise RuntimeError("没有找到「购买」")
                shift = max(hit.x2 - hit.x1, 28.0)
                box = (hit.x1 - shift, hit.y1 - 8.0, shift, max(hit.y2 - hit.y1, 16.0) + 16.0)
            if attempt:
                log("  还停在联络人卡片上，再点一次「购买」")
            self.window.focus()
            self.click(0, 0, "购买", tight=True, box=box)
            if self.wait_shop(2.0):
                return
        raise RuntimeError("点击购买后没有进入商店")

    def core_parts_state(self) -> tuple[str, tuple[int, int] | None]:
        assert self.image is not None
        height, width = self.image.shape[:2]
        x1, y1 = 0, int(height * 0.10)
        x2, y2 = int(width * 0.55), int(height * 0.28)
        crop = self.image[y1:y2, x1:x2]

        def shift(point):
            if point is None:
                return None
            return (point[0] + x1, point[1] + y1)

        factor = self.image.shape[0] / TEMPLATE_REF_HEIGHT
        scales = (factor * 0.82, factor, factor * 1.18)
        on_score, on_at = match_template(crop, TEMPLATE_ON, scales)
        off_score, off_at = match_template(crop, TEMPLATE_OFF, scales)
        if on_score >= 0.72 and on_score >= off_score:
            return "on", shift(on_at)
        if off_score >= 0.72:
            return "off", shift(off_at)
        return "missing", None

    def shop_tab_hits(self) -> list[Hit]:
        """商店顶上那排页签。只认这一条，避免整屏识别把购买后的切换拖过超时。"""
        assert self.image is not None
        height, width = self.image.shape[:2]
        y1, y2 = int(height * 0.10), int(height * 0.24)
        x2 = int(width * 0.55)
        hits = read_text(self.image[y1:y2, 0:x2])
        for hit in hits:
            hit.cy += y1
            hit.y1 += y1
            hit.y2 += y1
        return hits

    def shop_open(self) -> bool:
        """枪械页也算已经进店。核心配件没亮时，模板对不上不能当成还在店外。"""
        if self.core_parts_state()[0] != "missing":
            return True
        keys = ("核心", "心配", "枪械", "功能配件", "弹匣", "弹药", "头套", "耳机")
        blob = "".join(hit.text for hit in self.shop_tab_hits())
        return any(key in blob for key in keys)

    def wait_shop(self, timeout: float = 8) -> bool:
        """购买后的进店动画里，先看页签图案。整屏识别太慢，会把已经打开的商店判成没进去。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            check_abort()
            self.grab()
            if self.core_parts_state()[0] != "missing":
                return True
            dwell(0.08, 0.2)
        self.grab()
        return self.shop_open()

    def click_core_parts(self) -> None:
        state, point = self.core_parts_state()
        if state == "on":
            log("  已在「核心配件」")
            return
        if point is None:
            hit = pick(
                self.shop_tab_hits(),
                lambda text: ("核心" in text or "心配" in text) and "功能" not in text,
            )
            if hit is None:
                raise RuntimeError("没有找到「核心配件」页签")
            self.click(
                hit.cx,
                hit.cy,
                "核心配件",
                box=(hit.x1, hit.y1, max(hit.x2 - hit.x1, 48), max(hit.y2 - hit.y1, 24)),
            )
            return
        scale = self.image.shape[0] / TEMPLATE_REF_HEIGHT
        tab_w, tab_h = 160 * scale, 56 * scale
        self.click(
            point[0],
            point[1],
            "核心配件",
            box=(point[0] - tab_w / 2, point[1] - tab_h / 2, tab_w, tab_h),
        )

    def handguard(self) -> Hit | None:
        height, width = self.image.shape[:2]
        # 只认商店列表里的那一张，避开详情标题和确认框里的同名文字
        return pick(
            self.hits,
            is_handguard,
            x_max=width * 0.48,
            y_min=height * 0.25,
            y_max=height * 0.82,
        )

    def scroll_list(self, notches: int) -> None:
        height, width = self.image.shape[:2]
        list_w, list_h = width * 0.22, height * 0.28
        origin_x = width * random.uniform(0.12, 0.28)
        origin_y = height * random.uniform(0.32, 0.48)
        x, y = self.point_inside(origin_x, origin_y, list_w, list_h)
        screen_x, screen_y = self.to_screen(x, y)
        self.move_to(screen_x, screen_y, True)
        self.wheel(notches)
        dwell(0.05, 0.20)

    def reveal_handguard(self) -> Hit:
        hit = self.handguard()
        if hit is not None:
            return hit
        for _ in range(6):
            log("  商品列表里还没有目标，向下翻")
            self.scroll_list(-42)
            self.refresh()
            hit = self.handguard()
            if hit is not None:
                return hit
        for _ in range(6):
            log("  往回翻商品列表")
            self.scroll_list(24)
            self.refresh()
            hit = self.handguard()
            if hit is not None:
                return hit
        raise RuntimeError("核心配件里没有找到「FAL四导轨标准护木」")

    def click_handguard(self, quick: bool = False) -> None:
        hit = self.reveal_handguard()
        height = self.image.shape[0]
        click_y = hit.cy + height * ITEM_CLICK_DY
        if click_y > height * 0.88:
            self.scroll_list(6)
            self.refresh()
            hit = self.reveal_handguard()
            click_y = hit.cy + height * ITEM_CLICK_DY
        card_w = max(hit.x2 - hit.x1, self.image.shape[1] * 0.08)
        card_h = self.image.shape[0] * 0.05
        self.item_at = (hit.cx - card_w / 2, click_y - card_h / 2, card_w, card_h)
        self.click(hit.cx, click_y, "FAL四导轨标准护木", quick=quick, box=self.item_at)

    def poll(self, ready, timeout: float, *, settle: tuple[float, float] | None = (0.28, 0.60)) -> bool:
        # 先等弹窗开始出现，避免点完立刻用旧画面做判断。
        # 刚点过的那一步可以传更短的 settle，免得同一次点击被等两轮。
        if settle is not None:
            dwell(*settle)
        deadline = time.time() + timeout
        while time.time() < deadline:
            check_abort()
            self.grab()
            if ready():
                return True
            time.sleep(random.uniform(0.02, 0.07))
        return False

    def action_point(self) -> tuple[float, float, float, float] | None:
        buttons = orange_buttons(self.image)
        if not buttons:
            return None
        x, y, w, h = max(buttons, key=lambda item: item[1])
        if y + h / 2 < self.image.shape[0] * 0.62:
            return None
        return x, y, w, h

    def confirm_point(self) -> tuple[float, float, float, float] | None:
        buttons = orange_buttons(self.image)
        height = self.image.shape[0]
        chosen = None
        for x, y, w, h in buttons:
            cy = y + h / 2
            if abs(cy - height * 0.54) > height * 0.08:
                continue
            if chosen is None or abs(cy - height * 0.54) < abs((chosen[1] + chosen[3] / 2) - height * 0.54):
                chosen = (x, y, w, h)
        return chosen

    def open_item(self) -> None:
        if self.item_at is not None:
            self.click(0, 0, "FAL四导轨标准护木", quick=True, box=self.item_at)
            if self.poll(lambda: self.action_point() or self.confirm_point(), 2.4):
                return
            log("  原位置没有打开兑换，重新找商品")
            self.item_at = None
        self.refresh()
        self.click_handguard(quick=True)

    def dialog_open(self) -> bool:
        return any("道具兑换" in hit.text or "兑换列表" in hit.text for hit in self.hits)

    def confirm_open(self) -> bool:
        return any("是否兑换" in hit.text or "已集齐" in hit.text for hit in self.hits)

    def click_orange(self, which: str) -> bool:
        assert self.image is not None
        buttons = orange_buttons(self.image)
        if not buttons:
            return False
        if which == "confirm":
            button = min(buttons, key=lambda item: abs((item[1] + item[3] / 2) - self.image.shape[0] * 0.54))
            label = "确定"
        else:
            button = max(buttons, key=lambda item: item[1])
            text = pick(
                self.hits,
                lambda name: name in ("补齐道具", "兑换"),
                y_min=self.image.shape[0] * 0.7,
            )
            label = text.text if text is not None else "兑换按钮"
        x, y, w, h = button
        self.click(x + w / 2, y + h / 2, label, box=(x, y, w, h))
        return True

    def action_button_text(self) -> Hit | None:
        height = self.image.shape[0]
        return pick(self.hits, lambda name: name in ("补齐道具", "兑换"), y_min=height * 0.7)

    def red_block_banner(self) -> bool:
        """兑换窗底部那条红字：空间不足，不能再点补齐或确定。

        「补齐道具」本身是橙色实心按钮，色相大约 5，会落进这段红色范围。
        它在就说明还能兑换，不能当成仓库已满。
        """
        assert self.image is not None
        height, width = self.image.shape[:2]
        y1, y2 = int(height * 0.74), int(height * 0.90)
        x1, x2 = int(width * 0.38), int(width * 0.82)
        hsv = cv2.cvtColor(self.image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        red = (((hue <= 8) | (hue >= 168)) & (sat >= 90) & (val >= 40)).astype(np.uint8)
        count, _, stats, _ = cv2.connectedComponentsWithStats(red, 8)
        for index in range(1, count):
            bx, by, bw, bh, area = stats[index]
            if not (bw > width * 0.12 and 4 < bh < height * 0.06 and area > bw * bh * 0.35):
                continue
            fill = area / max(bw * bh, 1)
            patch = hue[by : by + bh, bx : bx + bw]
            mask = red[by : by + bh, bx : bx + bw] > 0
            # 橙色按钮几乎铺满，色相在 5 左右。红字更接近 0 或 180。
            if fill > 0.75 and mask.any() and 4 <= float(np.median(patch[mask])) <= 12:
                continue
            return True
        return False

    def warehouse_full(self) -> bool:
        """兑换窗上的「空间不足 / 仓库已满」。商店背景上的同名文字不在这块区域里。"""
        assert self.image is not None
        if self.red_block_banner():
            return True
        height, width = self.image.shape[:2]
        x1, y1 = int(width * 0.40), int(height * 0.58)
        x2, y2 = int(width * 0.92), int(height * 0.93)
        hits = read_text(self.image[y1:y2, x1:x2])
        keys = (
            "空间不足",
            "空间不足无法购买全部物品",
            "仓库已满",
            "仓库满",
            "背包已满",
            "无法放入",
        )
        return any(any(key in hit.text for key in keys) for hit in hits)

    def exchange_limit_reached(self) -> bool:
        """Recognize trader purchase limits separately from storage capacity."""
        assert self.image is not None
        height, width = self.image.shape[:2]
        hits = read_text(
            self.image[
                int(height * 0.12) : int(height * 0.92),
                int(width * 0.35) : int(width * 0.94),
            ]
        )
        blob = "".join(hit.text for hit in hits)
        return any(marker in blob for marker in FAL_HANDGUARD.exchange_limit_markers)

    def fill_props(self) -> None:
        """补齐时可能买到更低价，或那件已经被别人买走。按钮还在就再点。"""
        if self.warehouse_full():
            raise WarehouseFull("空间不足，停止兑换")
        for attempt in range(8):
            if self.warehouse_full():
                raise WarehouseFull("空间不足，停止兑换")
            if self.confirm_point() is not None:
                return
            point = self.action_point()
            if point is None:
                if not self.poll(lambda: self.confirm_point() or self.action_point(), 2.6):
                    break
                if self.confirm_point() is not None:
                    return
                point = self.action_point()
                if point is None:
                    break
            if attempt:
                log("  这次没有买到对应道具，再点一次「补齐道具」")
            self.click(0, 0, "补齐道具", quick=True, box=point)
            if self.poll(lambda: self.confirm_point() is not None or self.red_block_banner(), 3.2):
                if self.warehouse_full():
                    raise WarehouseFull("空间不足，停止兑换")
                return
        if self.warehouse_full():
            raise WarehouseFull("仓库已满")
        raise RuntimeError("没有出现确定")

    def exchange_once(self) -> None:
        self.open_item()
        if not self.poll(lambda: self.action_point() or self.confirm_point(), 3.2):
            if self.exchange_limit_reached():
                raise ExchangeLimitReached("该货物已达到兑换上限")
            raise RuntimeError("点击护木后没有打开道具兑换")
        if self.exchange_limit_reached():
            raise ExchangeLimitReached("该货物已达到兑换上限")
        if self.warehouse_full():
            raise WarehouseFull("空间不足，停止兑换")
        if self.confirm_point() is None:
            self.fill_props()
        if self.warehouse_full():
            raise WarehouseFull("空间不足，停止兑换")
        point = self.confirm_point()
        if point is None:
            raise RuntimeError("没有找到「确定」")
        self.click(0, 0, "确定", quick=True, box=point)
        if not self.poll(lambda: self.confirm_point() is None and self.action_point() is None, 4.2):
            if self.warehouse_full():
                raise WarehouseFull("仓库已满")
            raise RuntimeError("点了确定，兑换窗口没有关闭")
        log("  兑换窗口已关闭")

    def open_shop(self) -> None:
        log("进入联络人")
        self.refresh()
        if self.dialog_open():
            raise RuntimeError("屏幕上已经开着兑换窗口，请先手动关掉再运行")

        if self.on_market_screen():
            self.leave_market()
        header = pick(self.hits, lambda text: "兰德尔" in text or "费舍" in text, x_max=self.image.shape[1] * 0.4, y_max=self.image.shape[0] * 0.2)
        if (header is not None and self.shop_open()) or self.core_parts_state()[0] != "missing":
            log("已经在兰德尔的商店")
        else:
            height = self.image.shape[0]
            buy = pick(self.hits, lambda text: text == "购买", y_min=height * 0.16)
            if buy is None:
                self.enter_contact_list()
            self.click_randall()
            if not self.wait_until(
                lambda: pick(self.hits, lambda text: text == "购买", y_min=self.image.shape[0] * 0.16) is not None,
                4,
                "购买按钮",
            ):
                raise RuntimeError("选中兰德尔后没有出现「购买」")
            self.click_buy()

        log("打开核心配件")
        self.click_core_parts()
        if not self.wait_until(
            lambda: self.core_parts_state()[0] == "on" or self.handguard() is not None,
            4,
            "核心配件商品",
        ):
            raise RuntimeError("没有切到核心配件")


def run_exchange(count: int, skip_nav: bool = False, debug: bool = False, countdown: bool = True) -> int:
    if count < 1:
        log("兑换次数至少为 1")
        return 2
    if TEMPLATE_OFF is None or TEMPLATE_ON is None:
        log("缺少 templates/core_parts.png，无法识别「核心配件」页签")
        return 2

    window = GameWindow.find()
    if window is None:
        log("没有找到游戏窗口。请先打开《暗区突围无限》，标题里需要能看到「暗区」。")
        return 1
    left, top, right, bottom = window.rect()
    log(f"游戏窗口：{window.title}（{right - left}×{bottom - top}）")
    window.focus()
    if countdown:
        log("3 秒后开始。请停在游戏首页，脚本会自己进入联络人。")
        for second in (3, 2, 1):
            check_abort()
            log(f"  {second}…")
            pause(1)

    trader = Trader(window, debug=debug)
    if not skip_nav:
        trader.open_shop()
    else:
        trader.refresh()
        trader.click_core_parts()

    done = 0
    for index in range(1, count + 1):
        log(f"第 {index}/{count} 次兑换")
        try:
            trader.exchange_once()
        except ExchangeLimitReached:
            log("该货物已达到兑换上限，停止兑换")
            break
        except WarehouseFull:
            log("仓库已满，不能继续兑换")
            break
        done += 1
    log(f"完成，共兑换 {done} 次。")
    return 0


def shift_hits(hits, dx: float, dy: float):
    for hit in hits:
        hit.cx += dx
        hit.cy += dy
        hit.x1 += dx
        hit.y1 += dy
        hit.x2 += dx
        hit.y2 += dy
    return hits


class NoStock(RuntimeError):
    """仓库列表从头到尾都没有护木。"""


SELL_TAB_MARKERS = ("我的上架", "上架物品", "上架槽", "可上架")


class Seller(Trader):
    """上架。

    不再一个个点右边那一排容器图标。市场「出售」页右侧是一条连续滚动的列表，
    所有容器首尾相接，护木可能在任何一个容器里。这里直接滚这条列表，
    用护木图标的模板找格子（格子上的缩写文字 OCR 认不准，图标本身却一模一样），
    找到就把鼠标放上去按 F。
    """

    SCAN_MAX_STEPS = 90

    def __init__(self, window: GameWindow, debug: bool = False):
        super().__init__(window, debug)
        self.price_low: int | None = None
        self.price_high: int | None = None
        self._scale: float | None = None
        self._px_per_notch: float | None = None
        self._failed: list[tuple[float, float]] = []

    # ------------------------------------------------------------ 屏幕读取

    def read_crop(self, x1: int, y1: int, x2: int, y2: int):
        assert self.image is not None
        height, width = self.image.shape[:2]
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(width, int(x2)), min(height, int(y2))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return []
        crop = self.mask_panel(self.image[y1:y2, x1:x2], x1, y1)
        return shift_hits(read_text(crop), x1, y1)

    def mask_panel(self, crop: np.ndarray, x1: int, y1: int) -> np.ndarray:
        """悬浮窗自己的字（自动、兑换、上架、售价…）不是游戏界面，识别前涂黑。"""
        rect = PANEL_RECT
        if rect is None or self.image is None:
            return crop
        left, top, right, bottom = self.window.rect()
        scale_x = self.image.shape[1] / max(1, right - left)
        scale_y = self.image.shape[0] / max(1, bottom - top)
        px1 = int((rect[0] - left) * scale_x) - x1
        py1 = int((rect[1] - top) * scale_y) - y1
        px2 = int((rect[2] - left) * scale_x) - x1
        py2 = int((rect[3] - top) * scale_y) - y1
        height, width = crop.shape[:2]
        px1, py1, px2, py2 = max(0, px1), max(0, py1), min(width, px2), min(height, py2)
        if px2 <= px1 or py2 <= py1:
            return crop
        masked = crop.copy()
        masked[py1:py2, px1:px2] = 0
        return masked

    def list_button(self):
        point = self.action_point()
        if point is None:
            return None
        if point[1] + point[3] / 2 < self.image.shape[0] * 0.74:
            return None
        return point

    def fill_hits(self):
        assert self.image is not None
        height, width = self.image.shape[:2]
        return self.read_crop(int(width * 0.40), int(height * 0.70), int(width * 0.95), int(height * 0.95))

    def close_exchange(self) -> None:
        """兑换窗口还开着（空间不足、补齐不了）时，点左边的「取消」关掉。"""
        self.grab()
        point = self.action_point()
        if point is None:
            return
        blob = "".join(hit.text for hit in self.fill_hits())
        if not any(key in blob for key in ("补齐", "兑换", "取消", "价格", "空间不足")):
            return
        x, y, w, h = point
        gap = max(6.0, w * 0.03)
        box = (max(0.0, x - w - gap), y, w, h)
        log("关掉兑换窗口")
        self.click(0, 0, "取消", quick=True, box=box)
        if not self.poll(lambda: self.list_button() is None, 2.0, settle=(0.06, 0.14)):
            raise RuntimeError("兑换窗口没有关掉")

    def close_listing(self) -> None:
        """出售弹窗还开着时，先点「取消」，底部的联络人才点得进去。"""
        self.grab()
        point = self.list_button()
        if point is None:
            return
        x, y, w, h = point
        gap = max(6.0, w * 0.03)
        box = (max(0.0, x - w - gap), y, w, h)
        log("关掉出售窗口")
        self.click(0, 0, "取消", quick=True, box=box)
        if not self.poll(lambda: self.list_button() is None, 2.0, settle=(0.06, 0.14)):
            raise RuntimeError("出售窗口没有关掉")

    # ------------------------------------------------------------ 进入市场

    def market_top(self):
        """顶栏左半边：购买 / 出售 / 我的交易，以及「我的上架物品」。"""
        assert self.image is not None
        height, width = self.image.shape[:2]
        hits = self.read_crop(0, 0, int(width * 0.62), int(height * 0.22))
        return "".join(hit.text for hit in hits), hits

    def click_bottom_nav(self, matcher, label: str, fallback_x: float, lift: float = 0.0) -> None:
        """点底部导航。先认字，认不到就按比例位置点。"""
        self.grab()
        assert self.image is not None
        height, width = self.image.shape[:2]
        y0 = int(height * 0.86)
        hits = shift_hits(read_text(self.image[y0:]), 0, y0)
        hit = pick(hits, matcher, y_min=height * 0.86)
        if hit is not None:
            text_h = max(1.0, hit.y2 - hit.y1)
            box = (hit.x1, hit.y1 - lift, max(hit.x2 - hit.x1, width * 0.03), text_h + lift)
        else:
            cx, cy = width * fallback_x, height * 0.934
            box = (cx - width * 0.025, cy - height * 0.012 - lift, width * 0.05, height * 0.024 + lift)
        self.click(0, 0, label, box=box)

    def sell_page_open(self) -> tuple[bool, bool, list]:
        """返回 (已在出售页, 能看到购买/出售/我的交易页签, 顶栏文字)。

        出售页的标志有两处：顶栏「我的上架物品」，右下角「批量出售 / 上架物品」。
        页面是淡入的，顶栏那行小字常常晚一两秒才认得出，所以两处都看。
        """
        assert self.image is not None
        height, width = self.image.shape[:2]
        blob, hits = self.market_top()
        if any(key in blob for key in SELL_TAB_MARKERS):
            return True, True, hits
        tabs = "我的交易" in blob or ("购买" in blob and "出售" in blob)
        if tabs:
            low = "".join(
                hit.text for hit in self.read_crop(int(width * 0.50), int(height * 0.84), width, int(height * 0.96))
            )
            if "批量出售" in low or "上架物品" in low:
                return True, True, hits
        return False, tabs, hits

    def open_market_sell(self, timeout: float = 45.0) -> None:
        """进到市场的「出售」页。已经在就什么都不点。

        点完之后页面要几秒才换好。这里按时间一直看，不会因为这一秒还没换好就再点一次。
        同一个按钮至少隔几秒才会再点，最多各点三次。
        """
        self.window.focus()
        deadline = time.time() + timeout
        nav_at = tab_at = 0.0
        nav_clicks = tab_clicks = 0
        while time.time() < deadline:
            check_abort()
            self.grab()
            on_sell, tabs, hits = self.sell_page_open()
            if on_sell:
                log("已在市场出售页")
                return
            height, width = self.image.shape[:2]
            now = time.time()
            if tabs:
                if tab_clicks < 3 and now - tab_at > 4.0:
                    tab = pick(hits, lambda text: text == "出售" or text.endswith("出售"), x_max=width * 0.30, y_max=height * 0.13)
                    if tab is not None:
                        self.click(tab.cx, tab.cy, "出售页签", box=(tab.x1, tab.y1, tab.x2 - tab.x1, tab.y2 - tab.y1))
                    else:
                        self.click(0, 0, "出售页签", box=(width * 0.095, height * 0.06, width * 0.05, height * 0.05))
                    tab_at, tab_clicks = time.time(), tab_clicks + 1
            elif nav_clicks < 3 and now - nav_at > 8.0:
                log("进入市场")
                lift = height * (0.0, 0.03, 0.055)[nav_clicks]
                self.click_bottom_nav(lambda text: text.endswith("市场") and len(text) <= 4, "市场", 0.64, lift)
                nav_at, nav_clicks = time.time(), nav_clicks + 1
            dwell(0.3, 0.5)
        raise RuntimeError("没有进入市场的「出售」页。请确认底部能看到「市场」，并且没有弹窗挡着。")

    # ------------------------------------------------------------ 找护木

    def handguard_boxes(self) -> list[tuple[float, float, float, float]]:
        assert self.image is not None
        boxes, scale = vision.find_cells(self.image, HANDGUARD_TEMPLATE, scale_hint=self._scale)
        if scale is not None:
            self._scale = scale
        return boxes

    def pane_screen_box(self) -> tuple[int, int, int, int]:
        assert self.image is not None
        x1, y1, x2, y2 = vision.pane_box(self.image)
        left, top = self.to_screen(x1, y1)
        right, bottom = self.to_screen(x2, y2)
        return left, top, right, bottom

    def settle_pane(self, timeout: float = 1.0) -> None:
        """列表滚动、格子加载都要时间。等右侧画面停稳再去认，不然会把还没出来的护木漏掉。"""
        self.grab()
        previous = vision.pane_gray(self.image)
        deadline = time.time() + timeout
        while time.time() < deadline:
            pause(0.10)
            self.grab()
            current = vision.pane_gray(self.image)
            if vision.same_view(previous, current, 0.8):
                return
            previous = current

    def scroll_pane(self, notches: int) -> None:
        """鼠标放在右侧列表里滚。光标已经在里面就不再挪。"""
        assert self.image is not None
        left, top, right, bottom = self.pane_screen_box()
        point = pyautogui.position()
        inside = left + 20 < point.x < right - 20 and top + 20 < point.y < bottom - 20
        if not inside:
            height, width = self.image.shape[:2]
            x, y = self.point_inside(width * 0.70, height * 0.36, width * 0.12, height * 0.28)
            screen_x, screen_y = self.to_screen(x, y)
            self.move_to(screen_x, screen_y, True)
            if not cursor_near(screen_x, screen_y, tolerance=120):
                return
        self.wheel(notches)

    def page_notches(self) -> int:
        """滚一页要几格。第一次不知道，先滚 2 格量一下像素，再算。"""
        if self._px_per_notch is None:
            return 2
        assert self.image is not None
        pane_h = vision.pane_gray(self.image).shape[0]
        return max(1, min(14, int(pane_h * 0.7 / max(self._px_per_notch, 1.0))))

    def scroll_step(self, direction: int) -> bool:
        """direction=+1 往上翻一页，-1 往下翻一页。到头了返回 False。"""
        assert self.image is not None
        before = vision.pane_gray(self.image)
        notches = self.page_notches()
        self.scroll_pane(direction * notches)
        self.settle_pane()
        after = vision.pane_gray(self.image)
        if vision.same_view(before, after, 1.2):
            return False
        shift = vision.measure_scroll_shift(before, after)
        if shift is not None and abs(shift) >= 4:
            self._px_per_notch = abs(shift) / notches
        self._failed.clear()
        return True

    def scroll_to_end(self, direction: int) -> None:
        """一次滚很多格，直到画面不再变化，也就是到底（-1）或到顶（+1）。"""
        for _ in range(40):
            check_abort()
            before = vision.pane_gray(self.image)
            self.scroll_pane(direction * 30)
            self.settle_pane(0.7)
            if vision.same_view(before, vision.pane_gray(self.image), 2.0):
                break
        self._failed.clear()

    def locate_handguards(self) -> list[tuple[float, float, float, float]]:
        """返回当前能点的护木格子。这一屏没有，就从底滚到顶把整条列表找一遍。"""
        self.settle_pane(0.8)
        boxes = self.handguard_boxes()
        if boxes:
            return boxes
        log("  这一屏没有护木，滚动仓库列表查找")
        self.scroll_to_end(-1)
        boxes = self.handguard_boxes()
        if boxes:
            return boxes
        for _ in range(self.SCAN_MAX_STEPS):
            check_abort()
            if not self.scroll_step(+1):
                break
            boxes = self.handguard_boxes()
            if boxes:
                return boxes
        return []

    def pick_box(self, boxes):
        usable = [
            box
            for box in boxes
            if not any(abs(box[0] - fx) < 8 and abs(box[1] - fy) < 8 for fx, fy in self._failed)
        ]
        if not usable:
            return None
        assert self.image is not None
        left, top, right, bottom = vision.pane_box(self.image)
        middle = (top + bottom) / 2
        usable.sort(key=lambda box: abs(box[1] + box[3] / 2 - middle))
        return random.choice(usable[:4])

    # ------------------------------------------------------------ 上架

    def listing_limit_error(self) -> None:
        """没弹出出售窗口时，看看是不是栏位或次数用完了。"""
        self.grab()
        reason = self.listing_block_reason()
        if reason == "merchant_slots":
            raise MerchantSlotsFull("商家栏位已满，无法继续寄售")
        if reason == "listing_limit":
            raise ListingFull("上架次数已满，无法继续寄售")

    def open_listing(self) -> None:
        for _ in range(6):
            check_abort()
            boxes = self.locate_handguards()
            if not boxes:
                raise NoStock("仓库里没有找到 FAL 护木")
            box = self.pick_box(boxes)
            if box is None:
                self._failed.clear()
                continue
            x = box[0] + box[2] / 2
            y = box[1] + box[3] * 0.56
            screen_x, screen_y = self.to_screen(x, y)
            log(f"  按 F 上架  ({screen_x}, {screen_y})，当前屏 {len(boxes)} 个护木")
            self.aim(screen_x, screen_y, True)
            ctypes.windll.user32.SetCursorPos(int(screen_x), int(screen_y))
            dwell(0.08, 0.16)
            if cursor_near(screen_x, screen_y, tolerance=14):
                pyautogui.press("f")
                if self.poll(lambda: self.list_button() is not None, 2.0, settle=(0.06, 0.12)):
                    return
                self.click(0, 0, "护木", quick=True, tight=True, box=box)
                if self.poll(lambda: self.list_button() is not None, 1.6, settle=(0.06, 0.12)):
                    return
            else:
                log("  光标滑开了，这一格先跳过")
            self.listing_limit_error()
            log("  这一格没有打开出售，换一格")
            self._failed.append((box[0], box[1]))
        raise RuntimeError("按了 F，始终没有出现上架窗口")

    def apply_price(self) -> None:
        """把出售价改成悬浮窗里定的价格。只填一个是固定价，两个是区间里随机。"""
        low, high = self.price_low, self.price_high
        if low is None and high is None:
            return
        if low is None:
            low = high
        if high is None:
            high = low
        assert low is not None and high is not None
        low, high = min(low, high), max(low, high)
        price = random.randint(low, high)
        assert self.image is not None
        height, width = self.image.shape[:2]
        hits = self.read_crop(int(width * 0.42), int(height * 0.48), int(width * 0.88), int(height * 0.82))
        label = pick(hits, lambda text: "出售价格" in text or text.startswith("出售价"))
        number = None
        for hit in hits:
            digits = "".join(ch for ch in hit.text if ch.isdigit())
            if len(digits) < 3:
                continue
            if label is not None and (hit.cx <= label.cx or abs(hit.cy - label.cy) > height * 0.04):
                continue
            if number is None or (label is not None and abs(hit.cy - label.cy) < abs(number.cy - label.cy)):
                number = hit
        if number is not None:
            pad_x = max(8.0, (number.x2 - number.x1) * 0.35)
            pad_y = max(6.0, (number.y2 - number.y1) * 0.45)
            box = (
                number.x1 - pad_x,
                number.y1 - pad_y,
                (number.x2 - number.x1) + pad_x * 2,
                (number.y2 - number.y1) + pad_y * 2,
            )
        elif label is not None:
            scale = height / 1600
            box = (label.cx + 70 * scale, label.cy - 16 * scale, 150 * scale, 36 * scale)
        else:
            log("没有找到价格框，这次按市场价上架")
            return
        log(f"  售价 {price}")
        self.click(0, 0, "价格", quick=True, box=box)
        dwell(0.04, 0.14)
        pyautogui.hotkey("ctrl", "a")
        dwell(0.015, 0.06)
        for _ in range(8):
            check_abort()
            pyautogui.press("backspace")
            time.sleep(random.uniform(0.008, 0.028))
        for digit in str(price):
            check_abort()
            pyautogui.press(digit)
            time.sleep(random.uniform(0.010, 0.035))
        dwell(0.03, 0.11)
        if label is not None:
            self.click(
                label.cx,
                label.cy,
                "出售价格",
                quick=True,
                box=(label.x1, label.y1, max(label.x2 - label.x1, 24), max(label.y2 - label.y1, 16)),
            )
        dwell(0.05, 0.18)

    def listing_quantity(self) -> int | None:
        """出售窗口里的「出售数量」，游戏默认最多 5。"""
        assert self.image is not None
        height, width = self.image.shape[:2]
        hits = self.read_crop(
            int(width * 0.48),
            int(height * 0.49),
            int(width * 0.84),
            int(height * 0.62),
        )
        label = pick(hits, lambda text: "出售数量" in text)
        if label is None:
            return None
        candidates: list[tuple[float, int]] = []
        for hit in hits:
            if hit.cx <= label.cx or abs(hit.cy - label.cy) > height * 0.035:
                continue
            digits = "".join(char for char in hit.text if char.isdigit())
            if not digits:
                continue
            value = int(digits)
            candidates.append((abs(hit.cy - label.cy) + abs(hit.cx - label.cx) * 0.01, value))
        return min(candidates, default=(0.0, 0), key=lambda item: item[0])[1] or None

    def _dialog_lines(self) -> list:
        """只看弹窗那一块。悬浮窗上的「上限」「上架次数已满」不放进来。"""
        assert self.image is not None
        height, width = self.image.shape[:2]
        hits = self.read_crop(int(width * 0.12), int(height * 0.38), int(width * 0.88), int(height * 0.92))
        lines = []
        for hit in hits:
            screen_x, screen_y = self.to_screen(hit.cx, hit.cy)
            if point_on_panel(screen_x, screen_y):
                continue
            if "无法继续寄售" in hit.text:
                continue
            lines.append(hit.text)
        return lines

    def market_limit_reason(self) -> str | None:
        """市场顶栏的两个计数。

        ``我的上架物品 45/45`` 是商家栏位，``本周上架 1000/1000`` 是本周上架次数。
        """
        assert self.image is not None
        height, width = self.image.shape[:2]
        hits = self.read_crop(0, int(height * 0.10), int(width * 0.86), int(height * 0.24))
        blob = "".join(hit.text.replace(" ", "") for hit in hits)
        reason = parse_market_limit(blob)
        if reason == StopReason.MERCHANT_SLOTS_FULL:
            return "merchant_slots"
        if reason == StopReason.LISTING_LIMIT:
            return "listing_limit"
        return None

    def listing_block_reason(self) -> str | None:
        """出售时弹出的提示：栏位满了，还是本周次数用完了。两种状态要分开。"""
        market_reason = self.market_limit_reason()
        if market_reason is not None:
            return market_reason
        lines = self._dialog_lines()
        slot_keys = (
            "商家栏位已满",
            "商家栏位不足",
            "出售栏位已满",
            "上架栏位已满",
            "上架槽位已满",
            "没有空闲栏位",
            "无空闲栏位",
            "当前售单已达上限",
            "当前上架数量已满",
        )
        if any(any(key in text for key in slot_keys) for text in lines):
            return "merchant_slots"
        for text in lines:
            if "无法在市场上架" in text or "无法上架寄售" in text:
                return "listing_limit"
            if "上架次数" in text and any(key in text for key in ("已满", "上限", "用完")):
                return "listing_limit"
        return None

    def list_once(self) -> None:
        self.grab()
        blocked = self.market_limit_reason()
        if blocked == "merchant_slots":
            raise MerchantSlotsFull("当前上架数量已满（我的上架物品栏位已满）")
        if blocked == "listing_limit":
            raise ListingFull("本周上架次数已达上限")
        if self.list_button() is None:
            self.open_listing()
        self.grab()
        quantity = self.listing_quantity()
        if quantity is not None:
            if quantity > FAL_HANDGUARD.max_items_per_listing:
                raise RuntimeError(
                    f"出售数量 {quantity} 超过安全上限 "
                    f"{FAL_HANDGUARD.max_items_per_listing}，停止上架"
                )
            log(f"  本次合并上架 {quantity} 件")
        self.apply_price()
        self.grab()
        point = self.list_button()
        if point is None:
            raise RuntimeError("没有出现上架")
        self.click(0, 0, "上架", quick=True, box=point)
        if not self.poll(lambda: self.list_button() is None, 4.0, settle=(0.08, 0.16)):
            raise RuntimeError("点了上架，出售窗口没有关闭")
        self._failed.clear()
        # 一次上架会同时拿走几件，格子要重新排一下。等它停稳再认下一件。
        self.settle_pane(0.9)
        log("  已上架")

    def list_remaining(self, limit: int = 160) -> int:
        """把仓库里所有护木逐批上架，找不到了就停。"""
        done = 0
        while done < limit:
            check_abort()
            log(f"第 {done + 1} 次上架")
            try:
                self.list_once()
            except NoStock:
                if done == 0:
                    log("仓库里没有护木")
                else:
                    log("没有更多护木，停止上架")
                break
            done += 1
        log(f"上架结束，共 {done} 次。")
        return done

    def list_current_stock(self) -> int:
        """去市场出售页把仓库里现有的护木上架完。一个都没有就返回 0，调用方再去兑换。"""
        self.close_exchange()
        self.open_market_sell()
        return self.list_remaining()

    def back_to_shop(self) -> None:
        """上架结束后先离开出售页，再回兰德尔的核心配件。"""
        self.item_at = None
        self.close_listing()
        log("回到商店，继续兑换")
        self.open_shop()

    def prepare(self) -> None:
        self.window.focus()
        self.close_exchange()
        self.open_market_sell()


def describe_price(low: int | None, high: int | None) -> None:
    if low is None and high is None:
        log("上架沿用市场价")
        return
    if low is None or high is None or low == high:
        log(f"上架价格固定为 {low if low is not None else high}")
        return
    log(f"上架价格在 {min(low, high)} 到 {max(low, high)} 之间随机")


class _FalAutoDriver:
    """把决策循环接到当前的屏幕操作上。"""

    def __init__(self, seller: Seller, *, skip_nav: bool) -> None:
        self.seller = seller
        self.skip_nav = skip_nav

    def log(self, message: str) -> None:
        log(message)

    def prepare(self) -> None:
        # 上架直接走底部「市场」，不用先进商店，也不用碰右边的容器图标。
        self.seller.grab()
        if self.skip_nav:
            self.seller.refresh()
            self.seller.click_core_parts()

    def list_available_stock(self) -> int:
        log(
            f"检查仓库；每次上架由游戏默认选择最多 "
            f"{FAL_HANDGUARD.max_items_per_listing} 件"
        )
        return self.seller.list_current_stock()

    def ensure_exchange_ready(self) -> None:
        self.seller.grab()
        if not self.seller.shop_open():
            self.seller.open_shop()
        else:
            self.seller.click_core_parts()

    def exchange_once(self) -> None:
        self.seller.exchange_once()

    def return_to_exchange(self) -> None:
        self.seller.back_to_shop()


def run_auto(
    max_exchange: int,
    skip_nav: bool = False,
    debug: bool = False,
    countdown: bool = True,
    price_low: int | None = None,
    price_high: int | None = None,
) -> int:
    """观察、判断、操作的循环：先上架仓库里的护木，没有了再兑换，满了再上架。"""
    if max_exchange < 1:
        log("兑换上限至少为 1")
        return 2
    if TEMPLATE_OFF is None or TEMPLATE_ON is None:
        log("缺少 templates/core_parts.png，无法识别「核心配件」页签")
        return 2
    if HANDGUARD_TEMPLATE is None:
        log("缺少 templates/handguard_cell.png，无法识别仓库里的护木")
        return 2
    window = GameWindow.find()
    if window is None:
        log("没有找到游戏窗口。请先打开《暗区突围无限》，标题里需要能看到「暗区」。")
        return 1
    left, top, right, bottom = window.rect()
    log(f"游戏窗口：{window.title}（{right - left}×{bottom - top}）")
    window.focus()
    if countdown:
        log("3 秒后开始。先上架仓库里的护木，没有了再兑换。")
        for second in (3, 2, 1):
            check_abort()
            log(f"  {second}…")
            pause(1)

    seller = Seller(window, debug=debug)
    seller.price_low = price_low
    seller.price_high = price_high
    describe_price(price_low, price_high)
    report = AutoWorkflow(
        _FalAutoDriver(seller, skip_nav=skip_nav),
        exchange_limit=max_exchange,
    ).run()
    if report.stop_reason == StopReason.LISTING_LIMIT:
        raise ListingFull("上架次数已满，自动流程已停止")
    if report.stop_reason == StopReason.MERCHANT_SLOTS_FULL:
        raise MerchantSlotsFull("商家栏位已满，自动流程已停止")
    log(
        f"自动流程完成：兑换 {report.exchanged} 次，"
        f"上架 {report.listing_batches} 次。"
    )
    return 0


def run_sell(
    count: int,
    debug: bool = False,
    countdown: bool = True,
    price_low: int | None = None,
    price_high: int | None = None,
) -> int:
    if count < 1:
        log("上架次数至少为 1")
        return 2
    if HANDGUARD_TEMPLATE is None:
        log("缺少 templates/handguard_cell.png，无法识别仓库里的护木")
        return 2
    window = GameWindow.find()
    if window is None:
        log("没有找到游戏窗口。请先打开《暗区突围无限》，标题里需要能看到「暗区」。")
        return 1
    left, top, right, bottom = window.rect()
    log(f"游戏窗口：{window.title}（{right - left}×{bottom - top}）")
    window.focus()
    if countdown:
        log("3 秒后开始。停在任意带底部导航的界面即可，脚本会自己进入市场。")
        for second in (3, 2, 1):
            check_abort()
            log(f"  {second}…")
            pause(1)

    seller = Seller(window, debug=debug)
    seller.price_low = price_low
    seller.price_high = price_high
    describe_price(price_low, price_high)
    seller.prepare()
    done = 0
    for index in range(1, count + 1):
        log(f"第 {index}/{count} 次上架")
        try:
            seller.list_once()
        except NoStock:
            log("仓库里没有更多护木，停止上架")
            break
        done += 1
    log(f"完成，共上架 {done} 次。")
    return 0


class FloatPanel:
    """始终置顶的小窗：开始兑换，或立刻停下。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("FAL")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1b1d22")
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.busy = False
        self._counting = False
        self._tick_gen = 0
        self._pending: tuple = ("auto", 150, False, None, None)
        self._worker: threading.Thread | None = None
        self._done_message: str | None = None
        self._drag_x = 0
        self._drag_y = 0
        self._start_font = ("Microsoft YaHei UI", 10, "bold")
        self._count_font = ("Microsoft YaHei UI", 16, "bold")

        self.count_var = tk.StringVar(value="150")
        self.skip_var = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value="auto")
        self.price_low_var = tk.StringVar(value="")
        self.price_high_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="自动：先上架仓库里的护木，没有了再兑换。Esc 可停止。")

        shell = tk.Frame(self.root, bg="#1b1d22", highlightbackground="#3a3f4b", highlightthickness=1)
        shell.pack(fill="both", expand=True)

        title = tk.Frame(shell, bg="#1b1d22", cursor="fleur")
        title.pack(fill="x", padx=10, pady=(8, 0))
        grip = tk.Label(title, text="FAL", bg="#1b1d22", fg="#f4f1ea", font=("Microsoft YaHei UI", 11, "bold"))
        grip.pack(side="left")
        close = tk.Label(title, text="×", bg="#1b1d22", fg="#9aa0ab", font=("Microsoft YaHei UI", 14), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda _event: self.close())
        self.root.bind("<Escape>", lambda _event: self.stop())
        for widget in (title, grip):
            widget.bind("<Button-1>", self._press)
            widget.bind("<B1-Motion>", self._drag)

        modes = tk.Frame(shell, bg="#1b1d22")
        modes.pack(fill="x", padx=10, pady=(8, 0))
        for value, text in (("auto", "自动"), ("exchange", "兑换"), ("list", "上架")):
            tk.Radiobutton(
                modes,
                text=text,
                value=value,
                variable=self.mode_var,
                command=self._mode_changed,
                bg="#1b1d22",
                fg="#c8ccd4",
                selectcolor="#2a2e36",
                activebackground="#1b1d22",
                activeforeground="#f4f1ea",
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", expand=True)

        body = tk.Frame(shell, bg="#1b1d22")
        body.pack(fill="x", padx=10, pady=8)
        self.count_label = tk.Label(body, text="上限", bg="#1b1d22", fg="#c8ccd4", font=("Microsoft YaHei UI", 9))
        self.count_label.pack(side="left")
        count = tk.Spinbox(
            body,
            from_=1,
            to=300,
            width=4,
            textvariable=self.count_var,
            bg="#2a2e36",
            fg="#f4f1ea",
            buttonbackground="#2a2e36",
            relief="flat",
            font=("Microsoft YaHei UI", 10),
        )
        count.pack(side="left", padx=(6, 10))
        tk.Checkbutton(
            body,
            text="已在商店",
            variable=self.skip_var,
            bg="#1b1d22",
            fg="#c8ccd4",
            selectcolor="#2a2e36",
            activebackground="#1b1d22",
            activeforeground="#f4f1ea",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left")

        price_row = tk.Frame(shell, bg="#1b1d22")
        price_row.pack(fill="x", padx=10, pady=(0, 8))
        tk.Label(price_row, text="售价", bg="#1b1d22", fg="#c8ccd4", font=("Microsoft YaHei UI", 9)).pack(side="left")
        entry_style = {"bg": "#2a2e36", "fg": "#f4f1ea", "insertbackground": "#f4f1ea", "relief": "flat", "font": ("Microsoft YaHei UI", 10), "width": 7}
        tk.Entry(price_row, textvariable=self.price_low_var, **entry_style).pack(side="left", padx=(6, 0))
        tk.Label(price_row, text="到", bg="#1b1d22", fg="#c8ccd4", font=("Microsoft YaHei UI", 9)).pack(side="left", padx=4)
        tk.Entry(price_row, textvariable=self.price_high_var, **entry_style).pack(side="left")

        actions = tk.Frame(shell, bg="#1b1d22")
        actions.pack(fill="x", padx=10)
        self.start_btn = tk.Button(
            actions,
            text="开始",
            command=self.start,
            bg="#e07a2f",
            fg="#1b1d22",
            activebackground="#f0a15f",
            activeforeground="#1b1d22",
            relief="flat",
            font=self._start_font,
            disabledforeground="#1b1d22",
            cursor="hand2",
        )
        self.start_btn.pack(side="left", fill="x", expand=True, ipady=4)
        self.stop_btn = tk.Button(
            actions,
            text="停止",
            command=self.stop,
            state="disabled",
            bg="#3a3f4b",
            fg="#f4f1ea",
            activebackground="#c4453c",
            activeforeground="#ffffff",
            disabledforeground="#6d7380",
            relief="flat",
            font=("Microsoft YaHei UI", 10, "bold"),
            cursor="hand2",
        )
        self.stop_btn.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=4)
        # 松开才触发的话，脚本会在按下和松开之间把光标拽走，停止等于没点到。
        self.stop_btn.bind("<ButtonPress-1>", lambda _event: self.stop())

        tk.Label(
            shell,
            textvariable=self.status_var,
            bg="#1b1d22",
            fg="#9aa0ab",
            font=("Microsoft YaHei UI", 8),
            wraplength=292,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=10, pady=(8, 10))

        self.root.update_idletasks()
        width, height = 320, self.root.winfo_reqheight()
        x = self.root.winfo_screenwidth() - width - 28
        y = 72
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        _log_listeners.append(self._on_log)
        self.root.after(100, self._poll)

    def _press(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self.root.winfo_x()
        self._drag_y = event.y_root - self.root.winfo_y()

    def _drag(self, event: tk.Event) -> None:
        self.root.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _on_log(self, message: str) -> None:
        self.messages.put(("log", message))

    def _worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _poll(self) -> None:
        if PARK_PANEL.is_set():
            PARK_PANEL.clear()
            # 右上角会盖住仓库图标。挪到左上角，右侧格子和容器才能点。
            self.root.geometry("+16+8")
            self.root.update_idletasks()
        self._remember_stop_rect()
        alive = self._worker_alive()
        if (self.busy or alive) and aborted():
            self.stop()
        try:
            while True:
                kind, message = self.messages.get_nowait()
                if kind == "log" and not STOP.is_set():
                    self.status_var.set(message)
                elif kind == "done":
                    self._done_message = message
        except queue.Empty:
            pass
        alive = self._worker_alive()
        if alive:
            self.busy = True
            self.stop_btn.configure(state="normal", bg="#c4453c")
        elif self._done_message is not None and not self._counting:
            message = self._done_message
            self._done_message = None
            self._reset_idle(message or "已停止")
        if self.root.winfo_exists():
            self.root.after(40 if (self.busy or alive) else 100, self._poll)

    def _remember_stop_rect(self) -> None:
        global STOP_BUTTON_RECT, PANEL_RECT
        try:
            left = self.stop_btn.winfo_rootx()
            top = self.stop_btn.winfo_rooty()
            STOP_BUTTON_RECT = (left, top, left + self.stop_btn.winfo_width(), top + self.stop_btn.winfo_height())
            px = self.root.winfo_rootx()
            py = self.root.winfo_rooty()
            PANEL_RECT = (px, py, px + self.root.winfo_width(), py + self.root.winfo_height())
        except tk.TclError:
            STOP_BUTTON_RECT = None
            PANEL_RECT = None

    def _mode_changed(self) -> None:
        if self.busy:
            return
        mode = self.mode_var.get()
        if mode == "list":
            self.count_label.configure(text="次数")
            self.status_var.set("只上架。售价只填一个是固定价，两个数会在区间里随机。")
        elif mode == "exchange":
            self.count_label.configure(text="次数")
            self.status_var.set("只兑换。停在首页，或勾选已在商店。")
        else:
            self.count_label.configure(text="上限")
            self.status_var.set("自动：先上架仓库里的护木，没有了再兑换。")

    def start(self) -> None:
        if self.busy:
            return
        try:
            count = int(self.count_var.get())
        except ValueError:
            self.status_var.set("次数要填数字")
            return
        if count < 1:
            self.status_var.set("次数至少为 1")
            return
        try:
            price_low = self._price(self.price_low_var.get())
            price_high = self._price(self.price_high_var.get())
        except ValueError:
            self.status_var.set("售价要填整数")
            return
        if self._worker_alive():
            self.status_var.set("还在停，等这一下结束")
            self.stop()
            return
        STOP.clear()
        self._done_message = None
        self.busy = True
        self._counting = True
        self._pending = (self.mode_var.get(), count, bool(self.skip_var.get()), price_low, price_high)
        self._tick_gen += 1
        self.stop_btn.configure(state="normal", bg="#c4453c")
        self._count_down(3, self._tick_gen)

    def _count_down(self, second: int, gen: int) -> None:
        if gen != self._tick_gen or not self.root.winfo_exists():
            return
        if STOP.is_set() or gen != self._tick_gen:
            self._reset_idle("已停止")
            return
        if second <= 0:
            if STOP.is_set() or gen != self._tick_gen:
                self._reset_idle("已停止")
                return
            self._counting = False
            self.start_btn.configure(text="开始", font=self._start_font, state="disabled")
            self.status_var.set("开始执行")
            self._worker = threading.Thread(target=self._run, args=self._pending, daemon=True)
            self._worker.start()
            return
        self.start_btn.configure(text=str(second), font=self._count_font)
        log(f"{second} 秒后开始，请切到游戏")
        self.root.after(1000, lambda: self._count_down(second - 1, gen))

    def _reset_idle(self, message: str) -> None:
        self.busy = False
        self._counting = False
        self.start_btn.configure(state="normal", text="开始", font=self._start_font)
        self.stop_btn.configure(state="disabled", bg="#3a3f4b")
        self.status_var.set(message)

    def _price(self, text: str) -> int | None:
        text = text.strip().replace(",", "").replace("，", "")
        if not text:
            return None
        value = int(text)
        if value < 1:
            raise ValueError
        return value

    def _run(self, mode: str, count: int, skip_nav: bool, price_low: int | None, price_high: int | None) -> None:
        try:
            if mode == "list":
                code = run_sell(count, countdown=False, price_low=price_low, price_high=price_high)
            elif mode == "auto":
                code = run_auto(count, skip_nav=skip_nav, countdown=False, price_low=price_low, price_high=price_high)
            else:
                code = run_exchange(count, skip_nav=skip_nav, countdown=False)
            self.messages.put(("done", "完成" if code == 0 else ""))
        except KeyboardInterrupt as exc:
            self.messages.put(("done", str(exc) or "已停止"))
        except pyautogui.FailSafeException:
            self.messages.put(("done", "鼠标移到了屏幕左上角，已停止"))
        except ListingFull as exc:
            message = str(exc) or "上架次数已满，无法继续寄售"
            log(message)
            self.messages.put(("done", message))
        except MerchantSlotsFull as exc:
            message = str(exc) or "商家栏位已满，无法继续寄售"
            log(message)
            self.messages.put(("done", message))
        except Exception as exc:
            log(f"失败：{exc}")
            self.messages.put(("done", f"失败：{exc}"))

    def stop(self) -> None:
        alive = self._worker_alive()
        if not self.busy and not alive:
            return
        STOP.set()
        release_mouse()
        hold_script_mouse(False)
        self._tick_gen += 1
        if self._counting and not alive:
            self._reset_idle("已停止")
            return
        self.busy = True
        self.stop_btn.configure(state="normal", bg="#c4453c")
        self.status_var.set("正在停止…")

    def close(self) -> None:
        STOP.set()
        release_mouse()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def launch_panel() -> None:
    FloatPanel().run()


def main() -> int:
    parser = argparse.ArgumentParser(description="按录屏流程兑换 FAL四导轨标准护木")
    parser.add_argument("-n", "--count", type=int, default=3, help="兑换次数，录屏中是 3 次")
    parser.add_argument("--skip-nav", action="store_true", help="已经停在核心配件页面时，跳过前面的点击")
    parser.add_argument("--debug", action="store_true", help="每次点击前把标注图写到 debug_last.jpg")
    args = parser.parse_args()
    return run_exchange(args.count, skip_nav=args.skip_nav, debug=args.debug)


if __name__ == "__main__":
    ensure_admin()
    if "--cli" in sys.argv:
        sys.argv = [arg for arg in sys.argv if arg != "--cli"]
        try:
            raise SystemExit(main())
        except KeyboardInterrupt as exc:
            log(str(exc) or "已停止")
            raise SystemExit(1)
        except pyautogui.FailSafeException:
            log("鼠标移到了屏幕左上角，已停止")
            raise SystemExit(1)
        except Exception as exc:
            log(f"失败：{exc}")
            raise SystemExit(1)
    else:
        launch_panel()
