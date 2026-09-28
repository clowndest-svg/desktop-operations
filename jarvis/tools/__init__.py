"""Tool calling framework and built-in tools.

Responsibility (delivered in phase 12):
    * Tool interface, JSON-schema parameter models, auto-registration and
      dynamic discovery, dangerous-tool confirmation policy, and the
      built-in tool set (files, shell, search, office docs, system info,
      clipboard, notifications, ...).

Allowed dependencies: ``core``, ``config``; individual tools may use
capability packages (``vision``, ``ocr``, ``browser``, ``computer``,
``database``) through their public interfaces.
"""
