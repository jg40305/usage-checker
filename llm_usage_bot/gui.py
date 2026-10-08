"""Small desktop window for the usage bot: python -m llm_usage_bot.gui

The window owns the bot: while it is open the bot stays connected to Discord,
closing it disconnects. The bot runs on its own asyncio loop in a background
thread; the two sides only talk through a queue (bot -> UI) and
run_coroutine_threadsafe (UI -> bot).

Health checks never involve an LLM: the gateway state/latency is read locally
every couple of seconds, and a plain Discord REST call confirms that the token
and the network work.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import threading
import time
import tkinter as tk
import unicodedata
from datetime import datetime
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

import discord

from .app import build_bot
from .bot import UsageBot
from .config import ENV_PATH, NOTIFY_MODES, load_config, parse_ids, read_env, save_env

log = logging.getLogger("llm_usage_bot.gui")

STATUS_SECONDS = 2  # gateway state/latency: local, no request
HEALTH_SECONDS = 60  # REST health check
MAX_LOG_LINES = 2000

COLORS = {"ok": "#30A46C", "warn": "#F5A524", "error": "#E5484D", "idle": "#8B8D98"}


class QueueHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", (record.levelno, self.format(record))))


class BotRunner:
    """Runs the bot on a dedicated asyncio loop thread and reports status."""

    def __init__(self, events: queue.Queue):
        self.events = events
        self.loop: asyncio.AbstractEventLoop | None = None
        self.bot: UsageBot | None = None
        self.thread = threading.Thread(target=self._run, name="bot", daemon=True)
        self._health = ("尚未檢查", "idle")
        self._last_health_ok = True

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        """Ask the bot to disconnect; the thread exits once it has."""
        if self.loop and self.bot and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.bot.close(), self.loop)

    def check_now(self) -> None:
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._health_check(), self.loop)

    # ---- bot thread ----------------------------------------------------------

    def _status(self, state: str, level: str, latency: str = "—") -> None:
        self.events.put(("status", (state, level, latency) + self._health))

    def _run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
            # Stop the bot's own loops (polls, reset timers) before closing the loop.
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            self.loop.close()
            self.events.put(("stopped", None))

    async def _main(self) -> None:
        self._status("啟動中…", "warn")
        try:
            cfg = load_config()
        except SystemExit as e:  # config errors are reported via SystemExit
            log.error("%s", e)
            self._status("設定錯誤", "error")
            return
        self.bot = build_bot(cfg)
        monitor = asyncio.create_task(self._monitor())
        try:
            await self.bot.start(cfg.bot_token)
        except discord.LoginFailure:
            log.error("Discord 登入失敗：DISCORD_BOT_TOKEN 無效")
            self._status("登入失敗", "error")
            return
        except Exception:
            log.exception("bot 意外停止")
            self._status("已停止（錯誤）", "error")
            return
        finally:
            monitor.cancel()
            if not self.bot.is_closed():
                await self.bot.close()
        self._status("已斷線", "idle")

    async def _monitor(self) -> None:
        assert self.bot
        last_health = 0.0
        while True:
            bot = self.bot
            if bot.is_ready() and not bot.is_closed():
                latency = f"{bot.latency * 1000:.0f} ms" if math.isfinite(bot.latency) else "—"
                self._status(f"已連線（{bot.user}）", "ok", latency)
                if time.monotonic() - last_health >= HEALTH_SECONDS:
                    last_health = time.monotonic()
                    await self._health_check()
            else:
                self._status("連線中…", "warn")
            await asyncio.sleep(STATUS_SECONDS)

    async def _health_check(self) -> None:
        bot = self.bot
        if not bot or not bot.is_ready():
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        started = time.perf_counter()
        try:
            await bot.fetch_user(bot.user.id)  # GET /users/{id}: plain REST, needs a valid token
        except Exception as e:
            ok = False
            self._health = (f"{stamp} 失敗：{type(e).__name__}", "error")
            log.warning("健康檢查失敗：%s", e)
        else:
            ok = True
            ms = (time.perf_counter() - started) * 1000
            self._health = (f"{stamp} 正常（REST {ms:.0f} ms）", "ok")
        if ok and not self._last_health_ok:
            log.info("健康檢查已恢復正常")
        self._last_health_ok = ok


# (env key, label, hint, kind) — kind: secret / ids / id / number / mode
SETTINGS_FIELDS = (
    ("DISCORD_BOT_TOKEN", "Bot token", "Developer Portal → Bot → Reset Token", "secret"),
    ("DISCORD_ALLOWED_USER_IDS", "允許的使用者 ID", "選填，留空 = 伺服器裡的人都能用；私訊會發給這些人", "ids"),
    ("DISCORD_GUILD_ID", "伺服器 ID", "選填，填了斜線指令會立即出現", "id"),
    ("NOTIFY_MODE", "通知方式", "定時回報、重置提醒與通知要發到哪裡", "mode"),
    ("NOTIFY_CHANNEL_ID", "通知頻道 ID", "通知方式含「頻道」時需要", "id"),
    ("AUTO_REPORT_MINUTES", "自動回報（分鐘）", "超過幾分鐘沒手動查就自動發，0 = 關閉", "number"),
    ("REMINDER_MINUTES", "重置前提醒（分鐘）", "5 小時／每週額度重置前幾分鐘提醒，0 = 關閉", "number"),
)
REQUIRED = {"DISCORD_BOT_TOKEN"}
NUMBER_DEFAULT_60 = {"AUTO_REPORT_MINUTES", "REMINDER_MINUTES"}
MODE_LABELS = dict(zip(NOTIFY_MODES, ("頻道", "私訊給我", "頻道＋私訊")))


def _normalize(label: str, kind: str, value: str) -> str:
    """Return the value to store, or raise ValueError with a message for the user."""
    if not value:
        return value
    if kind in ("id", "ids"):
        try:
            ids = parse_ids(value)
        except ValueError as e:
            raise ValueError(f"「{label}」裡的「{e}」不是 Discord ID（應該是一串數字）") from e
        if kind == "id" and len(ids) > 1:
            raise ValueError(f"「{label}」只能填一個 ID")
        return ",".join(map(str, ids))
    if kind == "mode":
        return next(mode for mode, text in MODE_LABELS.items() if text == value)
    if kind == "number":
        try:
            number = float(unicodedata.normalize("NFKC", value))
        except ValueError:
            number = -1
        if number < 0:
            raise ValueError(f"「{label}」要填 0 以上的數字")
        return f"{number:g}"
    if any(c.isspace() for c in value):  # secret: a token never contains spaces
        raise ValueError(f"「{label}」中間不能有空白")
    return value


class SettingsDialog(tk.Toplevel):
    """Edit the Discord settings stored in .env."""

    def __init__(self, parent: tk.Tk, on_saved):
        super().__init__(parent)
        self.on_saved = on_saved
        self.title("Discord 設定")
        self.resizable(False, False)
        self.transient(parent)

        current = read_env()
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self.vars: dict[str, tk.StringVar] = {}
        row = 0
        for key, label, hint, kind in SETTINGS_FIELDS:
            star = " *" if key in REQUIRED else ""
            ttk.Label(frame, text=label + star).grid(row=row, column=0, sticky="w", padx=(0, 12))
            var = tk.StringVar(value=current.get(key, "60" if key in NUMBER_DEFAULT_60 else ""))
            if kind == "mode":
                var.set(MODE_LABELS.get(var.get().lower(), MODE_LABELS["channel"]))
                entry = ttk.Combobox(frame, textvariable=var, values=list(MODE_LABELS.values()), state="readonly")
            else:
                entry = ttk.Entry(frame, textvariable=var, width=52, show="•" if kind == "secret" else "")
            entry.grid(row=row, column=1, sticky="ew")
            if kind == "secret":
                self.token_entry = entry
                self.show_token = tk.BooleanVar(value=False)
                ttk.Checkbutton(
                    frame, text="顯示", variable=self.show_token, command=self._toggle_token
                ).grid(row=row, column=2, padx=(8, 0))
            ttk.Label(frame, text=hint, foreground=COLORS["idle"]).grid(
                row=row + 1, column=1, sticky="w", pady=(0, 8)
            )
            self.vars[key] = var
            row += 2

        ttk.Label(
            frame, text=f"儲存位置：{ENV_PATH}（已在 .gitignore，不會進 git）", foreground=COLORS["idle"]
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.error_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.error_var, foreground=COLORS["error"]).grid(
            row=row + 1, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=row + 2, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="儲存並重新連線", command=self._save).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.token_entry.focus_set()

    def _toggle_token(self) -> None:
        self.token_entry.configure(show="" if self.show_token.get() else "•")

    def _save(self) -> None:
        values = {key: var.get().strip() for key, var in self.vars.items()}
        for key, label, _hint, kind in SETTINGS_FIELDS:
            if key in REQUIRED and not values[key]:
                self.error_var.set(f"請填寫「{label}」")
                return
            try:
                values[key] = _normalize(label, kind, values[key])
            except ValueError as e:
                self.error_var.set(str(e))
                return
        if values["NOTIFY_MODE"] != "channel" and not values["DISCORD_ALLOWED_USER_IDS"]:
            self.error_var.set("要私訊的話，請在「允許的使用者 ID」填你自己的使用者 ID")
            return
        try:
            save_env(values)
        except OSError as e:
            self.error_var.set(f"無法寫入 .env：{e}")
            return
        log.info("設定已儲存到 %s", ENV_PATH.name)  # never log the token itself
        self.destroy()
        self.on_saved()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.runner = BotRunner(self.events)
        self._closing = False
        self._restarting = False

        root.title("LLM Usage Bot")
        root.geometry("820x520")
        root.minsize(560, 320)

        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        self.dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=COLORS["idle"], outline="")
        self.dot.grid(row=0, column=0, padx=(0, 8))
        self.state_var = tk.StringVar(value="啟動中…")
        ttk.Label(top, textvariable=self.state_var, font=("Microsoft JhengHei UI", 11, "bold")).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Button(top, text="立即健康檢查", command=lambda: self.runner.check_now()).grid(
            row=0, column=2, rowspan=2
        )
        ttk.Button(top, text="設定", command=self.open_settings).grid(row=0, column=3, rowspan=2, padx=(8, 0))

        self.detail_var = tk.StringVar(value="Gateway 延遲：—　｜　健康檢查：尚未檢查")
        self.detail = ttk.Label(top, textvariable=self.detail_var, foreground=COLORS["idle"])
        self.detail.grid(row=1, column=1, sticky="w", pady=(2, 0))

        ttk.Label(root, text="執行輸出", padding=(12, 0)).pack(anchor="w")
        self.log_view = ScrolledText(root, font=("Consolas", 9), state="disabled", wrap="word")
        self.log_view.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.log_view.tag_configure("warn", foreground="#B07A00")
        self.log_view.tag_configure("error", foreground=COLORS["error"])

        handler = QueueHandler(self.events)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

        root.protocol("WM_DELETE_WINDOW", self.close)
        self.runner.start()
        root.after(100, self._drain)
        if not read_env().get("DISCORD_BOT_TOKEN"):
            root.after(300, self.open_settings)  # first run: ask for the token right away

    def open_settings(self) -> None:
        SettingsDialog(self.root, on_saved=self.restart_bot)

    def restart_bot(self) -> None:
        if self.runner.thread.is_alive():
            self._restarting = True  # the "stopped" event starts the new runner
            self.state_var.set("重新連線中…")
            self.runner.stop()
        else:
            self._start_runner()

    def _start_runner(self) -> None:
        self.runner = BotRunner(self.events)
        self.runner.start()

    def _append_log(self, levelno: int, text: str) -> None:
        tag = "error" if levelno >= logging.ERROR else "warn" if levelno >= logging.WARNING else ""
        at_bottom = self.log_view.yview()[1] >= 0.999
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n", tag)
        excess = int(self.log_view.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_view.delete("1.0", f"{excess + 1}.0")
        self.log_view.configure(state="disabled")
        if at_bottom:  # don't yank the view while the user is scrolling back
            self.log_view.see("end")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(*payload)
                elif kind == "status":
                    state, level, latency, health, health_level = payload
                    self.state_var.set(state)
                    self.dot.itemconfigure(self.dot_id, fill=COLORS[level])
                    self.detail_var.set(f"Gateway 延遲：{latency}　｜　健康檢查：{health}")
                    self.detail.configure(foreground=COLORS[health_level if level == "ok" else level])
                elif kind == "stopped" and self._closing:
                    self._destroy()
                    return
                elif kind == "stopped" and self._restarting:
                    self._restarting = False
                    self._start_runner()
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.state_var.set("關閉中…")
        if not self.runner.thread.is_alive():
            self._destroy()
            return
        self.runner.stop()
        self.root.after(10_000, self._destroy)  # don't hang forever on a stuck shutdown

    def _destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:  # already gone
            pass


def main() -> None:
    try:  # crisp text on high-DPI Windows displays
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
