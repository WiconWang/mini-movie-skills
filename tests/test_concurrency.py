"""并行护栏测试：文件锁、任务级对齐产物与 SQLite 并发设置。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mmm import db, stage_asr, stage_index
from mmm.locks import LockBusy, exclusive_lock, exclusive_locks


class PipelineLockTests(unittest.TestCase):
    def test_exclusive_lock_blocks_same_key(self):
        with exclusive_lock("video:v1"):
            with self.assertRaises(LockBusy):
                with exclusive_lock("video:v1"):
                    pass

    def test_lock_is_released_and_multi_lock_is_atomic_enough_for_conflict(self):
        with exclusive_lock("video:v1"):
            pass
        with exclusive_lock("video:v1"):
            pass

        with exclusive_lock("video:v1"):
            with self.assertRaises(LockBusy):
                with exclusive_locks("video:v2", "video:v1"):
                    pass


class TaskScopedArtifactTests(unittest.TestCase):
    def test_align_task_does_not_write_shared_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "tasks" / "task-a"
            task_dir.mkdir(parents=True)
            script_path = task_dir / "script.jsonl"
            script_path.write_text('{"text":"第一句"}\n', encoding="utf-8")
            (task_dir / "task.json").write_text(json.dumps({
                "script_path": str(script_path.relative_to(root)),
            }), encoding="utf-8")
            videos = [{
                "video_id": "v1",
                "source_path": "materials/v1",
                "script_path": str(script_path.relative_to(root)),
            }]
            words = [{"text": "第一句", "start": 0.0, "end": 1.0}]

            with mock.patch("mmm.catalog.task_videos", return_value=videos), \
                    mock.patch("mmm.db.PROJECT_ROOT", root), \
                    mock.patch.object(stage_asr, "ensure_asr", return_value=words), \
                    mock.patch.object(stage_asr, "_video_duration", return_value=10.0):
                report = stage_asr.align_task("task-a")

            self.assertFalse((root / "workspace" / "v1" / "lines.json").exists())
            task_lines = task_dir / "workspace" / "v1" / "lines.json"
            self.assertTrue(task_lines.exists())
            self.assertEqual(report["total"], 1)

    def test_build_global_prefers_task_scoped_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = root / "workspace" / "v1"
            shared.mkdir(parents=True)
            shared.joinpath("timeline.json").write_text(json.dumps({
                "shots": [{"id": 1, "start": 0.0, "end": 10.0, "class": "A"}],
                "fades": [],
                "lines": [{"id": 1, "text": "shared"}],
            }), encoding="utf-8")
            scoped = root / "tasks" / "task-a" / "workspace" / "v1"
            scoped.mkdir(parents=True)
            scoped.joinpath("timeline.json").write_text(json.dumps({
                "shots": [{"id": 1, "start": 0.0, "end": 10.0, "class": "A"}],
                "fades": [],
                "lines": [{"id": 1, "text": "task-scoped"}],
            }), encoding="utf-8")
            videos = [{"video_id": "v1"}]

            with mock.patch("mmm.catalog.task_videos", return_value=videos), \
                    mock.patch("mmm.db.PROJECT_ROOT", root):
                stats = stage_index.build_global("task-a")

            self.assertEqual(stats["shots"], 1)
            output = json.loads(
                (root / "tasks" / "task-a" / "global_timeline.json").read_text()
            )
            self.assertEqual(output["lines"][0]["text"], "task-scoped")


class SQLiteConcurrencyTests(unittest.TestCase):
    def test_init_db_enables_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.sqlite"
            conn = db.init_db(path)
            try:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
