from __future__ import annotations

import re
from dataclasses import field
from typing import Any

from core.conversation.persistent_memory.models import TopicRecord, _utc_now
from core.conversation.persistent_memory.text import (
    _clean_excerpt,
    _extract_action_target,
    _extract_model_mentions,
    _extract_price_value,
    _extract_requirements,
    _extract_spec_facts,
    _query_coverage_score,
    _slugify_name,
    _split_sentences,
    _token_set,
)

def _summary_from_topic_state(state: dict[str, Any]) -> str:
    normalized = _normalize_topic_state(state, topic_id=0, topic_name=str(state.get("title", "General") or "General"))
    title = str(normalized.get("title", "General")).strip() or "General"
    lines = [f"Topic: {title}"]
    goal = str(normalized.get("goal", "")).strip()
    if goal:
        lines.append("Goal:")
        lines.append(f"- {goal[:220]}")
    requirements = normalized.get("requirements", []) if isinstance(normalized.get("requirements"), list) else []
    if requirements:
        lines.append("Requirements:")
        for item in requirements[:6]:
            if isinstance(item, dict):
                lines.append(f"- {str(item.get('label', ''))[:180]}")
            else:
                lines.append(f"- {str(item)[:180]}")
    items = normalized.get("items", []) if isinstance(normalized.get("items"), list) else []
    if items:
        lines.append("Active items:")
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {str(item.get('name', ''))[:180]}")
    rejected_items = normalized.get("rejected_items", []) if isinstance(normalized.get("rejected_items"), list) else []
    if rejected_items:
        lines.append("Rejected items:")
        for item in rejected_items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {str(item.get('name', ''))[:180]}")
    open_questions = normalized.get("open_questions", []) if isinstance(normalized.get("open_questions"), list) else []
    if open_questions:
        lines.append("Open questions:")
        for item in open_questions[:5]:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
                if text:
                    lines.append(f"- {text[:180]}")
            else:
                lines.append(f"- {str(item)[:180]}")
    return "\n".join(lines).strip()


def _build_public_topic_view(topic: TopicRecord, state: dict[str, Any], change_log: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = _normalize_topic_state(state, topic_id=topic.id, topic_name=topic.name)
    items = normalized.get("items", []) if isinstance(normalized.get("items"), list) else []
    rejected_items = normalized.get("rejected_items", []) if isinstance(normalized.get("rejected_items"), list) else []
    archived_items = normalized.get("archived_items", []) if isinstance(normalized.get("archived_items"), list) else []
    removed_items = normalized.get("removed_items", []) if isinstance(normalized.get("removed_items"), list) else []
    return {
        "topic_id": topic.id,
        "title": str(normalized.get("title", topic.name)).strip() or topic.name,
        "topic_type": str(normalized.get("topic_type", "generic")).strip() or "generic",
        "summary": topic.summary.strip(),
        "goal": str(normalized.get("goal", "")).strip(),
        "version": int(normalized.get("version", 1)),
        "updated_at": str(normalized.get("updated_at", topic.updated_at)),
        "requirements": normalized.get("requirements", []) if isinstance(normalized.get("requirements"), list) else [],
        "items": [item for item in items if isinstance(item, dict)],
        "rejected_items": [item for item in rejected_items if isinstance(item, dict)],
        "archived_items": [item for item in archived_items if isinstance(item, dict)],
        "removed_items": [item for item in removed_items if isinstance(item, dict)],
        "decisions": normalized.get("decisions", []) if isinstance(normalized.get("decisions"), list) else [],
        "open_questions": normalized.get("open_questions", []) if isinstance(normalized.get("open_questions"), list) else [],
        "notes": normalized.get("notes", []) if isinstance(normalized.get("notes"), list) else [],
        "change_history": [item for item in change_log if isinstance(item, dict)],
    }


def _normalize_topic_state(state: dict[str, Any], *, topic_id: int, topic_name: str) -> dict[str, Any]:
    value = state if isinstance(state, dict) else {}
    try:
        version_value = int(value.get("version", 1)) if str(value.get("version", "")).strip() else 1
    except (TypeError, ValueError):
        version_value = 1
    requirements_input = value.get("requirements", []) if isinstance(value.get("requirements"), list) else []
    requirements: list[dict[str, Any]] = []
    seen_requirement_ids: set[str] = set()
    for item in requirements_input:
        if isinstance(item, dict):
            label = str(item.get("label", "")).strip()
            req_id = str(item.get("id", "")).strip() or _slugify_name(label)
            status = str(item.get("status", "required")).strip() or "required"
        else:
            label = str(item).strip()
            req_id = _slugify_name(label)
            status = "required"
        if not label or not req_id or req_id in seen_requirement_ids:
            continue
        seen_requirement_ids.add(req_id)
        requirements.append({"id": req_id, "label": label, "status": status})

    result = {
        "schema": "topic_state_v2",
        "topic_id": str(value.get("topic_id") or topic_id),
        "title": str(value.get("title") or topic_name or "General").strip() or "General",
        "topic_type": str(value.get("topic_type") or "generic").strip() or "generic",
        "goal": str(value.get("goal") or "").strip(),
        "requirements": requirements,
        "items": _normalize_item_collection(value.get("items")),
        "rejected_items": _normalize_item_collection(value.get("rejected_items")),
        "archived_items": _normalize_item_collection(value.get("archived_items")),
        "removed_items": _normalize_item_collection(value.get("removed_items")),
        "decisions": _normalize_text_entries(value.get("decisions")),
        "open_questions": _normalize_open_questions(value.get("open_questions")),
        "notes": _normalize_text_entries(value.get("notes")),
        "version": max(1, version_value),
        "updated_at": str(value.get("updated_at") or _utc_now()),
    }
    return result


def _normalize_item_collection(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip() or _slugify_name(str(item.get("name", "")).strip())
        name = str(item.get("name", "")).strip()
        if not item_id or not name or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
        details = facts.get("details", []) if isinstance(facts.get("details"), list) else []
        attributes = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
        if "price" in facts and "price" not in attributes and facts.get("price"):
            attributes["price"] = str(facts.get("price"))
        output.append(
            {
                "id": item_id,
                "name": name,
                "status": str(item.get("status", "active") or "active"),
                "attributes": {str(key): value for key, value in attributes.items() if _is_valid_attribute_name(str(key))},
                "facts": {
                    "details": [str(detail).strip() for detail in details if str(detail).strip()],
                    "price": str(facts.get("price", "")).strip() if facts.get("price") is not None else None,
                },
                "pros": [str(entry).strip() for entry in item.get("pros", []) if str(entry).strip()] if isinstance(item.get("pros"), list) else [],
                "cons": [str(entry).strip() for entry in item.get("cons", []) if str(entry).strip()] if isinstance(item.get("cons"), list) else [],
                "aliases": [str(entry).strip() for entry in item.get("aliases", []) if str(entry).strip()] if isinstance(item.get("aliases"), list) else [],
                "manufacturer": str(item.get("manufacturer", "")).strip(),
                "model_number": str(item.get("model_number", "")).strip(),
                "source_refs": [entry for entry in item.get("source_refs", []) if isinstance(entry, dict)] if isinstance(item.get("source_refs"), list) else [],
                "rejection_reason": str(item.get("rejection_reason", "")).strip(),
            }
        )
    return output


def _normalize_open_questions(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    output: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            question_id = str(item.get("id", "")).strip() or _slugify_name(text)
            status = str(item.get("status", "open")).strip() or "open"
        else:
            text = str(item).strip()
            question_id = _slugify_name(text)
            status = "open"
        if not text or not question_id or question_id in seen_ids:
            continue
        seen_ids.add(question_id)
        output.append({"id": question_id, "text": text, "status": status})
    return output


def _normalize_text_entries(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _build_patch_operations_from_turn(
    *,
    current_state: dict[str, Any],
    topic_name: str,
    user_message: str,
    assistant_message: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    normalized_state = _normalize_topic_state(current_state, topic_id=0, topic_name=topic_name)
    scope = _derive_patch_scope(normalized_state, user_message)
    allow_new_items = bool(scope.get("allow_new_items", True))

    if not str(normalized_state.get("title", "")).strip():
        operations.append({"op": "set_topic_title", "value": topic_name})

    if not str(normalized_state.get("goal", "")).strip() and user_message.strip():
        operations.append({"op": "set_goal", "value": user_message.strip()[:220]})

    for requirement in _extract_spec_facts([user_message]):
        operations.append({"op": "add_requirement", "id": _slugify_name(requirement), "label": requirement, "status": "required"})

    for requirement in _extract_requirements([user_message]):
        operations.append({"op": "add_requirement", "id": _slugify_name(requirement), "label": requirement, "status": "required"})

    for model in _extract_model_mentions([assistant_message]):
        item_id = _slugify_name(model)
        existing_id = _resolve_item_reference_id(normalized_state, item_id) or _resolve_item_reference_id(normalized_state, model)
        if existing_id is not None:
            continue
        if allow_new_items:
            operations.append(
                {
                    "op": "add_item",
                    "item": {
                        "id": item_id,
                        "name": model,
                        "status": "active",
                        "attributes": {},
                        "facts": {"details": []},
                    },
                }
            )

    for sentence in _split_sentences(assistant_message):
        sentence_models = _extract_model_mentions([sentence])
        if not sentence_models:
            continue
        for model in sentence_models:
            item_id = _slugify_name(model)
            resolved_item_id = _resolve_item_reference_id(normalized_state, item_id) or _resolve_item_reference_id(normalized_state, model)
            if resolved_item_id is not None:
                item_id = resolved_item_id
            elif not allow_new_items:
                continue
            price = _extract_price_value(sentence)
            if price:
                operations.append(
                    {
                        "op": "set_item_attribute",
                        "item_id": item_id,
                        "field": "price",
                        "value": price,
                    }
                )
            detail = _clean_excerpt(sentence, 180)
            if detail:
                operations.append({"op": "update_item", "item_id": item_id, "add_detail": detail})

    reject_target = _extract_action_target(user_message, action="reject")
    if reject_target:
        matched_ids = _resolve_item_reference_ids(normalized_state, reject_target)
        if matched_ids:
            for item_id in matched_ids:
                operations.append({"op": "reject_item", "item_id": item_id, "reason": user_message[:180]})
        else:
            operations.append({"op": "reject_item", "item_id": reject_target, "reason": user_message[:180]})

    restore_target = _extract_action_target(user_message, action="restore")
    if restore_target:
        operations.append({"op": "restore_item", "item_id": restore_target})

    lower_user = user_message.strip().lower()
    if "only keep" in lower_user or "best three" in lower_user:
        selected = [_slugify_name(item) for item in _extract_model_mentions([assistant_message])]
        if selected:
            active_items = normalized_state.get("items", []) if isinstance(normalized_state.get("items"), list) else []
            for item in active_items:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("id", "")).strip()
                if not candidate_id or candidate_id in selected:
                    continue
                operations.append({"op": "reject_item", "item_id": candidate_id, "reason": "Moved out of active selection"})

    return operations[:100], scope


def _derive_patch_scope(state: dict[str, Any], user_message: str) -> dict[str, Any]:
    active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
    active_item_ids = [str(item.get("id", "")).strip() for item in active_items if isinstance(item, dict) and str(item.get("id", "")).strip()]
    has_active_items = bool(active_item_ids)

    if not has_active_items:
        return {
            "mode": "expand_candidates",
            "item_ids": [],
            "allow_new_items": True,
        }

    if _is_scope_expansion_request(user_message):
        return {
            "mode": "expand_candidates",
            "item_ids": active_item_ids,
            "allow_new_items": True,
        }

    return {
        "mode": "all_active_items",
        "item_ids": active_item_ids,
        "allow_new_items": False,
    }


def _is_scope_expansion_request(user_message: str) -> bool:
    lowered = (user_message or "").strip().lower()
    if not lowered:
        return False
    markers = (
        "recommend more",
        "add more",
        "add another",
        "broaden",
        "what else should i consider",
        "what else do you recommend",
        "expand",
        "more options",
        "replace these",
    )
    return any(marker in lowered for marker in markers)


def _apply_topic_operation(state: dict[str, Any], operation: dict[str, Any]) -> tuple[bool, str]:
    op_name = str(operation.get("op", "")).strip()
    if not op_name:
        return False, "missing_op"
    supported = {
        "set_topic_title",
        "set_goal",
        "add_requirement",
        "update_requirement",
        "remove_requirement",
        "add_item",
        "update_item",
        "set_item_attribute",
        "remove_item_attribute",
        "reject_item",
        "restore_item",
        "remove_item",
        "add_decision",
        "replace_decision",
        "remove_decision",
        "add_open_question",
        "resolve_open_question",
        "add_note",
        "remove_note",
    }
    if op_name not in supported:
        return False, "unsupported_operation"

    if op_name == "set_topic_title":
        value = str(operation.get("value", "")).strip()
        if not value:
            return False, "missing_value"
        state["title"] = value[:220]
        return True, "ok"

    if op_name == "set_goal":
        value = str(operation.get("value", "")).strip()
        if not value:
            return False, "missing_value"
        state["goal"] = value[:400]
        return True, "ok"

    if op_name in {"add_requirement", "update_requirement", "remove_requirement"}:
        requirements = state.get("requirements", []) if isinstance(state.get("requirements"), list) else []
        req_id = str(operation.get("id", "")).strip() or _slugify_name(str(operation.get("label", "")).strip())
        if not req_id:
            return False, "missing_requirement_id"
        if op_name == "remove_requirement":
            for index, item in enumerate(requirements):
                if isinstance(item, dict) and str(item.get("id", "")).strip() == req_id:
                    requirements.pop(index)
                    state["requirements"] = requirements
                    return True, "ok"
            return False, "requirement_not_found"
        label = str(operation.get("label", "")).strip()
        status = str(operation.get("status", "required")).strip() or "required"
        for item in requirements:
            if not isinstance(item, dict):
                continue
            if str(item.get("id", "")).strip() != req_id:
                continue
            if op_name == "add_requirement":
                return False, "requirement_exists"
            if label:
                item["label"] = label[:220]
            item["status"] = status[:80]
            state["requirements"] = requirements
            return True, "ok"
        if not label:
            return False, "missing_requirement_label"
        requirements.append({"id": req_id, "label": label[:220], "status": status[:80]})
        state["requirements"] = requirements
        return True, "ok"

    if op_name in {"add_item", "update_item", "set_item_attribute", "remove_item_attribute", "reject_item", "restore_item", "remove_item"}:
        target_id = str(operation.get("item_id", "")).strip()
        if op_name == "add_item":
            item_payload = operation.get("item") if isinstance(operation.get("item"), dict) else {}
            item_name = str(item_payload.get("name", "")).strip()
            item_id = str(item_payload.get("id", "")).strip() or _slugify_name(item_name)
            if not item_name or not item_id:
                return False, "missing_item_identity"
            existing_id = _resolve_item_reference_id(state, item_id) or _resolve_item_reference_id(state, item_name)
            if existing_id is not None:
                return False, "item_exists"
            new_item = _normalize_item_collection([item_payload])[0] if _normalize_item_collection([item_payload]) else {
                "id": item_id,
                "name": item_name,
                "status": "active",
                "attributes": {},
                "facts": {"details": [], "price": None},
                "pros": [],
                "cons": [],
                "aliases": [],
                "manufacturer": "",
                "model_number": "",
                "source_refs": [],
                "rejection_reason": "",
            }
            active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
            active_items.append(new_item)
            state["items"] = active_items
            return True, "ok"

        resolved_id = _resolve_item_reference_id(state, target_id or str(operation.get("item", "")).strip())
        if resolved_id is None:
            return False, "item_not_found"

        if op_name == "remove_item":
            removed = _remove_item_from_all_collections(state, resolved_id)
            if removed is None:
                return False, "item_not_found"
            removed["status"] = "removed"
            removed_items = state.get("removed_items", []) if isinstance(state.get("removed_items"), list) else []
            removed_items.append(removed)
            state["removed_items"] = removed_items
            return True, "ok"

        if op_name == "reject_item":
            removed = _remove_item_from_all_collections(state, resolved_id)
            if removed is None:
                return False, "item_not_found"
            removed["status"] = "rejected"
            removed["rejection_reason"] = str(operation.get("reason", "Rejected by user")).strip()[:220]
            rejected_items = state.get("rejected_items", []) if isinstance(state.get("rejected_items"), list) else []
            rejected_items.append(removed)
            state["rejected_items"] = rejected_items
            return True, "ok"

        if op_name == "restore_item":
            removed = _remove_item_from_collection(state, "rejected_items", resolved_id)
            if removed is None:
                removed = _remove_item_from_collection(state, "archived_items", resolved_id)
            if removed is None:
                removed = _remove_item_from_collection(state, "removed_items", resolved_id)
            if removed is None:
                return False, "item_not_found"
            removed["status"] = "active"
            removed["rejection_reason"] = ""
            active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
            active_items.append(removed)
            state["items"] = active_items
            return True, "ok"

        item = _find_item_in_collection(state, "items", resolved_id)
        if item is None:
            item = _find_item_in_collection(state, "rejected_items", resolved_id)
        if item is None:
            item = _find_item_in_collection(state, "archived_items", resolved_id)
        if item is None:
            item = _find_item_in_collection(state, "removed_items", resolved_id)
        if item is None:
            return False, "item_not_found"

        if op_name == "update_item":
            name = str(operation.get("name", "")).strip()
            if name:
                item["name"] = name[:220]
            detail = str(operation.get("add_detail", "")).strip()
            if detail:
                facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
                details = facts.get("details", []) if isinstance(facts.get("details"), list) else []
                if detail not in details:
                    details.append(detail)
                facts["details"] = details[:15]
                item["facts"] = facts
            return True, "ok"

        if op_name == "set_item_attribute":
            field = str(operation.get("field", "")).strip().lower()
            if not _is_valid_attribute_name(field):
                return False, "invalid_field"
            attributes = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            attributes[field] = operation.get("value")
            item["attributes"] = attributes
            if field == "price":
                facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
                facts["price"] = str(operation.get("value", "")).strip()
                item["facts"] = facts
            return True, "ok"

        if op_name == "remove_item_attribute":
            field = str(operation.get("field", "")).strip().lower()
            if not _is_valid_attribute_name(field):
                return False, "invalid_field"
            attributes = item.get("attributes", {}) if isinstance(item.get("attributes"), dict) else {}
            if field not in attributes:
                return False, "attribute_not_found"
            attributes.pop(field, None)
            item["attributes"] = attributes
            if field == "price":
                facts = item.get("facts", {}) if isinstance(item.get("facts"), dict) else {}
                facts["price"] = None
                item["facts"] = facts
            return True, "ok"

    if op_name in {"add_decision", "replace_decision", "remove_decision"}:
        decisions = state.get("decisions", []) if isinstance(state.get("decisions"), list) else []
        if op_name == "add_decision":
            value = str(operation.get("value", "")).strip()
            if not value:
                return False, "missing_value"
            if value not in decisions:
                decisions.append(value[:260])
            state["decisions"] = decisions
            return True, "ok"
        if op_name == "replace_decision":
            index = operation.get("index")
            value = str(operation.get("value", "")).strip()
            if not isinstance(index, int) or index < 0 or index >= len(decisions) or not value:
                return False, "invalid_decision_update"
            decisions[index] = value[:260]
            state["decisions"] = decisions
            return True, "ok"
        value = str(operation.get("value", "")).strip().lower()
        if not value:
            return False, "missing_value"
        filtered = [item for item in decisions if str(item).strip().lower() != value]
        if len(filtered) == len(decisions):
            return False, "decision_not_found"
        state["decisions"] = filtered
        return True, "ok"

    if op_name in {"add_open_question", "resolve_open_question"}:
        questions = state.get("open_questions", []) if isinstance(state.get("open_questions"), list) else []
        if op_name == "add_open_question":
            text = str(operation.get("text", "")).strip()
            if not text:
                return False, "missing_text"
            question_id = str(operation.get("id", "")).strip() or _slugify_name(text)
            for entry in questions:
                if isinstance(entry, dict) and str(entry.get("id", "")).strip() == question_id:
                    return False, "question_exists"
            questions.append({"id": question_id, "text": text[:260], "status": "open"})
            state["open_questions"] = questions
            return True, "ok"
        question_id = str(operation.get("id", "")).strip() or _slugify_name(str(operation.get("text", "")).strip())
        if not question_id:
            return False, "missing_question_id"
        for entry in questions:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id", "")).strip() != question_id:
                continue
            entry["status"] = "resolved"
            state["open_questions"] = questions
            return True, "ok"
        return False, "question_not_found"

    if op_name in {"add_note", "remove_note"}:
        notes = state.get("notes", []) if isinstance(state.get("notes"), list) else []
        value = str(operation.get("value", "")).strip()
        if not value:
            return False, "missing_value"
        if op_name == "add_note":
            if value not in notes:
                notes.append(value[:260])
            state["notes"] = notes
            return True, "ok"
        filtered = [item for item in notes if str(item).strip().lower() != value.lower()]
        if len(filtered) == len(notes):
            return False, "note_not_found"
        state["notes"] = filtered
        return True, "ok"

    return False, "unsupported_operation"


def _resolve_item_reference_id(state: dict[str, Any], token: str) -> str | None:
    matches = _resolve_item_reference_ids(state, token)
    if matches:
        return matches[0]
    return None


def _resolve_item_reference_ids(state: dict[str, Any], token: str) -> list[str]:
    needle = str(token or "").strip()
    if not needle:
        return []
    normalized_needle = _slugify_name(needle)
    needle_tokens = _token_set(needle)
    is_plural_reference = "laptops" in needle.lower() or "models" in needle.lower() or "options" in needle.lower()
    collections = ("items", "rejected_items", "archived_items", "removed_items")
    scored: list[tuple[float, str]] = []
    for name in collections:
        items = state.get(name, []) if isinstance(state.get(name), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            item_name = str(item.get("name", "")).strip()
            aliases = item.get("aliases", []) if isinstance(item.get("aliases"), list) else []
            if needle == item_id or normalized_needle == item_id:
                return [item_id]
            item_slug = _slugify_name(item_name)
            if item_slug == normalized_needle:
                return [item_id]
            for alias in aliases:
                if _slugify_name(str(alias)) == normalized_needle:
                    return [item_id]
            item_tokens = _token_set(item_name)
            if not needle_tokens or not item_tokens:
                continue
            overlap = _query_coverage_score(needle_tokens, item_tokens)
            if overlap > 0.0:
                scored.append((overlap, item_id))

    if not scored:
        return []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score = scored[0][0]
    if is_plural_reference:
        threshold = max(0.60, min(best_score, 0.80))
        selected_ids: list[str] = []
        seen: set[str] = set()
        for score, item_id in scored:
            if score < threshold:
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            selected_ids.append(item_id)
        return selected_ids

    if best_score < 0.70:
        return []
    return [scored[0][1]]


def _remove_item_from_collection(state: dict[str, Any], collection: str, item_id: str) -> dict[str, Any] | None:
    items = state.get(collection, []) if isinstance(state.get(collection), list) else []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() != item_id:
            continue
        removed = items.pop(index)
        state[collection] = items
        return removed
    return None


def _remove_item_from_all_collections(state: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for collection in ("items", "rejected_items", "archived_items", "removed_items"):
        removed = _remove_item_from_collection(state, collection, item_id)
        if removed is not None:
            return removed
    return None


def _find_item_in_collection(state: dict[str, Any], collection: str, item_id: str) -> dict[str, Any] | None:
    items = state.get(collection, []) if isinstance(state.get(collection), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() == item_id:
            return item
    return None


def _is_valid_attribute_name(field: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", field or ""))


def _build_patch_policy(state: dict[str, Any], scope: dict[str, Any] | None) -> dict[str, Any]:
    scoped = scope if isinstance(scope, dict) else {}
    active_items = state.get("items", []) if isinstance(state.get("items"), list) else []
    active_item_ids = [str(item.get("id", "")).strip() for item in active_items if isinstance(item, dict) and str(item.get("id", "")).strip()]
    default_mode = "all_active_items" if active_item_ids else "expand_candidates"
    mode = str(scoped.get("mode", default_mode)).strip() or default_mode
    allow_new_items = bool(scoped.get("allow_new_items", mode in {"expand_candidates", "replace_candidate_set"}))
    provided_item_ids = scoped.get("item_ids") if isinstance(scoped.get("item_ids"), list) else []
    item_ids = [str(item).strip() for item in provided_item_ids if str(item).strip()]
    if not item_ids and mode == "all_active_items":
        item_ids = active_item_ids
    return {
        "mode": mode,
        "item_ids": item_ids,
        "allow_new_items": allow_new_items,
    }


def _validate_operation_against_policy(
    state: dict[str, Any],
    operation: dict[str, Any],
    policy: dict[str, Any],
    planned_restore_targets: set[str],
) -> tuple[bool, str]:
    op_name = str(operation.get("op", "")).strip()
    if not op_name:
        return False, "missing_op"

    mode = str(policy.get("mode", "all_active_items")).strip() or "all_active_items"
    allowed_item_ids = {
        str(item).strip()
        for item in policy.get("item_ids", [])
        if str(item).strip()
    }
    allow_new_items = bool(policy.get("allow_new_items", True))

    rejected_items = state.get("rejected_items", []) if isinstance(state.get("rejected_items"), list) else []
    rejected_ids = {
        str(item.get("id", "")).strip()
        for item in rejected_items
        if isinstance(item, dict) and str(item.get("id", "")).strip()
    }

    if op_name == "add_item":
        if not allow_new_items:
            return False, "scope_disallows_add_item"
        item_payload = operation.get("item") if isinstance(operation.get("item"), dict) else {}
        item_name = str(item_payload.get("name", "")).strip()
        item_id = str(item_payload.get("id", "")).strip() or _slugify_name(item_name)
        if not item_id:
            return False, "missing_item_identity"
        matched_rejected = _resolve_item_reference_id(state, item_id) or _resolve_item_reference_id(state, item_name)
        if matched_rejected in rejected_ids and matched_rejected not in planned_restore_targets:
            return False, "cannot_readd_rejected_without_restore"
        return True, "ok"

    if op_name in {"update_item", "set_item_attribute", "remove_item_attribute", "reject_item", "remove_item"}:
        target_token = str(operation.get("item_id", "")).strip()
        if not target_token:
            return False, "missing_item_id"
        resolved = _resolve_item_reference_id(state, target_token)
        if resolved is None:
            return False, "item_not_found"
        if mode in {"existing_items_only", "selected_items", "all_active_items"} and allowed_item_ids and resolved not in allowed_item_ids:
            return False, "item_out_of_scope"
        if resolved in rejected_ids and op_name != "restore_item":
            return False, "rejected_item_requires_restore"
        return True, "ok"

    return True, "ok"


def _collect_restore_targets(state: dict[str, Any], operations: list[dict[str, Any]]) -> set[str]:
    output: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("op", "")).strip() != "restore_item":
            continue
        token = str(operation.get("item_id", "")).strip()
        if not token:
            continue
        resolved = _resolve_item_reference_id(state, token)
        if resolved is not None:
            output.add(resolved)
    return output
