"""Tests for permanent file-link retrieval and folder-picker UX; no credentials."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "bot.py": {"drive_file_url", "show_item", "_show_file_results", "_menu_keyboard", "_upload_folder_snapshot"},
}


def load_functions(file, names, namespace):
    source = ast.parse((ROOT / file).read_text(encoding="utf-8"))
    selected = [
        node for node in source.body if
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert {n.name for n in selected} == names
    exec(compile(ast.Module(body=selected, type_ignores=[]), file, "exec"), namespace)


class Button:
    def __init__(self, text, **kwargs):
        self.text = text
        self.url = kwargs.get("url")
        self.callback_data = kwargs.get("callback_data")


class Markup:
    def __init__(self, rows, **kwargs):
        self.rows = rows


class FileExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = {
            "FOLDER_MIME": "application/vnd.google-apps.folder",
            "InlineKeyboardButton": Button,
            "InlineKeyboardMarkup": Markup,
            "ReplyKeyboardMarkup": Markup,
            "FILES_BTN": "📁 ملفاتي",
            "RECENT_BTN": "🕘 آخر ملفاتي",
            "SEARCH_BTN": "🔎 بحث عن ملف",
            "NEW_FOLDER_BTN": "➕ مجلد جديد",
            "UPLOAD_BTN": "📤 رفع ملف",
            "STATUS_BTN": "📊 الحالة",
            "HELP_BTN": "❓ طريقة الاستخدام",
        }
        load_functions("bot.py", MODULES["bot.py"], self.ns)

    async def test_telegram_keyboard_has_search_and_recent_without_admin(self):
        markup = self.ns["_menu_keyboard"](12345)
        words = " ".join(str(item) for row in markup.rows for item in row)
        self.assertIn("بحث عن ملف", words)
        self.assertIn("آخر ملفاتي", words)
        self.assertNotIn("أدمن", words)

    async def test_file_details_have_permanent_link_and_open_button(self):
        file_id = "file_id_ABC123"
        meta = {
            "id": file_id, "name": "Lecture 1.pdf", "size": "1048576",
            "mimeType": "application/pdf", "parents": ["parent_folder"],
        }
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(require_item=object())
        self.ns["_drive_call"] = AsyncMock(return_value=meta)
        self.ns["BadRequest"] = Exception
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            callback_query=None, effective_user=SimpleNamespace(id=123, full_name="Student"),
            effective_message=msg,
        )
        await self.ns["show_item"](update, None, file_id)
        args, kwargs = msg.reply_text.await_args
        url = "https://drive.google.com/file/d/file_id_ABC123/view"
        self.assertIn(url, args[0])
        self.assertEqual(kwargs["reply_markup"].rows[0][0].url, url)

    async def test_search_results_have_one_tap_drive_link(self):
        data = [
            {"id": "f1", "name": "محاضرة 4.pdf"},
            {"id": "f2", "name": "محاضرة 5.pdf"},
        ]
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(search_files=object())
        self.ns["_drive_call"] = AsyncMock(return_value=data)
        self.ns["_item_button"] = lambda name, action, id: Button(name, callback_data=f"{action}:{id}")
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123, full_name="Student"), effective_message=msg)
        await self.ns["_show_file_results"](update, None, "محاضرة")
        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.rows[0][1].url, "https://drive.google.com/file/d/f1/view")
        self.assertEqual(markup.rows[1][1].url, "https://drive.google.com/file/d/f2/view")

    async def test_folder_picker_creates_defaults_and_shows_folders_only(self):
        folder = {"id": "root", "name": "root"}
        items = [
            {"id": "a", "name": "ملفات عامة", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "b", "name": "محاضرات", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "c", "name": "فويسات", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "d", "name": "file.pdf", "mimeType": "application/pdf"},
        ]
        user_drive = SimpleNamespace(
            ensure_defaults=MagicMock(), ensure_root=MagicMock(return_value=folder),
            require_folder=MagicMock(return_value=folder),
            list_children=MagicMock(return_value=(items, False)),
        )
        self.ns["USER_DRIVE"] = user_drive
        root, selected, folders, more = self.ns["_upload_folder_snapshot"](123, "Student", "root")
        user_drive.ensure_defaults.assert_called_once_with(123, "Student")
        self.assertEqual([x["name"] for x in folders], ["فويسات", "محاضرات", "ملفات عامة"])
        self.assertFalse(more)


if __name__ == "__main__":
    unittest.main()
