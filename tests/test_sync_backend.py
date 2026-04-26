from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sync_backend


THREADS_SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'vscode',
    model_provider TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    sandbox_policy TEXT NOT NULL DEFAULT '{}',
    approval_mode TEXT NOT NULL DEFAULT 'never',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT,
    agent_role TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    agent_path TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER
);
"""


class SyncBackendTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.codex_home = self.root / ".codex"
        self.codex_home.mkdir()
        (self.codex_home / "config.toml").write_text(
            'model_provider = "target-provider"\nmodel = "gpt-5.5"\n',
            encoding="utf-8",
        )
        self.db_path = self.codex_home / "state_5.sqlite"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(THREADS_SCHEMA)
        conn.commit()
        conn.close()
        self.paths = sync_backend.resolve_paths(str(self.codex_home))

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def insert_thread(
        self,
        *,
        thread_id: str,
        provider: str,
        title: str,
        cwd: str,
        updated_at_ms: int,
        git_origin_url: str = "",
        git_branch: str = "",
    ) -> Path:
        rollout_path = self.codex_home / f"{thread_id}.jsonl"
        session_meta = {
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "cwd": cwd,
                "model_provider": provider,
            },
        }
        rollout_path.write_text(
            json.dumps(session_meta, ensure_ascii=False) + "\n"
            + json.dumps({"type": "response_item", "payload": {"type": "message"}})
            + "\n",
            encoding="utf-8",
        )

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            INSERT INTO threads (
                id, rollout_path, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, git_branch, git_origin_url,
                first_user_message, model, updated_at_ms
            ) VALUES (?, ?, 'vscode', ?, ?, ?, '{}', 'never', ?, ?, ?, 'gpt-5.5', ?)
            """,
            (
                thread_id,
                str(rollout_path),
                provider,
                cwd,
                title,
                git_branch,
                git_origin_url,
                title,
                updated_at_ms,
            ),
        )
        conn.commit()
        conn.close()
        return rollout_path

    def rollout_provider(self, rollout_path: Path) -> str:
        first_line = rollout_path.read_text(encoding="utf-8").splitlines()[0]
        return json.loads(first_line)["payload"]["model_provider"]

    def test_projects_group_by_repo_then_cwd(self) -> None:
        self.insert_thread(
            thread_id="t1",
            provider="target-provider",
            title="Thread 1",
            cwd="/work/repo/app",
            updated_at_ms=3000,
            git_origin_url="https://github.com/example/repo.git",
            git_branch="main",
        )
        self.insert_thread(
            thread_id="t2",
            provider="legacy-provider",
            title="Thread 2",
            cwd="/work/repo/scripts",
            updated_at_ms=2000,
            git_origin_url="git@github.com:example/repo.git",
            git_branch="feature",
        )
        self.insert_thread(
            thread_id="t3",
            provider="legacy-provider",
            title="Thread 3",
            cwd="/work/other",
            updated_at_ms=1000,
        )

        payload = sync_backend.get_projects(self.paths)
        projects = {project["group_key"]: project for project in payload["projects"]}

        self.assertIn("repo:github.com/example/repo", projects)
        repo_group = projects["repo:github.com/example/repo"]
        self.assertEqual(repo_group["label"], "repo")
        self.assertEqual(repo_group["thread_count"], 2)
        self.assertEqual(repo_group["movable_threads"], 1)
        self.assertEqual(repo_group["cwd_variants"], 2)

        self.assertIn("cwd:/work/other", projects)
        cwd_group = projects["cwd:/work/other"]
        self.assertEqual(cwd_group["label"], "/work/other")
        self.assertEqual(cwd_group["thread_count"], 1)

    def test_sync_selected_threads_only_mutates_selected_rows(self) -> None:
        rollout_1 = self.insert_thread(
            thread_id="sel-1",
            provider="legacy-provider",
            title="Selected",
            cwd="/work/repo",
            updated_at_ms=3000,
            git_origin_url="https://github.com/example/repo.git",
            git_branch="main",
        )
        rollout_2 = self.insert_thread(
            thread_id="sel-2",
            provider="legacy-provider",
            title="Unselected",
            cwd="/work/repo",
            updated_at_ms=2000,
            git_origin_url="https://github.com/example/repo.git",
            git_branch="main",
        )
        self.insert_thread(
            thread_id="sel-3",
            provider="target-provider",
            title="Already Current",
            cwd="/work/repo",
            updated_at_ms=1000,
            git_origin_url="https://github.com/example/repo.git",
            git_branch="main",
        )

        payload = sync_backend.sync_selected_threads(self.paths, ["sel-1"])

        self.assertEqual(payload["selected_count"], 1)
        self.assertEqual(payload["updated_rows"], 1)
        self.assertEqual(payload["rollout_metadata"]["updated_files"], 1)
        self.assertTrue(payload["verified"])

        conn = sqlite3.connect(self.db_path)
        providers = dict(conn.execute("SELECT id, model_provider FROM threads"))
        conn.close()

        self.assertEqual(providers["sel-1"], "target-provider")
        self.assertEqual(providers["sel-2"], "legacy-provider")
        self.assertEqual(providers["sel-3"], "target-provider")
        self.assertEqual(self.rollout_provider(rollout_1), "target-provider")
        self.assertEqual(self.rollout_provider(rollout_2), "legacy-provider")
        self.assertEqual(payload["session_meta_backup"]["entries"], 1)


if __name__ == "__main__":
    unittest.main()
