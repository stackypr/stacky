`stacky` is a homebrewed tool to manage stacks of PRs. This allows developers to easily manage many smaller, more targeted PRs that depend on each other.


## Installation
You now have the choice on how to do that, we build pre-packaged version of stacky on new releases, they can be found on the [releases](https://github.com/rockset/stacky/releases) page and we also publish a package in `pypi`.

### Pre-packaged

Using `bazel` we provide pre-packaged version, they are self contained and don't require the installation of external modules. Just drop them in a directory that is part of the `$PATH` environment variable make it executable and you are good to go.

There is also a [xar](https://github.com/facebookincubator/xar/) version it should be faster to run but requires to have `xarexec_fuse` installed.

### Pip
```
pip3 install rockset-stacky
```

### Manual
`stacky` requires the following python3 packages installed on the host 
1. asciitree
2. ansicolors
3. simple-term-menu
```
pip3 install asciitree ansicolors simple-term-menu
```

After which `stacky` can be directly run with `./src/stacky/stacky.py`. We would recommend symlinking `stacky.py` into your path so you can use it anywhere


## Accessing Github
Stacky doesn't use any git or Github APIs. It expects `git` and `gh` cli commands to work and be properly configured. For instructions on installing the github cli `gh` please read their [documentation](https://cli.github.com/manual/).

## Usage
`stacky` stores all information locally, within your git repository
Syntax is as follows:
- `stacky info`: show all stacks , add `-pr` if you want to see GitHub PR numbers (slows things down a bit)
- `stacky branch`: per branch commands (shortcut: `stacky b`)
    - `stacky branch up` (`stacky b u`): move down the stack (towards `master`)
    - `stacky branch down` (`stacky b d`): move down the stack (towards `master`)
    - `stacky branch new <name>`: create a new branch on top of the current one
- `stacky commit [-m <message>] [--amend] [--allow-empty]`: wrapper around `git commit` that syncs everything upstack
    - `stacky amend`: will amend currently tracked changes to top commit
- Based on the first argument (`stack` vs `upstack` vs `downstack`), the following commands operate on the entire current stack, everything upstack from the current PR (inclusive), or everything downstack from the current PR:
    - `stacky stack info [--pr]`
    - `stacky stack sync`: sync (rebase) branches in the stack on top of their parents
    - `stacky stack push [--no-pr]`: push to origin, optionally not creating PRs if they don’t exist
- `stacky upstack onto <target>`: restack the current branch (and everything upstack from it) on top of another branch (like `gt us onto`), useful if you’ve made a separate PR that you want to include in your stack
- `stacky continue`: continue an interrupted stacky sync command (because of conflicts)
- `stacky update`: will pull changes from github and update master, and deletes branches that have been merged into master
- `stacky worktree gc [--max-spares N]`: remove extra spare pooled worktrees when using `use_worktree = true`
- `stacky shell setup`: generate completion and wrapper scripts for your shell

The indicators (`*`, `~`, `!`) mean:
- `*` — this is the current branch
- `~` — the branch is not in sync with the remote branch (you should push)
- `!` — the branch is not in sync with its parent in the stack (you should run `stacky stack sync`, which will do some rebases)

```
$ stacky --help
usage: stacky [-h] [--color {always,auto,never}]
              {continue,info,commit,amend,branch,b,stack,s,upstack,us,downstack,ds,update,worktree,shell,import,adopt,land,push,sync,checkout,co,sco} ...

Handle git stacks

positional arguments:
  {continue,info,commit,amend,branch,b,stack,s,upstack,us,downstack,ds,update,worktree,shell,import,adopt,land,push,sync,checkout,co,sco}
    continue            Continue previously interrupted command
    info                Stack info
    commit              Commit
    amend               Shortcut for amending last commit
    branch (b)          Operations on branches
    stack (s)           Operations on the full current stack
    upstack (us)        Operations on the current upstack
    downstack (ds)      Operations on the current downstack
    update              Update repo
    worktree            Manage stacky worktree pool
    shell               Generate shell integration scripts
    adopt               Adopt one branch
    land                Land bottom-most PR on current stack
    push                Alias for downstack push
    sync                Alias for stack sync
    checkout (co)       Checkout a branch
    sco                 Checkout a branch in this stack

optional arguments:
  -h, --help            show this help message and exit
  --color {always,auto,never}
                        Colorize output and error
```

### Sample Workflow 
1. Create a new working branch with `stacky branch new <branch_name>`. 
2. Update files and add files to git tracking like normal (`git add`)
3. Commit updates with `stacky commit -m <commit_message>`
4. Create a stacked branch with `stacky branch new <downstack_branch_name>`
5. Update files and add files in downstack branch (`git add`)
6. `stacky push` will create 2 PRs. Top branch will have a PR against master and bottom branch will have a PR against the top branch.
7. Update the upstack branch and run `stacky commit`. This will rebase changes in the upstack branch to the downstack branch
8. `stacky push` will update both the PRs.

```
$> stacky branch new change_part_1
branch 'change_part_1' set up to track 'master'.
$> touch adding_new_file
$> git add adding_new_file
$> stacky commit -m "Added new file"
[change_part_1 23b102a] Added new file
 1 file changed, 0 insertions(+), 0 deletions(-)
 create mode 100644 adding_new_file
~* change_part_1
✓ Not syncing branch change_part_1, already synced with parent master
$> stacky branch new change_part_2
branch 'change_part_2' set up to track 'change_part_1'.
$> touch second_file
$> git add second_file
$> stacky commit -m "Added second file"
[change_part_2 0805f57] Added second file
 1 file changed, 0 insertions(+), 0 deletions(-)
 create mode 100644 second_file
~* change_part_2
✓ Not syncing branch change_part_2, already synced with parent change_part_1
$> stacky info
 │   ┌── ~* change_part_2
 ├── ~ change_part_1
master
$> stacky push
     ┌── ~* change_part_2
 ┌── ~ change_part_1
master
✓ Not pushing base branch master
- Will push branch change_part_1 to origin/change_part_1
- Will create PR for branch change_part_1
- Will push branch change_part_2 to origin/change_part_2
- Will create PR for branch change_part_2

Proceed? [yes/no] yes
Pushing change_part_1
Creating PR for change_part_1
? Title change part 1
? Body <Received>
? What's next? Submit as draft
https://github.com/rockset/stacky/pull/2
Pushing change_part_2
Creating PR for change_part_2
? Title Added second file
? Body <Received>
? What's next? Submit as draft
https://github.com/rockset/stacky/pull/3
$> git co change_part_1
$> vim adding_new_file
$> git add adding_new_file
$> stacky commit -m "updated new file"
[change_part_1 aa06f71] updated new file
 1 file changed, 1 insertion(+)
 ┌── !~ change_part_2
~* change_part_1
✓ Not syncing branch change_part_1, already synced with parent master
- Will sync branch change_part_2 on top of change_part_1
```

## Tuning

The behavior of `stacky` allow some tuning. You can tune it by creating a `.stackyconfig`
the file has to be either at the top of your repository (ie. next to the `.git` folder) or in the `$HOME` folder.

If both files exists the one in the home folder takes precedence.
The format of that file is following the `ini` format and has the same structure as the `.gitconfig` file.

In the file you have sections and each sections define some parameters.

We currently have the following sections:
 * UI
 * command

List of parameters for each sections:

### UI
 * skip_confirm, boolean with a default value of `False`, set it to `True` to skip confirmation before doing things like reparenting or removing merged branches.
 * change_to_main: boolean with a default value of `False`, by default `stacky` will stop doing action is you are not in a valid stack (ie. a branch that was created or adopted by stacky), when set to `True` `stacky` will first change to `main` or `master` *when* the current branch is not a valid stack.
 * change_to_adopted: boolean with a default value of `False`, when set to `True` `stacky` will change the current branch to the adopted one.
 * share_ssh_session: boolean with a default value of `False`, when set to `True` `stacky` will create a shared `ssh` session to the `github.com` server. This is useful when you are pushing a stack of diff and you have some kind of 2FA on your ssh key like the ed25519-sk.
 * remote_name: string with a default value of `origin`, sets the default git remote used by commands such as `push` and `update`. You can still override it per command with `--remote-name` / `-r`.
 * use_worktree: boolean with a default value of `False`, when set to `True` branch checkout and branch creation use dedicated git worktrees.
 * worktree_root: string with a default value of `.stacky/worktrees`, controls where stacky stores managed worktrees.

### command

Override command prefixes used by Stacky. Each key is the command prefix Stacky would normally run, and each value is the replacement prefix. Stacky appends any remaining arguments from the original command.

For example:

```ini
[command]
git fetch = git cache fetch
```

With this config, a Stacky call that would normally run `git fetch origin` runs `git cache fetch origin` instead. Without an entry, commands use their normal defaults.

## Shell wrapper for worktree auto-cd

When using `use_worktree = true`, `stacky` prints the target directory on `stdout` (for example after `stacky checkout`, `stacky up`, or `stacky branch new`). A CLI process cannot change the parent shell directory directly, so use a shell function wrapper to auto-`cd` when a directory path is returned.

Generate both completion and wrapper scripts:

```bash
stacky shell setup --shell bash
# or:
stacky shell setup --shell zsh
```

The command prints the `source ...` lines to add to your shell config (`~/.bashrc` or `~/.zshrc`).

For manual use:
- `stacky shell completion --shell bash|zsh`
- `stacky shell wrapper --function-name st`

## License
- [MIT License](https://github.com/rockset/stacky/blob/master/LICENSE.txt)
