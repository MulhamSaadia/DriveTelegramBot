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
from telegram.error import NetworkError, TimedOut, BadRequest
from googleapiclient.errors import HttpError
from user_drive import UserDrive, FOLDER_MIME, FolderAccessError, safe_name
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
FILES_BTN = "📁 ملفاتي"
NEW_FOLDER_BTN = "➕ مجلد جديد"
UPLOAD_BTN = "📤 رفع ملف"
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


USER_DRIVE = UserDrive(drive, get_setting, set_setting, folder_id)


def clean(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


def with_ext(name, ext):
    name = clean(name)
    return name if (not ext or name.lower().endswith(ext.lower())) else name + ext


# ---------- UI helpers ----------
def kb_after_upload(file_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ غيّر الاسم", callback_data=f"ren:{file_id}"),
         InlineKeyboardButton("🗑 حذف", callback_data=f"trash:{file_id}")],
        [InlineKeyboardButton(FILES_BTN, callback_data="home"),
         InlineKeyboardButton(STATUS_BTN, callback_data="status")],
    ])


def kb_status():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ اسم افتراضي", callback_data="skipone"),
         InlineKeyboardButton("✅ الكل افتراضي", callback_data="skipall")],
        [InlineKeyboardButton(FILES_BTN, callback_data="home"),
         InlineKeyboardButton(STATUS_BTN, callback_data="status")],
    ])

def status_text(ud, folder_name=None):
    qd = ud.get("queue", [])
    up = ud.get("uploaded", [])
    lines = [f"⏳ بالانتظار: {len(qd)}", f"✅ انرفع: {len(up)}"]
    if qd:
        lines.append(f"التالي: {qd[0]['label']}")
    if up:
        lines.append(f"آخر ملف: {up[-1]['name']}")
    if folder_name:
        lines.append(f"📁 مكان الرفع: {folder_name}")
    return "\n".join(lines)


async def ask_next(ctx, chat_id, ud):
    qd = ud.get("queue", [])
    if not qd:
        return
    await ctx.bot.send_message(
        chat_id,
        f"شو الاسم يلي بدك ياه للملف؟\n«{qd[0]['label']}» (بالانتظار: {len(qd)})\n"
        f"📁 مكان الرفع: {qd[0].get('target_name', 'ملفاتي')}\n"
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


async def _get_telegram_file(bot, file_id, status_msg):
    """A local Bot API server may need minutes to prepare a large Telegram file."""
    for attempt in range(3):
        started = time.monotonic()
        task = asyncio.create_task(bot.get_file(
            file_id, read_timeout=600, connect_timeout=30, pool_timeout=30,
        ))
        while not task.done():
            await asyncio.wait({task}, timeout=12)
            if not task.done():
                elapsed = int(time.monotonic() - started)
                await _edit_upload_status(
                    status_msg,
                    f"📥 عم جيب الملف من Telegram... مرّت {elapsed} ثانية.\n"
                    "⏳ عم استنى تجهيز الملف؛ نسبة رفع Drive بتظهر بعده.",
                )
        try:
            return await task
        except (TimedOut, NetworkError) as exc:
            print(f"Telegram get_file attempt {attempt + 1}/3 failed: {type(exc).__name__}", flush=True)
            if attempt == 2:
                raise
            await _edit_upload_status(
                status_msg, f"🔄 اتصال Telegram تأخر، عم جرّب مرة {attempt + 2} من 3...",
            )
            await asyncio.sleep(2 ** (attempt + 1))


async def _download_telegram_file(tg_file, path, status_msg):
    for attempt in range(3):
        try:
            await tg_file.download_to_drive(
                path, read_timeout=600, connect_timeout=30, pool_timeout=30,
            )
            return
        except (TimedOut, NetworkError) as exc:
            print(f"Telegram download attempt {attempt + 1}/3 failed: {type(exc).__name__}", flush=True)
            if attempt == 2:
                raise
            await _edit_upload_status(
                status_msg, f"🔄 تنزيل الملف انقطع، عم جرّب مرة {attempt + 2} من 3...",
            )
            await asyncio.sleep(2 ** (attempt + 1))


async def upload_item(ctx, chat_id, ud, item, chosen_name):
    name = with_ext(chosen_name, item["ext"]) if chosen_name else item["default_name"]
    status_msg = await ctx.bot.send_message(chat_id, f"📥 عم جهّز «{name[:65]}» من Telegram...")
    tmp_path = None
    try:
        tg_file = await _get_telegram_file(ctx.bot, item["file_id"], status_msg)
        parent = (await _drive_call(
            USER_DRIVE.require_folder, item["uid"], item["target_folder"],
            item.get("owner_name", "مستخدم"),
        ))["id"]
        # With local Bot API, get_file() already returns a shared on-disk path.
        # Reuse it instead of duplicating multi-GB files on Render's ephemeral disk.
        local_path = tg_file.file_path
        if BOT_API_URL and local_path and os.path.isabs(local_path) and os.path.isfile(local_path):
            upload_path = local_path
        else:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=item["ext"] or ".bin")
            tmp_path = tmp.name
            tmp.close()
            await _download_telegram_file(tg_file, tmp_path, status_msg)
            upload_path = tmp_path
        total_bytes = os.path.getsize(upload_path)
        state = {"uploaded": 0}  # One writer (upload thread), one reader (event loop).

        def _create():
            media = MediaFileUpload(
                upload_path, mimetype=item["mime"], resumable=True,
                chunksize=8 * 1024 * 1024,
            )
            request = drive.files().create(
                body={"name": name, "parents": [parent],
                      "appProperties": {"tg_owner": str(item["uid"]), "tg_role": "file"}},
                media_body=media, fields="id"
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
        await _edit_upload_status(status_msg, f"❌ ما اكتمل رفع «{name[:65]}». الملف لسه بالطابور؛ اكتب الاسم أو /skip لإعادة المحاولة.")
        raise
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! صار لكل مستخدم مجلد خاص داخل Google Drive تبع البوت.\n"
        "📁 تصفح ملفاتك، ➕ أنشئ مجلد، أو ابعت أي ملف لرفعه.",
        reply_markup=_menu_keyboard(),
    )


async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    person = update.effective_user
    active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
    await update.effective_message.reply_text(
        status_text(ctx.user_data, active["name"]), reply_markup=_menu_keyboard(),
    )


async def newfolder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await _private_ui(update):
        return
    person = update.effective_user
    active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
    name = " ".join(ctx.args).strip() if ctx.args else ""
    if name:
        await _create_and_select(update, ctx, active["id"], name)
    else:
        ctx.user_data.pop("renaming", None)
        ctx.user_data["creating_folder_in"] = active["id"]
        await update.effective_message.reply_text("➕ شو اسم المجلد الجديد؟")


async def folder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_browser(update, ctx)

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
    target = await _drive_call(USER_DRIVE.current, u.id, u.full_name)
    item = {
        "file_id": att.file_id,
        "owner_name": u.full_name,
        "target_folder": target["id"],
        "target_name": target["name"],
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
    person = update.effective_user

    if text == FILES_BTN:
        ud.pop("renaming", None)
        ud.pop("creating_folder_in", None)
        await user_files(update, ctx)
        return
    if text == NEW_FOLDER_BTN:
        if not await _private_ui(update):
            return
        active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
        ud.pop("renaming", None)
        ud["creating_folder_in"] = active["id"]
        await update.message.reply_text("➕ شو اسم المجلد الجديد؟")
        return
    if text == UPLOAD_BTN:
        await upload_help(update, ctx)
        return
    if text == STATUS_BTN:
        await status(update, ctx)
        return

    if ud.get("creating_folder_in"):
        if not await _private_ui(update):
            return
        await _create_and_select(update, ctx, ud["creating_folder_in"], text)
        ud.pop("creating_folder_in", None)
        return

    if ud.get("renaming"):
        if not await _private_ui(update):
            return
        fid = ud["renaming"]
        old = await _drive_call(USER_DRIVE.require_item, person.id, fid, person.full_name)
        name = safe_name(text)
        if old.get("mimeType") != FOLDER_MIME:
            name = with_ext(name, os.path.splitext(old.get("name", ""))[1])
        renamed = await _drive_call(USER_DRIVE.rename, person.id, fid, name, person.full_name)
        ud.pop("renaming", None)
        for x in ud.get("uploaded", []):
            if x["id"] == fid:
                x["name"] = renamed["name"]
        await update.message.reply_text(
            f"✏️ الاسم الجديد: {renamed['name']}", reply_markup=_menu_keyboard(),
        )
        await ask_next(ctx, chat_id, ud)
        return

    queue = ud.get("queue", [])
    if not queue:
        return
    await upload_item(ctx, chat_id, ud, queue[0], text)
    queue.pop(0)
    await ask_next(ctx, chat_id, ud)

async def skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    qd = ud.get("queue", [])
    if not qd:
        return
    await upload_item(ctx, update.message.chat_id, ud, qd[0], None)
    qd.pop(0)
    await ask_next(ctx, update.message.chat_id, ud)


async def skipall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ud = ctx.user_data
    qd = ud.get("queue", [])
    while qd:
        await upload_item(ctx, update.message.chat_id, ud, qd[0], None)
        qd.pop(0)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    await cq.answer()
    if cq.data == "status":
        person = update.effective_user
        active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
        await cq.message.reply_text(status_text(ctx.user_data, active["name"]))
    elif cq.data.startswith("ren:"):
        if not await _private_ui(update):
            return
        person = update.effective_user
        fid = cq.data[4:]
        meta = await _drive_call(USER_DRIVE.require_item, person.id, fid, person.full_name)
        if (meta.get("appProperties") or {}).get("tg_role") == "root":
            await cq.message.reply_text("❌ ما فيك تغيّر اسم المجلد الأساسي.")
            return
        ctx.user_data.pop("creating_folder_in", None)
        ctx.user_data["renaming"] = fid
        await cq.message.reply_text(f"✏️ شو الاسم الجديد لـ «{meta.get('name', 'الملف')}»؟")


# ---------- per-user browser ----------
def _menu_keyboard():
    return ReplyKeyboardMarkup(
        [[FILES_BTN, NEW_FOLDER_BTN], [UPLOAD_BTN, STATUS_BTN]], resize_keyboard=True,
    )


async def _drive_call(fn, *args):
    async with DRIVE_LOCK:
        return await asyncio.to_thread(fn, *args)


async def _private_ui(update):
    if update.effective_chat.type == "private":
        return True
    await update.effective_message.reply_text("🔒 لإدارة ملفاتك افتح محادثة خاصة مع البوت.")
    return False


def _browser_snapshot(uid, display, folder_id):
    root = USER_DRIVE.ensure_root(uid, display)
    folder = USER_DRIVE.require_folder(uid, folder_id or root["id"], display)
    active = USER_DRIVE.current(uid, display)
    items, more = USER_DRIVE.list_children(uid, folder["id"], display)
    return root, folder, active, items, more


def _item_button(label, verb, file_id):
    payload = f"{verb}:{file_id}"
    if len(payload.encode("utf-8")) > 64:
        return None
    return InlineKeyboardButton(label, callback_data=payload)


async def show_browser(update, ctx, folder_id=None, page=0):
    if not await _private_ui(update):
        return
    person = update.effective_user
    root, folder, current, items, more = await _drive_call(
        _browser_snapshot, person.id, person.full_name, folder_id,
    )
    limit = 8
    pages = max(0, (len(items) - 1) // limit)
    page = max(0, min(int(page), pages))
    rows = []
    for item in items[page * limit:(page + 1) * limit]:
        is_folder = item.get("mimeType") == FOLDER_MIME
        btn = _item_button(
            ("📁 " if is_folder else "📄 ") + item.get("name", "ملف")[:36],
            "nav" if is_folder else "item", item["id"],
        )
        if btn:
            rows.append([btn])
    pager = []
    if page:
        pager.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"page:{folder['id']}:{page-1}"))
    if page < pages:
        pager.append(InlineKeyboardButton("التالي ➡️", callback_data=f"page:{folder['id']}:{page+1}"))
    if pager:
        rows.append(pager)
    rows.append([
        InlineKeyboardButton("✅ خلي الرفع هون", callback_data=f"choose:{folder['id']}"),
        InlineKeyboardButton("➕ مجلد جديد", callback_data=f"add:{folder['id']}"),
    ])
    controls = [InlineKeyboardButton("🔄 تحديث", callback_data=f"nav:{folder['id']}")]
    if folder["id"] != root["id"]:
        parent = (folder.get("parents") or [root["id"]])[0]
        controls.append(InlineKeyboardButton("⬅️ رجوع", callback_data=f"nav:{parent}"))
        deletion = _item_button("🗑 حذف هذا المجلد", "trash", folder["id"])
        if deletion:
            rows.append([deletion])
    else:
        controls.append(InlineKeyboardButton("🏠 مجلدي", callback_data="home"))
    rows.append(controls)
    summary = (
        f"📁 {folder.get('name', 'ملفاتي')}\n"
        f"📍 مكان الرفع: {current.get('name', '')}\n"
        f"📚 {len(items)} عنصر" + (" (أول 1000 فقط)" if more else "") + "\n\n" +
        ("اضغط على ملف أو مجلد من القائمة 👇" if items else "المجلد فاضي. ابعت ملف أو أنشئ مجلد.")
    )
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(summary, reply_markup=markup)
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                await update.effective_message.reply_text(summary, reply_markup=markup)
    else:
        await update.effective_message.reply_text(summary, reply_markup=markup)


async def show_item(update, ctx, file_id):
    if not await _private_ui(update):
        return
    person = update.effective_user
    meta = await _drive_call(USER_DRIVE.require_item, person.id, file_id, person.full_name)
    if meta.get("mimeType") == FOLDER_MIME:
        await show_browser(update, ctx, file_id)
        return
    parent = (meta.get("parents") or [None])[0]
    size = int(meta.get("size") or 0) / (1024 * 1024)
    text = f"📄 {meta.get('name', 'ملف')}\n📦 الحجم: {size:.1f} MB\n\nشو بدك تعمل؟"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ تغيير الاسم", callback_data=f"ren:{file_id}"),
         InlineKeyboardButton("🗑 حذف", callback_data=f"trash:{file_id}")],
        [InlineKeyboardButton("⬅️ رجوع", callback_data=f"nav:{parent}")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def user_files(update, ctx):
    await show_browser(update, ctx)


async def upload_help(update, ctx):
    if not await _private_ui(update):
        return
    person = update.effective_user
    active = await _drive_call(USER_DRIVE.current, person.id, person.full_name)
    await update.effective_message.reply_text(
        f"📤 ابعت الملف هون، ورح يروح على «{active['name']}».\n"
        "لتغيير المكان اضغط 📁 ملفاتي.", reply_markup=_menu_keyboard(),
    )


async def _create_and_select(update, ctx, parent_id, text):
    person = update.effective_user
    made = await _drive_call(
        USER_DRIVE.create_folder, person.id, parent_id, text, person.full_name,
    )
    await _drive_call(USER_DRIVE.select, person.id, made["id"], person.full_name)
    await update.effective_message.reply_text(f"✅ أنشأت «{made['name']}» وصار مجلد الرفع الحالي.")
    await show_browser(update, ctx, made["id"])


async def on_browser_button(update, ctx):
    cq = update.callback_query
    await cq.answer()
    if not await _private_ui(update):
        return
    person = update.effective_user
    uid, display = person.id, person.full_name
    action, _, arg = cq.data.partition(":")
    try:
        if action == "home":
            root = await _drive_call(USER_DRIVE.ensure_root, uid, display)
            await show_browser(update, ctx, root["id"])
        elif action == "nav":
            await show_browser(update, ctx, arg)
        elif action == "page":
            fid, pg = arg.rsplit(":", 1)
            await show_browser(update, ctx, fid, int(pg))
        elif action == "item":
            await show_item(update, ctx, arg)
        elif action == "choose":
            selected = await _drive_call(USER_DRIVE.select, uid, arg, display)
            await cq.message.reply_text(f"✅ الرفع الجديد رح يروح على «{selected['name']}».")
            await show_browser(update, ctx, selected["id"])
        elif action == "add":
            await _drive_call(USER_DRIVE.require_folder, uid, arg, display)
            ctx.user_data.pop("renaming", None)
            ctx.user_data["creating_folder_in"] = arg
            await cq.message.reply_text("➕ اكتب اسم المجلد الجديد:")
        elif action == "trash":
            meta = await _drive_call(USER_DRIVE.require_item, uid, arg, display)
            root = await _drive_call(USER_DRIVE.ensure_root, uid, display)
            if meta["id"] == root["id"]:
                raise FolderAccessError("المجلد الأساسي ما بينحذف.")
            warn = ("⚠️ حذف المجلد بينقل كل محتوياته كمان للسلة.\n"
                    if meta.get("mimeType") == FOLDER_MIME else "")
            await cq.edit_message_text(
                f"🗑 متأكد بدك تحذف «{meta['name']}»؟\n{warn}"
                "ممكن تستعيده من سلة Google Drive.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🗑 نعم، للسلة", callback_data=f"confirm:{arg}"),
                    InlineKeyboardButton("إلغاء", callback_data=f"cancel:{arg}"),
                ]]),
            )
        elif action == "confirm":
            deleted = await _drive_call(USER_DRIVE.trash, uid, arg, display)
            await cq.message.reply_text(f"✅ انتقل للسلة: {deleted.get('name', '')}")
            parent = (deleted.get("parents") or [None])[0]
            await show_browser(update, ctx, parent)
        elif action == "cancel":
            meta = await _drive_call(USER_DRIVE.require_item, uid, arg, display)
            if meta.get("mimeType") == FOLDER_MIME:
                await show_browser(update, ctx, meta["id"])
            else:
                await show_item(update, ctx, arg)
        elif action in ("skipone", "skipall"):
            queue = ctx.user_data.get("queue", [])
            if not queue:
                await cq.message.reply_text("ما في ملفات بانتظار اسم.")
                return
            while queue:
                await upload_item(ctx, cq.message.chat_id, ctx.user_data, queue[0], None)
                queue.pop(0)
                if action == "skipone":
                    await ask_next(ctx, cq.message.chat_id, ctx.user_data)
                    break
    except (FolderAccessError, ValueError) as exc:
        await cq.message.reply_text(f"❌ {exc}")
    except HttpError as exc:
        if getattr(exc.resp, "status", None) == 404:
            await cq.message.reply_text("❌ الملف أو المجلد ما عاد موجود.")
        else:
            raise

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
app.add_handler(CommandHandler("files", user_files))
app.add_handler(CommandHandler("skip", skip))
app.add_handler(CommandHandler("skipall", skipall))
app.add_handler(CallbackQueryHandler(on_browser_button, pattern=r"^(home$|nav:|page:|item:|choose:|add:|trash:|confirm:|cancel:|skipone$|skipall$)"))
app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(status|ren:)"))
app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(STATUS_BTN)}$"), status))
app.add_handler(MessageHandler(filters.ATTACHMENT, on_file))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
app.run_polling()
