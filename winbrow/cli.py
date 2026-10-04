"""
Developer CLI (M0/M1 scaffolding)
=================================
Usage:
    python -m winbrow observe [--max-nodes N] [--depth N]

Prints the control tree of the foreground window. Uses Windows UI
Automation when the `uiautomation` package is installed; otherwise falls
back to the existing foreground-app/process context with a pointer to
install it. Never crashes on exotic windows — per-node errors are skipped.
"""

from __future__ import annotations

import argparse
import sys


def _observe_uia(max_nodes: int, max_depth: int) -> int:
    try:
        import uiautomation as auto
    except ImportError:
        return -1

    try:
        top = auto.GetForegroundControl().GetTopLevelControl()
    except Exception as e:
        print(f"Could not read foreground window: {e}")
        return 1

    try:
        print(f"window: {top.Name!r}  app: {top.ProcessName}  hwnd: {top.NativeWindowHandle}")
    except Exception:
        print("window: <unreadable>")
    count = [0]

    def walk(ctrl, depth: int) -> None:
        if count[0] >= max_nodes or depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for ch in children:
            if count[0] >= max_nodes:
                return
            try:
                if ch.IsOffscreen or not ch.IsEnabled:
                    continue
                name = (ch.Name or "").strip()
                ctype = ch.ControlTypeName
                r = ch.BoundingRectangle
                print(
                    f"{'  ' * (depth + 1)}- [{ctype}] {name!r} "
                    f"rect=({r.left},{r.top},{r.right},{r.bottom})"
                )
                count[0] += 1
            except Exception:
                continue
            walk(ch, depth + 1)

    walk(top, 0)
    print(f"{count[0]} control(s) listed (cap {max_nodes}, depth {max_depth})")
    return 0


def _observe_fallback() -> int:
    from .windows import get_current_windows_context

    ctx = get_current_windows_context(use_cache=False)
    print(f"app: {ctx.active_app}")
    print(f"title: {ctx.active_title or '—'}")
    print(f"running_apps: {len(ctx.running_apps)}")
    print(f"volume: {ctx.volume_level}  dark_mode: {ctx.dark_mode}  battery: {ctx.battery_percent}")
    print("uiautomation is not installed — `pip install uiautomation` for the")
    print("full foreground control tree (M1 perception).")
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    rc = _observe_uia(args.max_nodes, args.depth)
    if rc == -1:
        return _observe_fallback()
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="winbrow", description="WinBrow developer CLI")
    sub = p.add_subparsers(dest="command", required=True)
    o = sub.add_parser("observe", help="Print the foreground window control tree")
    o.add_argument("--max-nodes", type=int, default=200)
    o.add_argument("--depth", type=int, default=6)
    o.set_defaults(func=cmd_observe)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
