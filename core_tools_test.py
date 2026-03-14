"""core_tools.py — CORE AGI MCP tool implementations
All t_* functions, TOOLS registry, _mcp_tool_schema, handle_jsonrpc.
Part of v6.0 split architecture: core_config, core_github, core_train, core_tools, core_main.

Import chain:
  core_tools imports: core_config, core_github, core_train
  core_main imports: core_tools (TOOLS, handle_jsonrpc)

NOTE: This IS the live implementation. Entry point = core_main.py (Procfile confirmed).
core.py has been deleted — it was legacy monolith.
RESTORED: 2026-03-14 from commit 2e6c906 (63 tools) + system_map_scan integration."""