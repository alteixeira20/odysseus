"""Non-executing Git metadata inspection for untrusted workspaces.

Runtime V2 must never ask the Git executable to inspect a repository selected
by an agent request.  Git's read-looking commands can execute fsmonitor hooks,
textconv drivers, external diff commands, pagers, aliases, and credential
helpers from configuration.  This service therefore treats Git metadata as
bounded, untrusted bytes and never invokes Git at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GitRepositoryIdentity:
    present: bool
    digest: str
    stable: bool
    head: Optional[str] = None


class HardenedGitService:
    """Read bounded repository metadata without parsing executable config."""

    _MAX_METADATA_FILE = 8 * 1024 * 1024
    _MAX_HOOK_FILES = 256
    _METADATA_NAMES = (
        "HEAD",
        "config",
        "config.worktree",
        "index",
        "packed-refs",
        "shallow",
        "info/attributes",
        "info/exclude",
    )

    # These repository-configured values can launch helpers or load more
    # configuration from outside the repository.  Internal inspection still
    # treats them as inert bytes, but a generic process approval cannot be
    # called a strong behavioral snapshot when any are present.
    _EXECUTABLE_CONFIG = re.compile(
        rb"(?im)^\s*(?:"
        rb"\[(?:include(?:if)?|alias|diff|difftool|filter|merge|mergetool|credential|pager|gpg|sequence)\b|"
        rb"(?:core\.)?(?:fsmonitor|hookspath|pager|editor|sshcommand)\s*=|"
        rb"(?:external|textconv|command|clean|smudge|process|helper|program)\s*="
        rb")"
    )

    @staticmethod
    def _bounded_bytes(path: str) -> tuple[bytes, bool]:
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return b"", True
        except OSError:
            return b"", False
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > HardenedGitService._MAX_METADATA_FILE
            ):
                return b"", False
            chunks: list[bytes] = []
            remaining = info.st_size + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) != info.st_size:
                return b"", False
            return raw, True
        except OSError:
            return b"", False
        finally:
            os.close(descriptor)

    @classmethod
    def _hooks_digest(cls, git_dir: str) -> tuple[bytes, bool]:
        """Hash default hook bytes without following links or executing Git."""

        hooks_dir = os.path.join(git_dir, "hooks")
        try:
            directory_info = os.stat(hooks_dir, follow_symlinks=False)
        except FileNotFoundError:
            return hashlib.sha256(b"no-hooks-directory").digest(), True
        except OSError:
            return b"", False
        if not stat.S_ISDIR(directory_info.st_mode):
            return b"", False
        try:
            names = sorted(os.listdir(hooks_dir))
        except OSError:
            return b"", False
        if len(names) > cls._MAX_HOOK_FILES:
            return b"", False
        material = hashlib.sha256(b"odysseus-default-git-hooks-v1\0")
        stable = True
        for name in names:
            if name in {".", ".."} or "/" in name or "\\" in name:
                stable = False
                continue
            raw, file_stable = cls._bounded_bytes(os.path.join(hooks_dir, name))
            stable = stable and file_stable
            material.update(name.encode("utf-8", errors="surrogateescape") + b"\0")
            material.update(hashlib.sha256(raw).digest())
        return material.digest(), stable

    @staticmethod
    def _git_directory(root: str) -> Optional[str]:
        marker = os.path.join(root, ".git")
        try:
            info = os.stat(marker, follow_symlinks=False)
        except OSError:
            return None
        if stat.S_ISDIR(info.st_mode):
            candidate = os.path.realpath(marker)
            try:
                confined = os.path.commonpath((root, candidate)) == root
            except ValueError:
                confined = False
            return candidate if confined else None
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            return None
        # Linked worktree gitdirs may point outside the selected root. Following
        # an attacker-controlled path would turn inspection into an arbitrary
        # host-file read, so external gitdirs are deliberately not followed.
        raw, stable = HardenedGitService._bounded_bytes(marker)
        try:
            text = raw.decode("utf-8", errors="strict").strip()
        except UnicodeError:
            return None
        if not stable:
            return None
        if not text.casefold().startswith("gitdir:"):
            return None
        candidate = os.path.realpath(
            os.path.join(root, text.split(":", 1)[1].strip())
        )
        try:
            if os.path.commonpath((root, candidate)) != root:
                return None
        except ValueError:
            return None
        return candidate if os.path.isdir(candidate) else None

    def inspect(self, root: str) -> GitRepositoryIdentity:
        canonical = os.path.realpath(root)
        marker = os.path.join(canonical, ".git")
        marker_present = os.path.lexists(marker)
        git_dir = self._git_directory(canonical)
        if git_dir is None:
            if marker_present:
                # A linked worktree, symlink, malformed marker, or inaccessible
                # metadata location is not the same thing as a non-repository.
                # Internal reads remain non-executing, but approvals must fail
                # closed because relevant Git config could not be bound without
                # following an attacker-controlled host path.
                return GitRepositoryIdentity(
                    True,
                    hashlib.sha256(b"uninspectable-git-metadata").hexdigest(),
                    False,
                )
            return GitRepositoryIdentity(
                False,
                hashlib.sha256(b"no-git").hexdigest(),
                True,
            )
        material = hashlib.sha256()
        material.update(b"odysseus-nonexecuting-git-v2\0")
        stable = True
        head_text: Optional[str] = None
        for name in self._METADATA_NAMES:
            raw, file_stable = self._bounded_bytes(os.path.join(git_dir, name))
            stable = stable and file_stable
            if name in {"config", "config.worktree"} and raw:
                stable = stable and not bool(self._EXECUTABLE_CONFIG.search(raw))
            material.update(name.encode("ascii") + b"\0")
            material.update(hashlib.sha256(raw).digest())
            if name == "HEAD" and raw:
                head_text = raw.decode("utf-8", errors="replace").strip()[:1024]

        # Resolve a loose HEAD ref as bytes only. No config includes, filters,
        # attributes, object readers, or helper programs are interpreted.
        if head_text and head_text.startswith("ref: "):
            ref = head_text[5:].strip().replace("\\", "/")
            if ref.startswith("refs/") and ".." not in ref.split("/"):
                ref_path = os.path.realpath(os.path.join(git_dir, *ref.split("/")))
                try:
                    confined = os.path.commonpath((git_dir, ref_path)) == git_dir
                except ValueError:
                    confined = False
                if confined:
                    raw, file_stable = self._bounded_bytes(ref_path)
                    stable = stable and file_stable
                    material.update(b"head-ref\0" + hashlib.sha256(raw).digest())
        hooks_digest, hooks_stable = self._hooks_digest(git_dir)
        stable = stable and hooks_stable
        material.update(b"default-hooks\0" + hooks_digest)
        return GitRepositoryIdentity(True, material.hexdigest(), stable, head_text)

    def default_hook_paths(self, root: str) -> tuple[str, ...]:
        """Return confined regular default hooks for interpreter binding.

        Custom ``core.hooksPath`` configuration makes ``inspect().stable``
        false, so callers never need to follow an arbitrary configured path.
        """

        canonical = os.path.realpath(root)
        git_dir = self._git_directory(canonical)
        if git_dir is None:
            return ()
        hooks_dir = os.path.join(git_dir, "hooks")
        try:
            info = os.stat(hooks_dir, follow_symlinks=False)
            names = sorted(os.listdir(hooks_dir))
        except OSError:
            return ()
        if not stat.S_ISDIR(info.st_mode) or len(names) > self._MAX_HOOK_FILES:
            return ()
        paths: list[str] = []
        for name in names:
            if name in {".", ".."} or "/" in name or "\\" in name:
                continue
            path = os.path.join(hooks_dir, name)
            try:
                hook_info = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(hook_info.st_mode) and hook_info.st_mode & 0o111:
                paths.append(path)
        return tuple(paths)


HARDENED_GIT = HardenedGitService()


__all__ = ["GitRepositoryIdentity", "HARDENED_GIT", "HardenedGitService"]
