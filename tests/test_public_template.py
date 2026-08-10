import ast
import io
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml
from PIL import Image
from pypdf import PdfWriter


ROOT = Path(__file__).resolve().parents[1]


def publishable_files() -> list[Path]:
    """Return Git-tracked files so a user's ignored local config cannot fail CI."""
    try:
        raw = subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files", "-z"], stderr=subprocess.DEVNULL
        )
        return [ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item]
    except (FileNotFoundError, subprocess.CalledProcessError):
        excluded = {".git", ".runtime", ".venv", "bin", "inbox", "markdown"}
        return [
            path
            for path in ROOT.rglob("*")
            if path.is_file() and not any(part in excluded for part in path.parts)
        ]


class PublicTemplateTests(unittest.TestCase):
    def test_ingest_parses(self) -> None:
        ast.parse((ROOT / "ingest.py").read_text("utf-8"))

    def test_cli_help_starts_without_runtime_state(self) -> None:
        """The documented entry point must load before any private setup exists."""
        state_db = ROOT / "state.db"
        self.assertFalse(state_db.exists())
        completed = subprocess.run(
            [sys.executable, str(ROOT / "ingest.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--search", completed.stdout)
        self.assertFalse(state_db.exists())

    def test_cli_rejects_empty_or_conflicting_primary_operations(self) -> None:
        """Malformed maintenance commands must never fall through to ingestion."""
        state_db = ROOT / "state.db"
        for arguments in (
            ["--search", ""],
            ["--sync-manual-deletions", ""],
            ["--status", "--prequeue-only"],
            ["--manual-deletion-dry-run"],
            ["--delete-old-indexes"],
            ["--migration-group", "group-1"],
        ):
            completed = subprocess.run(
                [sys.executable, str(ROOT / "ingest.py"), *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, (arguments, completed.stderr))
            self.assertFalse(state_db.exists())

    def test_safe_destructive_defaults(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertFalse(config["classification"]["delete_videos"])
        self.assertFalse(config["classification"]["delete_archives_after_extract"])
        self.assertFalse(
            config["document_classification"]["delete_other_source_after_markdown"]
        )
        self.assertFalse(config["cleanup"]["permanently_delete_source_after_search"])
        self.assertFalse(config["manual_deletions"]["auto_sync"])

    def test_manual_deletion_requires_explicit_confirmation(self) -> None:
        tree = ast.parse((ROOT / "ingest.py").read_text("utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        sync = functions["sync_manual_deletions"]
        confirmation_calls = [
            node
            for node in ast.walk(sync)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require_manual_deletion_sync_confirmation"
        ]
        self.assertEqual(len(confirmation_calls), 1)
        self.assertIn("if not dry_run", ast.unparse(sync))

    def test_delete_source_with_audit_requires_confirmation_itself(self) -> None:
        """Every source-deletion call path must enforce the safety interlock."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.pdf"
            canonical = work / "retained.md"
            source.write_bytes(b"source bytes")
            canonical.write_text("# Retained\n\nBody\n", encoding="utf-8")
            digest = ingest.sha256(source)
            database = sqlite3.connect(":memory:")
            database.execute("""CREATE TABLE deletion_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL,
                markdown_path TEXT NOT NULL, markdown_sha256 TEXT NOT NULL,
                requested_at INTEGER NOT NULL, completed_at INTEGER,
                success INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '')""")
            with mock.patch.dict(
                os.environ,
                {"QUESTION_BANK_ALLOW_PERMANENT_DELETE": ""},
            ):
                with self.assertRaisesRegex(RuntimeError, "尚未显式确认"):
                    ingest.delete_source_with_audit(
                        database,
                        source,
                        digest,
                        canonical,
                        "test deletion",
                        "group-1",
                    )
            self.assertTrue(source.is_file())
            self.assertEqual(
                database.execute("SELECT COUNT(*) FROM deletion_audit").fetchone()[0],
                0,
            )
            database.close()

    def test_pending_deletion_audit_is_reconciled_without_redeleting(self) -> None:
        """Startup recovery records the observed outcome of an interrupted delete."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            retained = root / "retained.md"
            retained.write_text("# Retained\n\nBody\n", encoding="utf-8")
            missing_source = root / "already-deleted.pdf"
            existing_source = root / "not-deleted.pdf"
            existing_source.write_bytes(b"still here")
            with mock.patch.object(ingest, "ROOT", root):
                database = ingest.db_open()
            retained_digest = ingest.stable_sha256(retained)
            now = int(time.time())
            database.executemany(
                """INSERT INTO deletion_audit(
                    source_path,sha256,group_id,reason,markdown_path,
                    markdown_sha256,requested_at,success,error
                ) VALUES(?,?,?,?,?,?,?,0,'')""",
                [
                    (
                        str(missing_source),
                        "a" * 64,
                        "group-1",
                        "test",
                        str(retained),
                        retained_digest,
                        now,
                    ),
                    (
                        str(existing_source),
                        ingest.sha256(existing_source),
                        "group-2",
                        "test",
                        str(retained),
                        retained_digest,
                        now,
                    ),
                ],
            )
            database.commit()
            try:
                result = ingest.reconcile_pending_deletion_audits(database)
                rows = database.execute(
                    "SELECT source_path,success,completed_at,error "
                    "FROM deletion_audit ORDER BY id"
                ).fetchall()
                self.assertEqual(result["confirmed_missing"], 1)
                self.assertEqual(result["not_deleted"], 1)
                self.assertEqual(rows[0]["success"], 1)
                self.assertIsNotNone(rows[0]["completed_at"])
                self.assertIn("已不存在", rows[0]["error"])
                self.assertEqual(rows[1]["success"], 0)
                self.assertIsNotNone(rows[1]["completed_at"])
                self.assertTrue(existing_source.is_file())
            finally:
                database.close()

    def test_cleanup_marks_completed_when_source_retention_is_configured(self) -> None:
        """Retention mode must not enqueue an endless deletion retry loop."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.pdf"
            canonical = work / "retained.md"
            source.write_bytes(b"source bytes")
            canonical.write_text("# Retained\n\nBody\n", encoding="utf-8")
            digest = ingest.sha256(source)
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.execute("""CREATE TABLE files(
                sha256 TEXT PRIMARY KEY, source_path TEXT, batch_id TEXT,
                state TEXT, markdown_path TEXT, error TEXT, updated_at INTEGER,
                weknora_doc_id TEXT, metrics_json TEXT NOT NULL DEFAULT '{}')""")
            database.execute("""CREATE TABLE group_files(
                group_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                source_path TEXT NOT NULL)""")
            database.execute(
                "INSERT INTO files(sha256,source_path,state,metrics_json) "
                "VALUES(?,?,?,?)",
                (digest, str(source), "verified", "{}"),
            )
            database.execute(
                "INSERT INTO group_files(group_id,sha256,source_path) VALUES(?,?,?)",
                ("group-1", digest, str(source)),
            )
            errors = ingest.cleanup_verified_sources(
                [source],
                canonical,
                "parent-doc",
                "group-1",
                {"cleanup": {"permanently_delete_source_after_search": False}},
                database,
            )
            row = database.execute(
                "SELECT state,error FROM files WHERE sha256=?", (digest,)
            ).fetchone()
            self.assertEqual(errors, [])
            self.assertTrue(source.is_file())
            self.assertEqual(row["state"], "completed")
            self.assertEqual(row["error"], "")
            database.close()

    def test_group_manifest_digest_wins_over_replaced_path_content(self) -> None:
        """Cleanup must bind to the bytes parsed for the group, not new bytes."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pdf"
            source.write_bytes(b"new replacement bytes")
            old_digest = "a" * 64
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.execute("""CREATE TABLE group_files(
                group_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                source_path TEXT NOT NULL)""")
            database.execute(
                "INSERT INTO group_files(group_id,sha256,source_path) VALUES(?,?,?)",
                ("group-1", old_digest, str(source)),
            )
            self.assertEqual(
                ingest.recorded_source_digest(database, source, "group-1"),
                old_digest,
            )
            database.close()

    def test_replaced_terminal_path_becomes_a_processing_candidate(self) -> None:
        """A completed path whose bytes changed must not be skipped forever."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            inbox = Path(temporary) / "inbox"
            inbox.mkdir()
            source = inbox / "source.txt"
            source.write_text("new content", encoding="utf-8")
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.execute("""CREATE TABLE files(
                sha256 TEXT PRIMARY KEY, source_path TEXT, batch_id TEXT,
                state TEXT, markdown_path TEXT, error TEXT, updated_at INTEGER,
                weknora_doc_id TEXT, metrics_json TEXT NOT NULL DEFAULT '{}')""")
            database.execute(
                """INSERT INTO files(
                    sha256,source_path,state,error,updated_at,metrics_json
                ) VALUES(?,?,?,?,?,?)""",
                ("b" * 64, str(source), "completed", "", 1, "{}"),
            )
            candidates = ingest.processing_candidates(
                {"folders": {"inbox": inbox}}, database
            )
            self.assertEqual(candidates, [source])
            database.close()

    def test_classification_migration_preserves_other_source_by_default(self) -> None:
        """Classification migration must honor the default no-delete policy."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source.pdf"
            old_markdown = work / "old.md"
            destination = work / "classified"
            scratch = work / "scratch"
            source.write_bytes(b"source bytes")
            old_markdown.write_text(
                "# Question 1\n\n**题目**\n\nBody\n",
                encoding="utf-8",
            )
            digest = ingest.sha256(source)
            with mock.patch.object(ingest, "ROOT", work):
                database = ingest.db_open()
            database.execute(
                """INSERT INTO files(
                    sha256,source_path,state,markdown_path,error,updated_at,
                    metrics_json
                ) VALUES(?,?,?,?,?,?,?)""",
                (digest, str(source), "completed", str(old_markdown), "", 1, "{}"),
            )
            database.execute(
                """INSERT INTO groups(
                    group_id,group_name,state,markdown_path,error,updated_at,
                    classification_json
                ) VALUES(?,?,?,?,?,?,?)""",
                ("group-1", "Other", "completed", str(old_markdown), "", 1, "{}"),
            )
            database.execute(
                "INSERT INTO group_files(group_id,sha256,source_path) VALUES(?,?,?)",
                ("group-1", digest, str(source)),
            )
            database.commit()
            classification = ingest.DocumentClassification(
                "其他资料",
                "未知机构",
                "综合",
                ("综合",),
                "rule",
                0.99,
                ("test evidence",),
                1,
            )
            config = {
                "folders": {"markdown": work, "work": scratch},
                "document_classification": {
                    "taxonomy": {"version": 1},
                    "version": 1,
                    "delete_other_source_after_markdown": False,
                },
                "pairing": {"child_chars": 384},
                "cleanup": {"delete_temporary_files": True},
                "weknora": {
                    "parent_knowledge_base": "parent-kb",
                    "child_knowledge_base": "child-kb",
                },
            }
            try:
                with (
                    mock.patch.object(
                        ingest,
                        "classify_group",
                        return_value=classification,
                    ),
                    mock.patch.object(
                        ingest,
                        "classification_directory",
                        return_value=destination,
                    ),
                    mock.patch.dict(
                        os.environ,
                        {"QUESTION_BANK_ALLOW_PERMANENT_DELETE": ""},
                    ),
                ):
                    ingest.migrate_classified_markdown(
                        config,
                        database,
                        dry_run=False,
                        delete_old_indexes=False,
                    )
                file_row = database.execute(
                    "SELECT state FROM files WHERE sha256=?", (digest,)
                ).fetchone()
                self.assertTrue(source.is_file())
                self.assertEqual(file_row["state"], "excluded_completed")
                self.assertEqual(
                    database.execute(
                        "SELECT COUNT(*) FROM deletion_audit"
                    ).fetchone()[0],
                    0,
                )
            finally:
                database.close()

    def test_classification_move_journal_recovers_after_interrupted_replace(self) -> None:
        """A crash after os.replace must not orphan the group's Markdown path."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown_root = root / "markdown"
            scratch = root / "work"
            old_path = markdown_root / "old.md"
            target = markdown_root / "试卷" / "target.md"
            markdown_root.mkdir()
            target.parent.mkdir(parents=True)
            old_path.write_text(
                "# Question 1\n\n**题目**\n\nBody\n",
                encoding="utf-8",
            )
            classification = ingest.DocumentClassification(
                "试卷",
                "未知机构",
                "力学",
                ("力学",),
                "rule",
                0.95,
                ("test evidence",),
                1,
            )
            record = ingest.classification_to_dict(classification)
            record["migration_phase"] = "classified_local"
            with mock.patch.object(ingest, "ROOT", root):
                database = ingest.db_open()
            database.execute(
                """INSERT INTO files(
                    sha256,source_path,state,markdown_path,error,updated_at,
                    metrics_json
                ) VALUES(?,?,?,?,?,?,?)""",
                ("a" * 64, str(root / "source.pdf"), "completed", str(old_path), "", 1, "{}"),
            )
            database.execute(
                """INSERT INTO groups(
                    group_id,group_name,state,markdown_path,error,updated_at,
                    classification_json
                ) VALUES(?,?,?,?,?,?,?)""",
                ("group-1", "Test", "completed", str(old_path), "", 1, "{}"),
            )
            database.commit()
            config = {
                "folders": {"markdown": markdown_root, "work": scratch},
            }
            target_text = ingest.classification_frontmatter(
                "group-1", "Test", classification, "parent"
            ) + ingest.classified_parent_body(old_path, classification)
            row = database.execute(
                "SELECT * FROM groups WHERE group_id='group-1'"
            ).fetchone()
            journal = ingest.write_classification_migration_journal(
                config,
                row,
                old_path,
                target,
                classification,
                record,
                target_text,
            )
            os.replace(old_path, target)
            try:
                recovered = ingest.recover_classification_migration_journals(
                    config, database
                )
                group = database.execute(
                    "SELECT state,markdown_path FROM groups WHERE group_id='group-1'"
                ).fetchone()
                file_row = database.execute(
                    "SELECT markdown_path FROM files WHERE sha256=?", ("a" * 64,)
                ).fetchone()
                self.assertEqual(recovered["recovered"], 1)
                self.assertEqual(recovered["errors"], 0)
                self.assertEqual(group["state"], "classification_migrating")
                self.assertEqual(Path(group["markdown_path"]), target)
                self.assertEqual(Path(file_row["markdown_path"]), target)
                self.assertEqual(target.read_text("utf-8"), target_text)
                self.assertFalse(journal.exists())
            finally:
                database.close()

    def test_classification_move_journal_rejects_unknown_target_bytes(self) -> None:
        """Recovery must retain evidence instead of adopting changed bytes."""
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown_root = root / "markdown"
            old_path = markdown_root / "old.md"
            target = markdown_root / "试卷" / "target.md"
            target.parent.mkdir(parents=True)
            old_path.write_text("# Original\n\nBody\n", encoding="utf-8")
            classification = ingest.DocumentClassification(
                "试卷", "未知机构", "综合", ("综合",), "rule", 0.9, (), 1
            )
            record = ingest.classification_to_dict(classification)
            with mock.patch.object(ingest, "ROOT", root):
                database = ingest.db_open()
            database.execute(
                """INSERT INTO groups(
                    group_id,group_name,state,markdown_path,error,updated_at
                ) VALUES(?,?,?,?,?,?)""",
                ("group-2", "Test", "completed", str(old_path), "", 1),
            )
            database.commit()
            config = {
                "folders": {"markdown": markdown_root, "work": root / "work"},
            }
            target_text = ingest.classification_frontmatter(
                "group-2", "Test", classification, "parent"
            ) + ingest.classified_parent_body(old_path, classification)
            row = database.execute(
                "SELECT * FROM groups WHERE group_id='group-2'"
            ).fetchone()
            journal = ingest.write_classification_migration_journal(
                config,
                row,
                old_path,
                target,
                classification,
                record,
                target_text,
            )
            os.replace(old_path, target)
            target.write_text("# Replacement\n", encoding="utf-8")
            try:
                recovered = ingest.recover_classification_migration_journals(
                    config, database
                )
                group_path = database.execute(
                    "SELECT markdown_path FROM groups WHERE group_id='group-2'"
                ).fetchone()[0]
                self.assertEqual(recovered["errors"], 1)
                self.assertEqual(Path(group_path), old_path)
                self.assertEqual(target.read_text("utf-8"), "# Replacement\n")
                self.assertTrue(journal.is_file())
            finally:
                database.close()

    def test_archive_status_matches_retention_policy(self) -> None:
        source = (ROOT / "ingest.py").read_text("utf-8")
        self.assertIn("压缩包已展开并永久删除原包", source)
        self.assertIn("压缩包已展开，原包已移至archives保留", source)

    def test_ambiguous_typescript_suffix_is_not_treated_as_video(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertNotIn(".ts", config["classification"]["video_extensions"])

    def test_archive_directory_paths_are_validated_before_extraction(self) -> None:
        import ingest

        config = {
            "archive_executable": Path("7z"),
            "max_archive_files": 10,
            "max_expanded_gb": 1,
        }
        with self.assertRaisesRegex(RuntimeError, "异常路径"):
            ingest.parse_archive_inventory_lines(
                ["Path = ../outside", "Folder = +", ""],
                config,
            )
        self.assertEqual(
            ingest.parse_archive_inventory_lines(
                [
                    "Path = C:\\input\\sample.zip",
                    "Type = zip",
                    "Physical Size = 123",
                    "",
                ],
                config,
            ),
            (0, 0),
        )

    def test_archive_listing_is_streamed_and_nested_budget_is_cumulative(self) -> None:
        import ingest

        config = {
            "archive_executable": Path("7z"),
            "max_archive_files": 10,
            "max_expanded_gb": 1,
        }

        class FakeProcess:
            def __init__(self, payload: bytes):
                self.stdout = io.BytesIO(payload)
                self.returncode = None

            def poll(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                self.returncode = 0 if self.returncode is None else self.returncode
                return self.returncode

        fake = FakeProcess(b"X" * 65)
        with (
            mock.patch.object(ingest, "MAX_ARCHIVE_LIST_BYTES", 64),
            mock.patch.object(ingest.subprocess, "Popen", return_value=fake),
        ):
            with self.assertRaisesRegex(RuntimeError, "清单输出超过"):
                ingest.archive_inventory(Path("sample.zip"), config)

        budget = {"entries": 0, "expanded": 0}
        ingest.charge_archive_chain_budget(budget, 6, 100, config)
        with self.assertRaisesRegex(RuntimeError, "累计条目数"):
            ingest.charge_archive_chain_budget(budget, 5, 100, config)

    def test_plain_document_does_not_require_7zip(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox = root / "inbox"
            inbox.mkdir()
            (inbox / "sample.pdf").write_bytes(b"not parsed in classification")
            config = {
                "folders": {
                    "inbox": inbox,
                    "failed": root / "failed",
                    "work": root / "work",
                },
                "classification": {
                    "archive_executable": root / "missing-7z.exe",
                    "archive_extensions": [".zip"],
                    "video_extensions": [".mp4"],
                    "transient_extensions": [".part"],
                    "archive_store": root / "archives",
                    "unsupported_to_failed": True,
                    "delete_videos": False,
                },
            }
            for folder in ("failed", "work", "archives"):
                (root / folder).mkdir()
            stats = ingest.classify_inbox(config)
            self.assertEqual(stats.supported_files, 1)

    def test_retrieval_only_defaults(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertNotIn("features", config["weknora"])
        self.assertNotIn("rerank", config["weknora"]["models"])
        self.assertNotIn("embedding_parallel", config["weknora"])
        self.assertEqual(
            config["mcp_public"]["external_url"],
            "https://mcp.example.com",
        )

    def test_no_private_identifiers_in_tracked_files(self) -> None:
        patterns = [
            re.compile(r"[A-Z]:\\Users\\", re.I),
            re.compile(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
                re.I,
            ),
        ]
        for path in publishable_files():
            if path.suffix not in {
                ".py",
                ".ps1",
                ".md",
                ".yaml",
                ".yml",
                ".toml",
                ".txt",
            }:
                continue
            text = path.read_text("utf-8", errors="ignore")
            for pattern in patterns:
                self.assertIsNone(pattern.search(text), f"{pattern.pattern}: {path}")

    def test_runtime_data_is_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text("utf-8")
        for entry in (
            ".env",
            "config.local.yaml",
            "state.db*",
            "inbox/",
            "markdown/",
            "outputs/",
            "*.pem",
            "*.key",
            "*.sqlite",
        ):
            self.assertIn(entry, ignored)

    def test_loopback_ports_and_pinned_weknora_source(self) -> None:
        bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text("utf-8")
        self.assertIn('"APP_PORT" "127.0.0.1:8080"', bootstrap)
        self.assertIn('"FRONTEND_PORT" "127.0.0.1:8088"', bootstrap)
        self.assertRegex(bootstrap, r'WeKnoraExpectedCommit = "[0-9a-f]{40}"')
        start = (ROOT / "scripts" / "start.ps1").read_text("utf-8")
        self.assertIn("--cd $WeKnora -- docker compose up -d", start)
        self.assertNotIn("wslpath", start)

    def test_mcp_uses_separate_profile_and_documents_write_boundary(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertEqual(config["mcp_public"]["weknora_profile"], "mcp-readonly")
        start = (ROOT / "mcp-public" / "start.ps1").read_text("utf-8")
        self.assertIn('"mcp-readonly"', start)
        guide = (ROOT / "docs" / "CHATGPT_MCP.md").read_text("utf-8")
        self.assertIn("chat", guide)
        self.assertIn("session_ask", guide)
        self.assertIn("禁用", guide)
        local_test = (ROOT / "mcp-public" / "test-local.ps1").read_text("utf-8")
        self.assertIn("-Method Post", local_test)
        self.assertNotIn("401, 403, 405", local_test)
        self.assertIn("[Uri]::TryCreate", start)
        self.assertIn('$ParsedExternalUrl.Scheme -ne "https"', start)
        root_start = (ROOT / "scripts" / "start.ps1").read_text("utf-8")
        self.assertIn("mcp_public.external_url", root_start)
        self.assertIn("-Profile $McpProfile", root_start)
        self.assertIn('"doctor", "--no-cache"', start)
        self.assertIn('"search", "chunks"', start)
        self.assertIn("real hybrid search", start)
        self.assertIn("ExpectedKnowledgeBaseIds", start)
        self.assertIn("cannot see every configured question-bank layer", start)
        proxy_start = start.index("Start-Process -FilePath $Proxy")
        self.assertLess(start.index('"doctor", "--no-cache"'), proxy_start)
        self.assertLess(start.index('"search", "chunks"'), proxy_start)
        tunnel_start = (ROOT / "mcp-public" / "start-cloudflare.ps1").read_text(
            "utf-8"
        )
        self.assertIn("does not match Cloudflare ingress hostname", tunnel_start)
        self.assertIn('"$PublicBase/healthz"', tunnel_start)
        stop = (ROOT / "scripts" / "stop.ps1").read_text("utf-8")
        self.assertIn("wsl-distro.txt", root_start)
        self.assertIn("wsl-distro.txt", stop)

    def test_password_hashing_enforces_bcrypt_limit_and_clears_bytes(self) -> None:
        script = (ROOT / "mcp-public" / "set-password.ps1").read_text("utf-8")
        self.assertIn("GetByteCount($FirstPlain) -gt 72", script)
        self.assertIn("[Array]::Clear($PasswordBytes", script)

    def test_readonly_profile_clears_api_key_and_stops_failed_child(self) -> None:
        script = (ROOT / "mcp-public" / "configure-readonly-profile.ps1").read_text(
            "utf-8"
        )
        self.assertIn("[Array]::Clear($KeyBytes", script)
        self.assertIn("$Process.Kill()", script)

    def test_cloudflare_credentials_and_tunnel_reuse_are_explicit(self) -> None:
        script = (ROOT / "mcp-public" / "setup-cloudflare.ps1").read_text(
            "utf-8"
        )
        self.assertIn("Protect-CloudflareCredentialPath", script)
        self.assertIn("/inheritance:r", script)
        self.assertIn("/remove:g", script)
        self.assertIn("Cloudflare credential ACL verification failed", script)
        self.assertIn("[switch]$ReuseExistingTunnel", script)
        self.assertIn("rerun with -ReuseExistingTunnel", script)

    def test_permissive_pdf_dependencies_replace_pymupdf(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        dependencies = "\n".join(project["project"]["dependencies"]).casefold()
        self.assertNotIn("pymupdf", dependencies)
        self.assertIn("pypdfium2", dependencies)
        self.assertIn("pillow", dependencies)
        source = (ROOT / "ingest.py").read_text("utf-8")
        self.assertNotIn("import fitz", source)

    def test_pdf_render_crop_and_image_resize(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            pdf = work / "sample.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=612, height=792)
            with pdf.open("wb") as output:
                writer.write(output)
            full = ingest.render_pdf_page(pdf, 0, work / "full.png", 120)
            crop = ingest.render_pdf_page(
                pdf, 0, work / "crop.png", 120, [100, 100, 500, 500]
            )
            with Image.open(full) as full_image, Image.open(crop) as crop_image:
                self.assertLess(crop_image.width, full_image.width)
                self.assertLess(crop_image.height, full_image.height)
            large = work / "large.png"
            Image.new("RGB", (2400, 1200), "white").save(large)
            resized, changed = ingest.prepare_model_image(large, work, 1600)
            self.assertTrue(changed)
            with Image.open(resized) as resized_image:
                self.assertEqual(max(resized_image.size), 1600)

    def test_embedding_probe_is_cached_only_inside_one_process(self) -> None:
        import sqlite3

        import ingest

        config = {
            "weknora": {
                "upload_command": ["weknora"],
                "parent_knowledge_base": "parent-kb",
                "child_knowledge_base": "child-kb",
                "raw_knowledge_base": "raw-kb",
                "profile": "local",
                "setup_profile": "local",
                "chunk_sizes": {},
                "models": {
                    "provider": "ollama",
                    "embedding": "qwen3-embedding:0.6b",
                    "embedding_dimension": 1024,
                },
            }
        }
        real_probe_calls = 0

        def fake_run_command(template, values, cwd, **kwargs):
            nonlocal real_probe_calls
            if "/api/v1/initialization/embedding/test" in template:
                real_probe_calls += 1
                return {"data": {"data": {"available": True, "dimension": 1024}}}
            self.assertIn("kb", template)
            self.assertIn("view", template)
            return {
                "data": {
                    "embedding_model_id": "embedding-model-id",
                    "chunking_config": {},
                }
            }

        database = sqlite3.connect(":memory:")
        database.execute(
            "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)"
        )
        ingest.EMBEDDING_VERIFIED_THIS_PROCESS.clear()
        ingest.EMBEDDING_WARMED_THIS_PROCESS.clear()
        self.addCleanup(ingest.EMBEDDING_VERIFIED_THIS_PROCESS.clear)
        self.addCleanup(ingest.EMBEDDING_WARMED_THIS_PROCESS.clear)
        with mock.patch.object(ingest, "run_command", side_effect=fake_run_command):
            ingest.verify_embedding_model(config, database)
            ingest.verify_embedding_model(config, database)
            self.assertEqual(real_probe_calls, 1)
            # Simulate a fresh process: the persisted DB fingerprint must not
            # suppress a new real call after Ollama or Docker may have changed.
            ingest.EMBEDDING_VERIFIED_THIS_PROCESS.clear()
            ingest.verify_embedding_model(config, database)
        self.assertEqual(real_probe_calls, 2)
        database.close()

    def test_preflight_does_not_trust_persisted_embedding_health(self) -> None:
        source = (ROOT / "ingest.py").read_text("utf-8")
        self.assertNotIn("Embedding配置已有验证指纹，跳过日常重复预热", source)
        preflight = source[source.index("def preflight(") : source.index("def clean_job(")]
        self.assertLess(
            preflight.index("verify_embedding_model(cfg, db)"),
            preflight.index('"kb", "status"'),
        )
        self.assertIn("知识库尚未达到可检索状态", preflight)
        self.assertIn('(\"原文\", wc[\"raw_knowledge_base\"])', preflight)

    def test_embedding_warmup_is_cached_only_inside_one_process(self) -> None:
        import ingest

        config = {
            "ollama": {"base_url": "http://127.0.0.1:11434"},
            "weknora": {
                "models": {
                    "embedding": "qwen3-embedding:0.6b",
                    "embedding_dimension": 4,
                }
            },
        }
        response = mock.Mock()
        response.json.return_value = {"embeddings": [[0.0, 0.0, 0.0, 0.0]]}
        ingest.EMBEDDING_WARMED_THIS_PROCESS.clear()
        self.addCleanup(ingest.EMBEDDING_WARMED_THIS_PROCESS.clear)
        with (
            mock.patch.object(ingest, "windows_memory_gb", return_value=(16, 8)),
            mock.patch.object(ingest.requests, "post", return_value=response) as post,
        ):
            ingest.warm_embedding_model(config)
            ingest.warm_embedding_model(config)
        self.assertEqual(post.call_count, 1)
        response.raise_for_status.assert_called_once_with()

    def test_weknora_configuration_is_rerunnable_without_stale_ids(self) -> None:
        script = (ROOT / "scripts" / "configure-weknora.ps1").read_text(
            "utf-8"
        )
        self.assertNotIn("2>&1", script)
        self.assertIn("$Raw = (& $Cli @Arguments 2>$null)", script)
        self.assertIn("Set-YamlScalar", script)
        self.assertIn('"model", "view"', script)
        self.assertIn("embedding_parameters.dimension", script)
        self.assertIn("embedding_model_id", script)
        self.assertIn("ExistingProfile.host", script)
        self.assertNotIn('.Replace(\'"__PARENT_KB_ID__"\'', script)

    def test_doctor_allows_retrieval_without_build_tools_or_provider_keys(self) -> None:
        script = (ROOT / "scripts" / "doctor.ps1").read_text("utf-8")
        self.assertIn("existing knowledge-base retrieval can still work", script)
        self.assertIn("existing WeKnora CLI can run", script)

    def test_mimo_documentation_matches_safe_example_limits(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        mimo = config["ollama"]["mimo"]
        self.assertEqual(mimo["parallel_per_key"], 2)
        self.assertEqual(mimo["parallel_cap"], 8)
        text = (ROOT / "config.example.yaml").read_text("utf-8")
        self.assertIn("有效Key数×2", text)
        self.assertIn("总上限为8", text)

    def test_optional_compose_profiles_have_network_warning(self) -> None:
        guide = (ROOT / "docs" / "CHATGPT_MCP.md").read_text("utf-8")
        self.assertIn("可选 Compose Profile 的端口", guide)
        self.assertIn("127.0.0.1", guide)
        self.assertIn("默认密码", guide)

    def test_example_env_has_no_inert_mcp_settings(self) -> None:
        example = (ROOT / ".env.example").read_text("utf-8")
        self.assertNotIn("MCP_EXTERNAL_URL", example)
        self.assertNotIn("MCP_PUBLIC_HOSTNAME", example)
        self.assertNotIn("CLOUDFLARE_TUNNEL_NAME", example)

    def test_release_audit_scans_structured_credentials(self) -> None:
        audit = (ROOT / "scripts" / "release-audit.ps1").read_text("utf-8")
        self.assertIn('".json"', audit)
        self.assertIn("TunnelSecret", audit)
        self.assertIn("WEKNORA_API_KEY", audit)
        self.assertIn("PASSWORD_HASH", audit)
        self.assertIn("AGPL PyMuPDF dependency", audit)

    def test_citation_has_authors_and_matches_package_version(self) -> None:
        citation = yaml.safe_load((ROOT / "CITATION.cff").read_text("utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        self.assertEqual(citation["cff-version"], "1.2.0")
        self.assertIsInstance(citation.get("authors"), list)
        self.assertGreater(len(citation["authors"]), 0)
        self.assertTrue(
            all(isinstance(author, dict) and author for author in citation["authors"])
        )
        self.assertTrue(all(author.get("alias") for author in citation["authors"]))
        self.assertEqual(citation["version"], project["project"]["version"])

    def test_release_audit_checks_reachable_commit_emails(self) -> None:
        audit = (ROOT / "scripts" / "release-audit.ps1").read_text("utf-8")
        self.assertIn("rev-list --all", audit)
        self.assertIn("--format='%B'", audit)
        self.assertIn('Test-PublishableText "commit-message"', audit)
        self.assertIn("%ae%x09%ce", audit)
        self.assertIn("users\\.noreply\\.github\\.com", audit)
        self.assertIn("non-noreply commit email", audit)

    def test_release_audit_restricts_workflow_capabilities(self) -> None:
        audit = (ROOT / "scripts" / "release-audit.ps1").read_text("utf-8")
        self.assertIn("actions/checkout", audit)
        self.assertIn("actions/setup-python", audit)
        self.assertIn("astral-sh/setup-uv", audit)
        self.assertIn("pull_request_target", audit)
        self.assertIn("persist-credentials", audit)
        self.assertIn("contents: read", audit)

    def test_workflow_targets_main_pushes_and_pull_requests(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "audit.yml").read_text("utf-8")
        self.assertRegex(workflow, r"(?m)^  push:\s*\n    branches: \[main\]$")
        self.assertRegex(workflow, r"(?m)^  pull_request:\s*$")
        self.assertIn("cancel-in-progress: true", workflow)
        parsed = yaml.safe_load(workflow)
        self.assertEqual(parsed["permissions"], {"contents": "read"})
        steps = parsed["jobs"]["public-template-audit"]["steps"]
        checkout = next(
            step
            for step in steps
            if step.get("uses", "").startswith("actions/checkout@")
        )
        self.assertFalse(checkout["with"]["persist-credentials"])

    def test_declared_python_range_has_ci_coverage(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        self.assertEqual(project["project"]["requires-python"], ">=3.11")
        workflow = (ROOT / ".github" / "workflows" / "audit.yml").read_text(
            "utf-8"
        )
        self.assertIn('python-version: ["3.11", "3.14"]', workflow)

    def test_retrieval_weights_are_not_claimed_for_official_mcp(self) -> None:
        configuration = (ROOT / "docs" / "CONFIGURATION.md").read_text("utf-8")
        self.assertIn("只影响 `ingest.py --search`", configuration)
        self.assertIn("不会改写官方 WeKnora MCP", configuration)

    def test_cloudflare_setup_points_to_complete_start_command(self) -> None:
        setup = (ROOT / "mcp-public" / "setup-cloudflare.ps1").read_text("utf-8")
        self.assertIn("if ($CreateDnsRoute)", setup)
        self.assertIn("DNS was not changed", setup)
        self.assertIn("mcp-public\\start-all.ps1 -ExternalUrl https://$Hostname", setup)

    def test_workflow_actions_are_immutable(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "audit.yml").read_text("utf-8")
        uses = re.findall(r"(?m)^\s*uses:\s*\S+@([^\s#]+)", workflow)
        self.assertGreaterEqual(len(uses), 3)
        for revision in uses:
            self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_setup_uv_binary_version_is_pinned(self) -> None:
        for relative_path in (
            Path(".github/workflows/audit.yml"),
            Path("docs/audit.workflow.example.yml"),
        ):
            workflow = yaml.safe_load((ROOT / relative_path).read_text("utf-8"))
            setup_steps = [
                step
                for job in workflow["jobs"].values()
                for step in job["steps"]
                if step.get("uses", "").startswith("astral-sh/setup-uv@")
            ]
            self.assertGreater(len(setup_steps), 0, str(relative_path))
            for step in setup_steps:
                self.assertEqual(
                    step.get("with", {}).get("version"),
                    "0.12.2",
                    str(relative_path),
                )

    def test_duplicate_mineru_part_result_is_rejected(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            first = ingest.SourcePart(work / "book.part-1-10.pdf", 0)
            second = ingest.SourcePart(work / "book.part-11-20.pdf", 10)
            first.path.write_bytes(b"first part")
            second.path.write_bytes(b"second part")
            duplicate_id = ingest.part_data_id(first)
            items = [
                {"data_id": duplicate_id, "full_zip_url": "https://one.invalid"},
                {"data_id": duplicate_id, "full_zip_url": "https://two.invalid"},
            ]

            def create_result(_url: str, target: Path) -> None:
                with zipfile.ZipFile(target, "w") as archive:
                    archive.writestr("full.md", "# first")

            with mock.patch.object(
                ingest, "download_mineru_zip", side_effect=create_result
            ) as downloader:
                with self.assertRaisesRegex(RuntimeError, "重复分卷"):
                    ingest.download_results(items, [first, second], work)
            self.assertEqual(downloader.call_count, 1)

    def test_mineru_result_zip_rejects_unsafe_paths(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.md", "unsafe")
            with self.assertRaisesRegex(RuntimeError, "不安全路径"):
                ingest.validate_mineru_zip(archive_path)

    def test_recovered_mineru_results_reject_duplicates_before_download(self) -> None:
        import ingest

        items = [
            {
                "data_id": "duplicate-id",
                "file_name": "book.part-1-100.pdf",
                "full_zip_url": "https://example.invalid/one.zip",
            },
            {
                "data_id": "duplicate-id",
                "file_name": "book.part-101-200.pdf",
                "full_zip_url": "https://example.invalid/two.zip",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            ingest, "download_mineru_zip"
        ) as download:
            with self.assertRaisesRegex(RuntimeError, "重复任务"):
                ingest.download_recovered_results(
                    items,
                    Path(temporary) / "book.pdf",
                    Path(temporary),
                )
        download.assert_not_called()

    def test_mineru_batch_response_is_validated_before_persisting(self) -> None:
        import ingest

        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "code": 0,
            "data": {"batch_id": "batch-1", "file_urls": []},
        }
        response.raise_for_status.return_value = None
        persisted = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample.pdf"
            source.write_bytes(b"pdf")
            part = ingest.SourcePart(source, 0)
            with mock.patch.object(ingest.requests, "post", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "上传地址数量或格式"):
                    ingest.mineru_submit(
                        [part],
                        {"primary": "placeholder-value"},
                        {
                            "base_url": "https://mineru.example.invalid",
                            "model_version": "vlm",
                            "language": "ch",
                        },
                        on_batch_created=persisted,
                        preferred_slot="primary",
                    )
        persisted.assert_not_called()

    def test_recovered_mineru_results_reject_duplicate_explicit_page_ranges(self) -> None:
        import ingest

        items = [
            {
                "data_id": "first",
                "file_name": "book.part-1-100.pdf",
                "full_zip_url": "https://example.invalid/one.zip",
            },
            {
                "data_id": "second",
                "file_name": "book.part-1-100.pdf",
                "full_zip_url": "https://example.invalid/two.zip",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            ingest, "download_mineru_zip"
        ) as download:
            with self.assertRaisesRegex(RuntimeError, "重复分卷起始页"):
                ingest.download_recovered_results(
                    items,
                    Path(temporary) / "book.pdf",
                    Path(temporary),
                )
        download.assert_not_called()

    def test_mimo_disabled_prevents_classification_network_call(self) -> None:
        import ingest

        rule = ingest.DocumentClassification(
            "待分类", "未知机构", "综合", ("综合",), "rule", 0.2, (), 1
        )
        cfg = {
            "document_classification": {
                "taxonomy": {"version": 1},
                "other_min_confidence": 0.9,
            },
            "ollama": {"mimo": {"enabled": False}},
        }
        with mock.patch.object(
            ingest,
            "rule_classification",
            return_value=(rule, True, {}, []),
        ), mock.patch.object(ingest, "mimo_classification") as remote:
            result = ingest.classify_group("uncertain", [], cfg)
        self.assertEqual(result, rule)
        remote.assert_not_called()

    def test_transient_cloud_failures_stay_retryable(self) -> None:
        import ingest

        self.assertTrue(ingest.is_retryable_parse_error("503 Service Unavailable"))
        self.assertTrue(
            ingest.is_retryable_parse_error(
                "MiMo全部已配置Key暂不可用: HTTP 429 Too Many Requests"
            )
        )
        self.assertFalse(
            ingest.is_permanent_source_parse_error("unexpected internal service error")
        )

    def test_existing_document_reuse_always_validates_content_and_kb(self) -> None:
        source = (ROOT / "ingest.py").read_text("utf-8")
        fragment = source[
            source.index("def weknora_find_existing(") : source.index(
                "def weknora_document("
            )
        ]
        self.assertNotIn(
            "if not layer and not group_id and classification is None", fragment
        )
        self.assertIn("remote_hash =", fragment)
        self.assertIn("actual_kb =", fragment)

    def test_verification_cannot_borrow_another_documents_hit(self) -> None:
        source = (ROOT / "ingest.py").read_text("utf-8")
        fragment = source[
            source.index("def weknora_verify(") : source.index(
                "def full_route_check_due("
            )
        ]
        self.assertNotIn("route_has_hits", fragment)
        self.assertIn("拒绝把其他文档的命中当作成功", fragment)

    def test_verification_rejects_remote_hash_or_kb_mismatch(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markdown = root / "document.md"
            source = root / "source.pdf"
            markdown.write_text("# unique content\n", encoding="utf-8")
            source.write_bytes(b"pdf")
            cfg = {
                "knowledge_base": "expected-kb",
                "profile": "test",
                "upload_command": ["weknora"],
                "wait_command": ["wait"],
                "search_command": ["search", "{query}"],
                "verification_limit": 50,
            }

            def mismatched_hash(command, values, cwd, timeout_seconds=600, api_key=None):
                if command[1:3] == ["doc", "view"]:
                    return {
                        "data": {
                            "parse_status": "completed",
                            "file_hash": "wrong",
                            "knowledge_base_id": "expected-kb",
                        }
                    }
                raise AssertionError(f"unexpected command after failed hash: {command}")

            with mock.patch.object(ingest, "run_command", side_effect=mismatched_hash):
                with self.assertRaisesRegex(RuntimeError, "内容摘要"):
                    ingest.weknora_verify(
                        markdown, source, cfg, "doc-1", wait=False
                    )

            def mismatched_kb(command, values, cwd, timeout_seconds=600, api_key=None):
                if command[1:3] == ["doc", "view"]:
                    return {
                        "data": {
                            "parse_status": "completed",
                            "file_hash": ingest.md5_digest(markdown),
                            "knowledge_base_id": "other-kb",
                        }
                    }
                raise AssertionError(f"unexpected command after failed kb: {command}")

            with mock.patch.object(ingest, "run_command", side_effect=mismatched_kb):
                with self.assertRaisesRegex(RuntimeError, "知识库归属"):
                    ingest.weknora_verify(
                        markdown, source, cfg, "doc-1", wait=False
                    )

    def test_normal_mode_preflights_before_mineru_submission(self) -> None:
        source = (ROOT / "ingest.py").read_text("utf-8")
        main_fragment = source[source.index("def main(") :]
        self.assertLess(
            main_fragment.index("preflight(cfg, db)"),
            main_fragment.index("prequeue_all_mineru(prequeue_needed"),
        )

    def test_equal_byte_sources_fail_safely_instead_of_sharing_state(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            first = work / "first.pdf"
            second = work / "second.pdf"
            first.write_bytes(b"identical")
            second.write_bytes(b"identical")
            digest = ingest.sha256(first)
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            database.execute(
                "CREATE TABLE files(sha256 TEXT PRIMARY KEY, source_path TEXT NOT NULL)"
            )
            database.execute(
                "INSERT INTO files(sha256,source_path) VALUES(?,?)",
                (digest, str(first)),
            )
            with self.assertRaisesRegex(RuntimeError, "不能安全并发追踪"):
                ingest.reject_active_duplicate_source_instances(
                    database, [second], {second: digest}
                )
            database.close()

    def test_public_mcp_requires_exact_three_layer_allowlist(self) -> None:
        start = (ROOT / "mcp-public" / "start.ps1").read_text("utf-8")
        self.assertIn("UnexpectedKnowledgeBaseIds", start)
        self.assertIn("outside the configured three layers", start)
        self.assertIn("foreach ($KnowledgeBaseId in $UniqueExpectedIds)", start)
        self.assertIn("returned no retrieval result", start)

    def test_cloudflare_ingress_and_orphan_processes_are_fail_closed(self) -> None:
        start = (ROOT / "mcp-public" / "start-cloudflare.ps1").read_text(
            "utf-8"
        )
        stop = (ROOT / "mcp-public" / "stop.ps1").read_text("utf-8")
        self.assertIn("$ServiceMatches.Count -ne 2", start)
        self.assertIn('"http://127.0.0.1:18081"', start)
        self.assertIn('"http_status:404"', start)
        self.assertIn("untracked or duplicate cloudflared", start)
        self.assertIn("Get-CimInstance Win32_Process", stop)
        self.assertIn("Stop-ProcessTree", stop)
        self.assertIn("[Threading.Mutex]::new", start)

    def test_stack_start_is_serialized_and_does_not_adopt_unrelated_wsl(self) -> None:
        start = (ROOT / "scripts" / "start.ps1").read_text("utf-8")
        stop = (ROOT / "scripts" / "stop.ps1").read_text("utf-8")
        self.assertIn("QuestionBank-$MutexDigest-StackStart", start)
        self.assertIn("$SharedWslPath", start)
        self.assertIn("if (-not (Test-Path $PidFile)) { return $null }", start)
        self.assertIn("[regex]::Escape($IngestScript)", start)
        self.assertIn("[regex]::Escape($IngestScript)", stop)
        self.assertIn("function Stop-ProcessTree", start)
        self.assertIn("function Stop-ProcessTree", stop)
        self.assertIn("Stop-ProcessTree ([int]$Row.ProcessId)", start)
        self.assertIn("Stop-ProcessTree $SavedPid", stop)

    def test_windows_powershell_reads_rewritten_utf8_files_explicitly(self) -> None:
        """Windows PowerShell 5.1 must not decode UTF-8 files as ANSI."""
        doctor = (ROOT / "scripts" / "doctor.ps1").read_text("utf-8")
        status = (ROOT / "scripts" / "status.ps1").read_text("utf-8")
        release = (ROOT / "scripts" / "release-audit.ps1").read_text("utf-8")
        self.assertIn("[IO.File]::ReadAllText($Config", doctor)
        self.assertIn("[IO.File]::ReadAllText($LocalConfig", status)
        self.assertIn("[IO.File]::ReadAllText($File.FullName", release)
        self.assertNotIn(
            "Get-Content -Raw -LiteralPath $File.FullName",
            release,
        )

    def test_weknora_images_and_pdf_security_floor_are_pinned(self) -> None:
        bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text("utf-8")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        self.assertIn(
            'Set-DotEnvValue $WeKnoraEnv "WEKNORA_VERSION" $WeKnoraVersion',
            bootstrap,
        )
        self.assertIn("pypdf>=6.15.0", project["project"]["dependencies"])
        self.assertIn('Join-Path $WeKnora "docker-compose.override.yml"', bootstrap)
        self.assertEqual(bootstrap.count("restart: unless-stopped"), 2)

    def test_manual_deletion_snapshot_cannot_delete_replacement_bytes(self) -> None:
        import ingest

        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            folders = {
                name: work / name
                for name in ("inbox", "failed", "markdown", "work")
            }
            for folder in folders.values():
                folder.mkdir(parents=True)
            source = folders["inbox"] / "source.pdf"
            canonical = folders["markdown"] / "source.md"
            source.write_bytes(b"old bytes")
            canonical.write_text("# retained", encoding="utf-8")
            old_digest = ingest.sha256(source)
            with mock.patch.object(ingest, "ROOT", work):
                database = ingest.db_open()
            old_time = int(time.time()) - 120
            database.execute(
                """INSERT INTO files(
                    sha256,source_path,state,error,updated_at,metrics_json
                ) VALUES(?,?,?,?,?,?)""",
                (old_digest, str(source), "completed", "", old_time, "{}"),
            )
            database.execute(
                """INSERT INTO groups(
                    group_id,group_name,state,markdown_path,updated_at
                ) VALUES(?,?,?,?,?)""",
                ("group-1", "group-1", "completed", str(canonical), old_time),
            )
            database.execute(
                "INSERT INTO group_files(group_id,sha256,source_path) VALUES(?,?,?)",
                ("group-1", old_digest, str(source)),
            )
            database.commit()
            source.unlink()
            selection = folders["work"] / "selection.json"
            cfg = {
                "folders": folders,
                "cleanup": {"permanently_delete_source_after_search": False},
            }
            detected = ingest.detect_manual_deletions(
                cfg, database, selection, grace_seconds=0
            )
            self.assertEqual(detected["affected_groups"], 1)
            source.write_bytes(b"new replacement bytes")
            with mock.patch.dict(
                os.environ,
                {"QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC": "I_UNDERSTAND"},
            ):
                with self.assertRaisesRegex(RuntimeError, "已恢复或被替换"):
                    ingest.sync_manual_deletions(
                        cfg, database, selection, dry_run=False
                    )
            self.assertEqual(source.read_bytes(), b"new replacement bytes")
            database.close()


if __name__ == "__main__":
    unittest.main()
