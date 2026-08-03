from __future__ import annotations

from pathlib import Path
import re


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex anchor, found {count}: {pattern[:160]!r}")
    target.write_text(updated, encoding="utf-8")


# ---------------------------------------------------------------------------
# Cancel only the exact active run, never a newer run reusing the session.
# ---------------------------------------------------------------------------
regex_once(
    "src/agent_runs.py",
    r'''def begin_turn\(\*, session_id: str, owner: Optional\[str\]\) -> TurnLease:.*?\n\n\ndef prepare_turn''',
    '''def begin_turn(*, session_id: str, owner: Optional[str]) -> TurnLease:
    """Supersede unfinished work as soon as a newer user message is accepted."""

    with _RUNS_LOCK:
        previous = _RUNS.get(str(session_id))
        if previous and previous.task and not previous.task.done():
            if previous.execution_context is not None:
                previous.execution_context.cancellation_token.cancel()
            previous.task.cancel()
        return RUN_OWNERSHIP.claim_turn(
            owner_id=str(owner or ""),
            conversation_id=str(session_id),
        )


def prepare_turn''',
)
regex_once(
    "src/agent_runs.py",
    r'''def stop\(session_id: str\) -> bool:.*?\n\n\n__all__ = \[''',
    '''def stop(session_id: str, *, expected_run_id: Optional[str] = None) -> bool:
    """Cancel an in-flight run only when its durable identity matches.

    ``expected_run_id`` prevents an operator request for an older durable run
    from cancelling a newer run that reused the same conversation/session.
    Existing callers that omit it retain the legacy session-scoped behavior.
    """
    with _RUNS_LOCK:
        run = _RUNS.get(str(session_id))
        if not run or not run.task or run.task.done():
            return False
        if expected_run_id is not None:
            actual_run_id = (
                run.execution_context.run_id
                if run.execution_context is not None
                else None
            )
            if actual_run_id != str(expected_run_id):
                return False
        if run.execution_context is not None:
            run.execution_context.cancellation_token.cancel()
        run.task.cancel()
        return True


__all__ = [''',
)
replace_once(
    "src/agent/runtime_v3/operations.py",
    '''        stopped = bool(row["session_id"] and agent_runs.stop(str(row["session_id"])))
''',
    '''        stopped = bool(
            row["session_id"]
            and agent_runs.stop(
                str(row["session_id"]),
                expected_run_id=str(run_id),
            )
        )
''',
)
replace_once(
    "src/agent/runtime_v3/operations.py",
    '''        final_status = "dispatched" if stopped else "completed"
        result = {"active_task_cancelled": stopped}
''',
    '''        relation = (
            self.ledger.session_run_relation(str(row["session_id"]), str(run_id))
            if row["session_id"]
            else "unknown"
        )
        final_status = "dispatched" if stopped else "completed"
        result = {
            "active_task_cancelled": stopped,
            "session_relation": relation,
        }
''',
)
replace_once(
    "src/agent/runtime_v3/operations.py",
    '''    def events(
        self,
        run_id: str,
''',
    '''    def event_window(self, run_id: str, *, owner: str | None) -> dict[str, int]:
        self._owned_row(run_id, owner)
        return self.ledger.event_window(str(run_id))

    def events(
        self,
        run_id: str,
''',
)

# ---------------------------------------------------------------------------
# Enforce durable replay count/byte retention and expose replay gaps.
# ---------------------------------------------------------------------------
replace_once(
    "src/agent/runtime_v3/ledger.py",
    '''from .contracts import EffectClass, EffectLease, EffectStatus, RetryPolicy, RunRecord, RunStatus
''',
    '''from .config import load_runtime_v3_limits
from .contracts import EffectClass, EffectLease, EffectStatus, RetryPolicy, RunRecord, RunStatus
''',
)
replace_once(
    "src/agent/runtime_v3/ledger.py",
    '''                    revision INTEGER NOT NULL DEFAULT 0,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT
''',
    '''                    revision INTEGER NOT NULL DEFAULT 0,
                    base_event_seq INTEGER NOT NULL DEFAULT 1,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    event_bytes INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT
''',
)
replace_once(
    "src/agent/runtime_v3/ledger.py",
    '''                );
            """)

    def create_run''',
    '''                );
            """)
            columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "base_event_seq" not in columns:
                self._conn.execute(
                    "ALTER TABLE agent_runs ADD COLUMN base_event_seq INTEGER NOT NULL DEFAULT 1"
                )
            if "event_bytes" not in columns:
                self._conn.execute(
                    "ALTER TABLE agent_runs ADD COLUMN event_bytes INTEGER NOT NULL DEFAULT 0"
                )
                self._conn.execute(
                    """UPDATE agent_runs SET event_bytes=COALESCE((
                           SELECT SUM(length(CAST(payload_json AS BLOB)))
                           FROM agent_events WHERE agent_events.run_id=agent_runs.run_id
                       ),0)"""
                )

    def create_run''',
)
regex_once(
    "src/agent/runtime_v3/ledger.py",
    r'''    def append_event\(self, run_id: str, event_type: str, payload: Any, \*, max_bytes: int = 1_048_576\) -> int:.*?\n\n    def replay''',
    '''    def append_event(self, run_id: str, event_type: str, payload: Any, *, max_bytes: int = 1_048_576) -> int:
        payload_json = _canonical(payload)
        encoded = payload_json.encode("utf-8")
        if len(encoded) > max_bytes:
            raise ValueError(f"event payload exceeds {max_bytes} bytes")
        limits = load_runtime_v3_limits()
        now = time.time()
        with self._tx() as db:
            row = db.execute(
                "SELECT base_event_seq,last_event_seq,event_bytes FROM agent_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            base_seq = max(1, int(row["base_event_seq"] or 1))
            seq = int(row["last_event_seq"]) + 1
            retained_bytes = int(row["event_bytes"] or 0) + len(encoded)
            db.execute(
                "INSERT INTO agent_events(run_id, seq, event_type, payload_json, payload_sha256, created_at) VALUES(?,?,?,?,?,?)",
                (run_id, seq, event_type, payload_json, hashlib.sha256(encoded).hexdigest(), now),
            )
            while (
                seq - base_seq + 1 > limits.max_replay_events
                or retained_bytes > limits.max_replay_bytes
            ):
                oldest = db.execute(
                    """SELECT seq,length(CAST(payload_json AS BLOB)) AS payload_bytes
                       FROM agent_events WHERE run_id=? ORDER BY seq LIMIT 1""",
                    (run_id,),
                ).fetchone()
                if oldest is None or int(oldest["seq"]) >= seq:
                    break
                db.execute(
                    "DELETE FROM agent_events WHERE run_id=? AND seq=?",
                    (run_id, int(oldest["seq"])),
                )
                retained_bytes = max(0, retained_bytes - int(oldest["payload_bytes"] or 0))
                base_seq = int(oldest["seq"]) + 1
            db.execute(
                """UPDATE agent_runs SET base_event_seq=?,last_event_seq=?,event_bytes=?,
                          heartbeat_at=?,updated_at=?,revision=revision+1 WHERE run_id=?""",
                (base_seq, seq, retained_bytes, now, now, run_id),
            )
            return seq

    def event_window(self, run_id: str) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT base_event_seq,last_event_seq,event_bytes FROM agent_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return {
            "available_from_seq": max(1, int(row["base_event_seq"] or 1)),
            "last_event_seq": int(row["last_event_seq"] or 0),
            "retained_bytes": int(row["event_bytes"] or 0),
        }

    def replay''',
)
replace_once(
    "routes/agent_runtime_routes.py",
    '''            events = get_runtime_operations().events(
                run_id,
                owner=_owner(request),
                after_seq=after_seq,
                limit=limit,
            )
''',
    '''            operations = get_runtime_operations()
            window = operations.event_window(run_id, owner=_owner(request))
            events = operations.events(
                run_id,
                owner=_owner(request),
                after_seq=after_seq,
                limit=limit,
            )
''',
)
replace_once(
    "routes/agent_runtime_routes.py",
    '''            "next_seq": events[-1]["seq"] if events else after_seq,
        }
''',
    '''            "next_seq": events[-1]["seq"] if events else max(
                after_seq,
                window["available_from_seq"] - 1,
            ),
            "available_from_seq": window["available_from_seq"],
            "last_event_seq": window["last_event_seq"],
            "retained_bytes": window["retained_bytes"],
            "truncated": after_seq < window["available_from_seq"] - 1,
        }
''',
)
replace_once(
    "routes/agent_runtime_routes.py",
    '''        async def generate():
            cursor = after_seq
            heartbeat = 0
            while True:
''',
    '''        async def generate():
            window = operations.event_window(run_id, owner=owner)
            cursor = after_seq
            if cursor < window["available_from_seq"] - 1:
                yield "event: replay_gap\\n"
                yield "data: " + json.dumps(
                    {
                        "requested_after_seq": cursor,
                        "available_from_seq": window["available_from_seq"],
                        "last_event_seq": window["last_event_seq"],
                    },
                    separators=(",", ":"),
                ) + "\\n\\n"
                cursor = window["available_from_seq"] - 1
            heartbeat = 0
            while True:
''',
)

# ---------------------------------------------------------------------------
# Native/Docker install of exact lockfile-owned browser MCP.
# ---------------------------------------------------------------------------
replace_once(
    "setup.py",
    '''def main():
''',
    '''def install_node_runtime():
    """Install exact production Node dependencies only when absent or stale."""
    if os.environ.get("ODYSSEUS_SKIP_NODE_RUNTIME_INSTALL", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        print("  [skip] Node runtime install disabled")
        return False

    import json

    package_path = os.path.join(BASE_DIR, "package.json")
    lock_path = os.path.join(BASE_DIR, "package-lock.json")
    if not os.path.isfile(package_path) or not os.path.isfile(lock_path):
        print("  [warn] package.json/package-lock.json missing; browser MCP disabled")
        return False
    with open(package_path, encoding="utf-8") as handle:
        package = json.load(handle)
    required = str(package.get("dependencies", {}).get("@playwright/mcp", "")).strip()
    if not re.fullmatch(r"\\d+\\.\\d+\\.\\d+", required):
        print("  [warn] @playwright/mcp is not pinned to an exact version")
        return False

    installed_manifest = os.path.join(
        BASE_DIR,
        "node_modules",
        "@playwright",
        "mcp",
        "package.json",
    )
    binary = os.path.join(
        BASE_DIR,
        "node_modules",
        ".bin",
        "mcp-server-playwright.cmd" if os.name == "nt" else "mcp-server-playwright",
    )
    try:
        with open(installed_manifest, encoding="utf-8") as handle:
            installed = str(json.load(handle).get("version", ""))
    except (OSError, ValueError):
        installed = ""
    if installed == required and os.path.isfile(binary):
        print(f"  [ok] Browser MCP runtime {required} already installed")
        return True

    npm = shutil.which("npm")
    if not npm:
        print("  [warn] npm unavailable; install Node.js 20+ for browser MCP")
        return False
    print(f"  Installing browser MCP runtime {required} from package-lock.json...")
    subprocess.run(
        [npm, "ci", "--omit=dev", "--ignore-scripts"],
        cwd=BASE_DIR,
        check=True,
        timeout=900,
    )
    if not os.path.isfile(binary):
        raise RuntimeError("npm ci completed but mcp-server-playwright is missing")
    print(f"  [ok] Browser MCP runtime {required} installed")
    return True


def main():
''',
)
replace_once(
    "setup.py",
    '''import platform
import shutil
''',
    '''import platform
import re
import shutil
''',
)
replace_once(
    "setup.py",
    '''    print("\\n2. Environment file...")
    create_env()

    print("\\n3. Checking dependencies...")
''',
    '''    print("\\n2. Browser MCP runtime...")
    try:
        install_node_runtime()
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f"  [warn] Browser MCP runtime install failed: {exc}")

    print("\\n3. Environment file...")
    create_env()

    print("\\n4. Checking dependencies...")
''',
)
replace_once("setup.py", 'print("\\n4. Initializing database...")', 'print("\\n5. Initializing database...")')
replace_once("setup.py", 'print("\\n5. Creating initial admin...")', 'print("\\n6. Creating initial admin...")')

replace_once(
    "Dockerfile",
    '''WORKDIR /app

# Install Python deps first (layer cache). Optional extras (PyMuPDF AGPL, etc.)
''',
    '''WORKDIR /app

# Install exact production Node dependencies from the repository lockfile.
# Application startup never resolves or downloads MCP executable code.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \\
    && test -x node_modules/.bin/mcp-server-playwright \\
    && npm cache clean --force

# Install Python deps first (layer cache). Optional extras (PyMuPDF AGPL, etc.)
''',
)
replace_once(
    "Dockerfile",
    '''# nodejs/npm provide npx for the built-in Browser MCP server.
''',
    '''# nodejs/npm install the lockfile-pinned built-in Browser MCP server.
''',
)

setup_docs = Path("docs/setup.md")
if setup_docs.exists():
    text = setup_docs.read_text(encoding="utf-8")
    if "Node.js 20+" not in text:
        text = text.replace(
            "- Python 3.10+",
            "- Python 3.10+\\n- Node.js 20+ with npm (for the pinned browser MCP runtime)",
            1,
        )
    setup_docs.write_text(text, encoding="utf-8")

env = Path(".env.example")
text = env.read_text(encoding="utf-8")
text = text.replace("ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE=1\\n", "")
if "ODYSSEUS_AGENT_MAX_REPLAY_BYTES" not in text:
    text = text.replace(
        "ODYSSEUS_AGENT_MAX_REPLAY_EVENTS=8192\\n",
        "ODYSSEUS_AGENT_MAX_REPLAY_EVENTS=8192\\nODYSSEUS_AGENT_MAX_REPLAY_BYTES=67108864\\n",
        1,
    )
env.write_text(text, encoding="utf-8")

print("Post-merge Runtime V3 audit repairs applied")
