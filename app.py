"""Send one configured message to one configured ChatGPT conversation."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
TASK_NAME = "ChatGPT Auto Hello"
APP_HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ChatGPTAutoHello"
# Keep this separate from the older Playwright Chromium profile and the user's
# ordinary Chrome profile. Chrome Stable may not accept a newer Chromium profile.
PROFILE = APP_HOME / "chrome-profile"
SCREENSHOTS = APP_HOME / "screenshots"
LOG_FILE = APP_HOME / "app.log"
DEFAULT_CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# Ordered from narrow composer-specific selectors to broader composer fallbacks.
EDITOR_SELECTORS = (
    '#prompt-textarea[contenteditable="true"]',
    'textarea#prompt-textarea',
    '[data-testid="composer-input"][contenteditable="true"]',
    '[data-testid="composer-input"] textarea',
    'textarea[data-testid="composer-input"]',
    'form textarea',
    'form [contenteditable="true"][role="textbox"]',
    'main [contenteditable="true"][role="textbox"]',
    'form [contenteditable="true"]',
    'main textarea',
)


class LoginRequired(RuntimeError):
    pass


class EditorNotFound(RuntimeError):
    pass


def setup_logging() -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw.exe has no console stream.
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def load_config() -> dict:
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    config.setdefault("browser_executable", DEFAULT_CHROME_EXE)
    url = config.get("conversation_url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or not re.search(
        r"(?:^|/)c/[^/]+/?$", parsed.path
    ):
        raise ValueError("Set conversation_url to an existing https://chatgpt.com/c/... conversation URL")
    if not isinstance(config.get("message"), str) or not config["message"].strip():
        raise ValueError("message must be a nonempty string")
    if not isinstance(config.get("interval_hours"), int) or isinstance(
        config.get("interval_hours"), bool
    ) or not 1 <= config["interval_hours"] <= 24:
        raise ValueError("interval_hours must be an integer from 1 to 24")
    if not isinstance(config.get("browser_executable"), str) or not config["browser_executable"].strip():
        raise ValueError("browser_executable must point to your installed Chrome.exe")
    return config


def chrome_executable(config: dict) -> Path:
    path = Path(config["browser_executable"])
    if not path.is_file():
        raise FileNotFoundError(f"Chrome not found at {path}; edit browser_executable in config.json")
    return path


def single_instance():
    """The task and manual test use the same per-user Windows mutex."""
    if os.name != "nt":
        raise RuntimeError("This application is intended to run on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    identity = hashlib.sha256(str(PROFILE).encode("utf-8")).hexdigest()[:16]
    handle = kernel32.CreateMutexW(None, False, "Local\\ChatGPTAutoHello_" + identity)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        logging.info("Another instance is running; skipping this trigger")
        return None
    return (kernel32, handle)


def screenshot(page, reason: str) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{reason}.png"
    try:
        page.screenshot(path=str(SCREENSHOTS / name), full_page=True, timeout=8000)
        logging.error("Screenshot: %s", SCREENSHOTS / name)
    except Exception:
        logging.exception("Failed to save screenshot")


def login_page(page) -> bool:
    current = urlparse(page.url)
    if (current.hostname in {"auth.openai.com", "auth.chatgpt.com"}) or bool(
        re.search(r"/(auth|login|signin)(?:/|$)", current.path, re.I)
    ):
        return True
    for selector in ('a[href*="/auth/login"]', 'a[href*="/auth/signin"]'):
        links = page.locator(selector)
        if links.count() and links.first.is_visible():
            return True
    return False


def wait_for_editor(page, timeout_seconds: int = 35):
    end = time.monotonic() + timeout_seconds
    while time.monotonic() < end:
        if login_page(page):
            raise LoginRequired("Login expired; run python app.py --login")
        for selector in EDITOR_SELECTORS:
            for i in range(min(page.locator(selector).count(), 4)):
                candidate = page.locator(selector).nth(i)
                if candidate.is_visible() and candidate.is_enabled():
                    logging.info("Composer located using %s", selector)
                    return candidate
        page.wait_for_timeout(400)
    if login_page(page):
        raise LoginRequired("Login expired; run python app.py --login")
    raise EditorNotFound("Could not find an enabled ChatGPT message composer")


def editor_text(editor) -> str:
    return editor.evaluate(
        "e => ('value' in e ? e.value : e.innerText || e.textContent || '').trim()"
    )


def check_conversation(page, expected_url: str) -> None:
    expected = urlparse(expected_url).path.rstrip("/")
    actual = urlparse(page.url).path.rstrip("/")
    if login_page(page):
        raise LoginRequired("Login expired; run python app.py --login")
    if actual != expected or urlparse(page.url).hostname != "chatgpt.com":
        raise RuntimeError(f"Conversation did not open (current URL: {page.url})")


def wait_for_conversation_ready(page, timeout_seconds: int = 25) -> None:
    """Do not type into a composer until the existing thread has rendered."""
    turns = page.locator(
        '[data-message-author-role="user"], '
        '[data-message-author-role="assistant"], '
        '[data-testid^="conversation-turn"]'
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if login_page(page):
            raise LoginRequired("Login expired; run python app.py --login")
        if turns.count() > 0:
            return
        page.wait_for_timeout(400)
    raise RuntimeError("Existing conversation messages did not load; refusing to send")


def foreground_browser_window(page) -> None:
    """Mark this page title, then find and activate that precise Chrome window."""
    page.bring_to_front()
    if os.name != "nt":
        raise RuntimeError("Foreground activation requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    marker = "AutoHello-" + hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    original_title = page.evaluate("document.title")
    page.evaluate("title => { document.title = title }", original_title + " " + marker)
    try:
        hwnd = None
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and hwnd is None:
            matches = []

            @callback_type
            def collect(window, _):
                length = user32.GetWindowTextLengthW(window)
                if length and user32.IsWindowVisible(window):
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(window, buf, len(buf))
                    if marker in buf.value:
                        matches.append(window)
                return True

            user32.EnumWindows(collect, 0)
            if len(matches) == 1:
                hwnd = matches[0]
            else:
                time.sleep(0.1)
        if hwnd is None:
            raise RuntimeError("Could not identify the automated Chrome window")
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if user32.GetForegroundWindow() != hwnd:
            foreground_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
            own_thread = kernel32.GetCurrentThreadId()
            attached = bool(foreground_thread and foreground_thread != own_thread and
                            user32.AttachThreadInput(own_thread, foreground_thread, True))
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(own_thread, foreground_thread, False)
        time.sleep(0.25)
        if user32.GetForegroundWindow() != hwnd:
            raise RuntimeError("Windows did not allow the browser to become the foreground window")
        logging.info("Automated Chrome window is in the foreground")
    finally:
        try:
            page.evaluate("title => { document.title = title }", original_title)
        except Exception:
            logging.warning("Could not restore the page title")


def manual_login(config: dict) -> None:
    """Log in through ordinary Chrome, before Playwright ever opens the profile."""
    PROFILE.mkdir(parents=True, exist_ok=True)
    chrome = chrome_executable(config)
    logging.info("Opening ordinary Chrome for manual login with profile %s", PROFILE)
    process = subprocess.Popen([
        str(chrome), f"--user-data-dir={PROFILE}", "--new-window", "https://chatgpt.com/"
    ])
    print("Sign in inside the opened Chrome window. Close THAT Chrome window completely, then press Enter here.")
    input()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Close the dedicated Chrome login window before pressing Enter") from exc


def open_context(playwright, config: dict):
    PROFILE.mkdir(parents=True, exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), executable_path=str(chrome_executable(config)),
        headless=False, viewport=None,
        args=["--start-maximized"], timeout=30000,
    )


def send_once(config: dict, *, login: bool) -> None:
    from playwright.sync_api import sync_playwright

    if login:
        manual_login(config)
    with sync_playwright() as playwright:
        context = open_context(playwright, config)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(config["conversation_url"], wait_until="domcontentloaded", timeout=45000)
            try:
                editor = wait_for_editor(page)
                check_conversation(page, config["conversation_url"])
                wait_for_conversation_ready(page)
                if login:
                    logging.info("Login validated; saved browser profile in %s", PROFILE)
                    return
                if editor_text(editor):
                    raise RuntimeError("Composer has an existing draft; refusing to overwrite it")
                foreground_browser_window(page)
                editor.focus()
                editor.fill(config["message"])
                if editor_text(editor) != config["message"].strip():
                    raise RuntimeError("Composer content differs from the configured message")
                focused = editor.evaluate("e => document.activeElement === e || e.contains(document.activeElement)")
                if not focused:
                    raise RuntimeError("Composer did not retain keyboard focus")

                user_messages = page.locator('[data-message-author-role="user"]')
                before = user_messages.count()
                logging.info("Pressing Enter once in %s", page.url)
                editor.press("Enter")  # Exactly one send attempt; never retry this action.

                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if user_messages.count() > before and user_messages.last.inner_text().strip() == config["message"].strip():
                        logging.info("Sent message confirmed in conversation")
                        return
                    page.wait_for_timeout(500)
                if not editor_text(editor):
                    logging.warning("Enter was pressed and composer cleared, but message confirmation is unavailable")
                    return
                screenshot(page, "send-unconfirmed")
                raise RuntimeError("Enter was pressed once, but send could not be confirmed; no retry was attempted")
            except Exception:
                screenshot(page, "login-or-composer-error")
                raise
        finally:
            context.close()


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def install_task(config: dict) -> None:
    if os.name != "nt":
        raise RuntimeError("Task installation requires Windows")
    python_path = Path(sys.executable)
    if python_path.name.lower() == "python.exe" and (python_path.parent / "pythonw.exe").exists():
        python_path = python_path.parent / "pythonw.exe"
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute {ps_quote(str(python_path))} -Argument {ps_quote('"' + str(ROOT / 'app.py') + '"')} -WorkingDirectory {ps_quote(str(ROOT))}
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(10) -RepetitionInterval (New-TimeSpan -Hours {config['interval_hours']}) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {ps_quote(TASK_NAME)} -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Send configured message to a ChatGPT conversation while the user is logged in.' -Force | Out-Null
Write-Output 'Task registered. First run in about 10 seconds; subsequent runs every {config['interval_hours']} hours.'
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", encoded], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test", action="store_true", help="Send the message once now (real send)")
    group.add_argument("--login", action="store_true", help="Log in manually and validate the target conversation")
    group.add_argument("--install-task", action="store_true", help="Install or update the Windows scheduled task")
    args = parser.parse_args()
    setup_logging()
    mutex = None
    try:
        config = load_config()
        if args.install_task:
            install_task(config)
            return 0
        mutex = single_instance()
        if mutex is None:
            return 0
        send_once(config, login=args.login)
        return 0
    except LoginRequired as exc:
        logging.error("%s", exc)
        return 2
    except Exception:
        logging.exception("Run failed")
        return 1
    finally:
        if mutex is not None:
            mutex[0].CloseHandle(mutex[1])


if __name__ == "__main__":
    sys.exit(main())
