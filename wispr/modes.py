"""F2 — app-aware modes: frontmost bundle id -> mode name, plus the focus
tracker that remembers which app the user was really in.

2026-09-06: `frontmost_app()` used to return whatever NSWorkspace said was
in front, and history shows that was sometimes Wispr itself
(com.wispr.dictation) — a paste aimed there lands nowhere. The tracker
below listens for app-activation notifications and always knows the last
*foreign* app that had focus, so a capture can never target Wispr.
"""
from __future__ import annotations

import os
import threading
import time

# Our own identities: the app bundle, and the bare interpreter when run
# from a terminal. Neither is ever a paste target.
OWN_BUNDLE_IDS = {"com.wispr.dictation", "org.python.python"}


def _own_pid() -> int:
    return os.getpid()


class FocusTracker:
    """Remembers the last frontmost app that is not Wispr.

    Two sources, either of which is enough: NSWorkspace's activation
    notifications (delivered on the main run loop, which the indicator
    owns), and every explicit `frontmost_app()` query. Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = None
        self._last_t = 0.0
        self._observer = None

    def note(self, app) -> None:
        if app is None or _is_own(app):
            return
        with self._lock:
            self._last = app
            self._last_t = time.time()

    def last_foreign(self):
        with self._lock:
            app = self._last
        try:
            if app is not None and app.isTerminated():
                return None
        except Exception:
            pass
        return app

    def install(self) -> None:
        """Subscribe to activation notifications. Safe to call from any
        thread; silently a no-op off macOS. Notifications arrive on the
        main thread's run loop, so the indicator must be running for the
        live feed — the explicit queries in `frontmost_app()` cover the
        rest."""
        try:
            from AppKit import NSWorkspace
            from Foundation import NSObject

            tracker = self

            class _Observer(NSObject):
                def activated_(self, note):  # noqa: N802
                    try:
                        app = note.userInfo().get("NSWorkspaceApplicationKey")
                        tracker.note(app)
                    except Exception:
                        pass

            self._observer = _Observer.alloc().init()
            NSWorkspace.sharedWorkspace().notificationCenter() \
                .addObserver_selector_name_object_(
                    self._observer, "activated:",
                    "NSWorkspaceDidActivateApplicationNotification", None)
            self.note(_raw_frontmost())
        except Exception:
            pass


TRACKER = FocusTracker()


def _is_own(app) -> bool:
    try:
        if int(app.processIdentifier()) == _own_pid():
            return True
        return (app.bundleIdentifier() or "") in OWN_BUNDLE_IDS
    except Exception:
        return False


def _raw_frontmost():
    try:
        from AppKit import NSWorkspace  # pyobjc
        return NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return None


def frontmost_app():
    """The app the user is working in, as a live NSRunningApplication.

    Callers grab this the instant the hotkey is pressed. If macOS reports
    Wispr itself as frontmost (it happens: a stray click on the pill's
    process, a race with the previous paste), the last foreign app the
    tracker saw is returned instead, because a paste aimed at Wispr is a
    paste into nothing.
    """
    app = _raw_frontmost()
    if app is not None and not _is_own(app):
        TRACKER.note(app)
        return app
    fallback = TRACKER.last_foreign()
    return fallback if fallback is not None else app


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
