# /org-hire

Create a new agent manifest to fill a capability gap.

## When to Use
- Human says "I need an agent for X", "hire", "staff up", "new agent"
- Triggered from Org Chart page quickfire button

## Procedure

1. **Ask the human** (if not already specified):
   - What should this agent do? (one sentence)
   - Which department should they join?
   - Any specific skills/tools needed?

2. **Check for overlap**
   ```bash
   curl -s -H "Authorization: Bearer $(cat $PORTAL_DIR/.portal-token)" http://localhost:8097/api/agents/orgchart
   ```
   Review existing agents — does someone already cover this? If so, suggest using the existing agent instead.

3. **Generate the agent manifest**
   Create a YAML manifest file at `~/.claude/agents/{agent-id}.md`:

   ```yaml
   ---
   name: {Agent Name}
   description: {One-line description of what this agent does}
   type: specialist
   department: {Department}
   capabilities:
     - {capability 1}
     - {capability 2}
     - {capability 3}
   is_lead: false
   ---

   # {Agent Name}

   ## Role
   {2-3 sentences describing the agent's purpose and how they fit into the team}

   ## Responsibilities
   - {responsibility 1}
   - {responsibility 2}
   - {responsibility 3}

   ## Tools & Access
   - {what tools/APIs/systems this agent needs}

   ## Escalation
   - Reports to: {department lead name}
   - Escalates to: {who to contact when stuck}
   ```

4. **Register via API** (creates both .md manifest AND database entry)
   ```bash
   TOKEN=$(cat $PORTAL_DIR/.portal-token)
   curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     http://localhost:8097/api/agents/create -d '{
       "id": "agent-id-here",
       "name": "Agent Name",
       "description": "What this agent does",
       "department": "Department Name",
       "model": "sonnet",
       "tools": "Read, Write, Edit, Bash, Grep, Glob",
       "prompt": "You are... (full system prompt)",
       "is_lead": false,
       "capabilities": ["Engineering", "Research"]
     }'
   ```
   This writes `~/.claude/agents/{id}.md` (Claude Code can spawn it) AND inserts into agents.db (portal shows it).

5. **Confirm to human** — show the new agent card, which department they joined, and their capabilities in plain English.

## Naming Convention
- Agent IDs: lowercase, hyphenated (e.g., `data-pipeline-lead`)
- Agent names: Title Case, descriptive (e.g., "Pipeline Architect")
- Keep names short — they appear in compact UI cards

## Do NOT
- Create duplicate agents (always check overlap first)
- Create agents without a clear department assignment
- Create lead agents without human approval (is_lead: true requires explicit ask)
- Create more than 3 agents in one session without checking with the human
