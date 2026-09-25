"""Bot interface. Any bot -- baseline or a real agent -- implements this."""
from __future__ import annotations

from abc import ABC, abstractmethod


class Bot(ABC):
    name: str = "bot"

    @abstractmethod
    def act(self, state: dict) -> str:
        """Given the state dict from LeducHand.state_for(player), return
        one of state['legal_actions']."""
        raise NotImplementedError
