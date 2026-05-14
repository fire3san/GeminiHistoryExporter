"""
Lightweight structural validators for the three target schemas the
converters emit. No external dependency (no `jsonschema` package needed).

Each function returns a list of error strings (empty list == valid).
The check is shape/type-only: it doesn't validate semantics (e.g. that a
referenced `parent` UUID actually exists), since the converters already
construct those deterministically.
"""

from __future__ import annotations

from typing import Any


# ------------------------------------------------------------------ helpers
def _type(name: str, value: Any, expected: type, errs: list[str]) -> bool:
    if not isinstance(value, expected):
        errs.append(f"{name}: expected {expected.__name__}, got {type(value).__name__}")
        return False
    return True


def _has(name: str, obj: dict, key: str, errs: list[str]) -> bool:
    if key not in obj:
        errs.append(f"{name}: missing key '{key}'")
        return False
    return True


# ------------------------------------------------------------------ OpenAI
# Shape (ChatGPT export):
# [
#   {
#     "title": str,
#     "create_time": float,
#     "update_time": float,
#     "mapping": {
#        "<uuid>": {
#           "id": str,
#           "message": null | {
#              "id": str,
#              "author": {"role": "user"|"assistant"|"system"},
#              "content": {"content_type": "text", "parts": [str, ...]},
#              "create_time": float | null
#           },
#           "parent": str | null,
#           "children": [str, ...]
#        }, ...
#     },
#     "current_node": str
#   }, ...
# ]
def validate_openai_schema(data: Any) -> list[str]:
    errs: list[str] = []
    if not _type("root", data, list, errs):
        return errs
    for i, c in enumerate(data):
        ctx = f"[{i}]"
        if not _type(ctx, c, dict, errs):
            continue
        if _has(ctx, c, "title", errs):
            _type(f"{ctx}.title", c["title"], str, errs)
        if _has(ctx, c, "mapping", errs) and _type(f"{ctx}.mapping", c["mapping"], dict, errs):
            for nid, node in c["mapping"].items():
                nctx = f"{ctx}.mapping[{nid}]"
                if not _type(nctx, node, dict, errs):
                    continue
                msg = node.get("message")
                if msg is None:
                    continue
                if not _type(f"{nctx}.message", msg, dict, errs):
                    continue
                author = msg.get("author") or {}
                role = author.get("role") if isinstance(author, dict) else None
                if role not in ("user", "assistant", "system"):
                    errs.append(f"{nctx}.message.author.role invalid: {role!r}")
                content = msg.get("content") or {}
                if not isinstance(content, dict):
                    errs.append(f"{nctx}.message.content not a dict")
                    continue
                parts = content.get("parts")
                if not isinstance(parts, list):
                    errs.append(f"{nctx}.message.content.parts not a list")
                    continue
                for p in parts:
                    if not isinstance(p, str):
                        errs.append(f"{nctx}.message.content.parts has non-str item")
                        break
    return errs


# ------------------------------------------------------------------ Claude
# Shape:
# [
#   {
#     "uuid": str, "name": str, "created_at": str, "updated_at": str,
#     "chat_messages": [
#       {
#         "uuid": str, "text": str,
#         "sender": "human" | "assistant",
#         "created_at": str, "updated_at": str,
#         "content": [{"type": "text", "text": str}, ...]
#       }, ...
#     ]
#   }, ...
# ]
def validate_claude_schema(data: Any) -> list[str]:
    errs: list[str] = []
    if not _type("root", data, list, errs):
        return errs
    for i, c in enumerate(data):
        ctx = f"[{i}]"
        if not _type(ctx, c, dict, errs):
            continue
        _has(ctx, c, "name", errs)
        if _has(ctx, c, "chat_messages", errs) and _type(f"{ctx}.chat_messages", c["chat_messages"], list, errs):
            for j, m in enumerate(c["chat_messages"]):
                mctx = f"{ctx}.chat_messages[{j}]"
                if not _type(mctx, m, dict, errs):
                    continue
                sender = m.get("sender")
                if sender not in ("human", "assistant"):
                    errs.append(f"{mctx}.sender invalid: {sender!r}")
                _type(f"{mctx}.text", m.get("text", ""), str, errs)
                content = m.get("content")
                if not isinstance(content, list):
                    errs.append(f"{mctx}.content not a list")
                    continue
                for k, part in enumerate(content):
                    pctx = f"{mctx}.content[{k}]"
                    if not isinstance(part, dict):
                        errs.append(f"{pctx} not a dict")
                        continue
                    if part.get("type") != "text":
                        errs.append(f"{pctx}.type invalid: {part.get('type')!r}")
                    if not isinstance(part.get("text", ""), str):
                        errs.append(f"{pctx}.text not a str")
    return errs


# ------------------------------------------------------------------ Grok
# Shape:
# {
#   "conversations": [
#     {
#       "conversation": {"_id": ..., "title": str, "createTime": {"$date": {"$numberLong": str}}, ...},
#       "responses": [
#         {
#           "_id": ..., "sender": "human"|"assistant", "message": str,
#           "createTime": {"$date": {"$numberLong": str}}, ...
#         }, ...
#       ]
#     }, ...
#   ]
# }
def _check_grok_date(name: str, value: Any, errs: list[str]) -> None:
    if not isinstance(value, dict):
        errs.append(f"{name}: not a dict")
        return
    d = value.get("$date")
    if not isinstance(d, dict):
        errs.append(f"{name}.$date: not a dict")
        return
    n = d.get("$numberLong")
    if not isinstance(n, str) or not n.lstrip("-").isdigit():
        errs.append(f"{name}.$date.$numberLong: not a numeric string ({n!r})")


def validate_grok_schema(data: Any) -> list[str]:
    """Accepts two shapes:
      flat:     responses[i] == {sender, message, create_time}
      wrapped:  responses[i] == {"response": {sender, message, create_time}, "share_link": ...}
    The wrapped form mirrors Grok's actual backend export.
    Date fields may be named `create_time` or `createTime`.
    """
    errs: list[str] = []
    if not _type("root", data, dict, errs):
        return errs
    if not _has("root", data, "conversations", errs):
        return errs
    convs = data["conversations"]
    if not _type("root.conversations", convs, list, errs):
        return errs
    for i, entry in enumerate(convs):
        ctx = f"conversations[{i}]"
        if not _type(ctx, entry, dict, errs):
            continue
        convo = entry.get("conversation")
        if not _type(f"{ctx}.conversation", convo, dict, errs):
            continue
        _type(f"{ctx}.conversation.title", convo.get("title", ""), str, errs)
        for tkey in ("create_time", "createTime"):
            if tkey in convo and isinstance(convo[tkey], dict):
                _check_grok_date(f"{ctx}.conversation.{tkey}", convo[tkey], errs)
        responses = entry.get("responses")
        if not _type(f"{ctx}.responses", responses, list, errs):
            continue
        for j, r in enumerate(responses):
            rctx = f"{ctx}.responses[{j}]"
            if not _type(rctx, r, dict, errs):
                continue
            # unwrap if needed
            inner = r.get("response") if isinstance(r.get("response"), dict) else r
            sender = inner.get("sender")
            if sender not in ("human", "assistant"):
                errs.append(f"{rctx}.sender invalid: {sender!r}")
            _type(f"{rctx}.message", inner.get("message", ""), str, errs)
            for tkey in ("create_time", "createTime"):
                if tkey in inner and isinstance(inner[tkey], dict):
                    _check_grok_date(f"{rctx}.{tkey}", inner[tkey], errs)
    return errs


SCHEMA_VALIDATORS = {
    "openai": validate_openai_schema,
    "claude": validate_claude_schema,
    "grok":   validate_grok_schema,
    # 'gemini' target produced by convert_grok_for_import.py reuses the
    # openai shape, so callers can route it through validate_openai_schema.
    "gemini": validate_openai_schema,
}
