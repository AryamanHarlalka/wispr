"""F2 — app-aware modes: frontmost bundle id -> mode name."""
from __future__ import annotations


def frontmost_bundle_id() -> str:
    try:
        from AppKit import NSWorkspace  # pyobjc
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.bundleIdentifier() or ""
    except Exception:
        return ""


def mode_for(bundle_id: str, modes: dict[str, str]) -> str:
    if bundle_id:
        for prefix, mode in modes.items():
            if prefix != "default" and bundle_id.startswith(prefix):
                return mode
    return modes.get("default", "neutral")
