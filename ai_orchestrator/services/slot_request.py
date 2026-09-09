"""Cancellable llama.cpp requests with separately measured slot activity."""

import asyncio
import json
import time
from contextlib import suppress
from urllib.parse import urlsplit

import aiohttp
from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings


class SlotProcessingTimeout(TimeoutError):
    pass


class InferenceDeliveryError(RuntimeError):
    pass


def _merge_completion_chunk(chunk, content, thinking):
    """Append text from either an SSE delta or a complete chat response."""
    if not isinstance(chunk, dict):
        return None
    if chunk.get("error"):
        raise InferenceDeliveryError(str(chunk["error"]))
    choices = chunk.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") or {}
    message = choice.get("message") or {}
    content.append(str(delta.get("content") or message.get("content") or ""))
    thinking.append(str(
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or message.get("reasoning_content")
        or message.get("reasoning")
        or ""
    ))
    return choice.get("finish_reason")


def _completed_completion(response_data, content, thinking, finish_reason):
    """Return the internal response shape assembled from streamed chunks."""
    if response_data is None:
        return None
    return {
        **response_data,
        "choices": [{
            "message": {
                "content": "".join(content),
                "reasoning_content": "".join(thinking),
            },
            "finish_reason": finish_reason or "stop",
        }],
    }


class SlotClock:
    """Charge only intervals bounded by observations of our active task."""

    def __init__(self, baseline_task):
        self.baseline_task = baseline_task
        self.task_id = None
        self.active_seconds = 0.0
        self.last_at = None
        self.was_active = False
        self.seen_active = False

    def observe(self, slot, now):
        active = bool(slot and slot.get("is_processing"))
        task_id = slot.get("id_task") if slot else None
        if active and task_id != self.baseline_task and task_id is not None and self.task_id is None:
            self.task_id = task_id
        active = active and self.task_id is not None and task_id == self.task_id
        if active and self.was_active and self.last_at is not None:
            self.active_seconds += max(0, now - self.last_at)
        self.seen_active |= active
        self.was_active = active
        self.last_at = now
        return active


def execute_slot_request(provider, body, *, active_timeout, progress):
    return async_to_sync(_execute)(provider, body, active_timeout, progress)


async def _execute(provider, body, active_timeout, progress):
    poll = max(0.1, float(getattr(settings, "AI_SLOT_POLL_SECONDS", 1)))
    delivery_grace = float(getattr(settings, "AI_SLOT_RESPONSE_GRACE_SECONDS", 60))
    parsed = urlsplit(provider.base_url)
    slots_url = f"{parsed.scheme}://{parsed.netloc}/slots"
    notify = sync_to_async(progress, thread_sensitive=True)
    probe_timeout = aiohttp.ClientTimeout(total=5)
    request_timeout = aiohttp.ClientTimeout(total=None, connect=provider.connect_timeout, sock_read=None)

    async with aiohttp.ClientSession(headers=provider._headers()) as session:
        async def slots():
            async with session.get(slots_url, timeout=probe_timeout) as response:
                if response.status == 503:
                    return []
                response.raise_for_status()
                value = await response.json()
                if not isinstance(value, list):
                    raise InferenceDeliveryError("VM slot status was not a list.")
                return value

        async def post(slot_id):
            async with session.post(
                provider._get_completions_url(),
                json={**body, "id_slot": slot_id, "stream": True}, timeout=request_timeout,
            ) as response:
                if response.status == 503:
                    return None
                response.raise_for_status()
                response_text = bytearray()
                response_data = None
                response_content = []
                response_thinking = []
                finish_reason = None
                pending_line = ""

                async for raw_line in response.content.iter_chunked(8192):
                    response_text.extend(raw_line)
                    pending_line += raw_line.decode("utf-8", errors="replace")
                    while "\n" in pending_line:
                        line, pending_line = pending_line.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith(":") or line.startswith("event:"):
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            return _completed_completion(
                                response_data, response_content, response_thinking, finish_reason,
                            )
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            raise InferenceDeliveryError("VM returned malformed SSE data.")
                        response_data = chunk
                        finish_reason = _merge_completion_chunk(
                            chunk, response_content, response_thinking,
                        ) or finish_reason
                        # llama.cpp can finish generation while keeping the
                        # HTTP connection open. The finish marker is enough
                        # to hand the result to the segment checkpoint.
                        if finish_reason:
                            return _completed_completion(
                                response_data, response_content, response_thinking, finish_reason,
                            )

                if pending_line.strip():
                    line = pending_line.strip()
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        return _completed_completion(
                            response_data, response_content, response_thinking, finish_reason,
                        )
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise InferenceDeliveryError("VM returned an incomplete response body.") from exc
                    response_data = chunk
                    finish_reason = _merge_completion_chunk(
                        chunk, response_content, response_thinking,
                    ) or finish_reason

                if response_data and (response_content or finish_reason):
                    return _completed_completion(
                        response_data, response_content, response_thinking, finish_reason,
                    )
                # Some llama.cpp builds return one JSON body even when stream
                # was requested. Keep that compatibility path.
                if response_text:
                    try:
                        response_data = json.loads(response_text.decode("utf-8"))
                    except json.JSONDecodeError as exc:
                        raise InferenceDeliveryError("VM returned an incomplete response body.") from exc
                    return response_data
                raise InferenceDeliveryError("VM returned an empty response body.")

        while True:
            await notify(inference_state="waiting_for_slot")
            # Pre-submission waiting has no processing deadline. The callback
            # checks cancellation even while the VM is loading or unavailable.
            try:
                available = [s for s in await slots() if not s.get("is_processing")]
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                await notify(inference_state="waiting_for_service", inference_service_error=type(exc).__name__)
                available = []
            if not available:
                await asyncio.sleep(poll)
                continue

            selected = available[0]
            slot_id = selected["id"]
            clock = SlotClock(selected.get("id_task"))
            await notify(inference_state="submitted", inference_slot_id=slot_id)
            request = asyncio.create_task(post(slot_id))
            inactive_since = time.monotonic()
            unavailable_since = None
            try:
                while True:
                    done, _ = await asyncio.wait({request}, timeout=poll)
                    if done:
                        data = request.result()
                        if data is None:
                            # Loading/rejected requests did not execute and
                            # must not consume a processing retry or timeout.
                            await notify(inference_state="waiting_for_service")
                            await asyncio.sleep(poll)
                            break
                        timings = data.get("timings") or {}
                        measured_ms = int(clock.active_seconds * 1000)
                        if all(isinstance(timings.get(key), (int, float)) for key in ("prompt_ms", "predicted_ms")):
                            measured_ms = max(0, int(timings["prompt_ms"] + timings["predicted_ms"]))
                        await notify(
                            inference_state="response_received",
                            inference_active_ms=measured_ms,
                            inference_vm_task_id=clock.task_id,
                            inference_slot_processing=False,
                        )
                        return data
                    now = time.monotonic()
                    try:
                        snapshot = await slots()
                        slot = next((s for s in snapshot if s.get("id") == slot_id), None)
                        if slot is None:
                            raise InferenceDeliveryError("Assigned VM slot is unavailable.")
                        unavailable_since = None
                    except (aiohttp.ClientError, asyncio.TimeoutError, InferenceDeliveryError):
                        clock.observe(None, now)
                        unavailable_since = unavailable_since if unavailable_since is not None else now
                        await notify(inference_state="slot_status_unavailable")
                        if now - unavailable_since >= delivery_grace:
                            raise InferenceDeliveryError("Lost VM slot status while awaiting the response.")
                        continue
                    active = clock.observe(slot, now)
                    await notify(
                        inference_state="processing" if active else "awaiting_response" if clock.seen_active else "submitted",
                        inference_slot_id=slot_id,
                        inference_vm_task_id=clock.task_id,
                        inference_active_ms=int(clock.active_seconds * 1000),
                        inference_slot_processing=active,
                    )
                    if active:
                        inactive_since = None
                        if clock.active_seconds >= active_timeout:
                            await notify(inference_failure_kind="slot_processing_timeout")
                            raise SlotProcessingTimeout(f"VM processing exceeded {active_timeout:g} active seconds.")
                    else:
                        inactive_since = inactive_since if inactive_since is not None else now
                        if now - inactive_since >= delivery_grace:
                            await notify(inference_failure_kind="response_delivery")
                            raise InferenceDeliveryError("VM slot is idle but no completed HTTP response arrived.")
            finally:
                # Cancelling the async request closes its HTTP connection
                # before the caller releases the inference lease.
                if not request.done():
                    request.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await request
