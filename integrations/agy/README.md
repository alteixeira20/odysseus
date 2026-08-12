# AGY Odysseus Integration

This directory contains the AGY plugin/skill bundle for Odysseus.

## User Flow

1. Open **Odysseus Settings > Integrations**.
2. Select the **Agents** category and click **Connect AGY CLI**.
3. Provide a connection name (e.g. "Workstation") and select permissions.
4. Copy the full setup commands shown after creating the CLI connection.
5. Configure the terminal session for your AGY CLI installation:

```bash
export ODYSSEUS_URL=http://your-odysseus-host:7000
export ODYSSEUS_API_TOKEN=ody_generated_token
mkdir -p ~/.gemini/skills/
curl -fsSL -H "Authorization: Bearer $ODYSSEUS_API_TOKEN" $ODYSSEUS_URL/api/agy/plugin.zip -o /tmp/odysseus-agy-skill.zip
python3 -m zipfile -e /tmp/odysseus-agy-skill.zip ~/.gemini/skills/
```

AGY auto-loads skills under `~/.gemini/skills/`, so the `odysseus` skill is available in any session that has `ODYSSEUS_URL` and `ODYSSEUS_API_TOKEN` in its environment.

## What's in the bundle?

- `skills/odysseus/SKILL.md` — the skill definition AGY reads.
- `skills/odysseus/scripts/odysseus_api.py` — small helper that calls the scoped `/api/codex/*` endpoints (these are the canonical scope-gated agent API; the `codex` path is historic and shared by all CLI agent integrations).

## Scope enforcement

Every tool surface is checked server-side in Odysseus, so even if AGY CLI tries to call a forbidden endpoint, it gets `403` until the user enables the matching toggle for that CLI connection.
