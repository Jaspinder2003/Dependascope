"""Isolated before/after build comparison for Python projects.

Detects how a project is built, then runs that build at the commit before and
the commit after a dependency update, in isolation from the host environment.
Used where the research question is specifically about the build step rather
than the test suite.
"""
