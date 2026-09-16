def vdr_failure_message(failed_items: list[dict], *, limit: int = 4000) -> str | None:
    if not failed_items:
        return None

    details = []
    for item in failed_items:
        name = str(item.get("file") or item.get("name") or "Untitled document")
        reason = str(item.get("error") or item.get("reason") or "Unknown processing error")
        details.append(f"{name}: {reason}")

    message = f"{len(failed_items)} VDR document(s) failed. " + "; ".join(details)
    return message if len(message) <= limit else message[: limit - 1] + "…"
