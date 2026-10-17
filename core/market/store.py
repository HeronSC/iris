# File: core/market/store.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.market.criteria import PROFILE_IDS, SHARED_KEYS, default_gates, default_settings


class MarketStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root / "settings.json"
        self.listings_path = self.root / "listings.json"
        self.runs_path = self.root / "runs.jsonl"
        self.dismissals_path = self.root / "dismissals.json"
        self.exclusions_path = self.root / "exclusions.json"
        self.pending_path = self.root / "pending.json"

    def settings(self) -> dict[str, Any]:
        stored = self._read(self.settings_path, {})
        base = default_settings()
        if not isinstance(stored, dict):
            return base
        merged = dict(base)
        merged.update({key: stored[key] for key in SHARED_KEYS if key in stored})
        if stored.get("active_profile") in PROFILE_IDS:
            merged["active_profile"] = stored["active_profile"]
        versions = dict(base["versions"])
        gates = {profile: dict(default_gates(profile)) for profile in PROFILE_IDS}
        stored_versions = stored.get("versions") if isinstance(stored.get("versions"), dict) else {}
        stored_gates = stored.get("gates") if isinstance(stored.get("gates"), dict) else {}
        for profile in PROFILE_IDS:
            if profile in stored_versions:
                versions[profile] = int(stored_versions[profile])
            if isinstance(stored_gates.get(profile), dict):
                gates[profile].update(stored_gates[profile])
        merged["versions"] = versions
        merged["gates"] = gates
        return merged

    def save_settings(self, settings: dict[str, Any]) -> None:
        self._write(self.settings_path, settings)

    def set_active_profile(self, profile: str) -> None:
        if profile not in PROFILE_IDS:
            raise ValueError(profile)
        settings = self.settings()
        settings["active_profile"] = profile
        self.save_settings(settings)

    def stage(self, key: str, value: float, profile: str | None) -> str:
        settings = self.settings()
        target = profile or str(settings["active_profile"])
        if key in SHARED_KEYS:
            self._write(self.pending_path, {"key": key, "value": value, "profile": None})
            return f"Set {key} to {value}? Confirm with /market criteria confirm."
        gates = settings["gates"].get(target)
        if not isinstance(gates, dict) or key not in gates:
            raise ValueError(key)
        self._write(self.pending_path, {"key": key, "value": value, "profile": target})
        return f"Set {target} {key} to {value}? Confirm with /market criteria confirm."

    def commit(self) -> str:
        pending = self._read(self.pending_path, None)
        if not isinstance(pending, dict):
            return "Nothing is waiting for confirmation."
        settings = self.settings()
        key = str(pending["key"])
        value = float(pending["value"])
        profile = pending.get("profile")
        if profile:
            settings["gates"][profile][key] = value
            settings["versions"][profile] = int(settings["versions"][profile]) + 1
        else:
            settings[key] = value
        self.save_settings(settings)
        self.pending_path.unlink(missing_ok=True)
        return f"Saved {key} = {value}."

    def dismiss(self, profile: str, kind: str, symbol: str, reason: str = "") -> None:
        settings = self.settings()
        rows = self.dismissals()
        rows.append(
            {
                "profile": profile if kind != "trim" else "",
                "kind": kind,
                "symbol": symbol,
                "reason": reason,
                "version": int(settings["versions"][profile]) if kind != "trim" else int(settings.get("weight_version", 1)),
                "weight": float(settings["max_weight"]) if kind == "trim" else None,
            }
        )
        self._write(self.dismissals_path, rows)

    def dismissals(self) -> list[dict[str, Any]]:
        payload = self._read(self.dismissals_path, [])
        return list(payload) if isinstance(payload, list) else []

    def active_dismissal(self, profile: str, kind: str, symbol: str) -> bool:
        settings = self.settings()
        for row in self.dismissals():
            if row.get("symbol") != symbol or row.get("kind") != kind:
                continue
            if kind == "trim":
                return float(row.get("weight") or 0) == float(settings["max_weight"])
            if row.get("profile") != profile:
                continue
            return int(row.get("version") or 0) == int(settings["versions"][profile])
        return False

    def toggle_exclusion(self, symbol: str) -> bool:
        current = set(self.exclusions())
        added = symbol not in current
        if added:
            current.add(symbol)
        else:
            current.discard(symbol)
        self._write(self.exclusions_path, sorted(current))
        return added

    def exclusions(self) -> list[str]:
        payload = self._read(self.exclusions_path, [])
        return [str(item) for item in payload] if isinstance(payload, list) else []

    def save_listings(self, symbols: list[str]) -> bool:
        cached = self.listings()
        if len(cached) >= 100 and len(symbols) < len(cached) / 2:
            return False
        self._write(self.listings_path, symbols)
        return True

    def listings(self) -> list[str]:
        payload = self._read(self.listings_path, [])
        return [str(item) for item in payload] if isinstance(payload, list) else []

    def remember_shown(self, profile: str, shown: list[dict[str, Any]], day: str) -> None:
        path = self.root / "history" / f"{profile}.json"
        rows = self._read(path, [])
        if not isinstance(rows, list):
            rows = []
        kept = [row for row in rows if isinstance(row, dict) and row.get("day") != day]
        kept.append({"day": day, "shown": shown})
        self._write(path, kept)

    def history(self, profile: str) -> list[dict[str, Any]]:
        payload = self._read(self.root / "history" / f"{profile}.json", [])
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def save_run(self, payload: dict[str, Any]) -> None:
        profile = str(payload.get("profile") or "")
        if profile:
            self._write(self.root / "profiles" / f"{profile}.json", payload)
        with self.runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        if not profile or profile == str(self.settings()["active_profile"]):
            self._write(self.root / "latest.json", payload)

    def latest(self, profile: str | None = None) -> dict[str, Any] | None:
        if profile:
            payload = self._read(self.root / "profiles" / f"{profile}.json", None)
            return payload if isinstance(payload, dict) else None
        payload = self._read(self.root / "latest.json", None)
        return payload if isinstance(payload, dict) else None

    def fresh(self, profile: str) -> bool:
        saved = self.latest(profile)
        settings = self.settings()
        if not isinstance(saved, dict):
            return False
        if int(saved.get("gate_version") or 0) != int(settings["versions"][profile]):
            return False
        if int(saved.get("universe") or 0) == 0 and not saved.get("ranked"):
            return False
        return saved.get("shared") == self.shared_stamp(settings)

    def shared_stamp(self, settings: dict[str, Any] | None = None) -> dict[str, float]:
        current = settings or self.settings()
        return {key: float(current[key]) for key in ("min_price", "max_price", "min_avg_share_volume_5", "advance_n", "max_weight")}

    def _read(self, path: Path, fallback: Any) -> Any:
        if not path.exists():
            return fallback
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback

    def _write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
