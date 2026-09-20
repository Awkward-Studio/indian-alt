def vdr_failure_message(
    failed_items: list[dict], *, limit: int = 4000, item_kind: str = "document"
) -> str | None:
    if not failed_items:
        return None

    details = []
    for item in failed_items:
        name = str(
            item.get("title")
            or item.get("file")
            or item.get("name")
            or f"Untitled {item_kind}"
        )
        reason = str(item.get("error") or item.get("reason") or "Unknown processing error")
        details.append(f"{name}: {reason}")

    item_label = item_kind if len(failed_items) == 1 else f"{item_kind}s"
    message = f"{len(failed_items)} VDR {item_label} failed. " + "; ".join(details)
    return message if len(message) <= limit else message[: limit - 1] + "…"
