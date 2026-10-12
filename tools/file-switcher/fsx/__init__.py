"""Folder switcher internals.

Layering, from the bottom up:

- ``paths``    where bundled resources and per-user data live
- ``errors``   backend error codes translated into plain Chinese
- ``backend``  the one path that talks to ``folder-switcher.ps1``
- ``store``    the one path that writes metadata
- ``model``    lookups into the loaded configuration
- ``ui``       Tk views; every dialog goes through ``ui.dialogs``
"""
