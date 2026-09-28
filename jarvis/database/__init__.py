"""Persistence: SQLite access layer (repositories, migrations, cache).

Responsibility (delivered from phase 11 onwards):
    * Connection management, schema migrations, repository classes and the
      SQLite-backed cache. No raw SQL outside this package.

Allowed dependencies: ``core``, ``config``.
"""
