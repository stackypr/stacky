#!/usr/bin/env python3

# GitHub helper for stacked diffs.
#
# Git maintains all metadata locally. Does everything by forking "git" and "gh"
# commands.
#
# Theory of operation:
#
# Each entry in a stack is a branch, set to track its parent (that is, `git
# config branch.<name>.remote` is ".", and `git config branch.<name>.merge` is
# "refs/heads/<parent>")
#
# For each branch, we maintain a ref (call it PC, for "parent commit") pointing
# to the commit at the tip of the parent branch, as `git update-ref
# refs/stack-parent/<name>`.
#
# When rebasing or restacking, we proceed in depth-first order (from "master"
# onwards). After updating a parent branch P, given a child branch C,
# we rebase everything from C's PC until C's tip onto P.
#
# That's all there is to it.

import configparser
import dataclasses
import importlib.metadata
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from argparse import ArgumentParser
from typing import (
    Dict,
    FrozenSet,
    Generator,
    List,
    NewType,
    NoReturn,
    Optional,
    Tuple,
    TypedDict,
    Union,
)

import asciitree  # type: ignore
import colors  # type: ignore
from simple_term_menu import TerminalMenu  # type: ignore

try:
    import _stacky_build_info as stacky_build_info  # type: ignore
except Exception:
    stacky_build_info = None

BranchName = NewType("BranchName", str)
PathName = NewType("PathName", str)
Commit = NewType("Commit", str)
CmdArgs = NewType("CmdArgs", List[str])
StackSubTree = Tuple["StackBranch", "BranchesTree"]
TreeNode = Tuple[BranchName, StackSubTree]
BranchesTree = NewType("BranchesTree", Dict[BranchName, StackSubTree])
BranchesTreeForest = NewType("BranchesTreeForest", List[BranchesTree])

JSON = Union[Dict[str, "JSON"], List["JSON"], str, int, float, bool, None]


class PRInfo(TypedDict):
    id: str
    number: int
    state: str
    mergeable: str
    url: str
    title: str
    baseRefName: str
    headRefName: str
    commits: List[Dict[str, str]]


@dataclasses.dataclass
class PRInfos:
    all: Dict[str, PRInfo]
    open: Optional[PRInfo]


@dataclasses.dataclass
class BranchNCommit:
    branch: BranchName
    parent_commit: Optional[str]


@dataclasses.dataclass
class WorktreeEntry:
    path: str
    branch: Optional[BranchName]


_LOGGING_FORMAT = "%(asctime)s %(module)s %(levelname)s: %(message)s"

# 2 minutes ought to be enough for anybody ;-)
MAX_SSH_MUX_LIFETIME = 120
COLOR_STDOUT: bool = os.isatty(1)
COLOR_STDERR: bool = os.isatty(2)
# Interactivity should depend on input/error streams. stdout may be captured
# by shell wrappers (for auto-cd) while still being fully interactive.
IS_TERMINAL: bool = os.isatty(0) and os.isatty(2)
CURRENT_BRANCH: BranchName
STACK_BOTTOMS: FrozenSet[BranchName] = frozenset([BranchName("master"), BranchName("main")])
TOP_LEVEL_DIR: str
REPO_TOP_LEVEL_DIR: str

STATE_FILE: str
TMP_STATE_FILE: str

LOGLEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def _normalize_embedded_commit(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{7,40}", v):
        return v
    return None


def get_version_string() -> str:
    package_name = "rockset-stacky"
    try:
        version = importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        version = "dev"

    commit = _normalize_embedded_commit(
        getattr(stacky_build_info, "STACKY_BUILD_COMMIT", None) if stacky_build_info is not None else None
    )
    if commit is None:
        commit = _normalize_embedded_commit(os.environ.get("STACKY_BUILD_COMMIT"))

    # Commit is build metadata embedded into the package version at build time.
    if commit is None:
        match = re.search(r"\+g([0-9a-fA-F]{7,40})$", version)
        commit = match.group(1).lower() if match is not None else None

    if commit is None:
        return f"stacky {version}"
    return f"stacky {version} (commit {commit})"


@dataclasses.dataclass
class StackyConfig:
    skip_confirm: bool = False
    change_to_main: bool = False
    change_to_adopted: bool = False
    share_ssh_session: bool = False
    remote_name: Optional[str] = None
    use_worktree: bool = False
    worktree_root: Optional[str] = None

    def read_one_config(self, config_path: str):
        rawconfig = configparser.ConfigParser()
        rawconfig.read(config_path)
        if rawconfig.has_section("UI"):
            self.skip_confirm = rawconfig.getboolean("UI", "skip_confirm", fallback=self.skip_confirm)
            self.change_to_main = rawconfig.getboolean("UI", "change_to_main", fallback=self.change_to_main)
            self.change_to_adopted = rawconfig.getboolean("UI", "change_to_adopted", fallback=self.change_to_adopted)
            self.share_ssh_session = rawconfig.getboolean("UI", "share_ssh_session", fallback=self.share_ssh_session)
            self.remote_name = rawconfig.get("UI", "remote_name", fallback=self.remote_name)
            self.use_worktree = rawconfig.getboolean("UI", "use_worktree", fallback=self.use_worktree)
            self.worktree_root = rawconfig.get("UI", "worktree_root", fallback=self.worktree_root)


CONFIG: Optional["StackyConfig"] = None


def get_config() -> StackyConfig:
    global CONFIG
    if CONFIG is None:
        CONFIG = read_config()
    return CONFIG


def read_config() -> StackyConfig:
    config = StackyConfig()
    repo_top_level = globals().get("REPO_TOP_LEVEL_DIR") or globals().get("TOP_LEVEL_DIR") or os.getcwd()
    config_paths = [
        f"{repo_top_level}/.stackyconfig",
        os.path.expanduser("~/.stackyconfig"),
    ]

    for p in config_paths:
        if os.path.exists(p):
            config.read_one_config(p)

    return config


def fmt(s: str, *args, color: bool = False, fg=None, bg=None, style=None, **kwargs) -> str:
    s = colors.color(s, fg=fg, bg=bg, style=style) if color else s
    return s.format(*args, **kwargs)


def cout(*args, **kwargs):
    return sys.stderr.write(fmt(*args, color=COLOR_STDERR, **kwargs))


def _log(fn, *args, **kwargs):
    return fn("%s", fmt(*args, color=COLOR_STDERR, **kwargs))


def emit_location(path: str):
    sys.stdout.write(f"{path}\n")
    sys.stdout.flush()


def debug(*args, **kwargs):
    return _log(logging.debug, *args, fg="green", **kwargs)


def info(*args, **kwargs):
    return _log(logging.info, *args, fg="green", **kwargs)


def warning(*args, **kwargs):
    return _log(logging.warning, *args, fg="yellow", **kwargs)


def error(*args, **kwargs):
    return _log(logging.error, *args, fg="red", **kwargs)


class ExitException(BaseException):
    def __init__(self, fmt, *args, **kwargs):
        super().__init__(fmt.format(*args, **kwargs))


def stop_muxed_ssh(remote: str = "origin"):
    config = get_config()
    if config.share_ssh_session:
        hostish = get_remote_type(remote)
        if hostish is not None:
            cmd = gen_ssh_mux_cmd()
            cmd.append("-O")
            cmd.append("exit")
            cmd.append(hostish)
            subprocess.Popen(cmd, stderr=subprocess.DEVNULL)


def die(*args, **kwargs) -> NoReturn:
    # We are taking a wild guess at what is the remote ...
    # TODO (mpatou) fix the assumption about the remote
    stop_muxed_ssh()
    raise ExitException(*args, **kwargs)


def _check_returncode(sp: subprocess.CompletedProcess, cmd: CmdArgs):
    rc = sp.returncode
    if rc == 0:
        return
    stderr = sp.stderr.decode("UTF-8")
    if rc < 0:
        die("Killed by signal {}: {}. Stderr was:\n{}", -rc, shlex.join(cmd), stderr)
    else:
        die("Exited with status {}: {}. Stderr was:\n{}", rc, shlex.join(cmd), stderr)


def run_multiline(cmd: CmdArgs, *, check: bool = True, null: bool = True, out: bool = False) -> Optional[str]:
    debug("Running: {}", shlex.join(cmd))
    sys.stdout.flush()
    sys.stderr.flush()
    sp = subprocess.run(
        cmd,
        stdout=1 if out else subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check:
        _check_returncode(sp, cmd)
    rc = sp.returncode
    if rc != 0:
        return None
    if sp.stdout is None:
        return ""
    return sp.stdout.decode("UTF-8")


def run_always_return(cmd: CmdArgs, **kwargs) -> str:
    out = run(cmd, **kwargs)
    assert out is not None
    return out


def run(cmd: CmdArgs, **kwargs) -> Optional[str]:
    out = run_multiline(cmd, **kwargs)
    return None if out is None else out.strip()


def remove_prefix(s: str, prefix: str) -> str:
    if not s.startswith(prefix):
        die('Invalid string "{}": expected prefix "{}"', s, prefix)
    return s[len(prefix) :]  # noqa: E203


def get_current_branch() -> Optional[BranchName]:
    s = run(CmdArgs(["git", "symbolic-ref", "-q", "HEAD"]))
    if s is not None:
        return BranchName(remove_prefix(s, "refs/heads/"))
    return None


def get_all_branches() -> List[BranchName]:
    branches = run_multiline(CmdArgs(["git", "for-each-ref", "--format", "%(refname:short)", "refs/heads"]))
    assert branches is not None
    return [BranchName(b) for b in branches.split("\n") if b]


def get_real_stack_bottom() -> Optional[BranchName]:  # type: ignore [return]
    """
    return the actual stack bottom for this current repo
    """
    branches = get_all_branches()
    candiates = set()
    for b in branches:
        if b in STACK_BOTTOMS:
            candiates.add(b)

    if len(candiates) == 1:
        return candiates.pop()


def get_stack_parent_branch(branch: BranchName) -> Optional[BranchName]:  # type: ignore [return]
    if branch in STACK_BOTTOMS:
        return None
    p = run(CmdArgs(["git", "config", "branch.{}.merge".format(branch)]), check=False)
    if p is not None:
        p = remove_prefix(p, "refs/heads/")
        return BranchName(p)


def get_stack_parent_commit(branch: BranchName) -> Optional[Commit]:  # type: ignore [return]
    c = run(
        CmdArgs(["git", "rev-parse", "refs/stack-parent/{}".format(branch)]),
        check=False,
    )

    if c is not None:
        return Commit(c)


def infer_stack_parent_branch(branch: BranchName, parent_commit: Commit) -> Optional[BranchName]:
    candidates: List[BranchName] = []
    for candidate in get_all_branches():
        if candidate == branch:
            continue
        merge_base = run(CmdArgs(["git", "merge-base", str(candidate), str(branch)]), check=False)
        if merge_base == parent_commit:
            candidates.append(candidate)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    exact_heads = [candidate for candidate in candidates if get_commit(candidate) == parent_commit]
    if len(exact_heads) == 1:
        return exact_heads[0]

    bottoms = [candidate for candidate in candidates if candidate in STACK_BOTTOMS]
    if len(bottoms) == 1:
        return bottoms[0]
    return None


def get_commit(branch: BranchName) -> Commit:  # type: ignore [return]
    c = run_always_return(CmdArgs(["git", "rev-parse", "refs/heads/{}".format(branch)]), check=False)
    return Commit(c)


def get_gh_repo(remote_name: str = "origin") -> Optional[str]:
    gh_resolved = run(CmdArgs(["git", "config", f"remote.{remote_name}.gh-resolved"]), check=False)
    if gh_resolved is not None and "/" in gh_resolved:
        return gh_resolved

    url = run(CmdArgs(["git", "config", f"remote.{remote_name}.url"]), check=False)
    if url is None:
        return None

    # Support common ssh/https remote URL formats.
    match = re.match(r"^(?:(?:https?|ssh)://(?:git@)?|git@)?[^/:]+[:/]([^/]+)/(.+?)(?:\.git)?/?$", url)
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def maybe_add_gh_repo(cmd: List[str], *, remote_name: str = "origin"):
    repo = get_gh_repo(remote_name)
    if repo is not None:
        cmd.extend(["-R", repo])


def get_pr_info(branch: BranchName, *, full: bool = False, remote_name: str = "origin") -> PRInfos:
    fields = [
        "id",
        "number",
        "state",
        "mergeable",
        "url",
        "title",
        "baseRefName",
        "headRefName",
    ]
    if full:
        fields += ["commits"]
    cmd = [
        "gh",
        "pr",
        "list",
        "--json",
        ",".join(fields),
        "--state",
        "all",
        "--head",
        branch,
    ]
    maybe_add_gh_repo(cmd, remote_name=remote_name)
    data = json.loads(run_always_return(CmdArgs(cmd)))
    raw_infos: List[PRInfo] = data

    infos: Dict[str, PRInfo] = {info["id"]: info for info in raw_infos}
    open_prs: List[PRInfo] = [info for info in infos.values() if info["state"] == "OPEN"]
    if len(open_prs) > 1:
        die(
            "Branch {} has more than one open PR: {}",
            branch,
            ", ".join([str(pr) for pr in open_prs]),
        )  # type: ignore[arg-type]
    return PRInfos(infos, open_prs[0] if open_prs else None)


def get_remote_commit(branch: BranchName) -> Commit | None:
    remote = "origin"
    c = run(
        CmdArgs(["git", "rev-parse", "refs/remotes/{}/{}".format(remote, branch)]),
        check=False,
    )
    if c is not None:
        c = Commit(c)
    return c


# (remote, remote_branch, remote_branch_commit)
def get_remote_info(branch: BranchName) -> Tuple[str, BranchName, Optional[Commit]]:
    if branch not in STACK_BOTTOMS:
        remote = run(CmdArgs(["git", "config", "branch.{}.remote".format(branch)]), check=False)
        if remote != ".":
            die("Misconfigured branch {}: remote {}", branch, remote)

    # TODO(tudor): Maybe add a way to change these.
    remote = "origin"
    remote_branch = branch

    commit = get_remote_commit(remote_branch)

    return (remote, BranchName(remote_branch), commit)


class StackBranch:
    def __init__(
        self,
        name: BranchName,
        parent: "StackBranch",
        parent_commit: Commit,
    ):
        self.name = name
        self.parent = parent
        self.parent_commit = parent_commit
        self.children: set["StackBranch"] = set()
        self.commit = get_commit(name)
        self.remote, self.remote_branch, self.remote_commit = get_remote_info(name)
        self.pr_info: Dict[str, PRInfo] = {}
        self.open_pr_info: Optional[PRInfo] = None
        self._pr_info_loaded = False

    def is_synced_with_parent(self):
        return self.parent is None or self.parent_commit == self.parent.commit

    def is_synced_with_remote(self):
        return self.commit == self.remote_commit

    def __repr__(self):
        return f"StackBranch: {self.name} {len(self.children)} {self.commit}"

    def load_pr_info(self):
        if not self._pr_info_loaded:
            self._pr_info_loaded = True
            if self.name not in STACK_BOTTOMS:
                pr_infos = get_pr_info(self.name)
                # FIXME maybe store the whole object and use it elsewhere
                self.pr_info, self.open_pr_info = (
                    pr_infos.all,
                    pr_infos.open,
                )


class StackBranchSet:
    def __init__(self: "StackBranchSet"):
        self.stack: Dict[BranchName, StackBranch] = {}
        self.tops: set[StackBranch] = set()
        self.bottoms: set[StackBranch] = set()

    def add(self, name: BranchName, **kwargs) -> StackBranch:
        if name in self.stack:
            s = self.stack[name]
            assert s.name == name
            for k, v in kwargs.items():
                if getattr(s, k) != v:
                    die(
                        "Mismatched stack: {}: {}={}, expected {}",
                        name,
                        k,
                        getattr(s, k),
                        v,
                    )
        else:
            s = StackBranch(name, **kwargs)
            self.stack[name] = s
            if s.parent is None:
                self.bottoms.add(s)
            self.tops.add(s)
        return s

    def __repr__(self) -> str:
        out = f"StackBranchSet: {self.stack}"
        return out

    def add_child(self, s: StackBranch, child: StackBranch):
        s.children.add(child)
        self.tops.discard(s)


def load_stack_for_given_branch(
    stack: StackBranchSet, branch: BranchName, *, check: bool = True
) -> Tuple[Optional[StackBranch], List[BranchName]]:
    """Given a stack of branch and a branch name,
    update the stack with all the parents of the specified branch
    if the branch is part of an existing stack.
    Return also a list of BranchName of all the branch bellow the specified one
    """
    branches: List[BranchNCommit] = []
    while branch not in STACK_BOTTOMS:
        parent = get_stack_parent_branch(branch)
        parent_commit = get_stack_parent_commit(branch)
        if parent is None and parent_commit is not None:
            inferred_parent = infer_stack_parent_branch(branch, parent_commit)
            if inferred_parent is not None:
                info("Recovered missing stack parent for {} -> {}", branch, inferred_parent)
                set_parent(branch, inferred_parent, set_origin=True)
                parent = inferred_parent
        branches.append(BranchNCommit(branch, parent_commit))
        if not parent or not parent_commit:
            if check:
                die("Branch is not in a stack: {}", branch)
            return None, [b.branch for b in branches]
        branch = parent

    branches.append(BranchNCommit(branch, None))
    top = None
    for b in reversed(branches):
        n = stack.add(
            b.branch,
            parent=top,
            parent_commit=b.parent_commit,
        )
        if top:
            stack.add_child(top, n)
        top = n

    return top, [b.branch for b in branches]


def load_all_stacks(stack: StackBranchSet) -> Optional[StackBranch]:
    """Given a stack return the top of it, aka the bottom of the tree"""
    all_branches = set(get_all_branches())
    current_branch_top = None
    while all_branches:
        b = all_branches.pop()
        top, branches = load_stack_for_given_branch(stack, b, check=False)
        all_branches -= set(branches)
        if top is None:
            if len(branches) > 1:
                # Incomplete (broken) stack
                warning("Broken stack: {}", " -> ".join(branches))
            continue
        if b == CURRENT_BRANCH:
            current_branch_top = top
    return current_branch_top


def make_tree_node(b: StackBranch) -> TreeNode:
    return (b.name, (b, make_subtree(b)))


def make_subtree(b) -> BranchesTree:
    return BranchesTree(dict(make_tree_node(c) for c in sorted(b.children, key=lambda x: x.name)))


def make_tree(b: StackBranch) -> BranchesTree:
    return BranchesTree(dict([make_tree_node(b)]))


def format_name(b: StackBranch, *, colorize: bool) -> str:
    prefix = ""
    severity = 0
    # TODO: Align things so that we have the same prefix length ?
    if not b.is_synced_with_parent():
        prefix += fmt("!", color=colorize, fg="yellow")
        severity = max(severity, 2)
    if not b.is_synced_with_remote():
        prefix += fmt("~", color=colorize, fg="yellow")
    if b.name == CURRENT_BRANCH:
        prefix += fmt("*", color=colorize, fg="cyan")
    else:
        severity = max(severity, 1)
    if prefix:
        prefix += " "
    fg = ["cyan", "green", "yellow", "red"][severity]
    suffix = ""
    if b.open_pr_info:
        suffix += " "
        suffix += fmt("(#{})", b.open_pr_info["number"], color=colorize, fg="blue")
        suffix += " "
        suffix += fmt("{}", b.open_pr_info["title"], color=colorize, fg="blue")
    return prefix + fmt("{}", b.name, color=colorize, fg=fg) + suffix


def format_tree(tree: BranchesTree, *, colorize: bool = False):
    return {
        format_name(branch, colorize=colorize): format_tree(children, colorize=colorize)
        for branch, children in tree.values()
    }


# Print upside down, to match our "upstack" / "downstack" nomenclature
_ASCII_TREE_BOX = {
    "UP_AND_RIGHT": "\u250c",
    "HORIZONTAL": "\u2500",
    "VERTICAL": "\u2502",
    "VERTICAL_AND_RIGHT": "\u251c",
}
_ASCII_TREE_STYLE = asciitree.drawing.BoxStyle(gfx=_ASCII_TREE_BOX)
ASCII_TREE = asciitree.LeftAligned(draw=_ASCII_TREE_STYLE)


def print_tree(tree: BranchesTree):
    global ASCII_TREE
    s = ASCII_TREE(format_tree(tree, colorize=COLOR_STDERR))
    lines = s.split("\n")
    print("\n".join(reversed(lines)), file=sys.stderr)


def print_forest(trees: List[BranchesTree]):
    for i, t in enumerate(trees):
        if i != 0:
            print(file=sys.stderr)
        print_tree(t)


def get_all_stacks_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    return BranchesTreeForest([make_tree(b) for b in stack.bottoms])


def get_current_stack_as_forest(stack: StackBranchSet):
    b = stack.stack[CURRENT_BRANCH]
    d: BranchesTree = make_tree(b)
    b = b.parent
    while b:
        d = BranchesTree({b.name: (b, d)})
        b = b.parent
    return [d]


def get_current_upstack_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    b = stack.stack[CURRENT_BRANCH]
    return BranchesTreeForest([make_tree(b)])


def get_current_downstack_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    b = stack.stack[CURRENT_BRANCH]
    d: BranchesTree = BranchesTree({})
    while b:
        d = BranchesTree({b.name: (b, d)})
        b = b.parent
    return BranchesTreeForest([d])


def init_git():
    push_default = run(["git", "config", "remote.pushDefault"], check=False)
    if push_default is not None:
        die("`git config remote.pushDefault` may not be set")
    auth_status = run(["gh", "auth", "status"], check=False)
    if auth_status is None:
        die("`gh` authentication failed")
    global CURRENT_BRANCH
    CURRENT_BRANCH = get_current_branch()


def forest_depth_first(
    forest: BranchesTreeForest,
) -> Generator[StackBranch, None, None]:
    for tree in forest:
        for b in depth_first(tree):
            yield b


def depth_first(tree: BranchesTree) -> Generator[StackBranch, None, None]:
    # This is for the regular forest
    for _, (branch, children) in tree.items():
        yield branch
        for b in depth_first(children):
            yield b


def menu_choose_branch(forest: BranchesTreeForest):
    if not IS_TERMINAL:
        die("May only choose from menu when using a terminal")

    global ASCII_TREE
    s = ""
    lines = []
    for tree in forest:
        s = ASCII_TREE(format_tree(tree))
        lines += [l.rstrip() for l in s.split("\n")]
    lines.reverse()

    initial_index = 0
    for i, l in enumerate(lines):
        if "*" in l:  # lol
            initial_index = i
            break

    menu = TerminalMenu(lines, cursor_index=initial_index)
    idx = menu.show()
    if idx is None:
        die("Aborted")

    branches = list(forest_depth_first(forest))
    branches.reverse()
    return branches[idx]


def load_pr_info_for_forest(forest: BranchesTreeForest):
    for b in forest_depth_first(forest):
        b.load_pr_info()


def cmd_info(stack: StackBranchSet, args):
    forest = get_all_stacks_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def get_worktree_root() -> str:
    config = get_config()
    base = globals().get("REPO_TOP_LEVEL_DIR") or globals().get("TOP_LEVEL_DIR") or os.getcwd()
    root = config.worktree_root or os.path.join(base, ".stacky", "worktrees")
    root = os.path.expanduser(root)
    if not os.path.isabs(root):
        root = os.path.join(base, root)
    return root


def worktree_dir_for_branch(branch: BranchName) -> str:
    safe = str(branch).replace("/", "__")
    return os.path.join(get_worktree_root(), safe)


def _parse_worktree_entries(out: str) -> List[WorktreeEntry]:
    entries: List[WorktreeEntry] = []
    current_path: Optional[str] = None
    current_branch: Optional[BranchName] = None
    for line in out.splitlines():
        if not line:
            if current_path is not None:
                entries.append(WorktreeEntry(path=current_path, branch=current_branch))
            current_path = None
            current_branch = None
            continue
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :].strip()
            current_branch = None
            continue
        if line.startswith("branch ") and current_path is not None:
            ref = line[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                current_branch = BranchName(ref[len("refs/heads/") :])
    if current_path is not None:
        entries.append(WorktreeEntry(path=current_path, branch=current_branch))
    return entries


def _parse_worktree_list(out: str) -> Dict[BranchName, str]:
    worktrees: Dict[BranchName, str] = {}
    for entry in _parse_worktree_entries(out):
        if entry.branch is not None:
            worktrees[entry.branch] = entry.path
    return worktrees


def get_worktree_map() -> Dict[BranchName, str]:
    return {entry.branch: entry.path for entry in get_worktree_entries() if entry.branch is not None}


def get_worktree_entries() -> List[WorktreeEntry]:
    out = run_multiline(CmdArgs(["git", "worktree", "list", "--porcelain"]))
    if out is None:
        return []
    return _parse_worktree_entries(out)


def _is_managed_worktree_path(root: str, path: str) -> bool:
    path_real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    if os.path.dirname(path_real) != root_real:
        return False
    return re.fullmatch(r"checkout-\d+", os.path.basename(path_real)) is not None


def _worktree_pool_index(root: str, path: str) -> Optional[int]:
    path_real = os.path.realpath(path)
    root_real = os.path.realpath(root)
    if os.path.dirname(path_real) != root_real:
        return None
    m = re.fullmatch(r"checkout-(\d+)", os.path.basename(path_real))
    if m is None:
        return None
    return int(m.group(1))


def _next_worktree_pool_path(root: str, existing_paths: List[str]) -> str:
    used_indices = set()
    for path in existing_paths:
        idx = _worktree_pool_index(root, path)
        if idx is not None:
            used_indices.add(idx)
    candidate = 1
    while candidate in used_indices:
        candidate += 1
    return os.path.join(root, f"checkout-{candidate}")


def _find_spare_worktree_path(root: str, entries: List[WorktreeEntry]) -> Optional[str]:
    spares = [entry.path for entry in entries if entry.branch is None and _is_managed_worktree_path(root, entry.path)]
    if not spares:
        return None
    spares.sort(key=lambda p: _worktree_pool_index(root, p) or 10**9)
    return spares[0]


def _get_worktree_reset_branch() -> BranchName:
    main_branch = get_real_stack_bottom()
    if main_branch is not None:
        return main_branch
    for candidate in ["main", "master"]:
        ref = f"refs/heads/{candidate}"
        if run(CmdArgs(["git", "rev-parse", "--verify", ref]), check=False) is not None:
            return BranchName(candidate)
    die("Cannot find main/master branch for worktree reset")
    return BranchName("main")


def _recycle_worktree_path(path: str):
    root = get_worktree_root()
    if not _is_managed_worktree_path(root, path):
        return
    target = _get_worktree_reset_branch()
    run(CmdArgs(["git", "-C", path, "checkout", "--detach", str(target)]))
    run(CmdArgs(["git", "-C", path, "reset", "--hard", str(target)]))


def _list_spare_worktree_paths(root: str, entries: List[WorktreeEntry]) -> List[str]:
    spares = [entry.path for entry in entries if entry.branch is None and _is_managed_worktree_path(root, entry.path)]
    spares.sort(key=lambda p: _worktree_pool_index(root, p) or 10**9)
    return spares


def ensure_worktree(branch: BranchName, *, create: bool, base: Optional[BranchName] = None) -> str:
    entries = get_worktree_entries()
    existing_path = next((entry.path for entry in entries if entry.branch == branch), None)
    if existing_path:
        return existing_path

    root = get_worktree_root()
    os.makedirs(root, exist_ok=True)
    spare_path = _find_spare_worktree_path(root, entries)
    if spare_path:
        if create:
            if base is None:
                base = CURRENT_BRANCH or get_current_branch()
            if base is None:
                die("Cannot create worktree branch from a detached HEAD")
            run(CmdArgs(["git", "-C", spare_path, "checkout", "-b", str(branch), str(base)]))
        else:
            run(CmdArgs(["git", "-C", spare_path, "checkout", str(branch)]))
        return spare_path

    path = _next_worktree_pool_path(root, [entry.path for entry in entries])

    if create:
        if base is None:
            base = CURRENT_BRANCH or get_current_branch()
        if base is None:
            die("Cannot create worktree branch from a detached HEAD")
        run(CmdArgs(["git", "worktree", "add", "-b", str(branch), path, str(base)]))
    else:
        run(CmdArgs(["git", "worktree", "add", path, str(branch)]))
    return path


def checkout(branch):
    config = get_config()
    if config.use_worktree:
        info("Checking out branch {} using worktree", branch)
        path = ensure_worktree(BranchName(str(branch)), create=False)
        emit_location(path)
        return

    info("Checking out branch {}", branch)
    run(["git", "checkout", branch])
    emit_location(TOP_LEVEL_DIR)


def cmd_branch_up(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    if not b.children:
        info("Branch {} is already at the top of the stack", CURRENT_BRANCH)
        return
    if len(b.children) > 1:
        if not IS_TERMINAL:
            die(
                "Branch {} has multiple children: {}",
                CURRENT_BRANCH,
                ", ".join(c.name for c in b.children),
            )
        cout(
            "Branch {} has {} children, choose one\n",
            CURRENT_BRANCH,
            len(b.children),
            fg="green",
        )
        forest = BranchesTreeForest([BranchesTree({BranchName(c.name): (c, BranchesTree({}))}) for c in b.children])
        child = menu_choose_branch(forest).name
    else:
        child = next(iter(b.children)).name
    checkout(child)


def cmd_branch_down(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        info("Branch {} is already at the bottom of the stack", CURRENT_BRANCH)
        return
    checkout(b.parent.name)


def create_branch(branch):
    config = get_config()
    if config.use_worktree:
        info("Creating worktree branch {}", branch)
        path = ensure_worktree(BranchName(str(branch)), create=True, base=CURRENT_BRANCH)
        # Keep stack metadata consistent with non-worktree branch creation.
        assert CURRENT_BRANCH is not None
        set_parent(BranchName(str(branch)), CURRENT_BRANCH, set_origin=True)
        emit_location(path)
        return

    run(["git", "checkout", "-b", branch, "--track"])
    emit_location(TOP_LEVEL_DIR)


def cmd_branch_new(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    assert b.commit
    name = args.name
    create_branch(name)
    run(CmdArgs(["git", "update-ref", "refs/stack-parent/{}".format(name), b.commit, ""]))


def cmd_branch_checkout(stack: StackBranchSet, args):
    branch_name = args.name
    if branch_name is None:
        forest = get_all_stacks_as_forest(stack)
        branch_name = menu_choose_branch(forest).name
    checkout(branch_name)


def cmd_stack_info(stack: StackBranchSet, args):
    forest = get_current_stack_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_stack_checkout(stack: StackBranchSet, args):
    forest = get_current_stack_as_forest(stack)
    branch_name = menu_choose_branch(forest).name
    checkout(branch_name)


def prompt(message: str, default_value: Optional[str]) -> str:
    cout(message)
    if default_value is not None:
        cout("({})", default_value, fg="gray")
        cout(" ")
    while True:
        sys.stderr.flush()
        r = input().strip()

        if len(r) > 0:
            return r
        if default_value:
            return default_value


def confirm(msg: str = "Proceed?"):
    config = get_config()
    if config.skip_confirm:
        return
    if not os.isatty(0):
        die("Standard input is not a terminal, use --force option to force action")
    print(file=sys.stderr)
    while True:
        cout("{} [yes/no] ", msg, fg="yellow")
        sys.stderr.flush()
        r = input().strip().lower()
        if r == "yes":
            break
        if r == "no":
            die("Not confirmed")
        cout("Please answer yes or no\n", fg="red")


def find_reviewers(b: StackBranch) -> Optional[List[str]]:
    out = run_multiline(
        CmdArgs(
            [
                "git",
                "log",
                "--pretty=format:%b",
                "-1",
                f"{b.name}",
            ]
        ),
    )
    assert out is not None
    for l in out.split("\n"):
        reviewer_match = re.match(r"^reviewers?\s*:\s*(.*)", l, re.I)
        if reviewer_match:
            reviewers = reviewer_match.group(1).split(",")
            logging.debug(f"Found the following reviewers: {', '.join(reviewers)}")
            return reviewers
    return None


def find_issue_marker(name: str) -> Optional[str]:
    match = re.search(r"(?:^|[_-])([A-Z]{3,}[_-]?\d{2,})($|[_-].*)", name)
    if match:
        res = match.group(1)
        if "_" in res:
            return res.replace("_", "-")
        if "-" not in res:
            newmatch = re.match(r"(...)(\d+)", res)
            assert newmatch is not None
            return f"{newmatch.group(1)}-{newmatch.group(2)}"
        return res

    return None


def create_gh_pr(b: StackBranch, prefix: str, *, remote_name: str = "origin"):
    cout("Creating PR for {}\n", b.name, fg="green")
    parent_prefix = ""
    if b.parent.name not in STACK_BOTTOMS:
        parent_prefix = prefix
    cmd = [
        "gh",
        "pr",
        "create",
        "--head",
        f"{prefix}{b.name}",
        "--base",
        f"{parent_prefix}{b.parent.name}",
    ]
    maybe_add_gh_repo(cmd, remote_name=remote_name)
    stdout_is_tty = os.isatty(1)
    if not stdout_is_tty:
        # Newer gh requires title/body (or --fill*) when stdout is not a tty.
        cmd.append("--fill")
        if IS_TERMINAL:
            # In shell-wrapper mode stdout may be captured, but stdin/stderr are
            # interactive; open the editor to preserve interactive PR editing.
            cmd.append("--editor")
    reviewers = find_reviewers(b)
    issue_id = find_issue_marker(b.name)
    if issue_id:
        out = run_multiline(
            CmdArgs(["git", "log", "--pretty=oneline", f"{b.parent.name}..{b.name}"]),
        )
        title = f"[{issue_id}] "
        # Just one line (hence 2 elements with the last one being an empty string when we
        # split on "\"n ?
        # Then use the title of the commit as the title of the PR
        if out is not None and len(out.split("\n")) == 2:
            out = run(
                CmdArgs(
                    [
                        "git",
                        "log",
                        "--pretty=format:%s",
                        "-1",
                        f"{b.name}",
                    ]
                ),
                out=False,
            )
            if out is None:
                out = ""
            if b.name not in out:
                title += out
            else:
                title = out

        if IS_TERMINAL:
            title = prompt(
                (
                    fmt("? ", color=COLOR_STDERR, fg="green")
                    + fmt("Title ", color=COLOR_STDERR, style="bold", fg="white")
                ),
                title,
            )
        cmd.extend(["--title", title.strip()])
    if reviewers:
        logging.debug(f"Adding {len(reviewers)} reviewer(s) to the review")
        for r in reviewers:
            r = r.strip()
            r = r.replace("#", "rockset/")
            if len(r) > 0:
                cmd.extend(["--reviewer", r])

    run(
        CmdArgs(cmd),
        out=True,
    )


def do_push(
    forest: BranchesTreeForest,
    *,
    force: bool = False,
    pr: bool = False,
    remote_name: str = "origin",
):
    start_muxed_ssh(remote_name)
    if pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)
    for b in forest_depth_first(forest):
        if not b.is_synced_with_parent():
            die(
                "Branch {} is not synced with parent {}, sync first",
                b.name,
                b.parent.name,
            )

    # (branch, push, pr_action)
    PR_NONE = 0
    PR_FIX_BASE = 1
    PR_CREATE = 2
    actions = []
    for b in forest_depth_first(forest):
        if not b.parent:
            cout("✓ Not pushing base branch {}\n", b.name, fg="green")
            continue

        push = False
        if b.is_synced_with_remote():
            cout(
                "✓ Not pushing branch {}, synced with remote {}/{}\n",
                b.name,
                b.remote,
                b.remote_branch,
                fg="green",
            )
        else:
            cout("- Will push branch {} to {}/{}\n", b.name, b.remote, b.remote_branch)
            push = True

        pr_action = PR_NONE
        if pr:
            if b.open_pr_info:
                expected_base = b.parent.name
                if b.open_pr_info["baseRefName"] != expected_base:
                    cout(
                        "- Branch {} already has open PR #{}; will change PR base from {} to {}\n",
                        b.name,
                        b.open_pr_info["number"],
                        b.open_pr_info["baseRefName"],
                        expected_base,
                    )
                    pr_action = PR_FIX_BASE
                else:
                    cout(
                        "✓ Branch {} already has open PR #{}\n",
                        b.name,
                        b.open_pr_info["number"],
                        fg="green",
                    )
            else:
                cout("- Will create PR for branch {}\n", b.name)
                pr_action = PR_CREATE

        if not push and pr_action == PR_NONE:
            continue

        actions.append((b, push, pr_action))

    if actions and not force:
        confirm()

    # Figure out if we need to add a prefix to the branch
    # ie. user:foo
    # We should call gh repo set-default before doing that
    val = run(CmdArgs(["git", "config", f"remote.{remote_name}.gh-resolved"]), check=False)
    if val is not None and "/" in val:
        # If there is a "/" in the gh-resolved it means that the repo where
        # the should be created is not the same as the one where the push will
        # be made, we need to add a prefix to the branch in the gh pr command
        val = run_always_return(CmdArgs(["git", "config", f"remote.{remote_name}.url"]))
        prefix = f"{val.split(':')[1].split('/')[0]}:"
    else:
        prefix = ""
    for b, push, pr_action in actions:
        if push:
            cout("Pushing {}\n", b.name, fg="green")
            run(
                CmdArgs(
                    [
                        "git",
                        "push",
                        "-f",
                        b.remote,
                        "{}:{}".format(b.name, b.remote_branch),
                    ]
                ),
                out=True,
            )
        if pr_action == PR_FIX_BASE:
            cout("Fixing PR base for {}\n", b.name, fg="green")
            assert b.open_pr_info is not None
            cmd = [
                "gh",
                "pr",
                "edit",
                str(b.open_pr_info["number"]),
                "--base",
                b.parent.name,
            ]
            maybe_add_gh_repo(cmd, remote_name=remote_name)
            run(
                CmdArgs(cmd),
                out=True,
            )
        elif pr_action == PR_CREATE:
            create_gh_pr(b, prefix, remote_name=remote_name)

    stop_muxed_ssh(remote_name)


def cmd_stack_push(stack: StackBranchSet, args):
    do_push(
        get_current_stack_as_forest(stack),
        force=args.force,
        pr=args.pr,
        remote_name=args.remote_name,
    )


def do_sync(forest: BranchesTreeForest):
    print_forest(forest)

    syncs: List[StackBranch] = []
    sync_names: List[BranchName] = []
    syncs_set: set[StackBranch] = set()
    for b in forest_depth_first(forest):
        if not b.parent:
            cout("✓ Not syncing base branch {}\n", b.name, fg="green")
            continue
        if b.is_synced_with_parent() and b.parent not in syncs_set:
            cout(
                "✓ Not syncing branch {}, already synced with parent {}\n",
                b.name,
                b.parent.name,
                fg="green",
            )
            continue
        syncs.append(b)
        syncs_set.add(b)
        sync_names.append(b.name)
        cout("- Will sync branch {} on top of {}\n", b.name, b.parent.name)

    if not syncs:
        return

    syncs.reverse()
    sync_names.reverse()
    # TODO: use list(syncs_set).reverse() ?
    inner_do_sync(syncs, sync_names)


def set_parent_commit(branch: BranchName, new_commit: Commit, prev_commit: Optional[str] = None):
    cmd = [
        "git",
        "update-ref",
        "refs/stack-parent/{}".format(branch),
        new_commit,
    ]
    if prev_commit is not None:
        cmd.append(prev_commit)
    run(CmdArgs(cmd))


def get_commits_between(a: Commit, b: Commit):
    lines = run_multiline(CmdArgs(["git", "rev-list", "{}..{}".format(a, b)]))
    assert lines is not None
    return [x.strip() for x in lines.split("\n")]


def inner_do_sync(syncs: List[StackBranch], sync_names: List[BranchName]):
    print(file=sys.stderr)
    while syncs:
        with open(TMP_STATE_FILE, "w") as f:
            json.dump({"branch": CURRENT_BRANCH, "sync": sync_names}, f)
        os.replace(TMP_STATE_FILE, STATE_FILE)  # make the write atomic

        b = syncs.pop()
        sync_names.pop()
        if b.is_synced_with_parent():
            cout("{} is already synced on top of {}\n", b.name, b.parent.name)
            continue
        if b.parent.commit in get_commits_between(b.parent_commit, b.commit):
            cout(
                "Recording complete rebase of {} on top of {}\n",
                b.name,
                b.parent.name,
                fg="green",
            )
        else:
            cout("Rebasing {} on top of {}\n", b.name, b.parent.name, fg="green")
            r = run(
                CmdArgs(["git", "rebase", "--onto", b.parent.name, b.parent_commit, b.name]),
                out=True,
                check=False,
            )
            if r is None:
                print(file=sys.stderr)
                die(
                    "Automatic rebase failed. Please complete the rebase (fix conflicts; `git rebase --continue`), then run `stacky continue`"
                )
            b.commit = get_commit(b.name)
        set_parent_commit(b.name, b.parent.commit, b.parent_commit)
        b.parent_commit = b.parent.commit
    run(CmdArgs(["git", "checkout", str(CURRENT_BRANCH)]))


def cmd_stack_sync(stack: StackBranchSet, args):
    do_sync(get_current_stack_as_forest(stack))


def do_commit(stack: StackBranchSet, *, message=None, amend=False, allow_empty=False, edit=True):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        die("Do not commit directly on {}", b.name)
    if not b.is_synced_with_parent():
        die(
            "Branch {} is not synced with parent {}, sync before committing",
            b.name,
            b.parent.name,
        )
    if amend and b.commit == b.parent.commit:
        die("Branch {} has no commits, may not amend", b.name)

    cmd = ["git", "commit"]
    if allow_empty:
        cmd += ["--allow-empty"]
    if amend:
        cmd += ["--amend"]
        if not edit:
            cmd += ["--no-edit"]
    elif not edit:
        die("--no-edit is only supported with --amend")
    if message:
        cmd += ["-m", message]
    run(CmdArgs(cmd), out=True)

    # Sync everything upstack
    b.commit = get_commit(b.name)
    do_sync(get_current_upstack_as_forest(stack))


def cmd_commit(stack: StackBranchSet, args):
    do_commit(
        stack,
        message=args.message,
        amend=args.amend,
        allow_empty=args.allow_empty,
        edit=not args.no_edit,
    )


def cmd_amend(stack: StackBranchSet, args):
    do_commit(stack, amend=True, edit=False)


def cmd_upstack_info(stack: StackBranchSet, args):
    forest = get_current_upstack_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_upstack_push(stack: StackBranchSet, args):
    do_push(
        get_current_upstack_as_forest(stack),
        force=args.force,
        pr=args.pr,
        remote_name=args.remote_name,
    )


def cmd_upstack_sync(stack: StackBranchSet, args):
    do_sync(get_current_upstack_as_forest(stack))


def set_parent(branch: BranchName, target: BranchName, *, set_origin: bool = False):
    if set_origin:
        run(CmdArgs(["git", "config", "branch.{}.remote".format(branch), "."]))

    run(
        CmdArgs(
            [
                "git",
                "config",
                "branch.{}.merge".format(branch),
                "refs/heads/{}".format(target),
            ]
        )
    )


def cmd_upstack_onto(stack: StackBranchSet, args):
    b = stack.stack[CURRENT_BRANCH]
    if not b.parent:
        die("May not restack {}", b.name)
    target = stack.stack[args.target]
    upstack = get_current_upstack_as_forest(stack)
    for ub in forest_depth_first(upstack):
        if ub == target:
            die("Target branch {} is upstack of {}", target.name, b.name)
    b.parent = target
    set_parent(b.name, target.name)

    do_sync(upstack)


def cmd_downstack_info(stack, args):
    forest = get_current_downstack_as_forest(stack)
    if args.pr:
        load_pr_info_for_forest(forest)
    print_forest(forest)


def cmd_downstack_push(stack: StackBranchSet, args):
    do_push(
        get_current_downstack_as_forest(stack),
        force=args.force,
        pr=args.pr,
        remote_name=args.remote_name,
    )


def cmd_downstack_sync(stack: StackBranchSet, args):
    do_sync(get_current_downstack_as_forest(stack))


def get_bottom_level_branches_as_forest(stack: StackBranchSet) -> BranchesTreeForest:
    return BranchesTreeForest(
        [
            BranchesTree(
                {
                    bottom.name: (
                        bottom,
                        BranchesTree({b.name: (b, BranchesTree({})) for b in bottom.children}),
                    )
                }
            )
            for bottom in stack.bottoms
        ]
    )


def get_remote_type(remote: str = "origin") -> Optional[str]:
    out = run_always_return(CmdArgs(["git", "remote", "-v"]))
    for l in out.split("\n"):
        match = re.match(r"^{}\s+(?:ssh://)?([^/]*):(?!//).*\s+\(push\)$".format(remote), l)
        if match:
            sshish_host = match.group(1)
            return sshish_host

    return None


def gen_ssh_mux_cmd() -> List[str]:
    args = [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPersist={MAX_SSH_MUX_LIFETIME}",
        "-o",
        "ControlPath=~/.ssh/stacky-%C",
    ]

    return args


def start_muxed_ssh(remote: str = "origin"):
    config = get_config()
    if not config.share_ssh_session:
        return
    hostish = get_remote_type(remote)
    if hostish is not None:
        info("Creating a muxed ssh connection")
        cmd = gen_ssh_mux_cmd()
        os.environ["GIT_SSH_COMMAND"] = " ".join(cmd)
        cmd.append("-MNf")
        cmd.append(hostish)
        # We don't want to use the run() wrapper because
        # we don't want to wait for the process to finish

        p = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        # Wait a little bit for the connection to establish
        # before carrying on
        while p.poll() is None:
            time.sleep(1)
        if p.returncode != 0:
            if p.stderr is not None:
                error = p.stderr.read()
            else:
                error = b"unknown"
            die(f"Failed to start ssh muxed connection, error was: {error.decode('utf-8').strip()}")


def get_branches_to_delete(forest: BranchesTreeForest) -> List[StackBranch]:
    deletes = []
    for b in forest_depth_first(forest):
        if not b.parent or b.open_pr_info:
            continue
        for pr_info in b.pr_info.values():
            if pr_info["state"] != "MERGED":
                continue
            cout(
                "- Will delete branch {}, PR #{} merged into {}\n",
                b.name,
                pr_info["number"],
                b.parent.name,
            )
            deletes.append(b)
            for c in b.children:
                cout(
                    "- Will reparent branch {} onto {}\n",
                    c.name,
                    b.parent.name,
                )
            break
    return deletes


def delete_branches(stack: StackBranchSet, deletes: List[StackBranch]):
    global CURRENT_BRANCH
    config = get_config()
    # Make sure we're not trying to delete the current branch
    for b in deletes:
        for c in b.children:
            info("Reparenting {} onto {}", c.name, b.parent.name)
            c.parent = b.parent
            set_parent(c.name, b.parent.name)
        info("Deleting {}", b.name)
        if b.name == CURRENT_BRANCH:
            new_branch = next(iter(stack.bottoms))
            info("About to delete current branch, switching to {}", new_branch.name)
            if config.use_worktree:
                # Branches cannot be deleted while checked out in this worktree.
                # Switch the target worktree to the desired branch, then detach
                # this worktree and emit the target location for shell wrappers.
                path = ensure_worktree(new_branch.name, create=False)
                run(CmdArgs(["git", "-C", path, "checkout", str(new_branch.name)]))
                run(CmdArgs(["git", "checkout", "--detach"]))
                current_path = globals().get("TOP_LEVEL_DIR")
                if current_path:
                    _recycle_worktree_path(current_path)
                emit_location(path)
            else:
                run(CmdArgs(["git", "checkout", new_branch.name]))
            CURRENT_BRANCH = new_branch.name
        elif config.use_worktree:
            branch_worktree = get_worktree_map().get(b.name)
            if branch_worktree:
                _recycle_worktree_path(branch_worktree)
        run(CmdArgs(["git", "branch", "-D", b.name]))


def cmd_worktree_gc(stack: StackBranchSet, args):  # noqa: ARG001
    config = get_config()
    if not config.use_worktree:
        info("Worktree pooling is disabled (`use_worktree = false`), nothing to GC")
        return
    if args.max_spares < 0:
        die("Invalid --max-spares {}, expected >= 0", args.max_spares)
    root = get_worktree_root()
    entries = get_worktree_entries()
    spares = _list_spare_worktree_paths(root, entries)
    if len(spares) <= args.max_spares:
        info("Spare worktrees: {}, max allowed: {} (nothing to remove)", len(spares), args.max_spares)
        return
    to_remove = spares[args.max_spares :]
    for path in to_remove:
        info("Removing spare worktree {}", path)
        run(CmdArgs(["git", "worktree", "remove", path]))
    run(CmdArgs(["git", "worktree", "prune"]))


def cmd_update(stack: StackBranchSet, args):
    remote = args.remote_name
    start_muxed_ssh(remote)
    info("Fetching from {}", remote)
    run(CmdArgs(["git", "fetch", remote]))

    # TODO(tudor): We should rebase instead of silently dropping
    # everything you have on local master. Oh well.
    global CURRENT_BRANCH
    for b in stack.bottoms:
        run(
            CmdArgs(
                [
                    "git",
                    "update-ref",
                    "refs/heads/{}".format(b.name),
                    "refs/remotes/{}/{}".format(remote, b.remote_branch),
                ]
            )
        )
        if b.name == CURRENT_BRANCH:
            run(CmdArgs(["git", "reset", "--hard", "HEAD"]))

    # We treat origin as the source of truth for bottom branches (master), and
    # the local repo as the source of truth for everything else. So we can only
    # track PR closure for branches that are direct descendants of master.

    info("Checking if any PRs have been merged and can be deleted")
    forest = get_bottom_level_branches_as_forest(stack)
    load_pr_info_for_forest(forest)

    deletes = get_branches_to_delete(forest)
    if deletes and not args.force:
        confirm()

    delete_branches(stack, deletes)
    stop_muxed_ssh(remote)


def cmd_import(stack: StackBranchSet, args):
    # Importing has to happen based on PR info, rather than local branch
    # relationships, as that's the only place Graphite populates.
    branch = args.name
    branches = []
    bottoms = set(b.name for b in stack.bottoms)
    while branch not in bottoms:
        pr_info = get_pr_info(branch, full=True)
        open_pr = pr_info.open
        info("Getting PR information for {}", branch)
        if open_pr is None:
            die("Branch {} has no open PR", branch)
            # Never reached because the die but makes mypy happy
            assert open_pr is not None
        if open_pr["headRefName"] != branch:
            die(
                "Branch {} is misconfigured: PR #{} head is {}",
                branch,
                open_pr["number"],
                open_pr["headRefName"],
            )
        if not open_pr["commits"]:
            die("PR #{} has no commits", open_pr["number"])
        first_commit = open_pr["commits"][0]["oid"]
        parent_commit = Commit(run_always_return(CmdArgs(["git", "rev-parse", "{}^".format(first_commit)])))
        next_branch = open_pr["baseRefName"]
        info(
            "Branch {}: PR #{}, parent is {} at commit {}",
            branch,
            open_pr["number"],
            next_branch,
            parent_commit,
        )
        branches.append((branch, parent_commit))
        branch = next_branch

    if not branches:
        return

    base_branch = branch
    branches.reverse()

    for b, parent_commit in branches:
        cout(
            "- Will set parent of {} to {} at commit {}\n",
            b,
            branch,
            parent_commit,
        )
        branch = b

    if not args.force:
        confirm()

    branch = base_branch
    for b, parent_commit in branches:
        set_parent(b, branch, set_origin=True)
        set_parent_commit(b, parent_commit)
        branch = b


def get_merge_base(b1: BranchName, b2: BranchName):
    return run(CmdArgs(["git", "merge-base", str(b1), str(b2)]))


def cmd_adopt(stack: StackBranch, args):
    """
    Adopt a branch that is based on the current branch (which must be a
    valid stack bottom or the stack bottom (master or main) will be used
    if change_to_main option is set in the config file
    """
    config = get_config()
    branch = args.name
    global CURRENT_BRANCH
    if CURRENT_BRANCH not in STACK_BOTTOMS:
        # TODO remove that, the initialisation code is already dealing with that in fact
        main_branch = get_real_stack_bottom()

        if config.change_to_main and main_branch is not None:
            if not config.use_worktree:
                run(CmdArgs(["git", "checkout", main_branch]))
            CURRENT_BRANCH = main_branch
        else:
            die(
                "The current branch {} must be a valid stack bottom: {}",
                CURRENT_BRANCH,
                ", ".join(sorted(STACK_BOTTOMS)),
            )
    parent_commit = get_merge_base(CURRENT_BRANCH, branch)
    set_parent(branch, CURRENT_BRANCH, set_origin=True)
    set_parent_commit(branch, parent_commit)
    if config.change_to_adopted:
        run(CmdArgs(["git", "checkout", branch]))


def cmd_rebuild(stack: StackBranch, args):
    config = get_config()
    branch = args.name
    global CURRENT_BRANCH

    main_branch = get_real_stack_bottom()
    assert main_branch is not None

    target_stack: list[BranchName] = []
    while branch != main_branch:
        target_stack.append(branch)
        cout(f"Getting PR info for branch {branch}\n", fg="green")
        pr_info = get_pr_info(branch).open
        if pr_info is None:
            die(f"Branch {branch} has no open PR")
        branch = BranchName(pr_info["baseRefName"])

    target_stack.reverse()

    cout(f"Switching to {main_branch}\n", fg="green")
    run(CmdArgs(["git", "checkout", str(main_branch)]))

    prev = main_branch
    remote = "origin"
    for branch in target_stack:
        cout(f"Creating branch {branch}\n", fg="green")
        run(CmdArgs(["git", "branch", "-f", "--no-track", branch, f"{remote}/{branch}"]))
        set_parent(branch, prev, set_origin=True)
        parent_commit = get_merge_base(branch, prev)
        set_parent_commit(branch, parent_commit)
        prev = branch

    if target_stack and config.change_to_adopted:
        run(CmdArgs(["git", "checkout", target_stack[0]]))


def cmd_land(stack: StackBranchSet, args):
    forest = get_current_downstack_as_forest(stack)
    assert len(forest) == 1
    branches = []
    p = forest[0]
    while p:
        assert len(p) == 1
        _, (b, p) = next(iter(p.items()))
        branches.append(b)
    assert branches
    assert branches[0] in stack.bottoms
    if len(branches) == 1:
        die("May not land {}", branches[0].name)

    b = branches[1]
    if not b.is_synced_with_parent():
        die(
            "Branch {} is not synced with parent {}, sync before landing",
            b.name,
            b.parent.name,
        )
    if not b.is_synced_with_remote():
        die(
            "Branch {} is not synced with remote branch, push local changes before landing",
            b.name,
        )

    b.load_pr_info()
    pr = b.open_pr_info
    if not pr:
        die("Branch {} does not have an open PR", b.name)
        assert pr is not None

    if pr["mergeable"] != "MERGEABLE":
        die(
            "PR #{} for branch {} is not mergeable: {}",
            pr["number"],
            b.name,
            pr["mergeable"],
        )

    if len(branches) > 2:
        cout(
            "The `land` command only lands the bottom-most branch {}; the current stack has {} branches, ending with {}\n",
            b.name,
            len(branches) - 1,
            CURRENT_BRANCH,
            fg="yellow",
        )

    msg = fmt("- Will land PR #{} (", pr["number"], color=COLOR_STDERR)
    msg += fmt("{}", pr["url"], color=COLOR_STDERR, fg="blue")
    msg += fmt(") for branch {}", b.name, color=COLOR_STDERR)
    msg += fmt(" into branch {}\n", b.parent.name, color=COLOR_STDERR)
    sys.stderr.write(msg)

    if not args.force:
        confirm()

    v = run(CmdArgs(["git", "rev-parse", b.name]))
    assert v is not None
    head_commit = Commit(v)
    cmd = ["gh", "pr", "merge", b.name, "--squash", "--match-head-commit", head_commit]
    maybe_add_gh_repo(cmd)
    if args.auto:
        cmd.append("--auto")
    run(CmdArgs(cmd), out=True)
    cout("\n✓ Success! Run `stacky update` to update local state.\n", fg="green")


def main():
    logging.basicConfig(format=_LOGGING_FORMAT, level=logging.INFO)
    try:
        parser = ArgumentParser(description="Handle git stacks")
        parser.add_argument(
            "--version",
            action="version",
            version=get_version_string(),
            help="Print version information",
        )
        parser.add_argument(
            "--log-level",
            default="info",
            choices=LOGLEVELS.keys(),
            help="Set the log level",
        )
        parser.add_argument(
            "--color",
            default="auto",
            choices=["always", "auto", "never"],
            help="Colorize output and error",
        )
        parser.add_argument(
            "--remote-name",
            "-r",
            default=None,
            help="name of the git remote where branches will be pushed",
        )

        subparsers = parser.add_subparsers(required=True, dest="command")

        # continue
        continue_parser = subparsers.add_parser("continue", help="Continue previously interrupted command")
        continue_parser.set_defaults(func=None)

        # down
        down_parser = subparsers.add_parser("down", help="Go down in the current stack (towards master/main)")
        down_parser.set_defaults(func=cmd_branch_down)
        # up
        up_parser = subparsers.add_parser("up", help="Go up in the current stack (away master/main)")
        up_parser.set_defaults(func=cmd_branch_up)
        # info
        info_parser = subparsers.add_parser("info", help="Stack info")
        info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        info_parser.set_defaults(func=cmd_info)

        # commit
        commit_parser = subparsers.add_parser("commit", help="Commit")
        commit_parser.add_argument("-m", help="Commit message", dest="message")
        commit_parser.add_argument("--amend", action="store_true", help="Amend last commit")
        commit_parser.add_argument("--allow-empty", action="store_true", help="Allow empty commit")
        commit_parser.add_argument("--no-edit", action="store_true", help="Skip editor")
        commit_parser.set_defaults(func=cmd_commit)

        # amend
        amend_parser = subparsers.add_parser("amend", help="Shortcut for amending last commit")
        amend_parser.set_defaults(func=cmd_amend)

        # branch
        branch_parser = subparsers.add_parser("branch", aliases=["b"], help="Operations on branches")
        branch_subparsers = branch_parser.add_subparsers(required=True, dest="branch_command")
        branch_up_parser = branch_subparsers.add_parser("up", aliases=["u"], help="Move upstack")
        branch_up_parser.set_defaults(func=cmd_branch_up)

        branch_down_parser = branch_subparsers.add_parser("down", aliases=["d"], help="Move downstack")
        branch_down_parser.set_defaults(func=cmd_branch_down)

        branch_new_parser = branch_subparsers.add_parser("new", aliases=["create"], help="Create a new branch")
        branch_new_parser.add_argument("name", help="Branch name")
        branch_new_parser.set_defaults(func=cmd_branch_new)

        branch_checkout_parser = branch_subparsers.add_parser("checkout", aliases=["co"], help="Checkout a branch")
        branch_checkout_parser.add_argument("name", help="Branch name", nargs="?")
        branch_checkout_parser.set_defaults(func=cmd_branch_checkout)

        # stack
        stack_parser = subparsers.add_parser("stack", aliases=["s"], help="Operations on the full current stack")
        stack_subparsers = stack_parser.add_subparsers(required=True, dest="stack_command")

        stack_info_parser = stack_subparsers.add_parser("info", aliases=["i"], help="Info for current stack")
        stack_info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        stack_info_parser.set_defaults(func=cmd_stack_info)

        stack_push_parser = stack_subparsers.add_parser("push", help="Push")
        stack_push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        stack_push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        stack_push_parser.set_defaults(func=cmd_stack_push)

        stack_sync_parser = stack_subparsers.add_parser("sync", help="Sync")
        stack_sync_parser.set_defaults(func=cmd_stack_sync)

        stack_checkout_parser = stack_subparsers.add_parser(
            "checkout", aliases=["co"], help="Checkout a branch in this stack"
        )
        stack_checkout_parser.set_defaults(func=cmd_stack_checkout)

        # upstack
        upstack_parser = subparsers.add_parser("upstack", aliases=["us"], help="Operations on the current upstack")
        upstack_subparsers = upstack_parser.add_subparsers(required=True, dest="upstack_command")

        upstack_info_parser = upstack_subparsers.add_parser("info", aliases=["i"], help="Info for current upstack")
        upstack_info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        upstack_info_parser.set_defaults(func=cmd_upstack_info)

        upstack_push_parser = upstack_subparsers.add_parser("push", help="Push")
        upstack_push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        upstack_push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        upstack_push_parser.set_defaults(func=cmd_upstack_push)

        upstack_sync_parser = upstack_subparsers.add_parser("sync", help="Sync")
        upstack_sync_parser.set_defaults(func=cmd_upstack_sync)

        upstack_onto_parser = upstack_subparsers.add_parser("onto", aliases=["restack"], help="Restack")
        upstack_onto_parser.add_argument("target", help="New parent")
        upstack_onto_parser.set_defaults(func=cmd_upstack_onto)

        # downstack
        downstack_parser = subparsers.add_parser(
            "downstack", aliases=["ds"], help="Operations on the current downstack"
        )
        downstack_subparsers = downstack_parser.add_subparsers(required=True, dest="downstack_command")

        downstack_info_parser = downstack_subparsers.add_parser(
            "info", aliases=["i"], help="Info for current downstack"
        )
        downstack_info_parser.add_argument("--pr", action="store_true", help="Get PR info (slow)")
        downstack_info_parser.set_defaults(func=cmd_downstack_info)

        downstack_push_parser = downstack_subparsers.add_parser("push", help="Push")
        downstack_push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        downstack_push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        downstack_push_parser.set_defaults(func=cmd_downstack_push)

        downstack_sync_parser = downstack_subparsers.add_parser("sync", help="Sync")
        downstack_sync_parser.set_defaults(func=cmd_downstack_sync)

        # update
        update_parser = subparsers.add_parser("update", help="Update repo")
        update_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        update_parser.set_defaults(func=cmd_update)

        # worktree
        worktree_parser = subparsers.add_parser("worktree", help="Manage stacky worktree pool")
        worktree_subparsers = worktree_parser.add_subparsers(required=True, dest="worktree_command")
        worktree_gc_parser = worktree_subparsers.add_parser("gc", help="Garbage collect spare pooled worktrees")
        worktree_gc_parser.add_argument(
            "--max-spares",
            type=int,
            default=2,
            help="Maximum number of spare pooled worktrees to keep",
        )
        worktree_gc_parser.set_defaults(func=cmd_worktree_gc)

        # import
        import_parser = subparsers.add_parser("import", help="Import Graphite stack")
        import_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        import_parser.add_argument("name", help="Foreign stack top")
        import_parser.set_defaults(func=cmd_import)

        # adopt
        adopt_parser = subparsers.add_parser("adopt", help="Adopt one branch")
        adopt_parser.add_argument("name", help="Branch name")
        adopt_parser.set_defaults(func=cmd_adopt)

        # rebuild
        rebuild_parser = subparsers.add_parser("rebuild", help="Rebuild one stack")
        rebuild_parser.add_argument("name", help="Top of stack branch name")
        rebuild_parser.set_defaults(func=cmd_rebuild)

        # land
        land_parser = subparsers.add_parser("land", help="Land bottom-most PR on current stack")
        land_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        land_parser.add_argument(
            "--auto",
            "-a",
            action="store_true",
            help="Automatically merge after all checks pass",
        )
        land_parser.set_defaults(func=cmd_land)

        # shortcuts
        push_parser = subparsers.add_parser("push", help="Alias for downstack push")
        push_parser.add_argument("--force", "-f", action="store_true", help="Bypass confirmation")
        push_parser.add_argument("--no-pr", dest="pr", action="store_false", help="Skip Create PRs")
        push_parser.set_defaults(func=cmd_downstack_push)

        sync_parser = subparsers.add_parser("sync", help="Alias for stack sync")
        sync_parser.set_defaults(func=cmd_stack_sync)

        checkout_parser = subparsers.add_parser("checkout", aliases=["co"], help="Checkout a branch")
        checkout_parser.add_argument("name", help="Branch name", nargs="?")
        checkout_parser.set_defaults(func=cmd_branch_checkout)

        checkout_parser = subparsers.add_parser("sco", help="Checkout a branch in this stack")
        checkout_parser.set_defaults(func=cmd_stack_checkout)

        args = parser.parse_args()
        logging.basicConfig(format=_LOGGING_FORMAT, level=LOGLEVELS[args.log_level], force=True)

        p = run_always_return(CmdArgs(["git", "rev-parse", "--show-toplevel"]))
        global TOP_LEVEL_DIR
        TOP_LEVEL_DIR = os.path.realpath(p)
        common_git_dir = os.path.realpath(run_always_return(CmdArgs(["git", "rev-parse", "--git-common-dir"])))
        global REPO_TOP_LEVEL_DIR
        if os.path.basename(common_git_dir) == ".git":
            REPO_TOP_LEVEL_DIR = os.path.dirname(common_git_dir)
        else:
            REPO_TOP_LEVEL_DIR = TOP_LEVEL_DIR

        mangled_state_prefix = REPO_TOP_LEVEL_DIR.replace("_", "_U").replace("~", "_T").replace("/", "_S")
        global STATE_FILE
        STATE_FILE = os.path.expanduser(f"~/.stacky.state.{mangled_state_prefix}")

        global TMP_STATE_FILE
        TMP_STATE_FILE = STATE_FILE + ".tmp"

        # Use a separate state file per repo

        global CONFIG
        CONFIG = read_config()
        config = get_config()
        if args.remote_name is None:
            args.remote_name = config.remote_name or "origin"

        global COLOR_STDERR
        global COLOR_STDOUT
        if args.color == "always":
            COLOR_STDERR = True
            COLOR_STDOUT = True
        elif args.color == "never":
            COLOR_STDERR = False
            COLOR_STDOUT = False

        init_git()

        stack = StackBranchSet()
        load_all_stacks(stack)

        global CURRENT_BRANCH
        if args.command == "continue":
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
            except FileNotFoundError as e:  # noqa: F841
                die("No previous command in progress")
            branch = state["branch"]
            run(["git", "checkout", branch])
            CURRENT_BRANCH = branch
            if CURRENT_BRANCH not in stack.stack:
                die("Current branch {} is not in a stack", CURRENT_BRANCH)

            sync_names = state["sync"]
            syncs = [stack.stack[n] for n in sync_names]

            inner_do_sync(syncs, sync_names)
        else:
            # TODO restore the current branch after changing the branch on some commands for
            # instance `info`
            if CURRENT_BRANCH not in stack.stack:
                main_branch = get_real_stack_bottom()

                if config.change_to_main and main_branch is not None:
                    if not config.use_worktree:
                        run(["git", "checkout", main_branch])
                    CURRENT_BRANCH = main_branch
                else:
                    die("Current branch {} is not in a stack", CURRENT_BRANCH)

            get_current_stack_as_forest(stack)
            args.func(stack, args)

        # Success, delete the state file
        try:
            os.remove(STATE_FILE)
        except FileNotFoundError:
            pass
    except ExitException as e:
        error("{}", e.args[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
