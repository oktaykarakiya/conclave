"""The worker sandbox (``execution.worker_sandbox``): an isolated opencode worker runs under
bubblewrap with its own opencode folders.

opencode keeps its config, credentials (``auth.json``), sessions (``opencode.db``) and state in
folders it resolves through xdg-basedir: ``$XDG_CONFIG_HOME``, ``$XDG_DATA_HOME`` and
``$XDG_STATE_HOME``, else ``~/.config``, ``~/.local/share`` and ``~/.local/state``, each followed
by ``opencode``. Its bash tool runs every command with opencode's own environment, so moving
those folders through ``XDG_*`` or ``HOME`` would move the paths of every tool the worker runs.
The sandbox moves nothing: ``bwrap --dev-bind / / <mounts> --die-with-parent -- <command>``
shows the whole host as it is, except for

- ``~/.claude/CLAUDE.md``, which opencode would load as a global instruction: it does not exist
  in the sandbox (:func:`hidden_file_mounts`);
- the operator's opencode config, data and state folders, over which Conclave's own folders for
  the dispatch's model provider are mounted (:func:`provider_folders`), so a worker never sees
  the operator's sessions, global config or credentials and the operator's folders never change.

bubblewrap refuses to mount onto a symlink, and a dotfiles setup often makes these folders
symlinks (``~/.config/opencode`` linking into a dotfiles checkout, say). So every path a mount
names is resolved first (:func:`_real`): a mount lands on the folder a symlink points to, where
the worker, which follows that symlink, finds it.

The cache folder (downloaded tools, ``models.json``) holds nothing of the operator's and is
shared. The mounts exist for the sandboxed processes only; the host does not change, except that
bubblewrap creates a missing mount point (an empty folder). Linux only.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

# The bubblewrap binary to use instead of `bwrap` on PATH (read at each call).
BWRAP_BIN_ENV = "CONCLAVE_BWRAP_BIN"
# The probe runs `true`: anything slower than this is a sandbox that does not work.
_PROBE_TIMEOUT_S = 10.0
_STDERR_TAIL_CHARS = 300
# The fixes every report of an unavailable sandbox names.
_INSTALL_FIX = "install bubblewrap (bwrap) and allow unprivileged user namespaces"
# How bubblewrap starts each of its own messages (on stderr) when it cannot set a sandbox up.
_BWRAP_MESSAGE = "bwrap: "

# Conclave's own opencode folders, under the daemon's home: one folder per model provider, and
# the one the operator's config is read with (a leading "." no provider folder name has).
OPENCODE_HOMES = "opencode-homes"
DISCOVERY_FOLDER = ".discovery"
# The file opencode writes into each config folder that has none (and stops when it cannot).
_GITIGNORE = ".gitignore"
# The key opencode adds to each config file that lacks it, writing the file back in place.
_SCHEMA_KEY = "$schema"


class SandboxError(Exception):
    """Why a sandboxed dispatch cannot start; the message names the cause and the fix."""


@dataclass(frozen=True)
class SandboxStatus:
    """Whether the worker sandbox works on this host (:func:`sandbox_status`)."""

    available: bool
    binary: str  # the bubblewrap binary: its path when found, else the name looked for
    reason: str = ""  # why the sandbox is unavailable ("" when it is available)


def sandbox_status() -> SandboxStatus:
    """Whether the worker sandbox is available: the bubblewrap binary (``$CONCLAVE_BWRAP_BIN``
    when set, read at each call, else ``bwrap`` on ``PATH``) exists and its probe ``<bwrap>
    --dev-bind / / -- true`` exits 0 within 10 s.

    The probe runs once per binary path and its result is kept for the process's lifetime; a
    binary that is not found is looked for again at the next call. Blocking: run it in a thread
    from async code.
    """
    configured = os.environ.get(BWRAP_BIN_ENV)
    name = configured or "bwrap"
    found = shutil.which(name)
    if found is None:
        reason = (
            f"{BWRAP_BIN_ENV}={configured!r} is not an executable file"
            if configured
            else "bubblewrap (bwrap) is not installed: it is not on PATH"
        )
        return SandboxStatus(available=False, binary=name, reason=reason)
    return _probe(os.path.abspath(found))


@functools.cache
def _probe(binary: str) -> SandboxStatus:
    """Run the probe of *binary*, once per path (:func:`sandbox_status`)."""
    command = [binary, "--dev-bind", "/", "/", "--", "true"]
    shown = " ".join(command)
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        reason = f"`{shown}` did not finish within {_PROBE_TIMEOUT_S:g} s"
        return SandboxStatus(available=False, binary=binary, reason=reason)
    except OSError as exc:
        return SandboxStatus(available=False, binary=binary, reason=f"`{shown}` failed: {exc}")
    if done.returncode != 0:
        detail = " ".join(done.stderr.decode("utf-8", errors="replace").split())
        reason = f"`{shown}` exited with {done.returncode}" + (
            f": {detail[-_STDERR_TAIL_CHARS:]}" if detail else ""
        )
        return SandboxStatus(available=False, binary=binary, reason=reason)
    return SandboxStatus(available=True, binary=binary)


def fallback_message(status: SandboxStatus) -> str:
    """What a dispatch under ``worker_sandbox: auto`` reports when *status* is unavailable: the
    cause, what that means and the fixes."""
    return (
        f"The worker sandbox is unavailable ({status.reason}), so isolated opencode workers run "
        "without it, on the operator's own opencode sessions, config and credentials. To fix, "
        f"{_INSTALL_FIX}, or set execution.worker_sandbox to off to silence this."
    )


def required_message(status: SandboxStatus) -> str:
    """Why a dispatch under ``worker_sandbox: required`` cannot start when *status* is
    unavailable, and the two fixes."""
    return (
        "execution.worker_sandbox is required, but the worker sandbox is unavailable "
        f"({status.reason}). To fix, {_INSTALL_FIX}, or set execution.worker_sandbox to auto "
        "or off."
    )


def setup_failure(output: str) -> str | None:
    """The error of a sandboxed run that bubblewrap could not set up, whose *output* (stdout and
    stderr) then starts with bubblewrap's own message: that message, what it means and the fixes.
    ``None`` when *output* starts otherwise (the sandboxed command ran)."""
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first.startswith(_BWRAP_MESSAGE):
        return None
    return (
        f"bubblewrap could not set up the worker sandbox ({first[-_STDERR_TAIL_CHARS:]}), so "
        "opencode did not start. The sandbox mounts Conclave's own opencode folders over this "
        "host's opencode config, data and state folders and hides ~/.claude/CLAUDE.md. To fix, "
        "make the path bubblewrap names mountable, or set execution.worker_sandbox to off for "
        "this project."
    )


def bwrap_command(binary: str, mounts: Sequence[str]) -> tuple[str, ...]:
    """The argv that runs a command in the sandbox, the command's own argv following it: the
    whole host bound at ``/`` (devices included), then *mounts* (bubblewrap options, applied in
    order). The sandbox ends with its parent."""
    return (binary, "--dev-bind", "/", "/", *mounts, "--die-with-parent", "--")


@dataclass(frozen=True)
class OpencodeFolders:
    """An opencode config, data and state folder."""

    config: Path
    data: Path
    state: Path


def home_folder(env: Mapping[str, str]) -> Path:
    """The home folder of a process whose environment is *env* (``os.homedir()``): ``$HOME``,
    else the user's entry in the password database."""
    return Path(env.get("HOME") or os.path.expanduser("~"))


def opencode_folders(env: Mapping[str, str]) -> OpencodeFolders:
    """The opencode folders of a process whose environment is *env*, resolved as opencode
    (xdg-basedir) resolves them: ``$XDG_CONFIG_HOME``, ``$XDG_DATA_HOME`` and ``$XDG_STATE_HOME``
    (an empty value counts as unset), else ``~/.config``, ``~/.local/share`` and
    ``~/.local/state``; each followed by ``opencode``. Symlinks are kept (see :func:`_real`)."""
    home = home_folder(env)

    def base(variable: str, *default: str) -> Path:
        return Path(env.get(variable) or home.joinpath(*default))

    return OpencodeFolders(
        config=base("XDG_CONFIG_HOME", ".config") / "opencode",
        data=base("XDG_DATA_HOME", ".local", "share") / "opencode",
        state=base("XDG_STATE_HOME", ".local", "state") / "opencode",
    )


def opencode_homes(env: Mapping[str, str]) -> Path:
    """``$CONCLAVE_HOME/opencode-homes``: the daemon's home (``$CONCLAVE_HOME``, else
    ``~/.local/share/conclave``, as ``conclave.main.conclave_home`` resolves it), then the folder
    of Conclave's own opencode folders."""
    override = env.get("CONCLAVE_HOME")
    home = (
        Path(override).expanduser()
        if override
        else home_folder(env) / ".local" / "share" / "conclave"
    )
    return home / OPENCODE_HOMES


def provider_folder_name(provider: str) -> str:
    """*provider* (a model's part before its first ``/``) as a folder name that is safe in a path
    and one-to-one: percent-encoded (letters, digits and ``_.-~`` kept), with a leading ``.``
    encoded too, so a name is never hidden, ``.`` or ``..``, or the discovery folder."""
    if not provider:
        raise ValueError("a model provider name is never empty")
    name = quote(provider, safe="")
    return "%2E" + name[1:] if name.startswith(".") else name


def provider_folders(homes: Path, provider: str) -> OpencodeFolders:
    """Conclave's opencode folders for the model provider *provider*: ``<homes>/<provider>/``
    ``config``, ``data`` and ``state``. Dispatches of one provider share them (a session resumed
    after a retry or a usage-limit wait stays on its provider); different providers never do."""
    home = homes / provider_folder_name(provider)
    return OpencodeFolders(config=home / "config", data=home / "data", state=home / "state")


def operator_credentials(env: Mapping[str, str], data: Path) -> dict[str, Any]:
    """The credentials the operator's opencode uses, read as its ``Auth.all()`` reads them: the
    daemon's ``OPENCODE_AUTH_CONTENT`` when it is set and valid JSON, else ``<data>/auth.json``
    (*data* is the operator's opencode data folder). ``{}`` when that is missing or holds no JSON
    object."""
    content = env.get("OPENCODE_AUTH_CONTENT")
    if content:
        try:
            value = json.loads(content)
        except ValueError:
            pass  # opencode ignores an unparsable value and reads the file
        else:
            return value if isinstance(value, dict) else {}
    try:
        value = json.loads((data / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def provider_credential(provider: str, credentials: Mapping[str, Any]) -> dict[str, Any] | None:
    """The credential of *provider* in *credentials* that a sandboxed worker gets: an ``api`` or
    ``wellknown`` entry, else ``None`` (a key in the environment still reaches the worker).
    Raises :class:`SandboxError` for an ``oauth`` entry, which is never copied: opencode refreshes
    such a login and writes it back, which could sign the operator out."""
    entry = credentials.get(provider)
    if not isinstance(entry, dict):
        return None
    kind = entry.get("type")
    if kind == "oauth":
        raise SandboxError(
            f"The operator's opencode credential for provider {provider!r} is an OAuth login, "
            "which the worker sandbox never copies: opencode would refresh it and could sign the "
            f"operator out. To fix, store an API key for {provider!r} instead (opencode auth "
            "login), or set execution.worker_sandbox to off for this project."
        )
    return entry if kind in ("api", "wellknown") else None


def prepare_provider_folders(
    folders: OpencodeFolders,
    provider: str,
    block: dict[str, Any] | None,
    credential: dict[str, Any] | None,
) -> None:
    """Create *folders* (mode 0700, as their parent) and refresh their two files: the config
    folder's ``opencode.json`` holds ``{"provider": {provider: block}}`` and the data folder's
    ``auth.json`` ``{provider: credential}``; each is removed when there is nothing to hold. A
    file is replaced atomically with mode 0600, so concurrent dispatches of one provider, which
    write the same content, never read a partial one.

    opencode adds a ``$schema`` key to a config file that lacks one, writing the file back in
    place: ``opencode.json`` is left as it is when it holds what it should besides that key, so
    concurrent dispatches do not keep rewriting it under each other's reads."""
    home = folders.config.parent
    for folder in (home.parent, home, folders.config, folders.data, folders.state):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_file = folders.config / "opencode.json"
    config = {"provider": {provider: block}} if block is not None else None
    if config is None or not _holds(config_file, config, besides=_SCHEMA_KEY):
        _write_private_json(config_file, config)
    _write_private_json(
        folders.data / "auth.json", {provider: credential} if credential is not None else None
    )


def _holds(path: Path, value: dict[str, Any], *, besides: str) -> bool:
    """Whether *path* is a private file (mode 0600) holding the JSON object *value*, apart from
    its key *besides*."""
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return False
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(held, dict) and {k: v for k, v in held.items() if k != besides} == value


def _write_private_json(path: Path, value: object | None) -> None:
    """Replace *path* atomically with *value* as JSON, mode 0600; remove it when *value* is
    ``None``."""
    if value is None:
        path.unlink(missing_ok=True)
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:  # mkstemp creates it with mode 0600
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def dispatch_mounts(operator: OpencodeFolders, own: OpencodeFolders, home: Path) -> list[str]:
    """The mounts of a sandboxed dispatch: ``CLAUDE.md`` hidden from the home folder *home*'s
    ``.claude`` (:func:`hidden_file_mounts`), then the provider's own folders *own* over the
    operator's opencode folders *operator*, each path resolved (:func:`_real`). The folders come
    last, so that they are what the sandbox shows even where a symlink puts one inside
    ``.claude``. Raises :class:`OSError` when ``.claude`` cannot be read."""
    return [
        *hidden_file_mounts(home / ".claude" / "CLAUDE.md"),
        *_bind("--bind", own.config, operator.config),
        *_bind("--bind", own.data, operator.data),
        *_bind("--bind", own.state, operator.state),
    ]


def prepare_discovery_folders(discovery: Path) -> None:
    """Create the folder *discovery* the operator's opencode config is read with, and its
    ``data`` and ``state`` folders (mode 0700, as their parent)."""
    for folder in (discovery.parent, discovery, discovery / "data", discovery / "state"):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)


def discovery_mounts(operator: OpencodeFolders, discovery: Path) -> list[str]:
    """The mounts under which the operator's opencode config is read: the operator's config
    folder read-only, and *discovery*'s ``data`` and ``state`` folders over the operator's data
    and state folders (:func:`prepare_discovery_folders` creates them), each path resolved
    (:func:`_real`).

    opencode writes a ``.gitignore`` into each of its config folders that has none and stops
    when it cannot. So when the operator's config folder has none (or does not exist), a tmpfs
    takes that write: it is mounted over the folder, and each entry of the folder is mounted back
    read-only (a symlink as the file or folder it points to). Either way nothing is written to
    the operator's files."""
    config = Path(_real(operator.config))
    if os.path.exists(config / _GITIGNORE):
        config_mounts = ["--ro-bind", str(config), str(config)]
    else:
        config_mounts = _tmpfs_mounts(config, bind="--ro-bind-try", symlinks="bind")
    return [
        *config_mounts,
        *_bind("--bind", discovery / "data", operator.data),
        *_bind("--bind", discovery / "state", operator.state),
    ]


def hidden_file_mounts(path: Path) -> list[str]:
    """Mounts under which *path* does not exist while every other entry of its folder stays where
    it is: an empty tmpfs over the folder (resolved, :func:`_real`), then each other entry bound
    back at its own path (a symlink recreated as it is). ``[]`` when *path* does not exist. The
    entries are those of this moment: one created in the sandbox lands on the tmpfs and is lost.
    Raises :class:`OSError` when the folder cannot be read."""
    folder = Path(_real(path.parent))
    if not os.path.lexists(folder / path.name):
        return []
    return _tmpfs_mounts(folder, bind="--bind-try", symlinks="recreate", skip=path.name)


def _real(path: Path) -> str:
    """*path* with every symlink in it resolved, as far as it exists (the rest is kept as it is).

    bubblewrap refuses to mount onto a symlink ("Can't mount on symlink destination"), so every
    path a mount names is given this way. The sandboxed processes still reach a mount through the
    symlink, which leads to it."""
    return os.path.realpath(path)


def _bind(option: str, source: Path, target: Path) -> list[str]:
    """The bubblewrap mount *option* of the folder *source* at *target*, both resolved."""
    return [option, _real(source), _real(target)]


def _tmpfs_mounts(
    folder: Path, *, bind: str, symlinks: Literal["recreate", "bind"], skip: str = ""
) -> list[str]:
    """An empty tmpfs over the resolved folder *folder* (with its mode), then each entry of it but
    *skip* mounted back at its own path with the bubblewrap option *bind*; a symlink is recreated
    as it is, or mounted like any entry (as what it points to, resolved). A folder that does not
    exist gets the tmpfs alone. Raises :class:`OSError` when *folder* cannot be read."""
    if not os.path.isdir(folder):
        return ["--tmpfs", str(folder)]
    mounts = ["--perms", f"{stat.S_IMODE(folder.stat().st_mode):04o}", "--tmpfs", str(folder)]
    with os.scandir(folder) as scan:
        entries = sorted(scan, key=lambda entry: entry.name)
    for entry in entries:
        if entry.name == skip:
            continue
        if symlinks == "recreate" and entry.is_symlink():
            mounts += ["--symlink", os.readlink(entry.path), entry.path]
        else:
            mounts += [bind, _real(Path(entry.path)), entry.path]
    return mounts
