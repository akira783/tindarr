"""One sub-package per external system (media servers, request backends, metadata, AI).

Adapters implement ``tindeerr.ports`` and may use vendor SDKs and httpx. Nothing but
``tindeerr.main`` imports them; import-linter enforces it. Empty until step 3.
"""
