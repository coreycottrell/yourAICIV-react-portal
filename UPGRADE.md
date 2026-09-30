# Upgrading an existing yourAICIV AI to the Reconnect Claude release

For the AI's own agent, updating its own portal on its human's say-so. New
births get this automatically (the orchestrator clones `main`). The human only
ever sees a finished result, never these steps.

## What changes (portal directory: `/home/aiciv/purebrain_portal`)

| File | Change |
|---|---|
| `portal_server.py` | replaced (real sign-in status, safe sign-in flow, `/api/auth/close`) |
| `react-portal/dist/index.html` | replaced (points at the new bundle) |
| `react-portal/dist/assets/index-*.js`, `index-*.css` | new bundle files added (old ones may stay) |
| `CHANGELOG.md`, `UPGRADE.md` | new, documentation only |

Nothing else changes: `trial_gate.py`, `site_proxy.py`, `env_file.py`,
`start.sh`, `skills/`, `civ-tools/` are identical to the previous release
(`5063321`). `tests/` and `react-portal/src/` are not deployed.

## What to preserve (never overwrite or delete)

- `.portal-token` (the owner's magic-link token)
- `portal_owner.json`, `portal-chat.jsonl`, `portal_uploads/`, `agentmail.db`,
  `scheduled_tasks.json`, `.env`, `~/.env`
- `~/.claude/` (credentials, settings, transcripts), memories, identity files
- **Any local customisation.** Before replacing `portal_server.py`, compare it
  with the released `5063321` copy (sha256 `2ad635e9…`). If it differs, the AI
  has changed its own portal: merge the new release into it instead of
  overwriting, and keep a note of what was merged.

## Steps (portal process restart only)

1. Back up: `cp -a /home/aiciv/purebrain_portal /home/aiciv/purebrain_portal.bak-<utc-stamp>`
2. Get the release: `git clone --depth 1 https://github.com/coreycottrell/yourAICIV-react-portal /tmp/yp`
   (or use the copy delivered to `/from-witness/`).
3. Copy only the files in the table above into `/home/aiciv/purebrain_portal`
   (`cp /tmp/yp/portal_server.py ...`, `cp -r /tmp/yp/react-portal/dist/. .../react-portal/dist/`).
4. `python3 -m py_compile /home/aiciv/purebrain_portal/portal_server.py`
5. Restart **only the portal process** — never the container, never the
   Claude session. The portal runs in the `portal-server` tmux session:
   stop it with Ctrl-C there and start it again the same way
   (`cd /home/aiciv/purebrain_portal && python3 portal_server.py 2>&1 | tee /tmp/portal.log`).
   `PORTAL_PUBLIC_URL` / `TRIAL_CONFIG_PATH` are re-read from `~/.env`.
   The AI's own Claude session is not touched.
6. Verify by effect:
   - `curl -s -H "Authorization: Bearer $(cat .portal-token)" http://127.0.0.1:<port>/api/auth/status`
     now includes a `"reason"` field and matches reality (signed in only if
     Claude really is).
   - The portal header shows **Reconnect Claude** (hard refresh the page).
   - Chat still answers (alive is not serving — send one real message).

## Rollback

Stop the portal process, move `purebrain_portal.bak-<stamp>` back into place,
start the portal process again. Nothing else was changed.
