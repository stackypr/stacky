#!/usr/bin/env python3
import io
import os
import tempfile
import unittest
from unittest import mock
from unittest.mock import MagicMock

import stacky.stacky as stacky_module
from stacky import PRInfos, find_issue_marker, read_config


class TestStringMethods(unittest.TestCase):
    def test_find_issue_marker(self):
        out = find_issue_marker("SRE-12")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("SRE-12-find-things")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("SRE_12")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("SRE_12-find-things")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("john_SRE_12")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("john_SRE_12-find-things")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("john_SRE12-find-things")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("anna_01_01_SRE-12")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("anna_01_01_SRE12")
        self.assertTrue(out is not None)
        self.assertEqual("SRE-12", out)

        out = find_issue_marker("john_test_12")
        self.assertTrue(out is None)

        out = find_issue_marker("john_test12")
        self.assertTrue(out is None)


class TestWorktreeSupport(unittest.TestCase):
    def test_parse_worktree_list(self):
        out = (
            "worktree /repo\n"
            "HEAD abc\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/.stacky/worktrees/feature\n"
            "HEAD def\n"
            "branch refs/heads/feature\n"
        )
        parsed = stacky_module._parse_worktree_list(out)
        self.assertEqual(parsed[stacky_module.BranchName("main")], "/repo")
        self.assertEqual(parsed[stacky_module.BranchName("feature")], "/repo/.stacky/worktrees/feature")

    def test_read_one_config_use_worktree(self):
        cfg = stacky_module.StackyConfig()
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("[UI]\nuse_worktree = true\nworktree_root = /tmp/worktrees\n")
            path = f.name
        try:
            cfg.read_one_config(path)
        finally:
            os.unlink(path)
        self.assertTrue(cfg.use_worktree)
        self.assertEqual(cfg.worktree_root, "/tmp/worktrees")

    @mock.patch.object(stacky_module, "run")
    @mock.patch.object(stacky_module, "run_multiline", return_value="")
    @mock.patch.object(stacky_module.os, "makedirs")
    @mock.patch.object(stacky_module.os.path, "exists", return_value=False)
    def test_ensure_worktree_creates_new(self, exists_mock, makedirs_mock, run_multiline_mock, run_mock):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        cfg.worktree_root = ".stacky/worktrees"
        stacky_module.TOP_LEVEL_DIR = "/repo"
        with mock.patch.object(stacky_module, "get_config", return_value=cfg):
            path = stacky_module.ensure_worktree(stacky_module.BranchName("feature"), create=False)
        self.assertEqual(path, "/repo/.stacky/worktrees/feature")
        run_mock.assert_called_with(
            stacky_module.CmdArgs(["git", "worktree", "add", "/repo/.stacky/worktrees/feature", "feature"])
        )

    def test_checkout_emits_worktree_location(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/feature"),
            mock.patch.object(stacky_module.sys, "stdout", new=io.StringIO()) as out,
        ):
            stacky_module.checkout(stacky_module.BranchName("feature"))
            self.assertEqual(out.getvalue().strip(), "/wt/feature")


class TestVersionReporting(unittest.TestCase):
    def test_get_version_string_uses_stamped_module_commit(self):
        with (
            mock.patch.object(stacky_module, "stacky_build_info", new=SimpleNamespace(STACKY_BUILD_COMMIT="cafef00d")),
            mock.patch.object(stacky_module.importlib.metadata, "version", return_value="1.0.13"),
        ):
            self.assertEqual(stacky_module.get_version_string(), "stacky 1.0.13 (commit cafef00d)")

    def test_get_version_string_uses_stamped_env_commit(self):
        with (
            mock.patch.object(stacky_module, "stacky_build_info", new=None),
            mock.patch.dict(stacky_module.os.environ, {"STACKY_BUILD_COMMIT": "cafef00d"}, clear=False),
            mock.patch.object(stacky_module.importlib.metadata, "version", return_value="1.0.13"),
        ):
            self.assertEqual(stacky_module.get_version_string(), "stacky 1.0.13 (commit cafef00d)")

    def test_get_version_string_uses_embedded_commit(self):
        with (
            mock.patch.dict(stacky_module.os.environ, {}, clear=False),
            mock.patch.object(stacky_module.importlib.metadata, "version", return_value="1.0.13+gdeadbeef"),
        ):
            self.assertEqual(stacky_module.get_version_string(), "stacky 1.0.13+gdeadbeef (commit deadbeef)")

    def test_get_version_string_without_embedded_commit(self):
        with (
            mock.patch.dict(stacky_module.os.environ, {"STACKY_BUILD_COMMIT": "not-a-sha"}, clear=False),
            mock.patch.object(stacky_module.importlib.metadata, "version", return_value="1.0.13"),
        ):
            self.assertEqual(stacky_module.get_version_string(), "stacky 1.0.13")

    def test_get_version_string_without_package_metadata(self):
        with (
            mock.patch.object(
                stacky_module.importlib.metadata,
                "version",
                side_effect=stacky_module.importlib.metadata.PackageNotFoundError,
            ),
        ):
            self.assertEqual(stacky_module.get_version_string(), "stacky dev")


if __name__ == "__main__":
    unittest.main()
