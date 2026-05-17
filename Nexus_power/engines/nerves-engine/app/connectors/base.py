"""
Nerves Engine — Base Connector.

Abstract base class for all Nerves connectors.
Defines the standard interface every connector must implement.
"""

from __future__ import annotations

import logging
from enum import Enum
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ConnectorStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"


class BaseConnector(ABC):
    """Base class for all Nerves connectors."""

    def __init__(self, name: str, config: dict = None):
        self.name = name
        self.config = config or {}
        self.status = ConnectorStatus.NOT_CONFIGURED

    @abstractmethod
    async def connect(self, credentials: dict) -> bool:
        """Establish connection to external system."""
        pass

    @abstractmethod
    async def execute(self, action: str, params: dict) -> dict:
        """Execute an action. Returns result dict."""
        pass

    @abstractmethod
    def get_available_actions(self) -> list[dict]:
        """List available actions for this connector."""
        pass

    async def disconnect(self):
        """Disconnect from external system."""
        self.status = ConnectorStatus.DISCONNECTED

    def get_status(self) -> dict:
        return {
            "connector": self.name,
            "status": self.status.value,
            "configured": bool(self.config),
        }
