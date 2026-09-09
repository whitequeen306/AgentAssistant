"""PyInstaller entry point for the hosted MCP engine (lean build).

Kept as a top-level script (PyInstaller needs a file, not ``-m``). Delegates
straight to the hosted server's ``main`` so all logic lives in the package.
"""

from agent_assistant.hosted.mcp_server import main

if __name__ == "__main__":
    main()
