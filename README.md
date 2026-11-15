# DriveTelegramBot
Telegram bot saves any file sent to it into a Google Drive folder. Creates folder automatically.

## Features
- Any file type (voice, audio, video, photo, documents)
- Multiple files at once (albums / many messages) — bot asks a name for each
- `/skip` default name for current file, `/skipall` default names for all queued
- Rename after upload (inline button)
- Status: `/status` or the "📊 الحالة" button
- `/newfolder <name>` create + switch Drive folder, `/folder` show link

## Setup
1. Create bot with @BotFather → `BOT_TOKEN`
2. Google Cloud Console → enable Drive API → OAuth client (Desktop app) → save as `credentials.json`
3. ```
   pip install -r requirements.txt
   BOT_TOKEN=... python bot.py
   ```
4. First run opens browser for Google login → `token.json` saved.

Telegram bot download limit: 20MB per file.
Never commit `token.json` / `credentials.json` (in `.gitignore`).
