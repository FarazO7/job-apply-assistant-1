"""Vercel serverless entrypoint.

Vercel's Python runtime discovers an ASGI ``app`` in a default location such as
``api/index.py`` and serves it as a single serverless function. We re-export the
real application unchanged, so the entrypoint stays ``app.api:app`` and no
application code moves or changes.
"""

from app.api import app  # noqa: F401  (re-exported for Vercel to serve)
