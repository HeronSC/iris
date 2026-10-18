# File: core/assistant/market_command.py

from __future__ import annotations

from dataclasses import replace
from typing import Any

from core.assistant.output import OutputSink, emit_output
from core.market.criteria import PROFILE_IDS, SHARED_KEYS
from core.market.models import MarketReport
from core.market.research import MarketResearch
from core.market.symbols import normalize_symbol

USAGE = (
    "Usage: /market run [compounder|dividend|short_term] | /market refresh | /market profile [compounder|dividend|short_term] | "
    "/market criteria | /market criteria set <gate> <value> | /market criteria confirm | "
    "/market dismiss <buy|sell|trim> <symbol> [reason] | /market exclude <symbol> | /market connect"
)

PROFILE_BUTTONS = (
    ("Compounder top 10", "/market run compounder"),
    ("Dividend top 10", "/market run dividend"),
    ("Short-term top 10", "/market run short_term"),
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
        if subcommand == "run" and len(parts) > 2 and parts[2].lower() in {"force", "refresh"}:
            self._refresh(state)
        elif subcommand == "run" and len(parts) > 2 and parts[2].lower() not in PROFILE_IDS:
            emit_output(self.output, "Profiles: " + ", ".join(PROFILE_IDS))
        elif subcommand == "run":
            self._run(state, parts[2].lower() if len(parts) > 2 else None)
        elif subcommand == "refresh":
            self._refresh(state)
        elif subcommand == "profile":
            self._profile(parts[2].lower() if len(parts) > 2 else "", state)
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
        state["command_actions"] = list(PROFILE_BUTTONS)
        return True

    def _progress(self, text: str, role: str = "progress") -> None:
        emit_output(self.output, text, role)

    def _refresh(self, state: dict[str, Any]) -> None:
        try:
            report = self.research.refresh(on_progress=self._progress)
        except Exception as error:
            report = MarketReport(False, f"Market refresh stopped: {error.__class__.__name__}: {error}", "", "", "")
        self._show(state, report)

    def _run(self, state: dict[str, Any], profile: str | None = None) -> None:
        try:
            report = self.research.run(profile, on_progress=self._progress)
        except Exception as error:
            report = MarketReport(False, f"Market research stopped: {error.__class__.__name__}: {error}", "", "", "")
        self._show(state, report)

    def _show(self, state: dict[str, Any], report: MarketReport) -> None:
        if not report.ok:
            state["command_failed"] = True
            state["command_conversation"] = report.message
            if report.message not in report.problems:
                emit_output(self.output, report.message, "error")
            return
        state["command_conversation"] = report.chat
        state["command_detail"] = {"type": "markdown", "title": "Market research", "content": report.details}
        emit_output(self.output, report.chat)
        if report.problems:
            emit_output(self.output, f"The refresh finished with {len(report.problems)} problem(s), shown above. The list may be missing names.", "error")

    def _profile(self, profile: str, state: dict[str, Any]) -> None:
        if not profile:
            emit_output(self.output, f"Active profile: {self.research.store.settings()['active_profile']}")
            return
        if profile not in PROFILE_IDS:
            emit_output(self.output, "Profiles: " + ", ".join(PROFILE_IDS))
            return
        self.research.store.set_active_profile(profile)
        report = self.research.report_saved(profile)
        if not report.ok:
            message = f"Active profile: {profile}. There is no saved {profile} list yet. /market run builds one."
            state["command_conversation"] = message
            emit_output(self.output, message)
            return
        self._show(state, replace(report, chat=f"Active profile: {profile}.\n{report.chat}"))

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
