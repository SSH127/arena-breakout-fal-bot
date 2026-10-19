# -*- coding: utf-8 -*-
"""旧入口。兑换、上架和悬浮窗都在 exchange_fal.py。"""

from __future__ import annotations

import sys

from exchange_fal import ensure_admin, launch_panel, run_sell

import pyautogui

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="把扩容仓库里的 FAL 护木按录屏流程上架")
    parser.add_argument("-n", "--count", type=int, default=5, help="上架次数，录屏里是 5 次")
    parser.add_argument("--debug", action="store_true", help="每次点击前把标注图写到 debug_last.jpg")
    args = parser.parse_args()
    return run_sell(args.count, debug=args.debug)


if __name__ == "__main__":
    ensure_admin()
    if "--cli" in sys.argv:
        sys.argv = [arg for arg in sys.argv if arg != "--cli"]
        try:
            raise SystemExit(main())
        except KeyboardInterrupt as exc:
            from exchange_fal import log

            log(str(exc) or "已停止")
            raise SystemExit(1)
        except pyautogui.FailSafeException:
            from exchange_fal import log

            log("鼠标移到了屏幕左上角，已停止")
            raise SystemExit(1)
        except Exception as exc:
            from exchange_fal import log

            log(f"失败：{exc}")
            raise SystemExit(1)
    else:
        launch_panel()
