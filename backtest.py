"""Prove each feature actually changed the machine, not just its own state.

Every feature here writes real OS state — the registry, the hosts file, a
login entry. The bug that keeps recurring is a toggle that reports success
while nothing visibly happens, so each check below performs the real action
and then reads the OS back to see whether it took.

    python backtest.py            # everything
    python backtest.py cursor     # one group

This mutates the machine while it runs and puts it back afterwards. It skips
anything that would need an elevation prompt unless you pass --elevated.
Colour and cursor checks are visible on screen while they run.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import platform as plat            # noqa: E402
from shared import settings_store as store     # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = ""):
    results.append((name, status, detail))
    print(f"  {status:<4} {name}{('  — ' + detail) if detail else ''}", flush=True)


def check(name: str, got, want, detail: str = ""):
    ok = got == want
    record(name, PASS if ok else FAIL,
           detail if ok else f"{detail} (got {got!r}, wanted {want!r})".strip())
    return ok


# ── colour filter ──
def test_colorblind():
    print("\ncolour filter")
    if not plat.IS_WINDOWS:
        record("colorblind", SKIP, "Windows-only path; Mac needs a real Mac")
        return
    import winreg

    import features.colorblind_filter.main as cf

    started_on = cf.is_filter_active()
    started_type = cf._read_registry_dword(
        winreg.HKEY_CURRENT_USER, cf.COLOR_FILTERING_PATH, "FilterType") or 0
    try:
        cf.set_filter(True, cf.FILTER_TYPES["Deuteranopia"])
        check("turns on", cf.is_filter_active(), True)
        check("hotkey allowed in registry",
              cf._read_registry_dword(winreg.HKEY_CURRENT_USER,
                                      cf.COLOR_FILTERING_PATH, "HotkeyEnabled"), 1,
              "off by default on a fresh install; we must set it")
        time.sleep(0.5)

        cf.set_filter(True, cf.FILTER_TYPES["Tritanopia"])
        check("changing type keeps it on", cf.is_filter_active(), True)
        check("type actually changed",
              cf._read_registry_dword(winreg.HKEY_CURRENT_USER,
                                      cf.COLOR_FILTERING_PATH, "FilterType"),
              cf.FILTER_TYPES["Tritanopia"])
        time.sleep(0.5)

        cf.set_filter(False, cf.FILTER_TYPES["Tritanopia"])
        check("turns off", cf.is_filter_active(), False)
    except Exception as e:
        record("colorblind", FAIL, str(e))
    finally:
        try:
            cf.set_filter(started_on, started_type)
        except Exception:
            pass


# ── cursor size ──
def test_cursor_size():
    print("\ncursor size")
    if not plat.IS_WINDOWS:
        record("cursor size", SKIP, "Windows-only")
        return
    import hub

    api = hub.Api()
    before = _cursor_state()
    try:
        api._apply_cursor_size(10)
        size, base = _cursor_state()
        check("slider value stored", size, 10)
        check("pixel size derived", base, "176", "32 + 16 * (10 - 1)")

        api._apply_cursor_size(1)
        size, base = _cursor_state()
        check("back to normal", (size, base), (1, "32"))
    except Exception as e:
        record("cursor size", FAIL, str(e))
    finally:
        try:
            api._apply_cursor_size(before[0] or 1)
        except Exception:
            pass


def _cursor_state():
    import winreg
    size = base = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"SOFTWARE\Microsoft\Accessibility", 0, winreg.KEY_READ) as k:
            size = winreg.QueryValueEx(k, "CursorSize")[0]
    except OSError:
        pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Control Panel\Cursors", 0, winreg.KEY_READ) as k:
            base = winreg.QueryValueEx(k, "CursorBaseSize")[0]
    except OSError:
        pass
    return size, base


# ── dyslexia font ──
def test_dyslexia_font():
    print("\ndyslexia font")
    if not plat.IS_WINDOWS:
        record("dyslexia font", SKIP, "Windows-only")
        return
    import hub

    from shared import windows_fonts as winfonts

    api = hub.Api()
    if winfonts.BACKUP_FILE.exists():
        record("dyslexia font", SKIP, "a substitution is already applied")
        return
    try:
        api._apply_dyslexia_toggle(True)
        check("backup written", winfonts.BACKUP_FILE.exists(), True,
              "so the original fonts can be restored")
        check("a font was actually substituted",
              _font_substitute("Segoe UI") not in (None, "Segoe UI"), True)

        api._apply_dyslexia_toggle(False)
        check("restored", winfonts.BACKUP_FILE.exists(), False,
              "backup removed, so 'applied' stays a truthful signal")
    except Exception as e:
        record("dyslexia font", FAIL, str(e))
        try:
            api._apply_dyslexia_toggle(False)
        except Exception:
            pass


def _font_substitute(name: str):
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows NT\CurrentVersion\FontSubstitutes",
                0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


# ── focus mode ──
def test_focus_mode(allow_elevation: bool):
    print("\nfocus mode")
    import features.focus_mode.main as fm

    if not allow_elevation and not fm.is_admin():
        record("focus mode", SKIP, "needs elevation; rerun with --elevated")
        return
    blocked_before = _hosts_has_block(fm)
    try:
        fm.apply_block(["backtest-example.invalid"])
        check("hosts file blocks the site", _hosts_has_block(fm), True,
              "this is the check that catches a block outliving the app")

        fm.remove_block()
        check("hosts file cleaned up", _hosts_has_block(fm), False)
    except Exception as e:
        record("focus mode", FAIL, str(e))
    finally:
        if not blocked_before:
            try:
                fm.remove_block()
            except Exception:
                pass


def _hosts_has_block(fm) -> bool:
    if not fm.HOSTS_PATH.exists():
        return False
    return fm.MARK_BEGIN in fm.HOSTS_PATH.read_text(encoding="utf-8", errors="ignore")


# ── start at login ──
def test_autostart():
    print("\nstart at login")
    from shared import autostart

    if not autostart.is_supported():
        record("start at login", SKIP, "not supported on this OS")
        return
    was_enabled = autostart.is_enabled()
    try:
        autostart.set_enabled(True)
        check("login entry created", autostart.is_enabled(), True)
        check("points at an interpreter that exists",
              autostart._launch_interpreter().exists(), True)

        autostart.set_enabled(False)
        check("login entry removed", autostart.is_enabled(), False)
    except Exception as e:
        record("start at login", FAIL, str(e))
    finally:
        try:
            autostart.set_enabled(was_enabled)
        except Exception:
            pass


# ── window placement (no GUI needed) ──
def test_placement():
    print("\nbubble placement")
    from shared import webbubble as wb

    sw, sh = wb.screen_size()

    class FakeBubble:
        w, h = 300, 120
        x = y = 0

        def move(self, x, y):
            self.x, self.y = int(x), int(y)

    fake = FakeBubble()
    wb.Bubble.place_bottom_right(fake)
    check("card sits inside the right edge", fake.x + fake.w <= sw, True)
    check("card sits inside the bottom edge", fake.y + fake.h <= sh, True)
    check("card is in the right half", fake.x > sw // 2, True,
          "bottom-right, not wherever the pointer was")

    wb.Bubble.place_bottom_center(fake)
    check("centred placement still works",
          abs(fake.x + fake.w // 2 - sw // 2) <= 2, True)


GROUPS = {
    "colorblind": test_colorblind,
    "cursor": test_cursor_size,
    "dyslexia": test_dyslexia_font,
    "focus": None,            # needs the elevation flag, wired below
    "autostart": test_autostart,
    "placement": test_placement,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("groups", nargs="*", choices=list(GROUPS) + [],
                        help="which checks to run (default: all)")
    parser.add_argument("--elevated", action="store_true",
                        help="also run checks that need an admin prompt")
    args = parser.parse_args()

    chosen = args.groups or list(GROUPS)
    print(f"Accessibility4all backtest — {len(chosen)} group(s)")
    for name in chosen:
        if name == "focus":
            test_focus_mode(args.elevated)
        else:
            GROUPS[name]()

    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = [r for r in results if r[1] == FAIL]
    skipped = sum(1 for _, s, _ in results if s == SKIP)
    print(f"\n{passed} passed, {len(failed)} failed, {skipped} skipped")
    for name, _, detail in failed:
        print(f"  FAIL {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
