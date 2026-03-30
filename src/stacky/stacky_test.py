#!/usr/bin/env python3
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from argparse import Namespace
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

    def test_get_worktree_root_uses_repo_top_level(self):
        cfg = stacky_module.StackyConfig(use_worktree=True, worktree_root=".stacky/worktrees")
        stacky_module.TOP_LEVEL_DIR = "/repo/.stacky/worktrees/dev__test"
        stacky_module.REPO_TOP_LEVEL_DIR = "/repo"
        with mock.patch.object(stacky_module, "get_config", return_value=cfg):
            self.assertEqual(stacky_module.get_worktree_root(), "/repo/.stacky/worktrees")

    def test_checkout_emits_worktree_location(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/feature"),
            mock.patch.object(stacky_module.sys, "stdout", new=io.StringIO()) as out,
        ):
            stacky_module.checkout(stacky_module.BranchName("feature"))
            self.assertEqual(out.getvalue().strip(), "/wt/feature")

    def test_create_branch_worktree_sets_parent(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        stacky_module.CURRENT_BRANCH = stacky_module.BranchName("main")
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/feature"),
            mock.patch.object(stacky_module, "set_parent") as set_parent_mock,
            mock.patch.object(stacky_module, "emit_location") as emit_location_mock,
        ):
            stacky_module.create_branch(stacky_module.BranchName("feature"))
        set_parent_mock.assert_called_once_with(
            stacky_module.BranchName("feature"),
            stacky_module.BranchName("main"),
            set_origin=True,
        )
        emit_location_mock.assert_called_once_with("/wt/feature")

    def test_cmd_adopt_worktree_change_to_main_does_not_checkout(self):
        cfg = stacky_module.StackyConfig(use_worktree=True, change_to_main=True)
        stacky_module.CURRENT_BRANCH = stacky_module.BranchName("feature")
        args = Namespace(name=stacky_module.BranchName("topic"))
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "get_real_stack_bottom", return_value=stacky_module.BranchName("main")),
            mock.patch.object(stacky_module, "run") as run_mock,
            mock.patch.object(stacky_module, "get_merge_base", return_value="abc123"),
            mock.patch.object(stacky_module, "set_parent") as set_parent_mock,
            mock.patch.object(stacky_module, "set_parent_commit") as set_parent_commit_mock,
        ):
            stacky_module.cmd_adopt(MagicMock(), args)
        run_mock.assert_not_called()
        set_parent_mock.assert_called_once_with(
            stacky_module.BranchName("topic"),
            stacky_module.BranchName("main"),
            set_origin=True,
        )
        set_parent_commit_mock.assert_called_once_with(stacky_module.BranchName("topic"), "abc123")

    def test_load_stack_for_given_branch_recovers_missing_parent(self):
        stack = MagicMock()
        bottom = MagicMock()
        child = MagicMock()
        stack.add.side_effect = [bottom, child]

        with (
            mock.patch.object(stacky_module, "get_stack_parent_branch", return_value=None),
            mock.patch.object(stacky_module, "get_stack_parent_commit", return_value=stacky_module.Commit("abc123")),
            mock.patch.object(
                stacky_module, "infer_stack_parent_branch", return_value=stacky_module.BranchName("main")
            ),
            mock.patch.object(stacky_module, "set_parent") as set_parent_mock,
        ):
            top, branches = stacky_module.load_stack_for_given_branch(
                stack, stacky_module.BranchName("feature"), check=True
            )

        self.assertIsNotNone(top)
        self.assertEqual(branches, [stacky_module.BranchName("feature"), stacky_module.BranchName("main")])
        set_parent_mock.assert_called_once_with(
            stacky_module.BranchName("feature"),
            stacky_module.BranchName("main"),
            set_origin=True,
        )

    def test_delete_branches_current_branch_worktree_detaches_and_emits_location(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        stacky_module.CURRENT_BRANCH = stacky_module.BranchName("feature")

        class Bottom:
            pass

        bottom = Bottom()
        bottom.name = stacky_module.BranchName("main")
        stack = MagicMock()
        stack.bottoms = {bottom}

        branch = MagicMock()
        branch.name = stacky_module.BranchName("feature")
        branch.children = []

        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/main") as ensure_mock,
            mock.patch.object(stacky_module, "emit_location") as emit_mock,
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.delete_branches(stack, [branch])

        ensure_mock.assert_called_once_with(stacky_module.BranchName("main"), create=False)
        emit_mock.assert_called_once_with("/wt/main")
        run_mock.assert_has_calls(
            [
                mock.call(stacky_module.CmdArgs(["git", "-C", "/wt/main", "checkout", "main"])),
                mock.call(stacky_module.CmdArgs(["git", "checkout", "--detach"])),
                mock.call(stacky_module.CmdArgs(["git", "branch", "-D", "feature"])),
            ]
        )
        self.assertEqual(stacky_module.CURRENT_BRANCH, stacky_module.BranchName("main"))


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


class TestGhPrCreate(unittest.TestCase):
    def test_create_gh_pr_non_interactive_uses_fill(self):
        parent = SimpleNamespace(name=stacky_module.BranchName("main"))
        branch = SimpleNamespace(name=stacky_module.BranchName("feature"), parent=parent)
        with (
            mock.patch.object(stacky_module, "IS_TERMINAL", False),
            mock.patch.object(stacky_module.os, "isatty", return_value=False),
            mock.patch.object(stacky_module, "find_reviewers", return_value=None),
            mock.patch.object(stacky_module, "find_issue_marker", return_value=None),
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.create_gh_pr(branch, "")
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        self.assertIn("--fill", cmd)

    def test_create_gh_pr_captured_stdout_uses_fill(self):
        parent = SimpleNamespace(name=stacky_module.BranchName("main"))
        branch = SimpleNamespace(name=stacky_module.BranchName("feature"), parent=parent)
        with (
            mock.patch.object(stacky_module, "IS_TERMINAL", True),
            mock.patch.object(stacky_module.os, "isatty", side_effect=lambda fd: False if fd == 1 else True),
            mock.patch.object(stacky_module, "find_reviewers", return_value=None),
            mock.patch.object(stacky_module, "find_issue_marker", return_value=None),
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.create_gh_pr(branch, "")
        cmd = run_mock.call_args.args[0]
        self.assertIn("--fill", cmd)
        self.assertIn("--editor", cmd)


if __name__ == "__main__":
    unittest.main()
