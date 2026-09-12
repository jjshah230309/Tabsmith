"""Drive Songsterr's AI transcription in a real, signed-in browser.

Songsterr has no public API for this. There *is* a private one (see
docs/songsterr-notes.md), but replaying it with lifted cookies breaks the moment
they change it and looks like abuse from their side. So Tabsmith drives the real
UI in a real browser, using a persistent profile: you sign in once, by hand, and
every later run reuses that session.

Selectors are chosen for durability — Songsterr's CSS classes are content-hashed
and change on every deploy, but element ids like `control-export-gp` are stable.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

BASE = "https://www.songsterr.com"
NEW_URL = f"{BASE}/new"
PROFILE_DIR = Path.home() / ".tabsmith" / "browser-profile"

# Straight from the upload input's accept attribute on /new. Songsterr takes
# nothing else — flac/opus/ogg are rejected, so chunks must be one of these.
ACCEPTED_UPLOAD = (".mp3", ".m4a", ".wav")
FILE_INPUT = "#audio-file-input, input[type=file]"

# A Songsterr tab URL ends in -s<songId>; the revision may follow as ?revision=
SONG_URL_RE = re.compile(r"/a/w[a-z]{2}/[^/?#]*-s(\d+)")


class SongsterrError(RuntimeError):
    pass


class ProfileLocked(SongsterrError):
    pass


_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def profile_lock_holder(profile: Path) -> int | None:
    """PID of the browser holding this profile, or None if free or stale.

    Chromium points SingletonLock at "<host>-<pid>". If that process is gone the
    lock is stale and safe to clear; if it is alive, a second launch would fail
    with "Opening in existing browser session".
    """
    lock = profile / "SingletonLock"
    if not lock.is_symlink():
        return None
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)          # signal 0 only tests existence
    except ProcessLookupError:
        return None              # stale
    except PermissionError:
        return pid               # alive, owned by someone else
    return pid


def clear_stale_lock(profile: Path) -> bool:
    """Remove lock files if no live process holds them."""
    if profile_lock_holder(profile) is not None:
        return False
    removed = False
    for name in _LOCK_FILES:
        f = profile / name
        if f.is_symlink() or f.exists():
            try:
                f.unlink()
                removed = True
            except OSError:
                pass
    return removed


class NotSignedIn(SongsterrError):
    pass


@dataclass
class Transcription:
    part: str
    song_id: str
    url: str
    gp_path: Path | None = None


def _play():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise SongsterrError(
            "Playwright is not installed. Run:  ~/Tabsmith/.venv/bin/pip install playwright "
            "&& ~/Tabsmith/.venv/bin/playwright install chromium"
        ) from exc
    return sync_playwright



def _clickable(page, pattern: str):
    """Find a clickable by visible text, whatever tag or ARIA role it wears.

    Songsterr's submit control is an <a> carrying role="dialog", so
    get_by_role("button"/"link") does not match it. Try progressively looser
    strategies and return the first visible hit.
    """
    rx = re.compile(pattern, re.I)
    strategies = [
        lambda: page.locator("a,button,input[type=submit],[role=button]").filter(has_text=rx),
        lambda: page.get_by_role("button", name=rx),
        lambda: page.get_by_role("link", name=rx),
        lambda: page.get_by_text(rx),
    ]
    for make in strategies:
        try:
            loc = make()
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 5)):
            item = loc.nth(i)
            try:
                if item.is_visible():
                    return item
            except Exception:
                continue
    return None


class Songsterr:
    """A signed-in Songsterr browser session."""

    def __init__(self, headless: bool = False, profile: Path = PROFILE_DIR,
                 timeout: float = 60.0, log=print):
        self.headless = headless
        self.profile = profile
        self.timeout = timeout * 1000
        self.log = log
        self._pw = None
        self.ctx = None
        self.page = None

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        self.profile.mkdir(parents=True, exist_ok=True)

        holder = profile_lock_holder(self.profile)
        if holder is not None:
            raise ProfileLocked(
                f"A Tabsmith browser window is already open (pid {holder}).\n"
                "Chromium allows only one process per profile, so this run cannot start.\n\n"
                "Close that browser window, then run this command again.\n"
                f"If no window is visible, end it with:  kill {holder}")
        if clear_stale_lock(self.profile):
            self.log("Cleared a stale browser lock from a previous run.")

        self._pw = _play()().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile),
            headless=self.headless,
            accept_downloads=True,
            viewport={"width": 1440, "height": 950},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.ctx.set_default_timeout(self.timeout)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    # -- session ---------------------------------------------------------
    def is_signed_in(self) -> bool:
        """True if the session is authenticated. Never navigates the page.

        `page.request` reuses the context's cookies without touching what is on
        screen — important during login, where navigating would yank the user
        off the form they are filling in.

        `/api/preference` is the probe because it is genuinely authenticated:
        401 signed out, 200 signed in. `/api/user/current` is NOT usable — it
        answers 200 to anyone, reading "current" as a profile name and returning
        a stub person.
        """
        try:
            resp = self.page.request.get(f"{BASE}/api/preference",
                                         headers={"accept": "application/json"})
            if resp.status == 200:
                return True
            if resp.status in (401, 403):
                return False
        except Exception:
            pass
        # Only if the probe itself failed: judge by whatever page is loaded,
        # still without navigating.
        try:
            if "/signin" in self.page.url or "/signup" in self.page.url:
                return False
            return self.page.locator("a[href*='/signin']").count() == 0
        except Exception:
            return False

    def login(self, wait_seconds: int = 600) -> bool:
        """Open the sign-in page and wait for the user to finish by hand."""
        self.page.goto(f"{BASE}/signin", wait_until="domcontentloaded")
        self.log("")
        self.log("  A browser window is open — sign in to Songsterr there.")
        self.log("  Tabsmith never sees your password; the browser handles it.")
        self.log(f"  Waiting up to {wait_seconds // 60} minutes. Ctrl+C to cancel.")
        self.log("")
        deadline = time.time() + wait_seconds
        announced = False
        while time.time() < deadline:
            if self.is_signed_in():
                self.log("  Signed in. The session is saved for future runs.")
                time.sleep(1.5)   # let the cookie settle to disk
                return True
            if not announced and time.time() > deadline - wait_seconds + 30:
                self.log("  (still waiting — finish signing in the browser window)")
                announced = True
            time.sleep(3)
        self.log("  Timed out waiting for sign-in.")
        return False

    def quota(self) -> int | None:
        """Transcriptions left this month, or None if the endpoint changed."""
        try:
            resp = self.page.request.get(
                f"{BASE}/api/contributions/available-transcriptions-count")
            if not resp.ok:
                return None
            data = resp.json()
            if isinstance(data, int):
                return data
            for key in ("count", "available", "availableCount", "transcriptionsCount"):
                if isinstance(data, dict) and isinstance(data.get(key), int):
                    return data[key]
        except Exception:
            return None
        return None

    # -- transcription ---------------------------------------------------
    def transcribe(self, audio: Path, title: str, artist: str,
                   tempo: int | None = None, signature: tuple[int, int] | None = None,
                   wait_minutes: float = 20.0) -> Transcription:
        """Upload one audio file and wait for the tab to be generated."""
        page = self.page
        page.goto(NEW_URL, wait_until="domcontentloaded")

        if page.locator("a[href*='/signin']").count() > 0:
            raise NotSignedIn("Not signed in to Songsterr. Run `tabsmith login` first.")

        # Pinning tempo/signature is what keeps independently-transcribed chunks
        # from each landing on a different grid.
        if tempo or signature:
            toggle = _clickable(page, r"show settings|advanced")
            if toggle is not None:
                try:
                    toggle.click()
                    page.wait_for_timeout(600)
                except Exception:
                    pass
            if tempo and not self._fill_if_present("#first-bar-tempo-input", str(int(tempo))):
                self.log("    warning: could not pin the tempo — Songsterr will detect it itself")
            if signature:
                ok = self._fill_if_present("#first-bar-numerator-input", str(signature[0]))
                ok &= self._fill_if_present("#first-bar-denominator-input", str(signature[1]))
                if not ok:
                    self.log("    warning: could not pin the time signature")

        # The file input is created on demand, so click the link then catch the chooser.
        upload = _clickable(page, r"upload an audio file")
        if upload is None:
            raise SongsterrError(
                "Could not find the 'upload an audio file' control on /new. "
                "Songsterr may have changed the page — see docs/songsterr-notes.md.")

        self._attach_audio(upload, audio)

        self._fill_if_present("input[name='title']", title)
        self._fill_if_present("input[name='artist']", artist)

        before = page.url
        submit = (_clickable(page, r"transcribe tab with ai")
                  or _clickable(page, r"^\s*transcribe\b(?!.*vocal)"))
        if submit is None:
            raise SongsterrError(
                "Could not find the 'Transcribe tab with AI' control. "
                "Songsterr may have changed the page — see docs/songsterr-notes.md.")
        submit.click()

        song_id, url = self._await_song(before, wait_minutes)
        return Transcription(part=audio.stem, song_id=song_id, url=url)


    def _attach_audio(self, upload_ctl, audio: Path) -> str:
        """Put `audio` into the upload form.

        Clicking 'upload an audio file' does NOT open a native file chooser — it
        reveals a hidden <input type=file id="audio-file-input"> in the DOM. So
        waiting on a filechooser event hangs forever. Set the input directly
        (Playwright can fill a hidden input), and keep the chooser path only as a
        fallback in case Songsterr switches to the native dialog later.
        """
        page = self.page
        if audio.suffix.lower() not in ACCEPTED_UPLOAD:
            raise SongsterrError(
                f"Songsterr will not accept {audio.suffix} files "
                f"(it takes {', '.join(ACCEPTED_UPLOAD)}). "
                "Re-split with --format mp3.")

        existing = page.locator(FILE_INPUT)
        if existing.count():
            existing.first.set_input_files(str(audio))
            return "existing input"

        chooser = []
        # Must be a real function: Playwright sets an attribute on the handler,
        # which a bound builtin like list.append does not allow.
        page.once("filechooser", lambda c: chooser.append(c))
        upload_ctl.click()

        deadline = time.time() + 25
        while time.time() < deadline:
            if chooser:
                chooser[0].set_files(str(audio))
                return "file chooser"
            found = page.locator(FILE_INPUT)
            if found.count():
                found.first.set_input_files(str(audio))
                return "revealed input"
            page.wait_for_timeout(250)

        raise SongsterrError(
            "Clicking 'upload an audio file' revealed neither a file input nor a "
            "file dialog. Songsterr may have changed the page — see "
            "docs/songsterr-notes.md.")

    def _fill_if_present(self, selector: str, value: str) -> bool:
        loc = self.page.locator(selector)
        if loc.count():
            try:
                loc.first.fill(value)
                return True
            except Exception:
                return False
        return False

    def _await_song(self, before_url: str, wait_minutes: float) -> tuple[str, str]:
        """Wait for the transcription to land on a real tab URL."""
        page = self.page
        deadline = time.time() + wait_minutes * 60
        last = ""
        while time.time() < deadline:
            url = page.url
            match = SONG_URL_RE.search(url)
            if match and url != before_url:
                # The URL appears before the score finishes rendering.
                try:
                    page.wait_for_selector("#control-more, #control-export, [id^=control-export]",
                                           timeout=90_000)
                except Exception:
                    pass
                return match.group(1), url
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=3000)[:400]
            except Exception:
                pass
            if body != last:
                snippet = " ".join(body.split())[:110]
                if snippet:
                    self.log(f"    … {snippet}")
                last = body
            for pattern in (r"quota", r"limit reached", r"upgrade to plus",
                            r"couldn'?t transcribe", r"transcription failed"):
                if re.search(pattern, body, re.I):
                    raise SongsterrError(f"Songsterr reported a problem: {' '.join(body.split())[:200]}")
            time.sleep(3)
        raise SongsterrError(
            f"Timed out after {wait_minutes} min waiting for the transcription. "
            "Songsterr may still be working — check the browser window.")

    # -- download --------------------------------------------------------
    def download_gp(self, song_url: str, dest: Path) -> Path:
        """Download the tab at `song_url` as Guitar Pro."""
        page = self.page
        if page.url.split("?")[0] != song_url.split("?")[0]:
            page.goto(song_url, wait_until="domcontentloaded")

        gp_button = page.locator("#control-export-gp")
        if not gp_button.count() or not gp_button.first.is_visible():
            for opener in ("#control-export", "#control-more"):
                btn = page.locator(opener)
                if btn.count():
                    try:
                        btn.first.click()
                        page.wait_for_selector("#download_modal", timeout=8000)
                        break
                    except Exception:
                        continue
            if not page.locator("#control-export-gp").count():
                dl = page.get_by_role("button", name=re.compile(r"^download$", re.I))
                if dl.count():
                    dl.first.click()
                    page.wait_for_selector("#download_modal", timeout=8000)
            gp_button = page.locator("#control-export-gp")

        if not gp_button.count():
            raise SongsterrError(
                "The Guitar Pro download button (#control-export-gp) was not found. "
                "Guitar Pro export needs Songsterr Plus on most accounts.")

        # Songsterr renders a padlock next to formats the account cannot export.
        try:
            locked = gp_button.first.evaluate(
                """e => { const w = e.closest('div'); return !!(w && w.querySelector(
                     '[class*=lock i]')); }""")
            if locked:
                self.log("    note: Guitar Pro shows as locked for this account — trying anyway")
        except Exception:
            pass

        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with page.expect_download(timeout=90_000) as dl_info:
                gp_button.first.click()
            download = dl_info.value
        except Exception:
            # No file came back. Usually the account cannot export Guitar Pro:
            # clicking opens an upgrade prompt instead of downloading.
            blurb = ""
            try:
                blurb = " ".join(page.locator("body").inner_text()[:1200].split())
            except Exception:
                pass
            if re.search(r"\bplus\b|upgrade|subscri|unlock", blurb, re.I):
                raise SongsterrError(
                    "Songsterr did not return a file — it showed an upgrade prompt. "
                    "Guitar Pro export needs Songsterr Plus (or a legacy account); "
                    "a free account can create the tab but not export it.\n"
                    f"The tab itself is saved at {song_url}") from None
            raise SongsterrError(
                "Clicking the Guitar Pro download produced no file within 90s.\n"
                f"The tab is saved at {song_url}") from None

        suffix = Path(download.suggested_filename).suffix or ".gp5"
        final = dest.with_suffix(suffix)
        download.save_as(str(final))
        if final.stat().st_size == 0:
            raise SongsterrError(f"Songsterr returned an empty file for {dest.name}.")
        return final
