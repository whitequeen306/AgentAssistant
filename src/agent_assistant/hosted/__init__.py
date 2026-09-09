"""Hosted mode: run the agent engine as an MCP stdio server.

This package adds a second entry point (``mcp_server``) that exposes the
existing agent loop + tool registry + confirm framework over the Model
Context Protocol (newline-delimited JSON-RPC on stdio), so a host such as
the LianYu desktop client can drive the engine as a delegated
``computer_task`` tool.

Standalone mode (``agent_assistant.main``) is untouched — hosted mode only
adds new modules and wires the existing global singletons differently at
startup (no UI, no daemon, confirm routed to the MCP client).
"""
