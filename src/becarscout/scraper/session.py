"""Facebook login session handling.

We never automate the Facebook login form — that's the fastest way to get an
account flagged. Instead we launch a real, visible browser window backed by
a persistent profile directory on disk, let a human log in normally (solving
any 2FA/checkpoint challenges themselves), and just leave the cookies where
Chromium already writes them as you use the browser. Every later scrape run
launches that same profile directory instead of logging in again.

`becarscout login` keeps the browser open and idles rather than blocking on
a terminal `input()` — it's designed to be started and stopped by whatever
is driving it (a person, or an agent controlling it as a background task).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

DEFAULT_PROFILE_DIR = Path("data/.session/profile")

# Chromium's profile lock keeps two processes from sharing a profile dir at
# once, so a scrape can't start while a login browser is still open. A cap
# here just means the login browser eventually closes itself if left idle.
MAX_LOGIN_SESSION_SECONDS = 30 * 60


async def launch_login_browser(profile_dir: Path = DEFAULT_PROFILE_DIR) -> None:
    """Open a visible, persistent-profile browser at Facebook login.

    Stays open until closed (by the user closing the window, or by whatever
    process started this being stopped) — cookies land in `profile_dir` as
    Chromium writes them, no separate export step needed.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=False
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.facebook.com/login")

        print(
            "READY: log in to Facebook in the opened browser window. "
            "Session is saved continuously to disk as you use it.",
            flush=True,
        )

        closed = asyncio.Event()
        page.once("close", lambda: closed.set())
        try:
            await asyncio.wait_for(closed.wait(), timeout=MAX_LOGIN_SESSION_SECONDS)
        except asyncio.TimeoutError:
            pass

        await context.close()

    print("Login session closed.", flush=True)


def has_saved_session(profile_dir: Path = DEFAULT_PROFILE_DIR) -> bool:
    return profile_dir.exists() and any(profile_dir.iterdir())
