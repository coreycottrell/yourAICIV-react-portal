# /org-health

Run a comprehensive health check across all agents and departments.

## When to Use
- Human says "health check", "how are the teams", "agent status"
- Triggered from Org Chart page quickfire button
- Run automatically as part of daily BOOP (optional)

## Procedure

1. **Fetch stats and org chart**
   ```bash
   TOKEN=$(cat $PORTAL_DIR/.portal-token)
   curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8097/api/agents/stats
   curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8097/api/agents/orgchart
   ```

2. **Analyze per department**
   For each department, compute:
   - Total agents vs active agents (readiness %)
   - Whether a lead is assigned
   - Whether any agent has been "offline" for more than 24h
   - Whether current_task fields are populated (are agents doing anything?)

3. **Generate health report**
   Format as a clear, scannable summary:

   ```
   ## Org Health Report — {date}

   Overall: {total} agents, {active} active ({pct}% readiness)

   ### Healthy (>75% ready)
   - Leadership: 4/5 active ✅
   - Systems & Technology: 3/3 active ✅

   ### Needs Attention (25-75% ready)
   - Marketing: 2/6 active ⚠️ (4 idle — no active campaigns?)

   ### Critical (<25% ready)
   - IT Support: 0/3 active 🔴 (all offline — investigate)

   ### Leaderless Departments
   - Research (no lead assigned)

   ### Stale Agents (offline >24h)
   - agent-name (Dept) — last active 3 days ago
   ```

4. **Suggest actions** for anything in Needs Attention or Critical.

## Do NOT
- Automatically restart or modify agents
- Send alerts to external systems without approval
- Change agent status — this is READ-ONLY analysis
