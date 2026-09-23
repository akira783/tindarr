"""The bundle of factories the composition root hands to the connector service.

Each port already says how its adapter is built; this only groups the four optional
ones, so nothing above the adapters has to name a class. It lives in ``ports`` rather
than beside the service because an adapter package may not import the layers above it,
and the composition root builds this bundle out of adapter modules.
"""

from dataclasses import dataclass

from tindarr.ports.llm import LlmProviderFactory
from tindarr.ports.metadata import MetadataFactory, RatingsFactory
from tindarr.ports.request_backend import RequestBackendFactory


@dataclass(frozen=True, slots=True)
class ConnectorFactories:
    """The adapters the optional connectors are built from."""

    metadata: MetadataFactory
    ratings: RatingsFactory
    request_backend: RequestBackendFactory
    llm: LlmProviderFactory
