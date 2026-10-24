import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dropbox.files import FileMetadata, FolderMetadata, RelocationBatchV2Launch, RelocationBatchV2JobStatus

import dropbox_batch as batch


def file(path="/a.wav", identity="id:a"):
    return FileMetadata(name=Path(path).name, id=identity, path_display=path,
                        client_modified=datetime(2026, 1, 1), server_modified=datetime(2026, 1, 1),
                        rev="0123456789", size=10, content_hash="a" * 64)


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps([{"from_path": "/a.wav", "to_path": "/month/a.wav"}]), encoding="utf-8")
        self.args = SimpleNamespace(command="copy-many", plan_file=str(self.plan), execute=False,
                                    state_file=str(self.root / "job.json"))
        self.dbx = mock.Mock()

    def test_preview_has_no_mutation(self):
        with mock.patch.object(batch, "metadata", side_effect=[file(), None]):
            self.assertEqual(batch.run(self.dbx, self.args)["status"], "preview")
        self.dbx.files_copy_batch_v2.assert_not_called()
        self.assertFalse(Path(self.args.state_file).exists())

    def test_overlap_and_case_collisions_are_rejected(self):
        for plan in ([{"from_path": "/a", "to_path": "/a/sub"}],
                     [{"from_path": "/a", "to_path": "/x"}, {"from_path": "/b", "to_path": "/X"}],
                     [{"from_path": "/a", "to_path": "/x"}, {"from_path": "/a/sub", "to_path": "/y"}]):
            with self.assertRaises(ValueError):
                batch.validate_plan(plan)

    def test_timeout_keeps_uncertain_state_and_reexecution_is_blocked(self):
        self.args.execute = True
        self.dbx.files_copy_batch_v2.side_effect = RuntimeError("timeout")
        with mock.patch.object(batch, "metadata", side_effect=[file(), None, file(), None]):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                batch.run(self.dbx, self.args)
            with self.assertRaises(FileExistsError):
                batch.run(self.dbx, self.args)
        self.assertEqual(json.loads(Path(self.args.state_file).read_text())["status"], "submitting")
        self.assertEqual(self.dbx.files_copy_batch_v2.call_count, 1)

    def test_async_job_can_be_resumed_and_verified_without_resubmission(self):
        self.args.execute = True
        self.dbx.files_copy_batch_v2.return_value = RelocationBatchV2Launch.async_job_id("job-1")
        with mock.patch.object(batch, "metadata", side_effect=[file(), None]):
            self.assertEqual(batch.run(self.dbx, self.args)["status"], "in_progress")
        outcome = SimpleNamespace(is_failure=lambda: False)
        completed = SimpleNamespace(entries=[outcome])
        self.dbx.files_copy_batch_check_v2.return_value = SimpleNamespace(is_complete=lambda: True, get_complete=lambda: completed)
        with mock.patch.object(batch, "metadata", return_value=file("/month/a.wav", "id:copy")):
            self.assertEqual(batch.status(self.dbx, self.args)["status"], "complete")
        self.assertEqual(self.dbx.files_copy_batch_v2.call_count, 1)

    def test_folder_moves_keep_identity_and_verify_old_path_is_gone(self):
        state = {"operation": "move", "entries": [{"from_path": "/folder", "to_path": "/month/folder",
                 "source": {"id": "id:folder", "path": "/folder", "kind": "folder"}}]}
        remote = FolderMetadata(name="folder", id="id:folder", path_display="/month/folder")
        result = SimpleNamespace(entries=[SimpleNamespace(is_failure=lambda: False)])
        with mock.patch.object(batch, "metadata", side_effect=[remote, None]):
            self.assertEqual(batch.finish(self.dbx, state, result, self.args.state_file)["status"], "complete")


if __name__ == "__main__":
    unittest.main()
