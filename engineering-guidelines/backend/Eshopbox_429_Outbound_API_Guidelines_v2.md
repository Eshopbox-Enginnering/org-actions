# Eshopbox Outbound Third-Party API — 429 Too Many Requests Handling Guidelines

## 1. Objective
Define consistent handling when **Eshopbox makes outbound HTTP requests to external providers (marketplaces, courier partners, sales channels, and other third-party APIs) and the external provider responds with HTTP 429**. Preserve existing Pub/Sub, Cloud Tasks, Redis, retry and dead-letter queue (DLQ) implementations. This document is independent of the 401 error handling and cache usage guidelines.

## 2. Implementation principle: PRESERVE + ENHANCE

### PRESERVE
- Reuse existing Pub/Sub subscriptions, Cloud Tasks queues, retry policies, DLQ handoff, Redis common pool, and logging utilities.
- Preserve existing API contracts, cache key conventions, business logic and working retry paths.
- Do not introduce a parallel retry pipeline or change production queue settings without reviewing their current behavior.
- Inspect whether each flow uses Pub/Sub, Cloud Tasks, synchronous requests, or a combination before making changes.

### ENHANCE
- Add missing 429-specific handling, `Retry-After` support, bounded backoff with jitter, retry budgeting, safe coordination, structured logs, and trace propagation.
- Fix incomplete or incorrect retry and DLQ paths with minimal changes.
- For new flows, use these guidelines from the start and prefer the existing infrastructure and utilities.

## 3. Scope and example

**In scope:** `Eshopbox service -> outbound HTTP request -> external provider API -> HTTP 429 -> existing retry mechanism (Pub/Sub or Cloud Tasks, as applicable) -> configured DLQ on terminal failure`.

**Out of scope:** Implementing inbound 429 responses or rate limiting for Eshopbox internal APIs. Do not modify internal API throttling as part of this guideline.

## 5. Core rules
1. This guideline applies to **429 responses received from external/third-party APIs called by Eshopbox**, not rate limiting of inbound Eshopbox internal APIs. HTTP 429 indicates rate limiting; **do not regenerate Auth0 tokens solely because of a 429**.
2. Honor a valid downstream `Retry-After` value (seconds or HTTP-date), subject to overall workflow deadlines. If the wait exceeds the deadline, defer safely or fail; do not retry earlier than permitted.
3. Without `Retry-After`, use exponential backoff with jitter and a bounded retry budget.
4. Avoid synchronized retries from multiple instances. Use the existing Redis common pool for shared cooldown or rate coordination where the downstream limit is shared across callers; a distributed lock on every 429 is not required.
5. Preserve the same `traceId` throughout the original operation, retries, Pub/Sub/Cloud Tasks execution and DLQ handoff. No `messageId` or `parentMessageId` is required for tracing.
6. A 429 is **not** sent immediately to DLQ. Apply the configured retry policy first; route terminal failures to the flow's configured DLQ.
7. Prevent duplicate business actions and duplicate DLQ handoffs through existing idempotency/deduplication mechanisms.

## 5. End-to-end decision flow
1. An Eshopbox service calls an **external provider API** using the existing outbound HTTP client, provider credentials, and request context.
2. On success, return/acknowledge according to the existing flow and record relevant telemetry.
3. On 429, capture status, safe response metadata, `Retry-After`, and current retry count.
4. Determine the existing execution mechanism:
   - **Synchronous request:** retry in process only if the delay fits the request deadline and retry budget. Otherwise use an existing deferred-processing path if supported; do not silently queue a synchronous operation.
   - **Pub/Sub consumer:** follow its configured redelivery/retry policy, coordinating backoff where necessary. Do not acknowledge a message as successfully processed until the intended outcome or durable handoff is confirmed.
   - **Cloud Tasks handler:** use the existing queue retry policy and scheduling controls; return the appropriate non-success response when retry is required, without creating duplicate tasks.
5. If retry succeeds, complete the operation and log the recovery.
6. If the configured overall retry budget is exhausted or the failure is terminal, hand off to the existing DLQ mechanism once, and emit a structured terminal-failure log.
7. DLQ records remain available for investigation and controlled replay after the cause is addressed.

## 6. Retry timing and budgets
Suggested defaults for **new** flows only; review against downstream API limits, queue configuration, and existing service-level objectives before adoption:

| Parameter | Suggested default |
| --- | --- |
| In-process retries | Up to 3 when deadline permits |
| Initial fallback backoff | 1 second |
| Backoff growth | Exponential (2x) |
| Jitter | Randomized |
| Fallback per-attempt delay cap | 30 seconds |
| `Retry-After` | Respect valid server value; never cap it and retry early |
| Overall attempts | Explicitly bounded across in-process and deferred processing |

Retry counters must distinguish local attempts from queue delivery attempts while enforcing an overall finite policy. Queue-managed retries should not accidentally multiply an inner retry loop into excessive downstream calls.

## 7. Pub/Sub and DLQ
- Preserve the existing subscription retry policy and dead-letter topic configuration.
- Ensure retryable 429 failures follow the established nack/redelivery behavior.
- After the configured delivery limit, use the existing dead-letter routing. Verify dead-letter policy configuration and operational permissions.
- Do not manually publish to DLQ in parallel with native dead-letter routing unless the existing design explicitly requires it and prevents duplicates.
- Propagate `traceId` through message attributes/payload according to the existing contract.

## 8. Cloud Tasks and DLQ
- Preserve existing Cloud Tasks queue retry settings and handler semantics.
- Cloud Tasks does not offer the same native dead-letter-topic mechanism as Pub/Sub. Eshopbox's Cloud Tasks terminal-failure-to-DLQ behavior must use its **existing application-managed handoff or monitoring/reconciliation workflow**.
- Inspect how the current implementation detects terminal failure; do not assume a final attempt can always be identified solely from one handler invocation.
- Make DLQ handoff durable and idempotent, and prevent a task from being marked complete before the required failure record is safely persisted.
- Propagate `traceId` in task headers or payload using existing conventions.

## 9. Redis-based shared rate coordination
Use Redis only when several instances/services share the same downstream quota and coordination materially reduces repeated 429s.

- Scope cooldown keys by **external provider, endpoint/quota group, credential/account/tenant**, and rate-limit dimension as applicable.
- Reuse existing cache key naming and Redis common-pool utilities.
- Store a cooldown-until timestamp or equivalent bounded state with TTL.
- Ensure concurrent updates cannot shorten an already-active cooldown unintentionally.
- Do not store secrets in keys or logs.
- For new implementations, document the exact quota scope, TTL, failure behavior and ownership.

## 10. Structured logging and tracing
Reuse the repository's existing structured logger. The same `traceId` must be present in every relevant log across the entire logical flow.

Recommended searchable fields where available (here `apiPath` means the **external API endpoint path**, not an Eshopbox internal route):
- `traceId`, `message`, `context.eventName`, `datetime`, `environment`, `level_name`
- `apiPath`, `httpMethod`, `statusCode`, `errorCode`
- `context.retryAttempt`, `context.maxRetries`, `context.retryAfterMs`, `context.retryMechanism`
- `context.queueName`, `context.durationMs`, `context.dlqHandoffStatus`, `context.externalProvider`

Use stable events such as `DOWNSTREAM_RATE_LIMITED`, `DOWNSTREAM_RETRY_SCHEDULED`, `DOWNSTREAM_RETRY_SUCCESS`, and `DOWNSTREAM_RETRY_EXHAUSTED`. Use WARNING for recoverable rate limiting and ERROR for terminal failures. Avoid duplicate final error logs across layers.

Example (sanitized):

```json
{
  "jsonPayload": {
    "message": "DOWNSTREAM_RETRY_EXHAUSTED",
    "level_name": "ERROR",
    "datetime": "2026-11-02T16:00:00+05:30",
    "environment": "production",
    "traceId": "trace-abc123",
    "apiPath": "/v1/shipments",
    "httpMethod": "GET",
    "statusCode": 429,
    "errorCode": "EXTERNAL_API_RATE_LIMIT_EXHAUSTED",
    "extra": {},
    "context": {
      "eventName": "DOWNSTREAM_RETRY_EXHAUSTED",
      "retryAttempt": 3,
      "maxRetries": 3,
      "retryAfterMs": 30000,
      "retryMechanism": "CLOUD_TASKS",
      "externalProvider": "example-courier",
      "dlqHandoffStatus": "CONFIRMED",
      "durationMs": 1200
    }
  }
}
```

Never log access tokens, authorization headers, credentials, sensitive customer data, or unsanitized request/response payloads. Preserve any existing trace/correlation conventions rather than introducing conflicting IDs.

## 11. DLQ operational requirements
- Store sufficient safe metadata to diagnose and replay the failed operation, including original `traceId`, failure category, attempts, downstream target and sanitized business identifiers.
- Prevent duplicate DLQ records and duplicate replay side effects.
- Provide a documented replay procedure, ownership, monitoring and alerting for DLQ backlog.
- DLQ replay must obey current downstream rate limits; it should not create a new 429 storm.

## 12. AI-assisted implementation instructions
Before changing code:
1. Identify the **outbound third-party API integration**, provider rate-limit policy, and whether its caller uses Pub/Sub, Cloud Tasks or direct HTTP calls.
2. Locate existing retry, rate-limit, Redis, structured logging and DLQ utilities.
3. Produce a gap analysis: already implemented / partial / missing / behavioral change.
4. Preserve working behavior and naming conventions.
5. Enhance only missing or incorrect 429 handling, including terminal DLQ routing and single-`traceId` propagation.
6. Show the affected files and potential behavior changes before implementing risky modifications.
7. Add focused tests and verify no duplicate retries, queue messages, or DLQ handoffs.

## 13. Acceptance tests
- Valid `Retry-After` seconds and HTTP-date are respected.
- Missing/invalid `Retry-After` uses bounded exponential backoff with jitter.
- Long delays do not block a synchronous request beyond its deadline.
- 429 does not trigger Auth0 token regeneration.
- Pub/Sub 429 follows the existing redelivery policy and terminal DLQ routing.
- Cloud Tasks 429 follows queue retry settings and existing application-managed DLQ handoff.
- Retry budgets remain finite across nested retry layers.
- Concurrent callers do not cause a synchronized retry storm.
- Retry success completes normally without DLQ handoff.
- Terminal failure is routed to DLQ exactly once to the extent supported by idempotent handoff design.
- Same `traceId` is searchable across all services, retries and DLQ records.
- Logs are structured, sanitized and do not duplicate terminal errors.
- Existing API contracts, Redis keys, queue settings and working flows remain compatible.

## 14. Flow-specific configuration to confirm
- External provider name, endpoint, documented quota scope, and `Retry-After` behavior.
- Existing queue/subscription names, retry schedules, delivery limits and deadlines.
- Downstream quota scope and whether shared Redis cooldown is needed.
- Existing Cloud Tasks terminal-failure detection and DLQ handoff mechanism.
- Overall retry budget, alert thresholds and DLQ replay ownership.

These are implementation configuration points, not permission to replace existing working infrastructure.
