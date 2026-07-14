"""F2 — app-aware modes: frontmost bundle id -> mode name."""
from __future__ import annotations


def frontmost_app():
    """Returns the live NSRunningApplication, not just its bundle id.

    Callers must grab this the instant the hotkey is pressed, before any
    Murmur UI shows — the floating pill activating itself (confirmed via
    history.jsonl logging "org.python.python" as frontmost on every
    dictation) means querying frontmost *after* recording/processing always
    returns Murmur itself, not the app the user was actually dictating into.
    The daemon re-activates this exact object right before sending the paste
    keystroke, which is what actually fixes where the text lands.
    """
    try:
        from AppKit import NSWorkspace  # pyobjc
        return NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None


def bundle_id_of(app) -> str:
    try:
        return (app.bundleIdentifier() or "") if app else ""
    except Exception:
        return ""


def frontmost_bundle_id() -> str:
    return bundle_id_of(frontmost_app())


def mode_for(bundle_id: str, modes: dict[str, str]) -> str:
    if bundle_id:
        for prefix, mode in modes.items():
            if prefix != "default" and bundle_id.startswith(prefix):
                return mode
    return modes.get("default", "neutral")
