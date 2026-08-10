"""Liveness and readiness for the *application* (SPEC §8.6).

Not to be confused with `home_ops.modules.health`, which is the household's
health records (SPEC §4.8). This package was called `health` until 2026-08-01,
when §4.8 arrived and needed the name for the thing a person would actually
mean by it. The endpoints are unchanged and still live at `/api/health` — that
URL is verified over the public hostname and must not move.

Deliberately empty of imports. See `home_ops.modules.__init__` for why.
"""
