# Changelog

## 2026-09-30 — Review round 2: the sign-in flow is safe in a live AI session

An independent code review of the Reconnect Claude release found ten problems.
All ten are fixed here (Witness ticket 3383):

- **Closing the dialog now stops the sign-in.** Before, the server kept
  retrying after the owner cancelled, pressing Escape and typing `/login` into
  the AI's session. `POST /api/auth/close` now stops the running flow first,
  and only one sign-in flow can run at a time (a second start is refused).
- **The sign-in code is typed only into Claude's "Paste code here" prompt.**
  If that prompt is not on screen, `POST /api/auth/code` types nothing and
  says so. The page then goes back to "Start sign-in" for a fresh link, never
  to "paste again". Codes that are not one clean token are refused too.
- **Ordinary words in the conversation no longer press keys.** Words like
  "thumbs up", "Would you recommend", "Do you want to trust" or "command not
  found" are only acted on when they newly appear after `/login` and are on the
  visible screen.
- **The portal never runs `pkill -f claude`.** The only Claude it ever stops
  is the `claude /login` it started itself in a shell pane, found by process
  ID. If it cannot tell which process that is, it stops nothing. First-boot
  stops only the Claude in its own pane. Native installs, which show up as
  `…/claude/versions/2.1.x`, are now recognised as Claude.
- **An idle AI is no longer shown as signed out.** An old access-token expiry
  with a refresh token present means "refreshes on its next request", not
  "signed out". It reads signed out only when the API itself reported an
  authentication failure after these credentials were written. On-screen text
  is no longer used as evidence.
- **Nothing is typed into a session that is mid-turn** ("esc to interrupt" on
  screen). A retry presses Escape only when a sign-in screen is actually open.
- **"Already signed in" now closes the dialog** instead of spinning forever.
- **A reconnect only counts a real new sign-in.** New `GET /api/auth/verify`
  needs a new "Login successful" on screen and freshly written, valid
  credentials. A background token refresh changes `expires_at` too, so that
  alone no longer counts.
- **The Status page's "Engine account" row is back.** It shows the account
  e-mail from the credentials, or from `~/.claude.json` `oauthAccount`, and
  never a token.
- **Status checks are cheap.** Transcripts are read incrementally (only the
  new bytes), not re-parsed on every call.

Tests: 223 backend (29 new in `tests/test_review_round2_t3383.py`; the rest
fail on the reviewed commit a8d2edd except for 2 positive controls), 102
frontend (5 new in `src/test/reconnect-review2.test.tsx`, all failing on
a8d2edd).

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
