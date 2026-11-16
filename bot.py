"""DriveTelegramBot: send files to a Telegram bot -> saved to a Google Drive folder.

Env vars:
  BOT_TOKEN          Telegram bot token (@BotFather)
  ADMIN_IDS          comma-separated Telegram user IDs of admins (@userinfobot)
  DATABASE_URL       Postgres connection string (Supabase pooler URI)
  GOOGLE_TOKEN_JSON  contents of token.json (authorized-user JSON w/ refresh token)
  FOLDER_NAME        optional, default "Telegram Uploads"
  BOT_API_URL        optional, self-hosted Bot API server URL (e.g. http://localhost:8081) -> files up to 2GB

Local first run (to create token.json): put credentials.json next to bot.py and run;
a browser opens for Google login. Then paste token.json contents into GOOGLE_TOKEN_JSON on the server.

Flow:
  - send 1+ files -> bot asks name for each (/skip, /skipall = default names)
  - after upload: button to rename the Drive file
  - status: /status or "📊 الحالة" button
  - /newfolder <name>, /folder
  - admin: /admin (users, ban/unban, approval mode), /ban <id>, /unban <id>
Download limit: 20MB (cloud Bot API) or 2GB with BOT_API_URL (local Bot API server).
"""
import asyncio, os, re, json, datetime, mimetypes, tempfile, traceback, time
import psycopg2
from psycopg2.extras import RealDictCursor
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler,
    TypeHandler, ApplicationHandlerStop, ContextTypes, filters,
)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
DEFAULT_FOLDER = os.environ.get("FOLDER_NAME", "Telegram Uploads")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
STATUS_BTN = "📊 الحالة"
# Optional: URL of a self-hosted Telegram Bot API server (run with --local) -> files up to 2GB.
# Without it, Telegram's cloud Bot API limits downloads to 20MB.
BOT_API_URL = os.environ.get("BOT_API_URL", "").rstrip("/")
MAX_BYTES = (2000 if BOT_API_URL else 20) * 1024 * 1024
DRIVE_LOCK = asyncio.Lock()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}


# ---------- database (Supabase Postgres, schema "drivebot") ----------
def q(sql, args=(), fetch=None):
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
    finally:
        conn.close()


def get_setting(key, default=None):
    r = q("select value from drivebot.settings where key=%s", (key,), "one")
    return r["value"] if r else default


def set_setting(key, value):
    q(
        "insert into drivebot.settings (key, value) values (%s, %s) "
        "on conflict (key) do update set value = excluded.value",
        (key, value),
    )


def touch_user(u, default_status):
    return q(
        "insert into drivebot.users (id, name, username, status) values (%s, %s, %s, %s) "
        "on conflict (id) do update set name = excluded.name, username = excluded.username, last_seen = now() "
        "returning *, (xmax = 0) as is_new",
        (u.id, u.full_name, u.username, default_status),
        "one",
    )


def set_status(uid, status):
    return q("update drivebot.users set status=%s where id=%s returning *", (status, int(uid)), "one")


def inc_uploads(uid):
    q("update drivebot.users set uploads = uploads + 1 where id=%s", (uid,))


def label(rec):
    return f"@{rec['username']}" if rec.get("username") else rec["name"]


# ---------- Google Drive ----------
def drive_service():
    creds = None
    tok = os.environ.get("GOOGLE_TOKEN_JSON")
    if tok:
        creds = Credentials.from_authorized_user_info(json.loads(tok), SCOPES)
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES).run_local_server(port=0)
        if not tok:
            with open("token.json", "w") as f:
                f.write(creds.to_json())
    return build("drive", "v3", credentials=creds)


drive = drive_service()


def make_folder(name):
    r = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id"
    ).execute()
    set_setting("folder_id", r["id"])
    return r["id"]


def folder_id():
    return get_setting("folder_id") or make_folder(DEFAULT_FOLDER)


def folder_url():
    return f"https://drive.google.com/drive/folders/{folder_id()}"


def clean(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def with_ext(name, ext):
    name = clean(name)
    return name if (not ext or name.lower().endswith(ext.lower())) else name + ext


# ---------- UI helpers ----------
def kb_after_upload(file_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ غيّر الاسم", callback_data=f"ren:{file_id}"),
        InlineKeyboardButton(STATUS_BTN, callback_data="status"),
    ]])


def kb_status():
    return InlineKeyboardMarkup([[InlineKeyboardButton(STATUS_BTN, callback_data="status")]])


def status_text(ud):
    qd = ud.get("queue", [])
    up = ud.get("uploaded", [])
    lines = [f"⏳ بالانتظار: {len(qd)}", f"✅ انرفع: {len(up)}"]
    if qd:
        lines.append(f"التالي: {qd[0]['label']}")
    if up:
        lines.append(f"آخر ملف: {up[-1]['name']}")
    lines.append(f"📁 {folder_url()}")
    return "\n".join(lines)


async def ask_next(ctx, chat_id, ud):
    qd = ud.get("queue", [])
    if not qd:
        return
    await ctx.bot.send_message(
        chat_id,
        f"شو الاسم يلي بدك ياه للملف؟\n«{qd[0]['label']}» (بالانتظار: {len(qd)})\n"
        "/skip = اسم افتراضي · /skipall = الكل افتراضي",
        reply_markup=kb_status(),
    )


# ---------- upload ----------
def _format_eta(seconds):
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانية"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} دقيقة و{seconds} ثانية"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ساعة و{minutes} دقيقة"


def _upload_progress_text(name, uploaded, total, elapsed):
    fraction = min(1.0, max(0.0, uploaded / total)) if total else 0.0
    percent = int(fraction * 100)
    blocks = int(fraction * 12)
    bar = "█" * blocks + "░" * (12 - blocks)
    if uploaded > 0 and uploaded < total:
        remaining = (total - uploaded) * elapsed / uploaded
        eta = f"⏳ باقي تقريباً: {_format_eta(remaining)}"
    elif uploaded >= total and total > 0:
        eta = "⏳ عم أكد حفظ الملف على Drive..."
    else:
        eta = "⏳ عم احسب الوقت من سرعة الرفع..."
    mb = 1024 * 1024
    short_name = name if len(name) <= 65 else name[:62] + "…"
    return (
        f"📤 عم ارفع على Google Drive: {short_name}\n"
        f"{bar} {percent}%\n"
        f"{uploaded / mb:.1f} / {total / mb:.1f} MB\n"
        f"{eta}"
    )


async def _edit_upload_status(message, text, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
        return True
    except Exception as exc:
        print(f"Could not update progress message: {type(exc).__name__}")
        return False


async def upload_item(ctx, chat_id, ud, item, chosen_name):
    name = with_ext(chosen_name, item["ext"]) if chosen_name else item["default_name"]
    status_msg = await ctx.bot.send_message(chat_id, f"📥 عم جهّز «{name[:65]}» من Telegram...")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=item["ext"] or ".bin")
    tmp.close()
    try:
        tg_file = await ctx.bot.get_file(item["file_id"])
        parent = await asyncio.to_thread(folder_id)
        await tg_file.download_to_drive(tmp.name)  # disk, not RAM
        total_bytes = os.path.getsize(tmp.name)
        state = {"uploaded": 0}  # One writer (upload thread), one reader (event loop).

        def _create():
            media = MediaFileUpload(
                tmp.name, mimetype=item["mime"], resumable=True,
                chunksize=8 * 1024 * 1024,
            )
            request = drive.files().create(
                body={"name": name, "parents": [parent]}, media_body=media, fields="id"
            )
            result = None
            while result is None:
                progress, result = request.next_chunk(num_retries=3)
                if progress is not None:
                    state["uploaded"] = min(total_bytes, progress.resumable_progress)
            state["uploaded"] = total_bytes
            return result

        await _edit_upload_status(status_msg, "⏳ الملف جاهز، عم استنى دوري للرفع...")
        async with DRIVE_LOCK:
            started = time.monotonic()
            worker = asyncio.create_task(asyncio.to_thread(_create))
            last_text = None
            while not worker.done():
                body = _upload_progress_text(name, state["uploaded"], total_bytes, time.monotonic() - started)
                if body != last_text:
                    await _edit_upload_status(status_msg, body)
                    last_text = body
                await asyncio.wait({worker}, timeout=4)
            result = await worker

        ud.setdefault("uploaded", []).append({"id": result["id"], "name": name})
        try:
            await asyncio.to_thread(inc_uploads, item["uid"])
        except Exception as exc:
            print(f"Upload counter update failed: {type(exc).__name__}")
        await _edit_upload_status(status_msg, "✅ اكتمل الرفع إلى Drive (100%)")
        await ctx.bot.send_message(
            chat_id, f"✅ انرفع على Google Drive: {name}",
            reply_markup=kb_after_upload(result["id"]),
        )
    except Exception:
        await _edit_upload_status(status_msg, f"❌ ما اكتمل رفع «{name[:65]}». جرّب مرة ثانية.")
        raise
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ابعتلي أي ملف (أو كذا ملف سوا) وبرفعو على Drive.",
        reply_markup=ReplyKeyboardMarkup([[STATUS_BTN]], resize_keyboard=True),
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(status_text(ctx.user_data))


async def newfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = " ".join(ctx.args) or DEFAULT_FOLDER
    fid = make_folder(name)
    await update.message.reply_text(f"📁 {name}\nhttps://drive.google.com/drive/folders/{fid}")


async def folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(folder_url())


async def on_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    ud = ctx.user_data
    att = m.effective_attachment
    if isinstance(att, (list, tuple)):  # photo -> biggest size
        att = att[-1]
    if not hasattr(att, "file_id"):
        return
    size = getattr(att, "file_size", None)
    if size and size > MAX_BYTES:
        await m.reply_text(
            f"❌ الملف كبير ({size / 1024 / 1024:.0f}MB). الحد الحالي {MAX_BYTES // 1024 // 1024}MB."
        )
        return

    mime = getattr(att, "mime_type", None)
    orig = getattr(att, "file_name", None)
    if m.photo:
        mime, ext, kind = "image/jpeg", ".jpg", "صورة"
    elif m.voice:
        mime, ext, kind = mime or "audio/ogg", ".ogg", "فويس"
    elif m.video_note:
        mime, ext, kind = "video/mp4", ".mp4", "فيديو"
    else:
        ext = (os.path.splitext(orig)[1] if orig else "") or mimetypes.guess_extension(mime or "") or ""
        kind = "ملف"
    mime = mime or "application/octet-stream"

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    u = m.from_user
    base = os.path.splitext(orig)[0] if orig else "file"
    item = {
        "file_id": att.file_id,
        "uid": u.id,
        "mime": mime,
        "ext": ext,
        "label": orig or f"{kind} {m.message_id}",
        "default_name": clean(f"{ts}_{u.username or u.id}_{m.message_id}_{base}{ext}"),
    }
    queue = ud.setdefault("queue", [])
    was_empty = not queue
    queue.append(item)

    if was_empty and not ud.get("renaming"):
        await ask_next(ctx, m.chat_id, ud)
    elif not m.media_group_id or ud.get("last_group") != m.media_group_id:
        ud["last_group"] = m.media_group_id
        await m.reply_text(f"➕ انضاف للطابور (بالانتظار: {len(queue)})", reply_markup=kb_status())


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    chat_id = update.message.chat_id
    text = update.message.text

    # rename an already-uploaded file
    if ud.get("renaming"):
        fid = ud.pop("renaming")
        cur = drive.files().get(fileId=fid, fields="name").execute()["name"]
        new = with_ext(text, os.path.splitext(cur)[1])
        drive.files().update(fileId=fid, body={"name": new}).execute()
        for x in ud.get("uploaded", []):
            if x["id"] == fid:
                x["name"] = new
        await update.message.reply_text(f"✏️ الاسم الجديد: {new}", reply_markup=kb_after_upload(fid))
        await ask_next(ctx, chat_id, ud)
        return

    # name for the next queued file
    qd = ud.get("queue", [])
    if not qd:
        return
    item = qd.pop(0)
    await upload_item(ctx, chat_id, ud, item, text)
    await ask_next(ctx, chat_id, ud)


async def skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    qd = ud.get("queue", [])
    if not qd:
        return
    await upload_item(ctx, update.message.chat_id, ud, qd.pop(0), None)
    await ask_next(ctx, update.message.chat_id, ud)


async def skipall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    qd = ud.get("queue", [])
    while qd:
        await upload_item(ctx, update.message.chat_id, ud, qd.pop(0), None)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if cq.data == "status":
        await cq.message.reply_text(status_text(ctx.user_data))
    elif cq.data.startswith("ren:"):
        ctx.user_data["renaming"] = cq.data[4:]
        await cq.message.reply_text("شو الاسم الجديد للملف؟")


# ---------- access gate + admin panel ----------
def is_admin(update):
    return update.effective_user and update.effective_user.id in ADMIN_IDS


def user_button(rec):
    uid, st = rec["id"], rec["status"]
    if st == "banned":
        return InlineKeyboardButton(f"♻️ فك حظر {label(rec)}", callback_data=f"adm:set:{uid}:active")
    if st == "pending":
        return InlineKeyboardButton(f"✅ قبول {label(rec)}", callback_data=f"adm:set:{uid}:active")
    return InlineKeyboardButton(f"🚫 حظر {label(rec)}", callback_data=f"adm:set:{uid}:banned")


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs before every handler: records users, blocks banned / unapproved."""
    u = update.effective_user
    if not u or u.id in ADMIN_IDS:
        return
    default = "pending" if get_setting("approval_mode", "false") == "true" else "active"
    rec = touch_user(u, default)
    if rec["is_new"]:
        for aid in ADMIN_IDS:
            await ctx.bot.send_message(
                aid, f"👤 مستخدم جديد: {label(rec)} ({u.id}) — {rec['status']}",
                reply_markup=InlineKeyboardMarkup([[user_button(rec)]]),
            )
    if rec["status"] == "banned":
        raise ApplicationHandlerStop
    if rec["status"] == "pending":
        if update.effective_chat:
            await ctx.bot.send_message(update.effective_chat.id, "⏳ بانتظار موافقة الأدمن.")
        raise ApplicationHandlerStop


def panel_markup():
    mode = "ON" if get_setting("approval_mode", "false") == "true" else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 المستخدمين", callback_data="adm:list"),
         InlineKeyboardButton("📈 إحصائيات", callback_data="adm:stats")],
        [InlineKeyboardButton(f"🔐 وضع الموافقة: {mode}", callback_data="adm:mode")],
    ])


def stats_text():
    rows = q(
        "select status, count(*) as c, coalesce(sum(uploads), 0) as up from drivebot.users group by status",
        fetch="all",
    )
    by = {r["status"]: r["c"] for r in rows}
    total = sum(by.values())
    uploads = sum(int(r["up"]) for r in rows)
    on = get_setting("approval_mode", "false") == "true"
    return (
        "🛠 لوحة الأدمن\n"
        f"👥 المستخدمين: {total}\n"
        f"✅ فعّال: {by.get('active', 0)} · ⏳ منتظر: {by.get('pending', 0)} · 🚫 محظور: {by.get('banned', 0)}\n"
        f"📤 مجموع الرفع: {uploads}\n"
        f"🔐 وضع الموافقة: {'ON (المستخدم الجديد لازم تقبلو)' if on else 'OFF'}\n"
        f"📁 {folder_url()}"
    )


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(stats_text(), reply_markup=panel_markup())


async def ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not ctx.args or not ctx.args[0].isdigit():
        return
    rec = set_status(ctx.args[0], "banned")
    await update.message.reply_text(f"🚫 {label(rec)}" if rec else "ما لقيت هالـ ID")


async def unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or not ctx.args or not ctx.args[0].isdigit():
        return
    rec = set_status(ctx.args[0], "active")
    await update.message.reply_text(f"♻️ {label(rec)}" if rec else "ما لقيت هالـ ID")


async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if cq.from_user.id not in ADMIN_IDS:
        await cq.answer("مو أدمن", show_alert=True)
        return
    await cq.answer()
    parts = cq.data.split(":")
    action = parts[1]

    if action == "set":
        rec = set_status(parts[2], parts[3])
        if rec:
            try:
                await ctx.bot.send_message(rec["id"], "🚫 انحظرت." if parts[3] == "banned" else "✅ تم السماح لك.")
            except Exception:
                pass
        action = "list"
    elif action == "mode":
        on = get_setting("approval_mode", "false") == "true"
        set_setting("approval_mode", "false" if on else "true")
        await cq.edit_message_text(stats_text(), reply_markup=panel_markup())
        return

    if action == "stats":
        await cq.edit_message_text(stats_text(), reply_markup=panel_markup())
    elif action == "list":
        recs = q("select * from drivebot.users order by last_seen desc limit 15", fetch="all")
        icon = {"active": "✅", "pending": "⏳", "banned": "🚫"}
        lines = [
            f"{icon[r['status']]} {label(r)} · {r['id']} · رفع: {r['uploads']} · آخر ظهور: {r['last_seen']:%Y-%m-%d %H:%M}"
            for r in recs
        ] or ["ما في مستخدمين بعد."]
        rows = [[user_button(r)] for r in recs]
        rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="adm:stats")])
        await cq.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Log every handler error; show details to admins, generic message to users."""
    err = ctx.error
    traceback.print_exception(type(err), err, err.__traceback__)
    detail = f"❌ {type(err).__name__}: {err}"[:600]
    chat_id = update.effective_chat.id if isinstance(update, Update) and update.effective_chat else None
    targets = set(ADMIN_IDS)
    if chat_id and chat_id not in ADMIN_IDS:
        try:
            await ctx.bot.send_message(chat_id, "❌ صار خطأ، جرّب مرة ثانية.")
        except Exception:
            pass
    for t in targets:
        try:
            await ctx.bot.send_message(t, detail)
        except Exception:
            pass


if not ADMIN_IDS:
    print("WARNING: ADMIN_IDS not set -> no admin panel. Get your ID from @userinfobot")

def start_health_server():
    """Tiny HTTP endpoint for hosts that require an open port (Render/Koyeb free) + uptime pingers."""
    port = os.environ.get("PORT")
    if not port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()


start_health_server()

builder = ApplicationBuilder().token(BOT_TOKEN)
if BOT_API_URL:
    builder = builder.base_url(f"{BOT_API_URL}/bot").base_file_url(f"{BOT_API_URL}/file/bot").local_mode(True)
app = builder.build()
app.add_error_handler(on_error)
app.add_handler(TypeHandler(Update, gate), group=-1)
app.add_handler(CommandHandler("admin", admin))
app.add_handler(CommandHandler("ban", ban))
app.add_handler(CommandHandler("unban", unban))
app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^adm:"))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("newfolder", newfolder))
app.add_handler(CommandHandler("folder", folder))
app.add_handler(CommandHandler("skip", skip))
app.add_handler(CommandHandler("skipall", skipall))
app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(status|ren:)"))
app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(STATUS_BTN)}$"), status))
app.add_handler(MessageHandler(filters.ATTACHMENT, on_file))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
app.run_polling()
