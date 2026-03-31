#!/usr/bin/env python3
"""TinkerClaw Telegram bot.

Standalone, low-risk Telegram polling bot for Dragon deployments.
- Uses Telegram Bot API directly via aiohttp.
- Uses OpenRouter directly for LLM responses.
- Stores per-chat history on disk in JSON.
- Branded as TinkerClaw.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

LOG = logging.getLogger("tinkerclaw.telegram")

DEFAULT_SYSTEM_PROMPT = (
    "You are TinkerClaw, a concise and capable AI assistant running on the "
    "Dragon hub. Keep replies helpful, direct, and practical. Avoid fluff. "
    "Do not claim actions you did not take. If unsure, say so."
)

HELP_TEXT = """TinkerClaw commands:
/start - intro
/help - this help
/model - show current OpenRouter model
/clear - clear this chat's memory
/status - runtime status
"""


@dataclass
class BotConfig:
    telegram_token: str
    openrouter_api_key: str
    openrouter_model: str = "google/gemma-3-4b-it"
    openrouter_url: str = "https://openrouter.ai/api/v1"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    state_dir: str = "/home/radxa/tinkerclaw/telegram"
    max_history_messages: int = 12
    poll_timeout_s: int = 30
    connect_timeout_s: int = 20
    read_timeout_s: int = 180
    temperature: float = 0.4
    max_tokens: int = 300

    @classmethod
    def from_env(cls) -> "BotConfig":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        if not or_key:
            raise SystemExit("OPENROUTER_API_KEY is required")
        return cls(
            telegram_token=token,
            openrouter_api_key=or_key,
            openrouter_model=os.environ.get("TINKERCLAW_OPENROUTER_MODEL", "google/gemma-3-4b-it"),
            openrouter_url=os.environ.get("TINKERCLAW_OPENROUTER_URL", "https://openrouter.ai/api/v1"),
            system_prompt=os.environ.get("TINKERCLAW_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            state_dir=os.environ.get("TINKERCLAW_TELEGRAM_STATE_DIR", "/home/radxa/tinkerclaw/telegram"),
            max_history_messages=int(os.environ.get("TINKERCLAW_MAX_HISTORY_MESSAGES", "12")),
            poll_timeout_s=int(os.environ.get("TINKERCLAW_POLL_TIMEOUT_S", "30")),
            connect_timeout_s=int(os.environ.get("TINKERCLAW_CONNECT_TIMEOUT_S", "20")),
            read_timeout_s=int(os.environ.get("TINKERCLAW_READ_TIMEOUT_S", "180")),
            temperature=float(os.environ.get("TINKERCLAW_TEMPERATURE", "0.4")),
            max_tokens=int(os.environ.get("TINKERCLAW_MAX_TOKENS", "300")),
        )


class ChatStateStore:
    def __init__(self, root: str, max_history_messages: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_history_messages = max_history_messages

    def _path(self, chat_id: int) -> Path:
        return self.root / f"{chat_id}.json"

    def load(self, chat_id: int) -> list[dict[str, str]]:
        path = self._path(chat_id)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text())
        except Exception:
            LOG.exception("Failed loading chat state for %s", chat_id)
            return []

    def save(self, chat_id: int, messages: list[dict[str, str]]) -> None:
        trimmed = messages[-self.max_history_messages :]
        self._path(chat_id).write_text(json.dumps(trimmed, ensure_ascii=False, indent=2))

    def clear(self, chat_id: int) -> None:
        path = self._path(chat_id)
        if path.exists():
            path.unlink()


class TinkerClawTelegramBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.store = ChatStateStore(config.state_dir, config.max_history_messages)
        self.offset = 0
        self._stop = asyncio.Event()
        self._telegram: aiohttp.ClientSession | None = None
        self._openrouter: aiohttp.ClientSession | None = None
        self.me: dict[str, Any] | None = None

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self.config.connect_timeout_s,
            sock_read=self.config.read_timeout_s,
        )
        self._telegram = aiohttp.ClientSession(timeout=timeout)
        self._openrouter = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self.config.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://tinkerclaw.local",
                "X-Title": "TinkerClaw Telegram Bot",
            },
        )
        self.me = await self._telegram_api("getMe")
        LOG.info("Telegram bot connected as @%s", self.me.get("username"))

    async def stop(self) -> None:
        self._stop.set()
        if self._telegram and not self._telegram.closed:
            await self._telegram.close()
        if self._openrouter and not self._openrouter.closed:
            await self._openrouter.close()

    async def run_forever(self) -> None:
        await self.start()
        while not self._stop.is_set():
            try:
                updates = await self._telegram_api(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": self.config.poll_timeout_s,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                for update in updates:
                    self.offset = max(self.offset, update["update_id"] + 1)
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Polling loop error")
                await asyncio.sleep(3)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id or not text:
            return

        user = message.get("from") or {}
        name = user.get("first_name") or user.get("username") or "there"
        LOG.info("message chat=%s user=%s text=%r", chat_id, user.get("id"), text[:120])

        if text == "/start":
            await self._send(chat_id, f"Hi {name} — I'm TinkerClaw on the Dragon hub. Send a message to chat.\n\n{HELP_TEXT}")
            return
        if text == "/help":
            await self._send(chat_id, HELP_TEXT)
            return
        if text == "/model":
            await self._send(chat_id, f"OpenRouter model: {self.config.openrouter_model}")
            return
        if text == "/clear":
            self.store.clear(chat_id)
            await self._send(chat_id, "Cleared this chat's memory.")
            return
        if text == "/status":
            uname = self.me.get("username") if self.me else "unknown"
            await self._send(
                chat_id,
                "\n".join([
                    "TinkerClaw Telegram bot is running.",
                    f"bot: @{uname}",
                    f"model: {self.config.openrouter_model}",
                    f"state_dir: {self.config.state_dir}",
                ]),
            )
            return

        await self._send_action(chat_id, "typing")
        reply = await self._chat_completion(chat_id, text)
        await self._send(chat_id, reply, reply_to_message_id=message.get("message_id"))

    async def _chat_completion(self, chat_id: int, user_text: str) -> str:
        history = self.store.load(chat_id)
        messages = [{"role": "system", "content": self.config.system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.config.openrouter_model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        assert self._openrouter is not None
        async with self._openrouter.post(f"{self.config.openrouter_url.rstrip('/')}/chat/completions", json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                LOG.error("OpenRouter error %s: %s", resp.status, body[:500])
                return f"OpenRouter error ({resp.status})."
            data = json.loads(body)
        try:
            content = data["choices"][0]["message"]["content"].strip()
        except Exception:
            LOG.error("Unexpected OpenRouter response: %s", body[:500])
            return "I got an unexpected model response."

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": content})
        self.store.save(chat_id, history)
        return content[:4000]

    async def _telegram_api(self, method: str, params: dict[str, Any] | None = None) -> Any:
        assert self._telegram is not None
        url = f"https://api.telegram.org/bot{self.config.telegram_token}/{method}"
        async with self._telegram.post(url, data=params or {}) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Telegram API {method} failed ({resp.status}): {body[:500]}")
            data = json.loads(body)
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API {method} error: {body[:500]}")
            return data["result"]

    async def _send(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
        params: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": "true",
        }
        if reply_to_message_id:
            params["reply_to_message_id"] = str(reply_to_message_id)
        await self._telegram_api("sendMessage", params)

    async def _send_action(self, chat_id: int, action: str) -> None:
        await self._telegram_api("sendChatAction", {"chat_id": str(chat_id), "action": action})


def _configure_logging() -> None:
    level = os.environ.get("TINKERCLAW_TELEGRAM_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _main() -> int:
    _configure_logging()
    config = BotConfig.from_env()
    bot = TinkerClawTelegramBot(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    try:
        await bot.run_forever()
        return 0
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(130)
