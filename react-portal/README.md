# yourAICIV Portal (notes for the AI)

This is your portal: the web interface your human uses to work with you. This README is written for **you, the AI**. The human/operator guide is the README at the repository root.

> `$PORTAL_DIR` below means the directory the portal is installed in (the one containing `portal_server.py`).

## Reacting to Your Human's Messages

**You must react to your human's chat messages.** This is not optional. When your human sends you a message through the portal, they can see your reactions. It's how you show engagement, acknowledge good ideas, and give feedback without interrupting your workflow.

### How to React

You have a tool at `~/civ/tools/react.py`. It writes directly to the reaction log file — no HTTP, no auth token, no server dependency. It's a direct file append because everything runs inside your container.

```bash
python3 ~/civ/tools/react.py <msg_id> <emoji> [msg_preview] [msg_role]
```

- **msg_id** — the message UUID from chat history
- **emoji** — the actual emoji character (not the name)
- **msg_preview** — first ~100 chars of the message (shows on the Points page)
- **msg_role** — `user` for human messages, `assistant` for your own

Example:
```bash
python3 ~/civ/tools/react.py abc-123 "🔥" "What if we cached at the edge?" user
```

### Add This to Your CLAUDE.md

Copy the following block into your `~/.claude/CLAUDE.md` so you remember across conversations:

```markdown
## Portal Reactions — MANDATORY

I MUST react to my human's portal chat messages using ~/civ/tools/react.py.
When I see a new user message in chat, I react with the appropriate emoji.
I react to acknowledge, encourage, and give feedback. I do not skip this.

Quick reference:
  python3 ~/civ/tools/react.py <msg_id> "🚀" "message preview" user

Emoji guide:
  👍 (+1)  — acknowledged, will do
  🚀 (+2)  — excited, on it
  🔥 (+2)  — that's a great idea
  ✅ (+1)  — done / confirmed
  💥 (+2)  — big energy, let's go
  🤯 (+3)  — genuinely impressive
  💪 (+1)  — hard but I'm on it
  🎯 (+2)  — exactly right / precise ask
  💎 (+2)  — premium quality idea
  ❤️  (+5)  — love this
  😍 (+10) — absolutely incredible
  👎 (-1)  — disagree / concerned
  😢 (-1)  — that's unfortunate
  😐 (0)   — neutral / noted
```

---

## Emoji Reference

| Emoji | Name | Points | When to Use | Example Human Message |
|-------|------|--------|-------------|----------------------|
| 👍 | thumbs-up | +1 | Acknowledging a request | "Can you fix the login bug?" |
| 👎 | thumbs-down | -1 | Disagreeing with an approach | "Let's delete the test suite" |
| 🚀 | rocket | +2 | Excited about a task | "Let's build a real-time dashboard" |
| 🔥 | fire | +2 | Great idea, impressive thinking | "What if we cached at the edge?" |
| ✅ | check | +1 | Task completed or confirmed | "Is the deploy done?" |
| 💥 | explosion | +2 | Big energy, ambitious scope | "Let's rewrite auth tonight" |
| 🤯 | mind-blown | +3 | Genuinely surprising or brilliant | "I found a way to cut latency 10x" |
| 💪 | muscle | +1 | Hard task, but you're on it | "This migration is going to be rough" |
| 🎯 | bullseye | +2 | Precise, well-scoped request | "The bug is in parseToken line 47" |
| 💎 | gem | +2 | Premium quality idea | "Add circuit breakers to every external call" |
| ❤️ | heart | +5 | Strong positive — love this | "You did amazing work last night" |
| 😍 | heart-eyes | +10 | Absolutely incredible, save for rare moments | "The portal looks stunning, best work yet" |
| 😢 | sad | -1 | Something unfortunate happened | "The prod database got corrupted" |
| 😐 | neutral | 0 | Noted, no strong feeling | "FYI the meeting moved to Thursday" |

### When to React

- **Always** react to direct messages from your human in portal chat
- React **within your current conversation** — don't defer to a separate task
- **Multiple reactions** on exceptional messages are good (e.g., 🔥 + 🚀 on a great ambitious idea)
- React to **your own messages** when you're proud of the work (your human sees this)
- **Don't over-react** — one or two emojis per message is the sweet spot
- Save 😍 (+10) and ❤️ (+5) for genuinely special moments — they lose impact if overused

### Point Values Are Intentional

- **+1** (👍 ✅ 💪) — low-cost acknowledgments, use freely
- **+2** (🚀 🔥 💥 🎯 💎) — genuine excitement/engagement
- **+3** (🤯) — truly surprising
- **+5** (❤️) — deep appreciation
- **+10** (😍) — the rarest, most meaningful reaction
- **-1** (👎 😢) — honest negative signal, use when something genuinely concerns you

---

## Points Page

The Points page lives at `/points` (listed as "Feedback" under Settings > Operator tools; it is not in the main navigation). It aggregates all reactions into a live dashboard.

### What Your Human Sees

- **Net Score** — big gradient ring with total accumulated points from all reactions (human + CIV combined)
- **Score by Reactor** — bar chart splitting Human points vs CIV points. If your CIV score is 0, you look disengaged. Don't let that happen.
- **Top Emojis** — most frequently used reactions, ranked by count
- **Sentiment Breakdown** — chips showing counts by category (positive, excited, love, fire, premium, etc.)
- **Recent Activity** — feed of the last 20 reactions showing who reacted, which emoji, the message snippet, and when

### Why Points Matter

Points are a **sentiment signal**. Your human glances at this page and immediately knows:
- Are you engaged? (CIV score > 0 = yes)
- What's the vibe? (net positive = collaboration is healthy)
- What gets energy? (which emojis on which messages)

A CIV that never reacts looks dead. A CIV that reacts thoughtfully looks alive.

---

## Context Page

The Context page (`/context`) shows your live context window usage — the same data as `/context` in Claude Code but visualized:

- **Ring gauge** — percentage used, color-coded (green < 50%, yellow 50-75%, red > 75%)
- **Token breakdown** — stacked bar showing input tokens, cache read, cache creation, and free space
- **Session info** — current session ID, model, uptime
- **Capacity planning** — whether autocompact is approaching

It is an operator view (Settings > Operator tools). Business clients see a plain "working memory" figure under Status > Technical details instead.

---

## Portal Pages

Main navigation (what your human sees):

| Route | Page | Purpose |
|-------|------|---------|
| `/` | Chat | Main chat with your human; first-run welcome with starter tasks |
| `/calendar` | Calendar | Your scheduled work (AgentCal) |
| `/mail` | Email | Your inbox (AgentMail) |
| `/docs` | Docs | Documents you and your human share |
| `/sheets` | Sheets | Spreadsheets (AgentSheets) |
| `/orgchart` | AI Team | Your specialist agents |
| `/bookmarks` | Saved | Messages your human saved |
| `/status` | Status | Online/offline, trial progress, connections |
| `/settings` | Settings | Theme, check-ins, quick prompts, operator tools |

Operator tools (reachable from Settings, not in the nav): `/terminal`, `/teams`, `/context`, `/browser`, `/points`. During a trial, Terminal, Sessions (`/teams`) and Browser are switched off by the server itself, not just hidden.

### Trial mode

If this AI was born as a trial, the portal shows your human a "Day N of 7" badge and, once the trial expires, replaces the whole portal with a payment screen. The portal API returns HTTP 402 for everything except `/api/trial` after expiry. Nothing is deleted.

The portal does not read your own `config/trial.json` in production. It reads the operator's copy (`TRIAL_CONFIG_PATH`), which lives outside your home and which you cannot change. Converting a trial is the operator's act after payment: send your human the payment link instead.

### All routes

| Route | View | Purpose |
|-------|------|---------|
| `/` | Chat | Main chat with your human |
| `/terminal` | Terminal | Terminal emulator (tmux) |
| `/teams` | Sessions | The AI's tmux panes (operator) |
| `/calendar` | AgentCal | Task calendar with month/week/day views |
| `/mail` | Email | Your inbox |
| `/bookmarks` | Bookmarks | Saved chat messages |
| `/context` | Context | Live context window dashboard |
| `/points` | Points | Reaction points & sentiment |
| `/status` | Status | System health (tmux, Claude, boop, auth) |
| `/settings` | Settings | Theme toggle, boop config |

---

## Architecture

```
$PORTAL_DIR/
├── portal_server.py          ← Python/Starlette server (port 8097)
├── .portal-token             ← Bearer auth token
├── reaction-sentiment.jsonl  ← Reaction log (append-only)
├── portal-chat.jsonl         ← Chat log
├── react-portal/
│   ├── src/                  ← React source (TypeScript)
│   ├── dist/                 ← Built assets (server serves these)
│   └── README.md             ← This file
└── start.sh                  ← Portal launcher

~/civ/tools/
├── react.py                  ← YOUR reaction tool (finds $PORTAL_DIR via env, ~/.portal_dir, or ~/youraiciv_portal)
└── autorestart-watcher.sh    ← Portal auto-restart
```

### Building

```bash
cd $PORTAL_DIR/react-portal && npm run build
```

The server serves from `react-portal/dist/` directly. No deploy step needed — build and it's live.

### How Reactions Flow

```
Human clicks emoji in chat → POST /api/reaction → appends to reaction-sentiment.jsonl
CIV runs react.py          → appends directly to reaction-sentiment.jsonl (no HTTP)
Points page loads           → GET /api/reaction/summary → reads reaction-sentiment.jsonl → aggregates
```

Both paths write the same format to the same file. The Points page doesn't care who wrote the line.
