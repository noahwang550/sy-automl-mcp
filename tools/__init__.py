"""MCP tools for sy-automl-mcp.

Each tool function is registered in ``server.py`` via ``@mcp.tool()``.
Tools are thin wrappers: they validate input, resolve paths via :mod:`config`,
and delegate heavy work to AutoGluon (lazily imported) — either inline for
fast calls or via :mod:`tasks.manager` for long-running fit/predict.
"""
