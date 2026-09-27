# yourAICIV Portal

The web portal your clients use to work with their own AI. Each client gets
one AI; this portal is its front door: chat, calendar, email, docs, sheets,
their AI team, and a plain-language status page. It also runs the
**7-day free trial**: a visible countdown while it's on, then a
payment screen when it ends.

It is one small Python server plus a React app. Each AI runs its own copy.

```
yourAICIV-react-portal/
├── portal_server.py     Python/Starlette backend: serves the app + /api + /ws
├── trial_gate.py        Trial contract: reads config/trial.json, enforces expiry
├── start.sh             Launcher
├── requirements.txt     Python dependencies
├── tests/               Backend tests (pytest)
├── react-portal/        The web app (React + TypeScript + Vite)
│   ├── src/             Source
│   ├── dist/            Built app (what the server serves)
│   └── README.md        Notes written for the AI itself
├── skills/             Org-chart skills the AI can install (health, hire, restructure)
└── civ-tools/react.py   Lets the AI react to chat messages with emoji
```

---

## Quick start

Requirements: Python 3.10+, Node 18+ (only to rebuild the app), tmux, and
the AI running in a tmux session on the same machine.

```bash
# 1. Python deps
pip3 install -r requirements.txt

# 2. Build the web app (a built copy ships in react-portal/dist; rebuild after changes)
cd react-portal && npm ci && npm run build && cd ..

# 3. Run
./start.sh            # port 8097 by default; ./start.sh 9000 or PORT=9000 ./start.sh
```

On first start the server creates `.portal-token` (the client's access code)
next to `portal_server.py`, readable only by its owner. It is never printed.

**Signing in.** Send the client a link that carries the code once:

```
https://<their portal address>/?token=<contents of .portal-token>
```

The app stores the code in the browser and removes it from the address bar.
The client can also paste the code on the sign-in screen. Treat the code like
a password: anyone who has it can use that AI.

---

## What the client sees

| Nav | Page | What it's for |
|-----|------|---------------|
| Work | **Chat** | Talk to their AI. First visit shows a welcome with four starter tasks. |
| | **Calendar** | What the AI has scheduled |
| | **Email** | The AI's inbox |
| | **Docs** / **Sheets** | Shared documents and spreadsheets |
| Your AI | **AI Team** | The AI's specialist agents |
| | **Saved** | Messages the client bookmarked |
| | **Status** | Online/offline, trial progress, connections |
| | **Settings** | Light/dark, scheduled check-ins, quick prompts, sign out |

**Operator tools** (Settings > Operator tools, not in the main nav): Terminal,
Sessions, Context window, Browser, Feedback. These are for your team when
supporting a client. On a paid install anyone with the access code can open
them. **During a trial (active or expired) the server refuses Terminal,
Sessions and Browser**: their endpoints answer HTTP 403
(`operator_tools_locked`) and their WebSockets close with code 4403. Refused
paths: `/api/panes`, `/api/inject/pane`, `/api/resume`, `/api/browser/*`,
`/ws/terminal`, `/ws/browser`. Context window and Feedback stay available.

Business websites built for a client have their own `/admin` dashboard. This
portal deliberately has no "Clients" tab.

---

## Trial mode (7-day free trial)

Trial mode is driven by one record the birth template writes when the AI is
created (`tools/apply_trial_profile.py apply`):

```
{
  "trial": true,
  "started_at": "2026-10-01T15:00:00Z",
  "duration_days": 7,
  "expires_at": "2026-10-08T15:00:00Z",
  "payment_url": "https://buy.stripe.com/...",
  "brand": "yourAICIV",
  "reseller": "...",
  "model": "MiniMax-M3"
}
```

**Where the portal reads it (one canonical path).** In production the portal
reads the **operator copy** the template publishes outside the civ tree, and
nothing else:

```bash
# at birth (template side, run as root so the copy is root-owned)
export TRIAL_OPERATOR_COPY="/etc/aiciv/$CIV_NAME/trial.json"
python3 tools/apply_trial_profile.py apply --root "$CIV_ROOT"

# for the portal process (read-only bind mount if the portal runs in the container)
export TRIAL_CONFIG_PATH="/etc/aiciv/$CIV_NAME/trial.json"
```

The AI can write its own `config/trial.json`, so that file must never decide
access. If `TRIAL_CONFIG_PATH` is unset (local development), the portal falls
back to `$CIV_ROOT/config/trial.json` (`$CIV_ROOT` defaults to `$HOME`) and
says at startup that this source is civ-writable. The startup log always
names the file it reads. The record is re-read every few seconds, so a
conversion takes effect without a restart.

| State | What happens |
|-------|--------------|
| No record, or `"trial": false` | Not a trial. Nothing is gated. |
| Trial active | Header shows **Day N of 7** with a Subscribe link. Chat and Status show the countdown. Terminal, Sessions and Browser are refused by the server (403 / WS 4403). |
| Record untrusted (see below) | Same as expired: fail closed. |
| Trial expired | The whole portal is replaced by a screen whose only action is the payment button. The server refuses every `/api/*` call except `/api/trial` with **HTTP 402**, and closes `/ws/*` connections with code **4402**. |

Nothing is deleted at expiry. Chats, files and the AI's memory stay on disk.
The AI side (birth template) also stops working and replies with a short note
and the payment link.

**Converting a customer (manual for now):** after payment, run the
template's `convert` against the operator copy:

```bash
python3 tools/apply_trial_profile.py convert --root "$CIV_ROOT" --operator-copy "$TRIAL_OPERATOR_COPY"
```

It writes `"trial": false` to both copies. Access, including the operator
tools, comes back within seconds, with no restart and no new login.
Follow-up: a Stripe webhook (`checkout.session.completed` for the payment
link) that runs `convert` automatically.

**If the record can't be trusted, the portal fails closed.** It treats the
AI as an expired trial (payment screen, 402, operator tools off) and logs
`FAIL CLOSED: <reason>` when:

- the record exists but is unreadable, not valid JSON, not an object, has a
  `"trial"` value other than `true`/`false`, or is a trial with no dates; or
- `TRIAL_CONFIG_PATH` is set, the operator copy is missing, and the AI's own
  `config/trial.json` exists (the AI was born as a trial).

`/api/trial` then also returns `"config_error": true`, and the payment screen
tells an already-paying client to contact support. Fix: restore the operator
copy (or rerun `convert`). With no record and no trial marker the AI is a paid
install and nothing is gated: a paid birth writes no trial file at all.

`GET /api/trial` needs no sign-in (so the payment screen always renders) and
returns only:

```json
{"trial": true, "day": 3, "days_left": 5, "duration_days": 7,
 "expires_at": "2026-10-08T15:00:00Z", "expired": false,
 "payment_url": "https://buy.stripe.com/..."}
```

Only `https://` payment links are ever shown to the browser.

---

## Configuration

Everything is optional. Integrations that aren't configured show as "not
configured" in the app instead of failing. Values can be set in the
environment or in `~/.env` (`KEY=value` lines).

| Variable | Purpose |
|----------|---------|
| `PORT` | Server port (default `8097`) |
| `PORTAL_TOKEN_FILE` | Where the access code lives (default `.portal-token` beside the server) |
| `PORTAL_ALLOWED_ORIGINS` | Extra CORS origins, comma-separated. Not needed when the app and API share an address. |
| `PORTAL_PUBLIC_URL` | The portal's public address, shown to the AI in its self-description |
| `TRIAL_CONFIG_PATH` | The operator copy of the trial record (production trials: required; see Trial mode) |
| `CIV_ROOT` | The AI's civ root (default `$HOME`): dev fallback for the trial record, and where the trial marker is checked |
| `PORTAL_DIR` | For `civ-tools/react.py` only: where the portal is installed. Usually not needed: the running portal writes `~/.portal_dir`, and the fallback is `~/youraiciv_portal`. The birth template's `tools/watchdog.sh` names its own install directory in its `PORTAL_DIR` line; if the portal lives there, it is found through `~/.portal_dir`. |
| `TRIAL_PAYMENT_URL` | Fallback payment link if `trial.json` has none |
| `PORTAL_ENGINE_MANAGED` | `1` = the AI's model login is handled by your model router, so never ask the client to sign in to Claude. Detected automatically when `trial.json` names a non-Claude model or `~/.claude/settings.json` sets `ANTHROPIC_BASE_URL`. |
| `AICIVCAL_URL`, `AICIVCAL_API_KEY` | Calendar service (plus `.aicivcal-calendar-id` beside the server) |
| `AGENTMAIL_API_KEY`, `AGENTMAIL_INBOX` | The AI's email inbox |
| `AGENTDOCS_URL` | Docs service |
| `AGENTSHEETS_URL`, `AGENTSHEETS_API_KEY` | Sheets service |
| `AGENTAUTH_URL`, `AGENTAUTH_PRIVATE_KEY`, `AGENTAUTH_PUBLIC_KEY` | Service sign-in for Docs/Sheets (optional) |
| `BROWSER_URL` | Browser-view service (default `http://localhost:8099`) |

Build-time (set in `react-portal/.env.local` before `npm run build`):

| Variable | Purpose |
|----------|---------|
| `VITE_SUPPORT_URL` | Adds a "Get help" link to the sidebar and Settings |
| `VITE_SUPPORT_LABEL` | Text for that link (default "Get help") |

---

## Brand

Colors, type and radii are CSS variables in
`react-portal/src/styles/tokens.css`; light and dark are both defined there.
The mark is `react-portal/public/favicon.svg`, drawn the same way in
`src/components/brand/BrandMark.tsx`. Product name and tagline are in
`src/utils/brand.ts`.

| Token | Dark | Light | Use |
|-------|------|-------|-----|
| Ember (`--accent-primary`) | `#F08A3E` | `#C2410C` | Actions, focus, brand |
| Pine (`--accent-secondary`) | `#3CC7B0` | `#0F766E` | "Working" and trial signals |
| Ink / Paper (`--bg-primary`) | `#0E1014` | `#F7F5F2` | Page background |

---

## Development

```bash
# Web app with hot reload (proxies /api and /ws to a portal on :8097)
cd react-portal && npm run dev

# Checks
cd react-portal && npm run lint && npm test && npm run build
cd .. && python3 -m pytest tests -q
```

`portal_server.py` serves `react-portal/dist/index.html` at `/` and the
assets under `/react/assets/` (hence `base: '/react/'` in `vite.config.ts`).
There is one web app in this repo: `react-portal/`.

---

## Security notes

- Every `/api` and `/ws` endpoint requires the access code, except `/health`,
  `/api/trial`, and `POST /api/agents/status` from this machine. That last
  one lets the AI's own hook scripts update an agent's display status in AI
  Team without a token. It accepts loopback callers only (127.0.0.1 / ::1,
  and never a request carrying `X-Forwarded-For`, `X-Real-IP` or
  `Forwarded`, since a reverse proxy on the same host would otherwise make
  every visitor look local). Anyone else needs the access code.
- Access codes are compared in constant time.
- Never commit `.portal-token`, `.env`, `*.db` or chat logs. `.gitignore`
  already excludes them.
- The Terminal operator view gives shell-level access to the AI's session.
  On paid installs, only share the access code with the client and your
  support team. During a trial the server refuses it (see Operator tools).
- Background schedulers (calendar check-ins, scheduled tasks) pause while a
  trial is expired, so nothing is typed into the AI after expiry.
