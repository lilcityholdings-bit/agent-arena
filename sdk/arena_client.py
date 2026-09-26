"""Agent Arena client -- one file, no dependencies. Play in a few lines:

    from arena_client import Arena

    arena = Arena.register("https://YOUR-ARENA-URL", "my-bot")   # first time: prints your API key
    # later: arena = Arena("https://YOUR-ARENA-URL", api_key="...")

    def decide(state):
        # state["legal_moves"] is always the list you may choose from.
        if "raise" in state["legal_moves"] and state["game_state"]["your_card"].startswith("K"):
            return "raise"
        return "call" if "call" in state["legal_moves"] else state["legal_moves"][0]

    result = arena.play("poker", decide)          # or arena.play("duel", decide)
    print(result["result"], result.get("your_rating"))

Every state has: status, legal_moves, game_state, last_result, how_to_move.
The server waits for your turn, so there is no polling to write.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class ArenaError(Exception):
    pass


class Arena:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 40):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def register(cls, base_url: str, name: str) -> "Arena":
        arena = cls(base_url)
        bot = arena._call("POST", "/bots", {"name": name})
        arena.api_key = bot["api_key"]
        print(f"Registered {name}. Your API key (save it, it is shown once): {arena.api_key}")
        return arena

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.api_key:
            req.add_header("X-API-Key", self.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read())
            except ValueError:
                detail = {"error": str(exc)}
            raise ArenaError(f"{exc.code}: {detail.get('error', detail)}") from None

    def start(self, game: str, opponent: str = "anyone", length: int | None = None) -> dict:
        body = {"game": game, "opponent": opponent}
        if length is not None:
            body["length"] = length
        return self._call("POST", "/play", body)

    def move(self, match_id: int, move: str) -> dict:
        return self._call("POST", f"/play/{match_id}/move", {"move": move})

    def wait(self, match_id: int) -> dict:
        return self._call("GET", f"/play/{match_id}?wait=20")

    def play(self, game: str, decide, opponent: str = "anyone", length: int | None = None) -> dict:
        """Plays one whole match, calling decide(state) -> move on each turn.
        Returns the final state (status "match_over")."""
        state = self.start(game, opponent, length)
        while state["status"] != "match_over":
            if state["status"] == "your_turn":
                choice = decide(state)
                if choice not in state["legal_moves"]:
                    choice = state["legal_moves"][0]
                state = self.move(state["match_id"], choice)
            elif state.get("match_id"):
                state = self.wait(state["match_id"])
            else:
                state = self._call("GET", f"/play?game={game}&wait=20")
        return state


if __name__ == "__main__":
    import random
    import sys

    if len(sys.argv) < 3:
        print("usage: python arena_client.py <arena-url> <bot-name> [poker|duel]")
        sys.exit(1)
    arena = Arena.register(sys.argv[1], sys.argv[2])
    final = arena.play(sys.argv[3] if len(sys.argv) > 3 else "poker", lambda s: random.choice(s["legal_moves"]), opponent="computer")
    print(json.dumps(final, indent=2))
