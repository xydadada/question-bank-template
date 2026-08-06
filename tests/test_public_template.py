import ast
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

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

    def test_safe_destructive_defaults(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertFalse(config["classification"]["delete_videos"])
        self.assertFalse(config["classification"]["delete_archives_after_extract"])
        self.assertFalse(
            config["document_classification"]["delete_other_source_after_markdown"]
        )
        self.assertFalse(config["cleanup"]["permanently_delete_source_after_search"])

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
            and node.func.id == "require_permanent_delete_confirmation"
        ]
        self.assertEqual(len(confirmation_calls), 1)
        self.assertIn("if not dry_run", ast.unparse(sync))

    def test_archive_status_matches_retention_policy(self) -> None:
        source = (ROOT / "ingest.py").read_text("utf-8")
        self.assertIn("压缩包已展开并永久删除原包", source)
        self.assertIn("压缩包已展开，原包已移至archives保留", source)

    def test_retrieval_only_defaults(self) -> None:
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text("utf-8"))
        self.assertEqual(
            config["weknora"]["features"],
            {"summaries": False, "wiki": False, "graph": False},
        )
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


if __name__ == "__main__":
    unittest.main()
