"""MCP integration: connect external Model Context Protocol servers.

Responsibility (delivered in phase 15):
    * MCP client, server registry driven purely by user configuration
      (filesystem, GitHub, SQLite, Postgres, Playwright, Notion, ...);
      discovered MCP tools are bridged into the ``tools`` registry so no
      code change is needed to add a new server.

Allowed dependencies: ``core``, ``config``, ``tools``.
"""
