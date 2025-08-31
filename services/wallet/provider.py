from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Optional

from libs.agentkit_ext.agentkit_wallet import get_agentkit_wallet


class WalletProvider(Protocol):
    def get_address(self) -> str: ...
    def sign_message(self, message: str) -> str: ...
    def send_transaction(self, tx: dict) -> str: ...


@dataclass
class AgentKitWalletAdapter:
    """Adapter exposing a minimal wallet interface over an AgentKit provider."""

    inner: WalletProvider

    @property
    def address(self) -> str:
        return self.inner.get_address()

    def sign_message(self, message: str) -> str:
        return self.inner.sign_message(message)

    def send_transaction(self, to: str, data: Optional[bytes] = None, value: int = 0) -> str:
        tx: dict = {"to": to, "value": value}
        if data is not None:
            tx["data"] = data
        return self.inner.send_transaction(tx)


def get_default_provider() -> AgentKitWalletAdapter:
    provider, _ = get_agentkit_wallet()
    return AgentKitWalletAdapter(inner=provider)  # type: ignore[arg-type]
