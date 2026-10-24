import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dropbox.files import FileMetadata, FolderMetadata
from dropbox.sharing import FileLinkMetadata, FolderLinkMetadata

import dropbox_shared as shared


class SharedTests(unittest.TestCase):
    def test_single_file_inventory_never_downloads(self):
        dbx = mock.Mock()
        dbx.sharing_get_shared_link_metadata.return_value = FileLinkMetadata(name="a.wav", id="id:a", size=10)
        result = shared.shared_inventory(dbx, "https://www.dropbox.com/s/test/a.wav")
        self.assertEqual(result["files"][0]["path"], None)
        dbx.sharing_get_shared_link_file.assert_not_called()

    def test_nested_folder_pagination_uses_link_relative_paths(self):
        dbx = mock.Mock()
        dbx.sharing_get_shared_link_metadata.return_value = FolderLinkMetadata(name="Promos")
        dbx.files_list_folder.side_effect = [
            SimpleNamespace(entries=[FolderMetadata(name="Sub", id="id:sub")], has_more=True, cursor="cursor"),
            SimpleNamespace(entries=[FileMetadata(name="b.wav", id="id:b", size=20)], has_more=False)]
        dbx.files_list_folder_continue.return_value = SimpleNamespace(
            entries=[FileMetadata(name="a.wav", id="id:a", size=10)], has_more=False)
        result = shared.shared_inventory(dbx, "https://www.dropbox.com/sh/test")
        self.assertEqual({row["path"] for row in result["files"]}, {"/a.wav", "/Sub/b.wav"})
        self.assertEqual(dbx.files_list_folder.call_args_list[1].args[0], "/Sub")
        self.assertEqual(result["requests"], 4)

    def test_truncated_download_never_becomes_a_completed_file(self):
        dbx = mock.Mock()
        response = SimpleNamespace(iter_content=lambda _: iter([b"abc"]), close=lambda: None)
        dbx.sharing_get_shared_link_file.return_value = (SimpleNamespace(size=10), response)
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "a.wav"
            with self.assertRaisesRegex(RuntimeError, "size"):
                shared.download_shared_file(dbx, "https://dropbox.com/s/test/a.wav", target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name("a.wav.partial").exists())

    def test_hash_mismatch_is_rejected_even_when_the_size_matches(self):
        dbx = mock.Mock()
        response = SimpleNamespace(iter_content=lambda _: iter([b"abc"]), close=lambda: None)
        dbx.sharing_get_shared_link_file.return_value = (SimpleNamespace(size=3), response)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, "hash"):
                shared.download_shared_file(dbx, "https://dropbox.com/s/test/a.wav", Path(root)/"a.wav", expected_hash="bad")


if __name__ == "__main__":
    unittest.main()
