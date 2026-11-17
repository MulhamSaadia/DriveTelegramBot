"""User-scoped Google Drive storage for DriveTelegramBot.

All Drive items belong to the same configured Google account. Ownership here
means Telegram-bot access control, NOT separate Google Drive permissions.
"""

import re

from googleapiclient.errors import HttpError

FOLDER_MIME = "application/vnd.google-apps.folder"
OWNER_KEY = "tg_owner"
ROLE_KEY = "tg_role"
META_FIELDS = "id,name,mimeType,parents,appProperties,trashed,size,webViewLink"


class FolderAccessError(PermissionError):
    """The requested file is not in this Telegram user's managed tree."""


def safe_name(raw):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(raw or "")).strip()[:90]
    if not name or name in (".", ".."):
        raise ValueError("اكتب اسم صالح (بين 1 و90 حرف).")
    return name


def _is_missing(exc):
    return isinstance(exc, HttpError) and getattr(exc.resp, "status", 0) == 404


class UserDrive:
    def __init__(self, drive, get_setting, set_setting, global_folder):
        self.drive = drive
        self.get_setting = get_setting
        self.set_setting = set_setting
        self.global_folder = global_folder

    @staticmethod
    def _key(uid, what):
        return f"tg_user:{int(uid)}:{what}"

    def _get(self, item_id):
        return self.drive.files().get(fileId=item_id, fields=META_FIELDS).execute()

    @staticmethod
    def _owned(meta, uid):
        return (not meta.get("trashed") and
                (meta.get("appProperties") or {}).get(OWNER_KEY) == str(uid))

    def _find_roots(self, uid, parent_id):
        query = (f"'{parent_id}' in parents and trashed = false and "
                 f"mimeType = '{FOLDER_MIME}' and "
                 f"appProperties has {{ key='{OWNER_KEY}' and value='{uid}' }}")
        found = self.drive.files().list(
            q=query, fields=f"files({META_FIELDS})", pageSize=1000,
        ).execute().get("files", [])
        return [f for f in found if (f.get("appProperties") or {}).get(ROLE_KEY) == "root"]

    def ensure_root(self, uid, display_name):
        uid = str(int(uid))
        base_id = self.global_folder()
        saved = self.get_setting(self._key(uid, "root"))
        if saved:
            try:
                meta = self._get(saved)
                if (self._owned(meta, uid) and meta.get("mimeType") == FOLDER_MIME and
                    (meta.get("appProperties") or {}).get(ROLE_KEY) == "root" and
                        base_id in meta.get("parents", [])):
                    return meta
            except HttpError as exc:
                if not _is_missing(exc):
                    raise
        existing = self._find_roots(uid, base_id)
        if existing:
            meta = existing[0]
        else:
            meta = self.drive.files().create(
                body={
                    "name": f"👤 {safe_name(display_name)[:50]} ({uid})",
                    "mimeType": FOLDER_MIME,
                    "parents": [base_id],
                    "appProperties": {OWNER_KEY: uid, ROLE_KEY: "root"},
                }, fields=META_FIELDS,
            ).execute()
        self.set_setting(self._key(uid, "root"), meta["id"])
        return meta

    def require_folder(self, uid, folder_id, display_name):
        root = self.ensure_root(uid, display_name)
        if not folder_id:
            raise FolderAccessError("ما في مجلد محدد.")
        visited = set()
        current = folder_id
        while current and current not in visited and len(visited) < 30:
            visited.add(current)
            meta = self._get(current)
            if not self._owned(meta, uid) or meta.get("mimeType") != FOLDER_MIME:
                raise FolderAccessError("ما عندك صلاحية لهالمجلد.")
            if current == root["id"]:
                return self._get(folder_id)
            parents = meta.get("parents") or []
            current = parents[0] if parents else None
        raise FolderAccessError("المجلد مو ضمن ملفاتك.")

    def require_item(self, uid, item_id, display_name):
        meta = self._get(item_id)
        if not self._owned(meta, uid):
            raise FolderAccessError("ما عندك صلاحية لهالملف.")
        root = self.ensure_root(uid, display_name)
        if item_id == root["id"]:
            return meta
        parents = meta.get("parents") or []
        if not parents:
            raise FolderAccessError("الملف مو ضمن ملفاتك.")
        self.require_folder(uid, parents[0], display_name)
        return meta

    def current(self, uid, display_name):
        root = self.ensure_root(uid, display_name)
        selected = self.get_setting(self._key(uid, "current"))
        if selected:
            try:
                return self.require_folder(uid, selected, display_name)
            except FolderAccessError:
                pass
            except HttpError as exc:
                if not _is_missing(exc):
                    raise
        self.set_setting(self._key(uid, "current"), root["id"])
        return root

    def select(self, uid, folder_id, display_name):
        folder = self.require_folder(uid, folder_id, display_name)
        self.set_setting(self._key(uid, "current"), folder["id"])
        return folder

    def create_folder(self, uid, parent_id, raw_name, display_name):
        parent = self.require_folder(uid, parent_id, display_name)
        meta = self.drive.files().create(
            body={
                "name": safe_name(raw_name),
                "mimeType": FOLDER_MIME,
                "parents": [parent["id"]],
                "appProperties": {OWNER_KEY: str(int(uid)), ROLE_KEY: "folder"},
            }, fields=META_FIELDS,
        ).execute()
        return meta

    def list_children(self, uid, folder_id, display_name):
        self.require_folder(uid, folder_id, display_name)
        query = (f"'{folder_id}' in parents and trashed = false and "
                 f"appProperties has {{ key='{OWNER_KEY}' and value='{int(uid)}' }}")
        response = self.drive.files().list(
            q=query, fields=f"nextPageToken,files({META_FIELDS})",
            pageSize=1000, orderBy="name_natural",
        ).execute()
        files = [f for f in response.get("files", []) if self._owned(f, uid)]
        files.sort(key=lambda f: (f.get("mimeType") != FOLDER_MIME, f.get("name", "").casefold()))
        return files, bool(response.get("nextPageToken"))


    def ensure_defaults(self, uid, display_name):
        """Create three useful folders once without moving any existing files."""
        key = self._key(uid, "defaults_ready")
        if self.get_setting(key) == "v1":
            return
        root = self.ensure_root(uid, display_name)
        existing, _ = self.list_children(uid, root["id"], display_name)
        names = {f["name"].casefold() for f in existing if f.get("mimeType") == FOLDER_MIME}
        for name in ("محاضرات", "فويسات", "ملفات عامة"):
            if name.casefold() not in names:
                self.create_folder(uid, root["id"], name, display_name)
        self.set_setting(key, "v1")

    def _find_owned_files(self, uid, display_name, phrase=None, limit=12):
        """Return only files verified to be in the Telegram user's Drive tree."""
        uid = int(uid)
        root = self.ensure_root(uid, display_name)
        query = (
            f"trashed = false and mimeType != '{FOLDER_MIME}' and "
            f"appProperties has {{ key='{OWNER_KEY}' and value='{uid}' }}"
        )
        if phrase is not None:
            phrase = str(phrase).strip()[:80]
            if len(phrase) < 2:
                raise ValueError("اكتب حرفين على الأقل للبحث.")
            escaped = phrase.replace("\\", "\\\\").replace("'", "\\'")
            query += f" and name contains '{escaped}'"
        found = []
        next_page = None
        scanned = 0
        while len(found) < limit and scanned < 250:
            response = self.drive.files().list(
                q=query, fields=f"nextPageToken,files({META_FIELDS})",
                pageSize=100, orderBy="createdTime desc", pageToken=next_page,
            ).execute()
            for item in response.get("files", []):
                scanned += 1
                if not self._owned(item, uid):
                    continue
                # Do not trust labels alone; a file may have been moved by Drive.
                try:
                    self.require_item(uid, item["id"], display_name)
                except FolderAccessError:
                    continue
                except HttpError as exc:
                    if _is_missing(exc):
                        continue
                    raise
                found.append(item)
                if len(found) == limit:
                    break
            next_page = response.get("nextPageToken")
            if not next_page:
                break
        return found

    def recent_files(self, uid, display_name):
        return self._find_owned_files(uid, display_name, limit=10)

    def search_files(self, uid, display_name, phrase):
        return self._find_owned_files(uid, display_name, phrase=phrase, limit=12)

    def rename(self, uid, item_id, new_name, display_name):
        meta = self.require_item(uid, item_id, display_name)
        if (meta.get("appProperties") or {}).get(ROLE_KEY) == "root":
            raise FolderAccessError("ما فيك تغيّر اسم المجلد الأساسي من البوت.")
        return self.drive.files().update(
            fileId=item_id, body={"name": safe_name(new_name)}, fields=META_FIELDS,
        ).execute()

    def trash(self, uid, item_id, display_name):
        meta = self.require_item(uid, item_id, display_name)
        if (meta.get("appProperties") or {}).get(ROLE_KEY) == "root":
            raise FolderAccessError("ما فيك تحذف المجلد الأساسي.")
        # Soft-delete only: file/folder remains recoverable from Google Drive Trash.
        self.drive.files().update(
            fileId=item_id, body={"trashed": True}, fields="id,trashed",
        ).execute()
        return meta
