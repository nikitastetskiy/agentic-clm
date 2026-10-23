"""Run what nobody has reviewed yet as another user, without the pipeline's secrets.

The self-heal run gives a model a shell to repair the repository, then runs the code it wrote: the
tests and a rebuild. On a GitHub runner every step runs as one user with passwordless sudo, so any
process could read the environment of any other (the pipeline's keys and token included) or become
root. Once ci_sandbox_start (ci/lib.sh) has set SANDBOX_USER, those steps run as that user instead: no
sudo, no way into the runner's home (where the checkout the pipeline runs from lives), and an
environment without a single secret. The gateway key is swapped for the stand-ins of
scripts/key_relay.py (SANDBOX_RELAY_ENV). Bob's key cannot be relayed: Bob Shell alone reads it, from its
standard input, and runs no command (lib/bob.py).

Without SANDBOX_USER (a laptop, a pipeline without sudo) commands run as they are, as this user.
"""
from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

from lib import redact

# Never inherited, besides every credential: who the runner is, the files through which a step talks
# to the workflow, and what is rebuilt for the sandbox (the gateway's address goes through the relay).
RUNNER_VARS = ("HOME", "USER", "LOGNAME", "MAIL", "PWD", "OLDPWD", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
               "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "GITHUB_ENV", "GITHUB_OUTPUT",
               "GITHUB_PATH", "GITHUB_STATE", "GITHUB_STEP_SUMMARY", "ICA_BASE_URL", "OPENCODE_CONFIG_CONTENT",
               "OPENCODE_CONFIG_DIR")
RUNNER_PREFIXES = ("ACTIONS_", "SANDBOX_", "SUDO_", "BOBSHELL_", "BASH_FUNC_")


def user(env=None) -> str:
    return (os.environ if env is None else env).get("SANDBOX_USER", "")


def active(env=None) -> bool:
    return bool(user(env))


def stand_ins(env=None) -> dict[str, str]:
    """The key relay's stand-ins for the gateway's address and key (scripts/key_relay.py)."""
    path = (os.environ if env is None else env).get("SANDBOX_RELAY_ENV", "")
    if not path or not Path(path).exists():
        return {}
    return dict(line.split("=", 1) for line in Path(path).read_text(encoding="utf-8").splitlines() if "=" in line)


def environment(env=None) -> dict[str, str]:
    """`env` (default: this process's) as the sandbox gets it: no secret, the relay's stand-ins for the
    gateway, and the sandbox user's own home."""
    env = os.environ if env is None else env
    out = {k: v for k, v in env.items()
           if k not in redact.SECRET_VARS and not redact.SECRET_NAME.search(k) and k not in RUNNER_VARS
           and not k.startswith(RUNNER_PREFIXES)}
    out.update(stand_ins(env))
    name = user(env)
    out.update(HOME=pwd.getpwnam(name).pw_dir, USER=name, LOGNAME=name)
    return out


def command(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> list[str]:
    """`cmd`, run in `cwd` as the sandbox user with exactly `env` (which holds nothing secret: it is
    on the command line)."""
    return ["sudo", "-n", "-u", user(), "--", "env", "-i", *(f"{k}={v}" for k, v in sorted(env.items())),
            "/bin/bash", "-c", 'cd "$1" && shift && exec "$@"', "sandbox", str(cwd), *cmd]


def clear() -> None:
    """End every process of the sandbox user: nothing one step started outlives it."""
    if active():
        subprocess.run(["sudo", "-n", "pkill", "-KILL", "-u", user()], capture_output=True)


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run(cmd) in `cwd`: as the sandbox user with `env` (default: environment()) when there
    is one, else as it is (`env` default: this process's)."""
    if not active():
        return subprocess.run(cmd, cwd=str(cwd), env=env, **kwargs)
    try:
        return subprocess.run(command(cmd, cwd=cwd, env=environment() if env is None else env), **kwargs)
    finally:
        clear()


def give(path: Path) -> None:
    """Make `path` (a directory this user created) the sandbox user's."""
    if active():
        subprocess.run(["sudo", "-n", "chown", "-R", user(), str(path)], check=True, capture_output=True)


def take(path: Path) -> None:
    """Make `path` this user's again (so that it can be removed)."""
    if active():
        subprocess.run(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(path)], capture_output=True)


def is_file(path: Path) -> bool:
    """Whether `path` is a regular file, not a link. The sandbox's files are not this user's to read, so
    root looks."""
    if not active():
        return path.is_file() and not path.is_symlink()
    return subprocess.run(["sudo", "-n", "test", "-f", str(path), "-a", "!", "-L", str(path)],
                          capture_output=True).returncode == 0


def unlink_links(root: Path) -> None:
    """Remove every symbolic link under `root` (the repository has none), so that a tool confined to
    `root` cannot be led out of it by one."""
    run(["find", str(root), "-type", "l", "-delete"], cwd=root, capture_output=True)
