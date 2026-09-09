# Segment queue incident, 9 September 2026

## Evidence

The two 4700 file audits show successful segment handoff:

- MIS segment 1 completed at 07:38:14.190 UTC; segment 2 acquired the lease at 07:38:14.710.
- Financial Model segment 1 completed at 07:41:40.188 UTC; segment 2 acquired the lease at 07:41:40.717.

The later active rows were failed during the administrator reset. An idle slot snapshot does not prove whether a request was still delivering its response, had not arrived, or had been interrupted. No worker stack trace was captured, so an indefinitely hung HTTP response is a failure hypothesis, not a proven cause for every reported pause.

The broker inspection after stopping the consumer found 17 visible low-priority messages and 17 unacknowledged deliveries. Purging visible queue lists alone had left delivery state that could be restored on worker restart. Both broker structures were cleared while the consumer was stopped. Result records and folder/document links were preserved.

The application cancellation guard checked only a metadata flag. Marking the parent FAILED during a reset did not prevent a redelivered child from running. Audit notifications also used synchronous calls to async websocket delivery without a deadline. Notification delivery could interrupt progress or delay the next segment even after model completion.

## Changed behavior

Document segments execute sequentially on the document task thread. Each completed segment is checkpointed before the next starts. Failure stops the document attempt; the existing bounded document retries reuse completed checkpoints. A completed model audit can recover a missing Redis checkpoint using its content/model/run checksum.

Terminal or deleted parents stop redeliveries and retries. Terminal segment writes are conditional, so a late successful response cannot overwrite an administrator cancellation. Truncated or incomplete evidence is failed rather than marked complete. The finalizer records completed_at and checks terminal state before changing the parent.

The VDR transport holds the shared lease, waits for an idle llama.cpp slot, submits with id_slot, and observes the new id_task. Only intervals with consecutive observations of that task processing count toward the processing allowance. Successful responses use server timing totals when supplied. Polling is approximate; it can undercount intervals shorter than the polling interval.

Queue, slot, and model-loading waits do not consume the processing allowance. Document-wide wall-clock limits are disabled. A slot that remains idle without a completed HTTP response for 60 seconds is a response-delivery failure, not a processing timeout. Lost slot status also has a separate failure threshold. Cancellation closes the HTTP connection before releasing the lease. The next segment waits for an idle slot, including when a cancelled remote request is still draining.

Lease renewal and release compare the serialized owner atomically in Redis. A waiting segment can remove a terminal owner's lease and then wait for the VM to become idle. Websocket audit notifications have a three-second deadline and cannot invalidate persisted completion.

## Operational settings

- One Celery worker consumer, concurrency 1 and prefetch 1.
- VDR_ARTIFACT_SEGMENT_TIMEOUT=1800 active seconds.
- AI_SLOT_POLL_SECONDS=1.
- AI_SLOT_RESPONSE_GRACE_SECONDS=60.
- AI_AUDIT_BROADCAST_TIMEOUT=3.
- CELERY_VISIBILITY_TIMEOUT=604800, seven days. This delays recovery after an abrupt worker loss if shutdown cannot restore deliveries; it prevents the default short visibility window from redelivering long documents prematurely.
- CELERY_RESULT_EXPIRES=2592000, thirty days, so chord results survive long folder runs.

A sudden worker death can still require recovery of a nonterminal audit and an expiring lease. These changes do not claim exactly-once execution across process or database failures. Checkpoints and terminal guards reduce duplicate work, and slot admission prevents a second segment being submitted while the observed VM slot is busy.

## Verification

Regression tests cover actual local HTTP transport behavior, queue/loading waits, active processing timeout, idle response failure, cancellation, consecutive requests, terminal persistence, lease release, truncated output, late completion after cancellation, and checkpoint recovery.

Two pre-existing general parser tests fail on both the committed baseline and this branch: test_parse_standard_response_salvages_truncated_repeated_extraction_payload and test_standard_response_marks_salvaged_extraction_as_completed. Segment checkpoints now use strict JSON decoding separately from that general repair behavior.

Two small synthetic requests also passed against the hosted database, Redis lease, and production VM from the local venv:

- d0c0bbd5-9c61-465a-8957-831d32a29ada: completed, VM task 11550, lease released.
- f453590c-7419-431e-a0e3-95fac7cb28bb: completed, VM task 11572, lease released.

Both used slot 0. Folder links remained 1179 and linked documents 60. These are synthetic handoff checks, not a claim that a full customer VDR was rerun.
