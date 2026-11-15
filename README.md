# DriveTelegramBot
Telegram bot saves any file sent to it into a Google Drive folder. Creates folder automatically.

## Features
- Any file type (voice, audio, video, photo, documents)
- Multiple files at once (albums / many messages) — bot asks a name for each
- `/skip` default name for current file, `/skipall` default names for all queued
- Rename after upload (inline button)
- Status: `/status` or the "📊 الحالة" button
- `/newfolder <name>` create + switch Drive folder, `/folder` show link

## Admin panel
Set `ADMIN_IDS` (comma-separated Telegram IDs, get yours from @userinfobot).
- `/admin`: stats, user list (last seen, upload count), ban / unban / approve buttons
- Approval mode: new users blocked until admin approves (toggle in panel)
- Admins get a notification on each new user; `/ban <id>`, `/unban <id>`
- Users stored in Supabase (`drivebot.users`)

## Setup
1. Create bot with @BotFather -> `BOT_TOKEN`
2. Google Cloud Console -> enable Drive API -> OAuth client (Desktop app) -> `credentials.json`
3. Run once locally (`pip install -r requirements.txt`, set the env vars below, `python bot.py`) -> browser login -> `token.json`
4. Database: schema `drivebot` (tables `users`, `settings`) in Supabase Postgres. Use the Session pooler connection string as `DATABASE_URL`.
5. Deploy (Railway): start command `python bot.py`, set variables:

| Var | Value |
|---|---|
| `BOT_TOKEN` | Telegram token |
| `ADMIN_IDS` | your Telegram ID(s), comma-separated |
| `DATABASE_URL` | Supabase pooler URI |
| `GOOGLE_TOKEN_JSON` | full contents of `token.json` |

## Big files (> 20MB)
Telegram's cloud Bot API limits bot downloads to 20MB. To go up to 2GB, run a local Bot API server and point the bot at it:
1. Get `api_id` / `api_hash` at my.telegram.org
2. Log the bot out of the cloud API once: `https://api.telegram.org/bot<TOKEN>/logOut`
3. Run the server on the same machine as the bot:
   `telegram-bot-api --api-id=ID --api-hash=HASH --local --dir=$HOME/tgapi --http-port=8081`
   (NixOS: `nix-shell -p telegram-bot-api`; Docker: `aiogram/telegram-bot-api`)
4. Start the bot with `BOT_API_URL=http://localhost:8081`

Without `BOT_API_URL` the limit is 20MB per file.
Never commit `token.json` / `credentials.json` / tokens (see `.gitignore`).
