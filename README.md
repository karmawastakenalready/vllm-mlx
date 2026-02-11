# LM Studio Discord Bot

A Discord bot that uses LM Studio's OpenAI-compatible API for replies.

Behavior:
- Responds only to whitelisted users when they ping/mention the bot.
- By default, only user `1211280974411866144` is whitelisted.
- You can whitelist/unwhitelist other users with owner-only commands.
- Keeps rolling context per `(guild/channel/user)` for back-to-back messages.

## Setup

1. Install dependencies:

```bash
cd /Users/karma/projects/lm-to-webhook
python3 -m pip install -r requirements.txt
```

2. Create a Discord bot and invite it with permissions to read/send messages.

3. In the Discord Developer Portal for your bot, enable **Message Content Intent**.

4. Export your bot token:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
```

5. Run:

```bash
python3 /Users/karma/projects/lm-to-webhook/lm_discord_tui.py
```

## Owner Commands

Only owner user ID `1211280974411866144` (or `OWNER_USER_ID` if overridden) can run these:

- `!allow <user_id|@mention>`: whitelist a user
- `!deny <user_id|@mention>`: remove user from whitelist
- `!allowed`: show current whitelist
- `!clearctx`: clear cached context in current channel

Whitelist is persisted in:
- `/Users/karma/projects/lm-to-webhook/whitelist.json`

## Environment Variables

- `DISCORD_BOT_TOKEN` (required)
- `LM_STUDIO_BASE_URL` (default: `http://127.0.0.1:1234`)
- `LM_STUDIO_MODEL` (default: auto-detect first model from `/v1/models`)
- `OWNER_USER_ID` (default: `1211280974411866144`)
- `WHITELIST_FILE` (default: `./whitelist.json`)
- `MAX_CONTEXT_MESSAGES` (default: `20`)
