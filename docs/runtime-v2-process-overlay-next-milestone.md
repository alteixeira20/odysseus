# Runtime V2 process-write overlay: next milestone

The current `PROCESS_WORKSPACE_WRITE` capability is intentionally disabled by
default and independent from transactional `WORKSPACE_WRITE`. When explicitly
approved it grants non-transactional direct process writes to the bound root.
Partial changes can survive command failure, timeout, cancellation, or service
restart; no rollback is claimed.

The next milestone is a disposable overlay/worktree execution design: create a
run-bound writable view, execute the process only inside that view, calculate a
bounded reviewed diff, and commit selected changes through the existing
workspace transaction service. The design must define untracked-file handling,
Git metadata isolation, process descendants, disk quotas, crash recovery,
conflict detection, and cleanup before it can replace the interim capability.
