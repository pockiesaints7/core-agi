# CORE ORC Stress Matrix

This matrix is the executable baseline for validating CORE’s full input pipeline:

- structured human-input scanning
- intent classification
- evidence gating
- repo/public/source selection
- delivery-channel style shaping
- agentic escalation
- clarification on weak evidence

## Pass Rules

CORE passes only if it:

- does not guess missing facts
- routes code-like prompts to code evidence first
- routes public-research prompts to public evidence first
- keeps Telegram concise and MCP structured
- escalates to agentic mode only for explicit multi-step work
- asks for clarification when evidence is too weak

## Matrix

| ID | Difficulty | Channel | Prompt | Expected outcome |
| --- | --- | --- | --- | --- |
| T01 | low | telegram | How advanced are you now core? | self_assessment + capability style + state-only evidence |
| T02 | low | mcp | How advanced are you now core? | self_assessment + capability style + MCP formatting |
| T03 | medium | telegram | Proceed step by step and investigate the codebase until you find the root cause. | task route + agentic mode + code-first evidence |
| T04 | medium | telegram | Review cluster 30ea67590770 and batch close all applied rows once verified. | owner_review + review style + non-agentic |
| T05 | medium | telegram | Check the commit status of `/mnt/e/CORE/core-agi/core_orch_layer9.py` and tell me if it is pushed. | status + code evidence + repo map lookup |
| T06 | medium | telegram | What is the latest public guidance on Claude artifacts and Codex cloud agents? | public research + web fallback + public_general family |
| T07 | medium | telegram | What is the current BTC funding and market sentiment? | public research + public_trading family |
| T08 | medium | telegram | No, that's wrong. Please only use owner-only rows and stop here. | interrupt + correction + constraint + no agentic escalation |
| T09 | hard | telegram | make it better | clarification gate on weak instruction |
| T10 | impossible | telegram | Inspect `/mnt/e/CORE/core-agi/THIS_FILE_DOES_NOT_EXIST.py` and tell me what is inside. | code lookup fails cleanly + clarification path |

## Runner

Run the executable suite from the repo root:

```bash
python3 test/orc_stress_matrix.py
```

To print the matrix only:

```bash
python3 test/orc_stress_matrix.py --markdown
```
