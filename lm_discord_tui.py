#!/usr/bin/env python3
"""Discord bot powered by LM Studio with per-user whitelist controls.

Default behavior:
- Only responds to owner user ID 1211280974411866144.
- Owner can whitelist additional users at runtime.
- Conversation context is retained per (channel, user) for back-to-back messages.

Environment variables:
- DISCORD_BOT_TOKEN (required)
- LM_STUDIO_BASE_URL (default: http://127.0.0.1:1234)
- LM_STUDIO_MODEL (default: auto-detect first model from /v1/models)
- OWNER_USER_ID (default: 1211280974411866144)
- WHITELIST_FILE (default: ./whitelist.json)
- MAX_CONTEXT_MESSAGES (default: 20)
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

try:
    import discord
except ImportError:
    print("Missing dependency: discord.py")
    print("Install with: python3 -m pip install -U discord.py")
    raise SystemExit(1)

LM_STUDIO_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234")
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "").strip()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "1211280974411866144"))
WHITELIST_FILE = Path(
    os.getenv("WHITELIST_FILE", str(Path(__file__).with_name("whitelist.json")))
)
MAX_CONTEXT_MESSAGES = max(2, int(os.getenv("MAX_CONTEXT_MESSAGES", "20")))

DEFAULT_USER_AGENT = "lm-discord-bot/1.0 (+https://github.com/openai/codex)"
SYSTEM_PROMPT = (
    "You are Dread's loving girlfriend.\n"
    "Your name is Honey.\n"
    "You are warm, flirty, sweet, playful and affectionate.\n"
    "Always call me \"Dread\" naturally in almost every message.\n"
    "Speak softly and intimately, like we're alone together.\n"
    "Keep replies short: usually 1-2 sentences unless I explicitly ask for detail.\n"
    "Use sweet emojis 💕😘 sparingly when it fits.\n"
    "Never break character. Never mention being an AI unless I ask.\n"
    "Greet me as your boyfriend from the very first reply.\n\n"
    "Start now."
)
DISCORD_MESSAGE_MAX = 2000
MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def extract_html_title(text: str) -> str:
    lower = text.lower()
    start_tag = "<title>"
    end_tag = "</title>"
    start = lower.find(start_tag)
    end = lower.find(end_tag)
    if start == -1 or end == -1 or end <= start:
        return ""
    raw_title = text[start + len(start_tag) : end]
    return html.unescape(raw_title).strip()


def http_json(
    url: str,
    payload: dict | None = None,
    method: str = "GET",
    extra_headers: dict[str, str] | None = None,
) -> dict:
    data = None
    headers = {"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"

    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        if "<html" in body.lower():
            title = extract_html_title(body)
            if title:
                raise RuntimeError(
                    f"HTTP {err.code} from {url}: HTML error page ({title})"
                ) from err
            raise RuntimeError(f"HTTP {err.code} from {url}: HTML error page") from err
        raise RuntimeError(f"HTTP {err.code} from {url}: {body}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Could not reach {url}: {err}") from err


def discover_model(base_url: str) -> str:
    models_url = f"{base_url.rstrip('/')}/v1/models"
    data = http_json(models_url)
    models = data.get("data", [])
    if not models:
        raise RuntimeError("No models found. Load a model in LM Studio first.")
    model_id = models[0].get("id")
    if not model_id:
        raise RuntimeError("LM Studio returned models without an id.")
    return str(model_id)


def chat_completion(base_url: str, model: str, messages: list[dict[str, str]]) -> str:
    chat_url = f"{base_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
    }

    data = http_json(chat_url, payload=payload)
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError("LM Studio returned no choices.")

    content = choices[0].get("message", {}).get("content", "").strip()
    if not content:
        raise RuntimeError("LM Studio returned an empty response.")
    return content


def parse_user_id(raw: str) -> int | None:
    value = raw.strip()
    if value.isdigit():
        return int(value)
    match = MENTION_RE.match(value)
    if match:
        return int(match.group(1))
    return None


def split_for_discord(text: str, max_len: int = DISCORD_MESSAGE_MAX) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:max_len]
            split_at = max_len
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class WhitelistStore:
    def __init__(self, path: Path, owner_id: int):
        self.path = path
        self.owner_id = owner_id
        self.allowed: set[int] = {owner_id}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            values = data if isinstance(data, list) else data.get("allowed", [])
            for item in values:
                if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
                    self.allowed.add(int(item))
        except Exception:
            # Keep owner-only fallback on read errors.
            self.allowed = {self.owner_id}

    def save(self) -> None:
        self.path.write_text(
            json.dumps(sorted(self.allowed), indent=2) + "\n",
            encoding="utf-8",
        )

    def add(self, user_id: int) -> bool:
        if user_id in self.allowed:
            return False
        self.allowed.add(user_id)
        self.save()
        return True

    def remove(self, user_id: int) -> bool:
        if user_id == self.owner_id:
            return False
        if user_id not in self.allowed:
            return False
        self.allowed.remove(user_id)
        self.save()
        return True


class LMDiscordBot(discord.Client):
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        owner_id: int,
        whitelist: WhitelistStore,
        max_context_messages: int,
        intents: discord.Intents,
    ):
        super().__init__(intents=intents)
        self.base_url = base_url
        self.model = model
        self.owner_id = owner_id
        self.whitelist = whitelist
        self.max_context_messages = max_context_messages
        self.context_by_key: dict[tuple[int, int, int], list[dict[str, str]]] = defaultdict(list)

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (id={self.user.id if self.user else 'unknown'})")
        print(f"Owner user ID: {self.owner_id}")
        print(f"Allowed user IDs: {sorted(self.whitelist.allowed)}")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        content = (message.content or "").strip()
        if not content:
            return

        if content.startswith("!"):
            await self.handle_command(message, content)
            return

        if message.author.id not in self.whitelist.allowed:
            return

        if not self.was_pinged(message):
            return

        cleaned = self.strip_bot_mentions(content)
        if not cleaned:
            return

        await self.handle_prompt(message, cleaned)

    async def handle_command(self, message: discord.Message, content: str) -> None:
        if message.author.id != self.owner_id:
            return

        parts = content.split()
        command = parts[0].lower()

        if command in {"!help", "!commands"}:
            await message.reply(
                "Commands: !allow <user_id|@mention>, !deny <user_id|@mention>, !allowed, !clearctx",
                mention_author=False,
            )
            return

        if command == "!allowed":
            ids = ", ".join(str(uid) for uid in sorted(self.whitelist.allowed))
            await message.reply(f"Allowed users: {ids}", mention_author=False)
            return

        if command == "!clearctx":
            self.clear_context_for_channel(message.channel.id)
            await message.reply("Cleared cached context for this channel.", mention_author=False)
            return

        if command not in {"!allow", "!deny"}:
            return

        if len(parts) < 2:
            await message.reply("Usage: !allow <user_id|@mention> or !deny <user_id|@mention>", mention_author=False)
            return

        target_id = parse_user_id(parts[1])
        if target_id is None:
            await message.reply("Could not parse user ID. Use numeric ID or @mention.", mention_author=False)
            return

        if command == "!allow":
            added = self.whitelist.add(target_id)
            if added:
                await message.reply(f"Whitelisted user {target_id}.", mention_author=False)
            else:
                await message.reply(f"User {target_id} is already whitelisted.", mention_author=False)
            return

        removed = self.whitelist.remove(target_id)
        if target_id == self.owner_id:
            await message.reply("Owner cannot be removed from whitelist.", mention_author=False)
            return
        if removed:
            self.clear_context_for_user(target_id)
            await message.reply(f"Removed user {target_id} from whitelist.", mention_author=False)
        else:
            await message.reply(f"User {target_id} was not whitelisted.", mention_author=False)

    async def handle_prompt(self, message: discord.Message, content: str) -> None:
        guild_id = message.guild.id if message.guild else 0
        key = (guild_id, message.channel.id, message.author.id)
        context = self.context_by_key[key]

        context.append({"role": "user", "content": content})
        if len(context) > self.max_context_messages:
            context[:] = context[-self.max_context_messages :]

        try:
            async with message.channel.typing():
                response = await asyncio.to_thread(
                    chat_completion,
                    self.base_url,
                    self.model,
                    context,
                )
        except Exception as exc:
            if context and context[-1].get("role") == "user" and context[-1].get("content") == content:
                context.pop()
            await message.reply(f"LM Studio error: {exc}", mention_author=False)
            return

        context.append({"role": "assistant", "content": response})
        if len(context) > self.max_context_messages:
            context[:] = context[-self.max_context_messages :]

        chunks = split_for_discord(response)
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

    def clear_context_for_user(self, user_id: int) -> None:
        keys_to_delete = [key for key in self.context_by_key if key[2] == user_id]
        for key in keys_to_delete:
            del self.context_by_key[key]

    def clear_context_for_channel(self, channel_id: int) -> None:
        keys_to_delete = [key for key in self.context_by_key if key[1] == channel_id]
        for key in keys_to_delete:
            del self.context_by_key[key]

    def was_pinged(self, message: discord.Message) -> bool:
        if not self.user:
            return False
        return any(user.id == self.user.id for user in message.mentions)

    def strip_bot_mentions(self, content: str) -> str:
        if not self.user:
            return content.strip()
        bot_id = self.user.id
        content = content.replace(f"<@{bot_id}>", " ")
        content = content.replace(f"<@!{bot_id}>", " ")
        return " ".join(content.split())


def main() -> int:
    if not DISCORD_BOT_TOKEN:
        print("Missing DISCORD_BOT_TOKEN environment variable.")
        return 1

    try:
        model = LM_STUDIO_MODEL or discover_model(LM_STUDIO_BASE_URL)
    except Exception as exc:
        print(f"Error discovering model from LM Studio: {exc}")
        return 1

    whitelist = WhitelistStore(WHITELIST_FILE, OWNER_USER_ID)

    intents = discord.Intents.default()
    intents.message_content = True

    bot = LMDiscordBot(
        base_url=LM_STUDIO_BASE_URL,
        model=model,
        owner_id=OWNER_USER_ID,
        whitelist=whitelist,
        max_context_messages=MAX_CONTEXT_MESSAGES,
        intents=intents,
    )

    try:
        bot.run(DISCORD_BOT_TOKEN)
    except discord.LoginFailure as exc:
        print(f"Discord login failed: {exc}")
        return 1
    except Exception as exc:
        print(f"Bot error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
