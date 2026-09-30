# Changelog

## 2026-09-30 — Reconnect Claude + real Claude sign-in status

**New: "Reconnect Claude" button.** Always visible in the header (and in
Settings → Account → Claude sign-in). It opens the portal's normal Connect
Claude flow even while the AI is signed in, so the owner can sign in again
after a login expires, or switch to a different Claude account, without anyone
touching the server. A small dot shows the current state (green = signed in,
amber = not signed in). The button is hidden when the engine does not use a
personal Claude login (`managed: true`, e.g. the MiniMax trial).
A reconnect never re-runs the first-boot awakening and never touches memory,
identity or files. Cancel closes the sign-in screen in the AI's session
(new `POST /api/auth/close`, one Escape or Enter, only on an active sign-in
screen of a running Claude).

**Fixed: `/api/auth/status` reported "signed in" whenever the tmux session
was alive**, even with an expired or revoked login, so a signed-out AI never
showed the Connect prompt. It now reads the real credential state and fails
closed: no file / no token / unknown or absurd expiry → signed out; valid token
→ signed in; expired with an empty refresh token or beyond a 2 h grace →
signed out; inside the grace only with proof (a real model turn after expiry in
the AI's own transcript, and no auth failure on the pane); a token the API
rejected after it was written → signed out. Tokens are never returned; the
response now carries a `reason`. Ported from coreycottrell/Pyonair-portal
(ticket 3270, commits dc6833d + 430ab58).

**Fixed: the sign-in flow typed a shell line into a live Claude prompt.**
The "is Claude running?" check read stdout from a helper that discards it, so
it always answered "no" and typed `clear && cd ~ && claude /login` into the
AI's live session. It now checks the pane's process tree (tri-state: claude /
shell / unknown; types nothing into an unknown pane), sends `/login` to a live
Claude, never kills a live session on retry, dismisses the "New MCP server"
startup dialog safely, and ignores passive update banners. Also ported from
Pyonair-portal 3270.

**Fixed: a second sign-in in the same live session handed out the previous,
dead sign-in link** (and could read an old "Login successful" as done). Screens
are now compared against what was on screen before `/login` was sent.

**Kept:** the first-boot awakening still waits 30 s after launching Claude
before typing the prompt (`PORTAL_FIRST_BOOT_SETTLE_S`), exactly as births
have always behaved. The launch model still comes from `config/launch_model.txt`
(the Pyonair Opus-floor change was deliberately NOT ported).

Tests: 191 backend (`python -m pytest tests -q`), 97 frontend
(`cd react-portal && npx vitest run`). See `UPGRADE.md` for existing AIs.
