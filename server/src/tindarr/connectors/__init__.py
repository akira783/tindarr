"""The optional connectors: TMDb, OMDb, the request backend and the AI provider.

The media server connector lives in ``tindarr.auth``, because changing it decides who
can sign in and who is an administrator. These four decide nothing about identity: they
can be added, tested, changed and removed by any administrator from the console, and a
household that configures none of them still has a working Tindarr with no swipe engine.

What they do share with the media server is the rule that matters most here — **a stored
secret is never sent to an address it was not stored for** (the security model,
section 7). Every save, test and model listing goes through the same check, so a stolen
admin session cannot turn Tindarr into a courier for the keys it holds.
"""

from tindarr.connectors.service import (
    OPTIONAL_CONNECTOR_KINDS,
    ConnectorInput,
    ConnectorService,
    ConnectorState,
    LlmSettings,
    MetadataSettings,
    OptionalConnectorKind,
    RequestsSettings,
)

__all__ = [
    "OPTIONAL_CONNECTOR_KINDS",
    "ConnectorInput",
    "ConnectorService",
    "ConnectorState",
    "LlmSettings",
    "MetadataSettings",
    "OptionalConnectorKind",
    "RequestsSettings",
]
