# File: core/assistant/market_command.py

from __future__ import annotations

from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.market.criteria import PROFILE_IDS, SHARED_KEYS
from core.market.research import MarketResearch
from core.market.symbols import normalize_symbol

USAGE = (
    "Usage: /market run | /market profile [compounder|dividend|short_term] | "
    "/market criteria | /market criteria set <gate> <value> | /market criteria confirm | "
    "/market dismiss <buy|sell|trim> <symbol> [reason] | /market exclude <symbol> | /market connect"
)


class MarketCommandHandler:
    def __init__(self, research: MarketResearch, output: OutputSink | None = None) -> None:
        self.research = research
        self.output = output

    def handle(self, user_input: str, state: dict[str, Any]) -> bool:
        text = user_input.strip()
        if text.lower() != "/market" and not text.lower().startswith("/market "):
            return False
        parts = text.split()
        subcommand = parts[1].lower() if len(parts) > 1 else "run"
        if subcommand == "run":
            self._run(state)
        elif subcommand == "profile":
            self._profile(parts[2] if len(parts) > 2 else "")
        elif subcommand == "criteria":
            self._criteria(parts[2:])
        elif subcommand == "dismiss":
            self._dismiss(parts[2:])
        elif subcommand == "exclude":
            self._exclude(parts[2] if len(parts) > 2 else "")
        elif subcommand == "connect":
            emit_output(self.output, "E*TRADE is read-only. Set etrade:consumer_key and etrade:consumer_secret with /secrets, then reconnect.")
        else:
            emit_output(self.output, USAGE)
        return True

    def _run(self, state: dict[str, Any]) -> None:
        report = self.research.run()
        if not report.ok:
            state["command_failed"] = True
            state["command_conversation"] = report.message
            emit_output(self.output, report.message)
            return
        state["command_conversation"] = report.chat
        state["command_detail"] = {"type": "markdown", "title": "Market research", "content": report.details}
        emit_output(self.output, report.chat)

    def _profile(self, profile: str) -> None:
        if not profile:
            emit_output(self.output, f"Active profile: {self.research.store.settings()['active_profile']}")
            return
        if profile not in PROFILE_IDS:
            emit_output(self.output, "Profiles: " + ", ".join(PROFILE_IDS))
            return
        self.research.store.set_active_profile(profile)
        emit_output(self.output, f"Active profile: {profile}")

    def _criteria(self, parts: list[str]) -> None:
        if not parts:
            settings = self.research.store.settings()
            profile = str(settings["active_profile"])
            lines = [f"Profile {profile}"]
            lines.extend(f"{key} = {settings[key]}" for key in SHARED_KEYS)
            lines.extend(f"{key} = {value}" for key, value in settings["gates"][profile].items())
            emit_output(self.output, "\n".join(lines))
            return
        if parts[0] == "confirm":
            emit_output(self.output, self.research.store.commit())
            return
        if parts[0] != "set" or len(parts) < 3:
            emit_output(self.output, "Usage: /market criteria set <gate> <value>")
            return
        try:
            value = float(parts[2])
        except ValueError:
            emit_output(self.output, "A gate needs a number.")
            return
        try:
            emit_output(self.output, self.research.store.stage(parts[1], value, None))
        except ValueError:
            emit_output(self.output, f"Unknown gate {parts[1]}.")

    def _dismiss(self, parts: list[str]) -> None:
        if len(parts) < 2 or parts[0] not in {"buy", "sell", "trim"}:
            emit_output(self.output, "Usage: /market dismiss <buy|sell|trim> <symbol> [reason]")
            return
        symbol = normalize_symbol(parts[1])
        if not symbol:
            emit_output(self.output, "That symbol is not usable.")
            return
        profile = str(self.research.store.settings()["active_profile"])
        self.research.store.dismiss(profile, parts[0], symbol, " ".join(parts[2:]))
        emit_output(self.output, f"Dismissed {parts[0]} {symbol}.")

    def _exclude(self, raw: str) -> None:
        symbol = normalize_symbol(raw)
        if not symbol:
            emit_output(self.output, "Usage: /market exclude <symbol>")
            return
        added = self.research.store.toggle_exclusion(symbol)
        emit_output(self.output, f"{'Excluded' if added else 'Included'} {symbol}.")
