"""Agent-local service wiring."""

from src.services.adjudication_ledger import ledger_config, make_ledger
from src.services.runtime import RuntimeServices, services_from_config

__all__ = ["RuntimeServices", "ledger_config", "make_ledger", "services_from_config"]
