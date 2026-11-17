"""Verify folder breadcrumb and ownership enforcement without Drive credentials."""
import ast
from pathlib import Path
import unittest


DRIVE_SOURCE = Path(__file__).resolve().parents[1] / "user_drive.py"
parsed = ast.parse(DRIVE_SOURCE.read_text(encoding="utf-8"))
cls = next(node for node in parsed.body if isinstance(node, ast.ClassDef) and node.name == "UserDrive")
class FolderAccessError(PermissionError):
    pass
env = {
    "FolderAccessError": FolderAccessError,
    "FOLDER_MIME": "application/vnd.google-apps.folder",
    "OWNER_KEY": "tg_owner",
    "ROLE_KEY": "tg_role",
}
exec(compile(ast.Module(body=[cls], type_ignores=[]), str(DRIVE_SOURCE), "exec"), env)
UserDrive = env["UserDrive"]


def folder(identifier, owner, name, parent=None):
    return {
        "id": identifier, "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "appProperties": {"tg_owner": str(owner)},
        "parents": [parent] if parent else [],
    }


class BreadcrumbAccessTests(unittest.TestCase):
    def setUp(self):
        self.root = folder("root", 123, "Student (123)", "app_root")
        self.lecture = folder("lectures", 123, "محاضرات", "root")
        self.logic = folder("logic", 123, "نظم منطقية", "lectures")
        self.items = {
            "root": self.root, "lectures": self.lecture, "logic": self.logic,
        }
        self.user_drive = UserDrive(None, lambda key: None, lambda key, value: None, lambda: "app_root")
        self.user_drive.ensure_root = lambda uid, name: self.root
        self.user_drive._get = lambda file_id: self.items[file_id]

    def test_nested_breadcrumb_and_root_validation(self):
        root, leaf, names = self.user_drive.folder_path(123, "logic", "Student")
        self.assertEqual(root["id"], "root")
        self.assertEqual(leaf["id"], "logic")
        self.assertEqual(names, ["ملفاتي", "محاضرات", "نظم منطقية"])
        self.assertEqual(self.user_drive.require_folder(123, "logic", "Student")["id"], "logic")

    def test_cannot_browse_a_different_users_folder(self):
        self.items["logic"] = folder("logic", 456, "Other", "lectures")
        with self.assertRaises(FolderAccessError):
            self.user_drive.folder_path(123, "logic", "Student")

    def test_cannot_browse_a_cycle_outside_root(self):
        self.items["lectures"] = folder("lectures", 123, "محاضرات", "logic")
        with self.assertRaises(FolderAccessError):
            self.user_drive.folder_path(123, "logic", "Student")


if __name__ == "__main__":
    unittest.main()
