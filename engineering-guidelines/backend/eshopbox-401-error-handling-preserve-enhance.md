# Eshopbox Internal API — 401 Error Handling Implementation Guidelines

## 1. Objective

Apply a **PRESERVE + ENHANCE** strategy to Eshopbox internal API authentication. Preserve existing working authentication flows, Redis cache key naming, API contracts, and unrelated business logic. Correct incomplete or incorrect 401 retry and token regeneration behavior. Any newly added authentication-related code must follow the enhanced design below.

## 2. PRESERVE — Existing Implementations

- Preserve existing architecture, service boundaries, authentication behavior, API contracts, and Redis cache key naming conventions.
- Reuse existing Auth0 token generation, Redis caching, HTTP client, and logging utilities.
- Do not duplicate or rewrite working functionality or modify unrelated business logic.
- Preserve existing static token TTL behavior where already working; do not migrate it to dynamic expiry solely for consistency.
- **Exception:** If retry or regeneration is missing, incorrect, unsafe under concurrency, or can loop indefinitely, make the smallest targeted correction necessary.

### Existing flow validation

1. Check the existing Redis cache key for an access token.
2. If a token exists, call the downstream API with it.
3. On cache miss, generate a token through the existing Auth0 integration, store it in Redis, and call the downstream API.
4. On downstream HTTP 401, attempt token recovery using the concurrency-safe flow in Section 4.
5. Retry the downstream request **at most once** with a refreshed token.
6. If the retry returns 401, stop; emit a structured failure log and return the failure according to the existing error contract.
7. Do not change code that already meets these requirements.

## 3. ENHANCE — Missing Functionality and New Code

- Add missing 401 recovery, retry limits, Redis concurrency protection, and structured logging to existing implementations.
- All **new** token-caching implementations should derive Redis TTL from the actual Auth0 token expiry (`expires_in` or a validated `exp` claim), without an additional safety buffer. Do not silently alter TTL in existing flows.
- For new cache keys, follow the repository's established naming convention while ensuring the token context (tenant/client/audience/scopes as applicable) cannot collide.
- Use existing shared authentication utilities rather than introducing parallel implementations.

## 4. Concurrency-Safe 401 Recovery

On receiving a downstream 401 for token `T_failed`:

1. Read the token currently stored under the **existing** Redis cache key.
2. If a different token is already cached, use it for the one permitted downstream retry; do not regenerate.
3. Otherwise attempt to acquire a **per-cache-key Redis distributed lock**, using an atomic acquisition with a lease/expiration and a unique owner identifier.
4. If lock acquisition succeeds, **recheck the cache**. If it now contains a token different from `T_failed`, reuse that token. Otherwise generate a new Auth0 token and write it to the existing cache key with the applicable TTL.
5. Release the lock only if still owned by the current request, using an atomic ownership check. The lock lease must be configured separately from the waiting timeout and must cover expected Auth0 latency (or be safely extended).
6. If another request owns the lock, poll Redis for up to **2 seconds** for a token different from `T_failed`. Reuse it if available.
7. If no refreshed token becomes available within 2 seconds, stop recovery, emit a structured timeout/failure log, and follow the existing error-handling contract. Do not regenerate outside the lock.
8. Retry the downstream API **once at most** using the recovered token.
9. If the retried request returns 401, do not refresh again; log the second 401 and propagate/translate the failure according to the existing API contract.

**Important:** Do not blindly delete the cache key on 401. Another request may already have replaced it with a valid token. If invalidation is necessary, use compare-and-delete semantics so a newer token is never removed.

## 5. Auth0 Generation Failures

- Invalid `client_id`, `client_secret`, audience, or other Auth0 configuration errors must not cause infinite regeneration or downstream retries.
- Record a sanitized structured error and emit monitoring/alerts through existing infrastructure.
- Never log tokens, client secrets, authorization headers, or other sensitive values.
- Preserve existing error response contracts.

## 6. Structured Logging and Monitoring

Use the project's existing structured logging framework. Record relevant events for:

- Redis token cache hit and miss
- Downstream 401 received
- Token refresh requested, started, succeeded, or failed
- Lock acquired, contention, timeout, and release failure
- Retry attempted, succeeded, or failed
- Second downstream 401
- Auth0 invalid credentials/configuration errors

Recommended fields (where available): `event`, `service`, `downstream_service`, `endpoint_template`, `http_method`, `correlation_id`, `cache_key_identifier` (non-sensitive), `status_code`, `retry_count`, `error_category`, `outcome`, `duration_ms`.

Example:

```json
{
  "event": "internal_api.auth.retry_failed",
  "service": "calling-service",
  "downstream_service": "internal-api",
  "endpoint_template": "/v1/example/{id}",
  "http_method": "GET",
  "correlation_id": "request-correlation-id",
  "status_code": 401,
  "retry_count": 1,
  "error_category": "DOWNSTREAM_UNAUTHORIZED_AFTER_REFRESH",
  "outcome": "failed"
}
```

Use sanitized endpoint templates, not full URLs with secrets or sensitive query parameters. Never log raw token values or credentials.

## 7. AI-Assisted Implementation Rules

### Phase 1 — Inspect

Locate the current Auth0 integration, Redis cache key construction, TTL behavior, downstream HTTP calls, 401 handling, retry policy, and logging infrastructure.

### Phase 2 — Gap analysis

Classify each requirement as **already implemented**, **partially implemented**, **missing**, or **requires targeted correction**. Distinguish existing-flow behavior from newly introduced code.

### Phase 3 — Minimal changes

- Preserve correct existing behavior and cache key conventions.
- Fix incorrect or missing 401 retry and regeneration.
- Add concurrency protection and structured logging where absent.
- For new authentication code, follow the ENHANCE requirements.
- Do not change static TTL or API contracts without explicit approval.
- Avoid unrelated refactoring or duplicate token management paths.

### Phase 4 — Verification

Run existing tests and add focused coverage for the scenarios below. Review changed files for compatibility and unintended behavior changes.

## 8. Acceptance Criteria / Test Scenarios

1. Cache hit: downstream request uses existing cached token.
2. Cache miss: token is generated, cached, and used.
3. First 401: token is refreshed/recovered and downstream call retried once.
4. Second 401: no further refresh or retry; structured failure log emitted.
5. Multiple concurrent 401s: only one regeneration occurs for a given cache key when contention overlaps.
6. Waiting request sees refreshed token in Redis and reuses it.
7. Waiting request exceeds 2 seconds: no regeneration outside lock; failure logged.
8. Lock holder rechecks cache before regenerating.
9. Lock expiration/ownership safety: a request cannot release another request's lock.
10. A 401 does not delete a newer token cached by another request.
11. Invalid Auth0 credentials: bounded failure with sanitized logs; no infinite loop.
12. Existing cache key names and API contracts remain unchanged.
13. Existing static TTL remains unchanged unless explicitly approved.
14. New token-caching code uses actual token expiry for TTL.
15. Structured logs cover cache, refresh, retry, and failure outcomes without leaking secrets.

## 9. Out of Scope

- Changing existing Redis cache key naming solely to match a new convention.
- Migrating existing working static TTL to dynamic TTL without approval.
- Changing API response contracts or unrelated business logic.
- Adding method-specific POST/PATCH retry policy in this document.

## 10. Implementation Principle

> **PRESERVE what already works. ENHANCE what is missing or incorrect. New authentication-related code must follow the enhanced 401 handling design.**
