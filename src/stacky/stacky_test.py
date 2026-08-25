#!/usr/bin/env python3
import io
import os
import subprocess
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


class TestInit(unittest.TestCase):
    def test_init_git_does_not_check_gh_auth(self):
        calls = []

        def fake_run(cmd, check=True):  # noqa: ARG001
            calls.append(cmd)
            if cmd == ["git", "config", "remote.pushDefault"]:
                return None
            if cmd == ["git", "symbolic-ref", "-q", "HEAD"]:
                return "refs/heads/main"
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch.object(stacky_module, "run", side_effect=fake_run):
            stacky_module.init_git()

        self.assertNotIn(["gh", "auth", "status"], calls)
        self.assertEqual(stacky_module.CURRENT_BRANCH, "main")

    def test_init_gh_checks_auth(self):
        with mock.patch.object(stacky_module, "run", return_value="ok") as run_mock:
            stacky_module.init_gh()

        run_mock.assert_called_once_with(["gh", "auth", "status"], check=False)

    def test_args_need_gh_for_pr_and_github_commands(self):
        self.assertFalse(stacky_module.args_need_gh(Namespace(command="info", pr=False)))
        self.assertTrue(stacky_module.args_need_gh(Namespace(command="info", pr=True)))
        self.assertFalse(stacky_module.args_need_gh(Namespace(command="sync")))

        for command in ("update", "import", "rebuild", "land"):
            self.assertTrue(stacky_module.args_need_gh(Namespace(command=command)))


class TestGitHubAuthentication(unittest.TestCase):
    def test_read_one_config_use_gh_auth(self):
        cfg = stacky_module.StackyConfig()
        with tempfile.NamedTemporaryFile("w", delete=False) as config_file:
            config_file.write("[UI]\nuse_gh_auth = true\n")
            path = config_file.name
        try:
            cfg.read_one_config(path)
        finally:
            os.unlink(path)

        self.assertTrue(cfg.use_gh_auth)

    def test_git_command_is_unchanged_when_gh_auth_is_disabled(self):
        cmd = stacky_module.CmdArgs(["git", "fetch", "origin"])
        with mock.patch.object(stacky_module, "CONFIG", stacky_module.StackyConfig()):
            self.assertEqual(stacky_module._git_command_with_gh_auth(cmd), cmd)

    def test_git_command_uses_gh_credential_helper_and_rewrites_ssh_remotes(self):
        cmd = stacky_module.CmdArgs(["git", "-C", "/repo/worktree", "push", "origin", "feature"])
        with mock.patch.object(stacky_module, "CONFIG", stacky_module.StackyConfig(use_gh_auth=True)):
            configured_cmd = stacky_module._git_command_with_gh_auth(cmd)

        self.assertEqual(
            configured_cmd,
            [
                "git",
                "-c",
                "credential.https://github.com.helper=",
                "-c",
                "credential.https://github.com.helper=!gh auth git-credential",
                "-c",
                "url.https://github.com/.insteadOf=git@github.com:",
                "-c",
                "url.https://github.com/.insteadOf=ssh://git@github.com/",
                "-C",
                "/repo/worktree",
                "push",
                "origin",
                "feature",
            ],
        )

    def test_gh_command_is_unchanged_when_gh_auth_is_enabled(self):
        cmd = stacky_module.CmdArgs(["gh", "auth", "status"])
        with mock.patch.object(stacky_module, "CONFIG", stacky_module.StackyConfig(use_gh_auth=True)):
            self.assertEqual(stacky_module._git_command_with_gh_auth(cmd), cmd)

    def test_run_multiline_applies_gh_auth_to_git_subprocesses(self):
        cmd = stacky_module.CmdArgs(["git", "fetch", "origin"])
        completed = subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"fetched\n", stderr=b"")
        with (
            mock.patch.object(stacky_module, "CONFIG", stacky_module.StackyConfig(use_gh_auth=True)),
            mock.patch.object(stacky_module.subprocess, "run", return_value=completed) as subprocess_run,
        ):
            self.assertEqual(stacky_module.run_multiline(cmd), "fetched\n")

        configured_cmd = subprocess_run.call_args.args[0]
        self.assertIn("credential.https://github.com.helper=!gh auth git-credential", configured_cmd)
        self.assertEqual(configured_cmd[-2:], ["fetch", "origin"])

    def test_push_without_pr_checks_gh_auth_only_when_enabled(self):
        push_args = (
            Namespace(command="push", pr=False),
            Namespace(command="stack", stack_command="push", pr=False),
            Namespace(command="upstack", upstack_command="push", pr=False),
            Namespace(command="downstack", downstack_command="push", pr=False),
        )
        for enabled in (False, True):
            with mock.patch.object(stacky_module, "CONFIG", stacky_module.StackyConfig(use_gh_auth=enabled)):
                for args in push_args:
                    with self.subTest(enabled=enabled, command=args.command):
                        self.assertEqual(stacky_module.args_need_gh(args), enabled)
                self.assertFalse(stacky_module.args_need_gh(Namespace(command="info", pr=False)))
                self.assertFalse(stacky_module.args_need_gh(Namespace(command="sync")))

    def test_gh_auth_disables_shared_ssh_sessions(self):
        cfg = stacky_module.StackyConfig(share_ssh_session=True, use_gh_auth=True)
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "get_remote_type") as get_remote_type,
            mock.patch.object(stacky_module.subprocess, "Popen") as popen,
        ):
            stacky_module.start_muxed_ssh()
            stacky_module.stop_muxed_ssh()

        get_remote_type.assert_not_called()
        popen.assert_not_called()


class TestPush(unittest.TestCase):
    def test_read_one_config_checkout_before_push(self):
        cfg = stacky_module.StackyConfig()
        for contents, expected in (
            ("[UI]\ncheckout_before_push = true\n", True),
            ("[UI]\nskip_confirm = true\n", True),
            ("[UI]\ncheckout_before_push = false\n", False),
        ):
            with self.subTest(contents=contents), tempfile.NamedTemporaryFile("w") as config_file:
                config_file.write(contents)
                config_file.flush()
                cfg.read_one_config(config_file.name)
                self.assertEqual(cfg.checkout_before_push, expected)

    @staticmethod
    def make_forest(*branch_names):
        parent = SimpleNamespace(name=stacky_module.BranchName("main"))
        branches = [
            SimpleNamespace(
                name=stacky_module.BranchName(name),
                parent=parent,
                remote="origin",
                remote_branch=stacky_module.BranchName(name),
                is_synced_with_parent=lambda: True,
                is_synced_with_remote=lambda: False,
            )
            for name in branch_names
        ]
        return stacky_module.BranchesTreeForest(
            [stacky_module.BranchesTree({branch.name: (branch, stacky_module.BranchesTree({})) for branch in branches})]
        )

    def test_push_does_not_checkout_branches_by_default(self):
        with (
            mock.patch.object(stacky_module, "get_config", return_value=stacky_module.StackyConfig()),
            mock.patch.object(stacky_module, "start_muxed_ssh"),
            mock.patch.object(stacky_module, "stop_muxed_ssh"),
            mock.patch.object(stacky_module, "print_forest"),
            mock.patch.object(stacky_module, "run", return_value=None) as run_mock,
        ):
            stacky_module.do_push(self.make_forest("feature"), force=True)

        self.assertFalse(any(call.args[0][:2] == ["git", "checkout"] for call in run_mock.call_args_list))
        run_mock.assert_any_call(stacky_module.CmdArgs(["git", "push", "-f", "origin", "feature:feature"]), out=True)

    def test_push_checkouts_each_branch_and_restores_original_branch(self):
        cfg = stacky_module.StackyConfig(use_worktree=False, checkout_before_push=True)
        with (
            mock.patch.object(stacky_module, "CURRENT_BRANCH", stacky_module.BranchName("original"), create=True),
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "start_muxed_ssh"),
            mock.patch.object(stacky_module, "stop_muxed_ssh"),
            mock.patch.object(stacky_module, "print_forest"),
            mock.patch.object(stacky_module, "run", return_value=None) as run_mock,
        ):
            stacky_module.do_push(self.make_forest("first", "second"), force=True)

        run_mock.assert_has_calls(
            [
                mock.call(stacky_module.CmdArgs(["git", "checkout", "first"])),
                mock.call(stacky_module.CmdArgs(["git", "push", "-f", "origin", "first:first"]), out=True),
                mock.call(stacky_module.CmdArgs(["git", "checkout", "second"])),
                mock.call(stacky_module.CmdArgs(["git", "push", "-f", "origin", "second:second"]), out=True),
                mock.call(stacky_module.CmdArgs(["git", "checkout", "original"])),
            ]
        )

    def test_push_checkout_runs_pushes_in_existing_worktrees(self):
        cfg = stacky_module.StackyConfig(use_worktree=True, checkout_before_push=True)
        worktrees = {
            stacky_module.BranchName("first"): "/wt/first",
            stacky_module.BranchName("second"): "/wt/second",
        }

        with (
            mock.patch.object(stacky_module, "CURRENT_BRANCH", stacky_module.BranchName("original"), create=True),
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "start_muxed_ssh"),
            mock.patch.object(stacky_module, "stop_muxed_ssh"),
            mock.patch.object(stacky_module, "print_forest"),
            mock.patch.object(stacky_module, "ensure_worktree", side_effect=lambda branch, **_: worktrees[branch]),
            mock.patch.object(stacky_module, "run", return_value=None) as run_mock,
        ):
            stacky_module.do_push(self.make_forest("first", "second"), force=True)

        run_mock.assert_has_calls(
            [
                mock.call(
                    stacky_module.CmdArgs(["git", "-C", "/wt/first", "push", "-f", "origin", "first:first"]),
                    out=True,
                ),
                mock.call(
                    stacky_module.CmdArgs(["git", "-C", "/wt/second", "push", "-f", "origin", "second:second"]),
                    out=True,
                ),
            ]
        )
        self.assertFalse(any(call.args[0][:2] == ["git", "checkout"] for call in run_mock.call_args_list))

    def test_push_restores_original_branch_after_push_error(self):
        cfg = stacky_module.StackyConfig(use_worktree=False)

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "push"]:
                raise stacky_module.ExitException("push failed")
            return None

        with (
            mock.patch.object(stacky_module, "CURRENT_BRANCH", stacky_module.BranchName("original"), create=True),
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "start_muxed_ssh"),
            mock.patch.object(stacky_module, "stop_muxed_ssh"),
            mock.patch.object(stacky_module, "print_forest"),
            mock.patch.object(stacky_module, "run", side_effect=fake_run) as run_mock,
        ):
            with self.assertRaises(stacky_module.ExitException):
                stacky_module.do_push(self.make_forest("feature"), force=True, checkout_before_push=True)

        self.assertEqual(run_mock.call_args, mock.call(stacky_module.CmdArgs(["git", "checkout", "original"])))

    def test_all_push_commands_use_config_and_checkout_overrides(self):
        forest = self.make_forest("feature")
        stack = SimpleNamespace(stack={"original": None})
        for command in (["push"], ["ds", "push"], ["stack", "push"], ["upstack", "push"]):
            for configured in (False, True):
                for options, expected in (([], configured), (["--checkout"], True), (["--no-checkout"], False)):
                    with self.subTest(command=command, configured=configured, options=options):
                        cfg = stacky_module.StackyConfig(checkout_before_push=configured)
                        with (
                            mock.patch.object(
                                stacky_module.sys, "argv", ["stacky", *command, "--force", "--no-pr", *options]
                            ),
                            mock.patch.object(stacky_module, "CONFIG", cfg),
                            mock.patch.object(stacky_module, "read_config", return_value=cfg),
                            mock.patch.object(stacky_module, "CURRENT_BRANCH", "original", create=True),
                            mock.patch.object(stacky_module, "run_always_return", side_effect=["/repo", "/repo/.git"]),
                            mock.patch.object(stacky_module, "init_git"),
                            mock.patch.object(stacky_module, "StackBranchSet", return_value=stack),
                            mock.patch.object(stacky_module, "load_all_stacks"),
                            mock.patch.object(stacky_module, "get_current_stack_as_forest", return_value=forest),
                            mock.patch.object(stacky_module, "get_current_upstack_as_forest", return_value=forest),
                            mock.patch.object(stacky_module, "get_current_downstack_as_forest", return_value=forest),
                            mock.patch.object(stacky_module, "print_forest"),
                            mock.patch.object(stacky_module, "run", return_value=None) as run_mock,
                            mock.patch.object(stacky_module.os, "remove"),
                        ):
                            stacky_module.main()

                        expected_commands = [["git", "push", "-f", "origin", "feature:feature"]]
                        if expected:
                            expected_commands.insert(0, ["git", "checkout", "feature"])
                            expected_commands.append(["git", "checkout", "original"])
                        self.assertEqual(
                            [call.args[0] for call in run_mock.call_args_list],
                            [["git", "config", "remote.origin.gh-resolved"], *expected_commands],
                        )

    def test_all_push_commands_accept_checkout_option(self):
        for command in (["push"], ["ds", "push"], ["stack", "push"], ["upstack", "push"]):
            with self.subTest(command=command):
                with (
                    mock.patch.object(stacky_module.sys, "argv", ["stacky", *command, "--help"]),
                    mock.patch.object(stacky_module.sys, "stdout", new=io.StringIO()) as output,
                ):
                    with self.assertRaises(SystemExit) as exit_context:
                        stacky_module.main()

                self.assertEqual(exit_context.exception.code, 0)
                self.assertIn("--checkout", output.getvalue())
                self.assertIn("--no-checkout", output.getvalue())


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

    def test_read_one_config_remote_name(self):
        cfg = stacky_module.StackyConfig()
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("[UI]\nremote_name = upstream\n")
            path = f.name
        try:
            cfg.read_one_config(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg.remote_name, "upstream")

    def test_get_gh_repo_parses_https_remote(self):
        with mock.patch.object(stacky_module, "run", side_effect=[None, "https://github.com/org/repo.git"]):
            self.assertEqual(stacky_module.get_gh_repo("upstream"), "org/repo")

    def test_get_gh_repo_parses_ssh_remote(self):
        with mock.patch.object(stacky_module, "run", side_effect=[None, "git@github.com:org/repo.git"]):
            self.assertEqual(stacky_module.get_gh_repo("upstream"), "org/repo")

    @mock.patch.object(stacky_module, "run")
    @mock.patch.object(stacky_module, "run_multiline", return_value="")
    @mock.patch.object(stacky_module.os, "makedirs")
    def test_ensure_worktree_creates_new(self, makedirs_mock, run_multiline_mock, run_mock):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        cfg.worktree_root = ".stacky/worktrees"
        stacky_module.TOP_LEVEL_DIR = "/repo"
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "_run_worktree_branch_command", return_value=None) as worktree_cmd_mock,
        ):
            path = stacky_module.ensure_worktree(stacky_module.BranchName("feature"), create=False)
        self.assertEqual(path, "/repo/.stacky/worktrees/checkout-1")
        worktree_cmd_mock.assert_called_once_with(
            stacky_module.CmdArgs(["git", "worktree", "add", "/repo/.stacky/worktrees/checkout-1", "feature"]),
            stacky_module.BranchName("feature"),
        )

    @mock.patch.object(stacky_module, "run")
    @mock.patch.object(
        stacky_module,
        "run_multiline",
        return_value=(
            "worktree /repo\n"
            "HEAD abc\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/.stacky/worktrees/checkout-2\n"
            "HEAD def\n"
            "detached\n"
        ),
    )
    @mock.patch.object(stacky_module.os, "makedirs")
    def test_ensure_worktree_reuses_spare_checkout(self, makedirs_mock, run_multiline_mock, run_mock):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        cfg.worktree_root = ".stacky/worktrees"
        stacky_module.TOP_LEVEL_DIR = "/repo"
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "_run_worktree_branch_command", return_value=None) as worktree_cmd_mock,
        ):
            path = stacky_module.ensure_worktree(stacky_module.BranchName("feature"), create=False)
        self.assertEqual(path, "/repo/.stacky/worktrees/checkout-2")
        worktree_cmd_mock.assert_called_once_with(
            stacky_module.CmdArgs(["git", "-C", "/repo/.stacky/worktrees/checkout-2", "checkout", "feature"]),
            stacky_module.BranchName("feature"),
        )

    @mock.patch.object(
        stacky_module,
        "run_multiline",
        return_value=(
            "worktree /repo\n"
            "HEAD abc\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /repo/.stacky/worktrees/checkout-2\n"
            "HEAD def\n"
            "detached\n"
        ),
    )
    @mock.patch.object(stacky_module.os, "makedirs")
    def test_ensure_worktree_returns_existing_path_from_checkout_error(self, makedirs_mock, run_multiline_mock):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        cfg.worktree_root = ".stacky/worktrees"
        stacky_module.TOP_LEVEL_DIR = "/repo"
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(
                stacky_module, "_run_worktree_branch_command", return_value="/repo/.stacky/worktrees/checkout-6"
            ),
        ):
            path = stacky_module.ensure_worktree(stacky_module.BranchName("feature"), create=False)
        self.assertEqual(path, "/repo/.stacky/worktrees/checkout-6")

    @mock.patch.object(stacky_module.subprocess, "run")
    def test_run_worktree_branch_command_returns_existing_worktree_from_git_error(self, subprocess_run_mock):
        subprocess_run_mock.return_value = subprocess.CompletedProcess(
            args=["git", "-C", "/repo/.stacky/worktrees/checkout-2", "checkout", "feature"],
            returncode=128,
            stdout=b"",
            stderr=b"fatal: 'feature' is already used by worktree at '/repo/.stacky/worktrees/checkout-6'\n",
        )
        path = stacky_module._run_worktree_branch_command(
            stacky_module.CmdArgs(["git", "-C", "/repo/.stacky/worktrees/checkout-2", "checkout", "feature"]),
            stacky_module.BranchName("feature"),
        )
        self.assertEqual(path, "/repo/.stacky/worktrees/checkout-6")

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

    def test_cmd_worktree_gc_removes_excess_spares(self):
        cfg = stacky_module.StackyConfig(use_worktree=True, worktree_root=".stacky/worktrees")
        args = Namespace(max_spares=1)
        entries = [
            stacky_module.WorktreeEntry(path="/repo/.stacky/worktrees/checkout-1", branch=None),
            stacky_module.WorktreeEntry(path="/repo/.stacky/worktrees/checkout-2", branch=None),
            stacky_module.WorktreeEntry(
                path="/repo/.stacky/worktrees/checkout-3", branch=stacky_module.BranchName("feature")
            ),
        ]
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "get_worktree_root", return_value="/repo/.stacky/worktrees"),
            mock.patch.object(stacky_module, "get_worktree_entries", return_value=entries),
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.cmd_worktree_gc(MagicMock(), args)
        run_mock.assert_has_calls(
            [
                mock.call(stacky_module.CmdArgs(["git", "worktree", "remove", "/repo/.stacky/worktrees/checkout-2"])),
                mock.call(stacky_module.CmdArgs(["git", "worktree", "prune"])),
            ]
        )

    def test_reset_checked_out_branch_uses_branch_worktree(self):
        with mock.patch.object(stacky_module, "run") as run_mock:
            stacky_module.reset_checked_out_branch(
                stacky_module.BranchName("main"),
                {stacky_module.BranchName("main"): "/wt/main"},
            )

        run_mock.assert_called_once_with(stacky_module.CmdArgs(["git", "-C", "/wt/main", "reset", "--hard", "HEAD"]))

    def test_reset_checked_out_branch_uses_current_worktree_without_branch_worktree(self):
        stacky_module.CURRENT_BRANCH = stacky_module.BranchName("main")
        with mock.patch.object(stacky_module, "run") as run_mock:
            stacky_module.reset_checked_out_branch(stacky_module.BranchName("main"), {})

        run_mock.assert_called_once_with(stacky_module.CmdArgs(["git", "reset", "--hard", "HEAD"]))

    def test_update_bottom_branch_stashes_and_restores_worktree_changes(self):
        with mock.patch.object(
            stacky_module, "run", side_effect=[" M file.txt", None, None, None, None, None]
        ) as run_mock:
            stacky_module.update_bottom_branch(
                "upstream",
                stacky_module.BranchName("main"),
                stacky_module.BranchName("main"),
                {stacky_module.BranchName("main"): "/wt/main"},
            )

        run_mock.assert_has_calls(
            [
                mock.call(
                    stacky_module.CmdArgs(["git", "-C", "/wt/main", "status", "--porcelain", "--untracked-files=all"])
                ),
                mock.call(
                    stacky_module.CmdArgs(
                        [
                            "git",
                            "-C",
                            "/wt/main",
                            "stash",
                            "push",
                            "--include-untracked",
                            "-m",
                            "stacky update: main",
                        ]
                    )
                ),
                mock.call(
                    stacky_module.CmdArgs(["git", "update-ref", "refs/heads/main", "refs/remotes/upstream/main"])
                ),
                mock.call(stacky_module.CmdArgs(["git", "-C", "/wt/main", "reset", "--hard", "HEAD"])),
                mock.call(stacky_module.CmdArgs(["git", "-C", "/wt/main", "stash", "apply", "--index", "stash@{0}"])),
                mock.call(stacky_module.CmdArgs(["git", "-C", "/wt/main", "stash", "drop", "stash@{0}"])),
            ]
        )

    def test_rebase_branch_onto_parent_uses_branch_worktree(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        parent = SimpleNamespace(name=stacky_module.BranchName("parent"))
        branch = SimpleNamespace(
            name=stacky_module.BranchName("child"),
            parent=parent,
            parent_commit=stacky_module.Commit("old-parent"),
        )
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/child") as ensure_mock,
            mock.patch.object(stacky_module, "run", return_value="ok") as run_mock,
        ):
            self.assertEqual(stacky_module.rebase_branch_onto_parent(branch), "ok")

        ensure_mock.assert_called_once_with(stacky_module.BranchName("child"), create=False)
        run_mock.assert_called_once_with(
            stacky_module.CmdArgs(["git", "-C", "/wt/child", "rebase", "--onto", "parent", "old-parent"]),
            out=True,
            check=False,
        )

    def test_rebase_branch_onto_parent_without_worktree_rebases_named_branch(self):
        cfg = stacky_module.StackyConfig(use_worktree=False)
        parent = SimpleNamespace(name=stacky_module.BranchName("parent"))
        branch = SimpleNamespace(
            name=stacky_module.BranchName("child"),
            parent=parent,
            parent_commit=stacky_module.Commit("old-parent"),
        )
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree") as ensure_mock,
            mock.patch.object(stacky_module, "run", return_value="ok") as run_mock,
        ):
            self.assertEqual(stacky_module.rebase_branch_onto_parent(branch), "ok")

        ensure_mock.assert_not_called()
        run_mock.assert_called_once_with(
            stacky_module.CmdArgs(["git", "rebase", "--onto", "parent", "old-parent", "child"]),
            out=True,
            check=False,
        )

    def test_restore_sync_location_uses_current_branch_worktree(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        stacky_module.CURRENT_BRANCH = stacky_module.BranchName("parent")
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/parent") as ensure_mock,
            mock.patch.object(stacky_module, "run") as run_mock,
            mock.patch.object(stacky_module.sys, "stdout", new=io.StringIO()) as out,
        ):
            stacky_module.restore_sync_location()

        ensure_mock.assert_called_once_with(stacky_module.BranchName("parent"), create=False)
        run_mock.assert_not_called()
        self.assertEqual(out.getvalue(), "/wt/parent\n")

    def test_restore_sync_location_without_worktree_checkouts_current_branch(self):
        cfg = stacky_module.StackyConfig(use_worktree=False)
        stacky_module.CURRENT_BRANCH = stacky_module.BranchName("parent")
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree") as ensure_mock,
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.restore_sync_location()

        ensure_mock.assert_not_called()
        run_mock.assert_called_once_with(stacky_module.CmdArgs(["git", "checkout", "parent"]))

    def test_emit_conflicted_sync_location_uses_conflicted_branch_worktree(self):
        cfg = stacky_module.StackyConfig(use_worktree=True)
        branch = SimpleNamespace(name=stacky_module.BranchName("child"))
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "ensure_worktree", return_value="/wt/child") as ensure_mock,
            mock.patch.object(stacky_module.sys, "stdout", new=io.StringIO()) as out,
        ):
            stacky_module.emit_conflicted_sync_location(branch)

        ensure_mock.assert_called_once_with(stacky_module.BranchName("child"), create=False)
        self.assertEqual(out.getvalue(), "/wt/child\n")

    def test_cmd_update_uses_selected_remote(self):
        stack = MagicMock()
        stack.bottoms = set()
        args = Namespace(remote_name="upstream", force=True)
        cfg = stacky_module.StackyConfig(use_worktree=False)
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "start_muxed_ssh") as start_mux_mock,
            mock.patch.object(stacky_module, "stop_muxed_ssh") as stop_mux_mock,
            mock.patch.object(stacky_module, "run") as run_mock,
            mock.patch.object(stacky_module, "get_bottom_level_branches_as_forest", return_value=[]),
            mock.patch.object(stacky_module, "load_pr_info_for_forest"),
            mock.patch.object(stacky_module, "get_branches_to_delete", return_value=[]),
            mock.patch.object(stacky_module, "delete_branches"),
        ):
            stacky_module.cmd_update(stack, args)
        start_mux_mock.assert_called_once_with("upstream")
        run_mock.assert_any_call(stacky_module.CmdArgs(["git", "fetch", "upstream"]))
        stop_mux_mock.assert_called_once_with("upstream")

    def test_cmd_update_resets_checked_out_bottom_worktree(self):
        class Bottom:
            pass

        bottom = Bottom()
        bottom.name = stacky_module.BranchName("main")
        bottom.remote_branch = stacky_module.BranchName("main")
        stack = MagicMock()
        stack.bottoms = {bottom}
        args = Namespace(remote_name="upstream", force=True)
        cfg = stacky_module.StackyConfig(use_worktree=True)
        with (
            mock.patch.object(stacky_module, "get_config", return_value=cfg),
            mock.patch.object(stacky_module, "get_worktree_map", return_value={bottom.name: "/wt/main"}),
            mock.patch.object(stacky_module, "start_muxed_ssh"),
            mock.patch.object(stacky_module, "stop_muxed_ssh"),
            mock.patch.object(stacky_module, "run", return_value="") as run_mock,
            mock.patch.object(stacky_module, "get_bottom_level_branches_as_forest", return_value=[]),
            mock.patch.object(stacky_module, "load_pr_info_for_forest"),
            mock.patch.object(stacky_module, "get_branches_to_delete", return_value=[]),
            mock.patch.object(stacky_module, "delete_branches"),
        ):
            stacky_module.cmd_update(stack, args)

        run_mock.assert_has_calls(
            [
                mock.call(stacky_module.CmdArgs(["git", "fetch", "upstream"])),
                mock.call(
                    stacky_module.CmdArgs(["git", "-C", "/wt/main", "status", "--porcelain", "--untracked-files=all"])
                ),
                mock.call(
                    stacky_module.CmdArgs(["git", "update-ref", "refs/heads/main", "refs/remotes/upstream/main"])
                ),
                mock.call(stacky_module.CmdArgs(["git", "-C", "/wt/main", "reset", "--hard", "HEAD"])),
            ]
        )


class TestShellSupport(unittest.TestCase):
    def test_render_shell_wrapper_uses_custom_names(self):
        out = stacky_module.render_shell_wrapper("myst", "my-stacky")
        self.assertIn("myst()", out)
        self.assertIn("command my-stacky", out)
        self.assertIn("branch|b)", out)

    def test_render_shell_completion_contains_complete_target(self):
        out = stacky_module.render_shell_completion("bash", "/usr/local/bin/stacky")
        self.assertIn("complete -F _stacky_complete stacky st", out)
        self.assertIn('_stacky__parse_subcommands "$stacky_cmd"', out)
        self.assertIn("_stacky__local_branches", out)
        self.assertIn('[[ "$first" == "checkout" || "$first" == "co" ]]', out)

    def test_cmd_shell_setup_writes_scripts_and_prints_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_tmp = os.path.realpath(tmp)
            args = Namespace(
                shell="bash",
                output_dir=tmp,
                function_name="st",
                stacky_command="stacky",
                completion_target=[],
            )
            with mock.patch.object(stacky_module.sys, "stdout", new=io.StringIO()) as out:
                stacky_module.cmd_shell_setup(args)
                stdout_value = out.getvalue()
            completion_path = os.path.join(real_tmp, "stacky-completion.bash")
            wrapper_path = os.path.join(real_tmp, "stacky-wrapper.bash")
            self.assertTrue(os.path.exists(completion_path))
            self.assertTrue(os.path.exists(wrapper_path))
            self.assertIn(f"source {completion_path}", stdout_value)
            self.assertIn(f"source {wrapper_path}", stdout_value)


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
            mock.patch.object(stacky_module, "get_gh_repo", return_value=None),
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
            mock.patch.object(stacky_module, "get_gh_repo", return_value=None),
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.create_gh_pr(branch, "")
        cmd = run_mock.call_args.args[0]
        self.assertIn("--fill", cmd)
        self.assertIn("--editor", cmd)

    def test_create_gh_pr_adds_repo_target(self):
        parent = SimpleNamespace(name=stacky_module.BranchName("main"))
        branch = SimpleNamespace(name=stacky_module.BranchName("feature"), parent=parent)
        with (
            mock.patch.object(stacky_module.os, "isatty", return_value=True),
            mock.patch.object(stacky_module, "find_reviewers", return_value=None),
            mock.patch.object(stacky_module, "find_issue_marker", return_value=None),
            mock.patch.object(stacky_module, "get_gh_repo", return_value="acme/repo"),
            mock.patch.object(stacky_module, "run") as run_mock,
        ):
            stacky_module.create_gh_pr(branch, "")
        cmd = run_mock.call_args.args[0]
        self.assertIn("-R", cmd)
        self.assertIn("acme/repo", cmd)


if __name__ == "__main__":
    unittest.main()
