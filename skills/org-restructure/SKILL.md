# /org-restructure

Analyze the current org chart and suggest improvements.

## When to Use
- Human says "reorganize", "restructure", "optimize teams", "rebalance"
- Triggered from Org Chart page quickfire button

## Procedure

1. **Fetch current state**
   ```bash
   curl -s -H "Authorization: Bearer $(cat $PORTAL_DIR/.portal-token)" http://localhost:8097/api/agents/orgchart | python3 -m json.tool
   ```

2. **Analyze**
   - Which departments are overloaded? (too many agents, all active)
   - Which departments are underutilized? (all idle/offline)
   - Are there agents whose capabilities overlap?
   - Are there capability gaps? (work being done that no department covers)
   - Are department leads assigned? Every dept needs one.

3. **Propose changes** — present to the human as a simple list:
   - "Move Agent X from Dept A to Dept B (because...)"
   - "Create new department 'Data Engineering' (because...)"
   - "Merge Dept A and Dept B (because...)"
   - "Promote Agent X to lead of Dept Y (because...)"

4. **On approval** — update the agents database:
   ```python
   # Update agent department
   curl -X POST http://localhost:8097/api/agents/status \
     -H "Content-Type: application/json" \
     -d '{"agent": "agent-id", "status": "idle", "task": "Reassigned to Dept X"}'
   ```
   Note: Full department reassignment requires direct SQLite update to agents.db.

5. **Report** — summarize changes made in plain English.

## Output Format
Always present changes as a before/after comparison the human can understand at a glance. No technical jargon. Use department names and agent names, not IDs.

## Do NOT
- Delete agents without explicit human approval
- Create more than 5 agents at once
- Change the Primary node
