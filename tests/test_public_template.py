import ast
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PublicTemplateTests(unittest.TestCase):
    def test_ingest_parses(self) -> None:
        ast.parse((ROOT / "ingest.py").read_text("utf-8"))

    def test_safe_destructive_defaults(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertFalse(config["classification"]["delete_videos"])
        self.assertFalse(config["classification"]["delete_archives_after_extract"])
        self.assertFalse(
            config["document_classification"]["delete_other_source_after_markdown"]
        )
        self.assertFalse(config["cleanup"]["permanently_delete_source_after_search"])

    def test_no_private_identifiers(self) -> None:
        patterns = [
            re.compile(r"[A-Z]:\\Users\\", re.I),
            re.compile(
                r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
                re.I,
            ),
        ]
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(
                part in {".git", ".runtime", ".venv", "bin"} for part in path.parts
            ):
                continue
            if path.name in {"test_public_template.py", "release-audit.ps1"}:
                continue
            if path.suffix not in {".py", ".ps1", ".md", ".yaml", ".yml", ".toml", ".txt"}:
                continue
            text = path.read_text("utf-8", errors="ignore")
            for pattern in patterns:
                self.assertIsNone(pattern.search(text), f"{pattern.pattern}: {path}")

    def test_runtime_data_is_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text("utf-8")
        for entry in (".env", "config.local.yaml", "state.db*", "inbox/", "markdown/", "outputs/"):
            self.assertIn(entry, ignored)


if __name__ == "__main__":
    unittest.main()
