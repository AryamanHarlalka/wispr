"""F6b — menu-bar companion (rumps). Separate process from the daemon to
avoid AppKit/Tk event-loop fights: polls ~/.wispr/state.json + history.
Run: `wispr menubar` (optional; the pill alone is enough)."""
from __future__ import annotations

import json
import subprocess

from .config import WISPR_HOME
from .history import recent

ICONS = {"idle": "🎙", "listening": "🔴", "transcribing": "🟠",
         "cleaning": "🔵", "pasted": "🟢", "error": "❌"}


def main() -> None:
    import rumps

    class WisprBar(rumps.App):
        def __init__(self) -> None:
            super().__init__("🎙", quit_button="Quit Wispr menu bar")
            self.menu = ["Recent dictations", None]
            self.timer = rumps.Timer(self.tick, 0.5)
            self.timer.start()

        def tick(self, _) -> None:
            try:
                st = json.loads((WISPR_HOME / "state.json").read_text())
                self.title = ICONS.get(st.get("state", "idle"), "🎙")
            except Exception:
                self.title = "🎙"

        @rumps.clicked("Recent dictations")
        def show_recent(self, _) -> None:
            items = recent(5)
            if not items:
                rumps.notification("Wispr", "", "No dictations yet")
                return
            lines = "\n".join(f"• {r['cleaned'][:70]}" for r in items)
            subprocess.run(["pbcopy"], input=items[0]["cleaned"].encode())
            rumps.notification("Wispr — recent (latest copied)", "", lines)

    WisprBar().run()
