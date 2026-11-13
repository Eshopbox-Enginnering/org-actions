# Eshopbox Cache Usage Guidelines — PRESERVE + ENHANCE

## Purpose
Standardize when and how Eshopbox services use caching while preserving existing working implementations. This document is independent of the 401 error-handling design.

## Existing infrastructure
- Eshopbox projects already share a common Redis pool. Reuse existing Redis clients, configuration, serialization and connection utilities.
- **PRESERVE:** Existing working cache keys, API contracts, TTL behavior and business flows. Do not rename keys or rewrite caching without a specific need and approval.
- **ENHANCE:** New caching flows and missing safeguards should follow the guidance below. Validate current behavior before making targeted fixes.

## When caching is appropriate
Cache data when it is read frequently, expensive to fetch or compute, sufficiently reusable, and can tolerate a defined freshness window. Do not cache sensitive or rapidly changing data without a clear isolation, invalidation and access-control strategy. Avoid caching data that is cheap to fetch or has poor reuse.

## Choosing a cache
| Situation | Recommended cache | Notes |
| --- | --- | --- |
| Shared across services or instances | Common Redis pool | Centralized access and TTL; use existing pool |
| Very hot, small, short-lived data | In-memory cache | Per-process only; stale or different values across instances are possible |
| Small reference tables / datasets | Redis with table- or flow-specific key | Cache-aside; invalidate on writes if freshness matters |
| Large filtered datasets | Redis with bounded buckets or pages | Do not put unbounded order lists into a single value |

In-memory cache is optional and depends on the flow. Set short TTLs, memory bounds and an eviction policy. Never assume it is shared between application replicas.

## Small dataset / table cache-aside flow
1. Construct a table-specific or flow-specific key that includes relevant tenant and filter dimensions.
2. Read Redis. If a valid value exists, reuse it.
3. On a miss, fetch the authoritative data from the database/service.
4. Store the result with an explicit TTL, then return it.
5. On updates, invalidate or update affected entries when waiting for TTL would be unacceptable.

Example: `reference:shipping-zones:{tenantId}:{version}`. Choose TTL based on update frequency and acceptable staleness; do not assume one universal TTL. Distinguish a cached empty result from a cache miss when useful.

## Large datasets and bucketed caching
Example query: yesterday's orders for a pincode. Prefer a scoped, descriptive key:

`orders:by-pincode:{tenantId}:{yyyy-MM-dd}:{pincode}:page:{pageNumber}:sort:{sortKey}`

- Include every parameter that changes the result (tenant, date, pincode, filters, page/cursor and stable sort order).
- Use bounded pages/buckets to avoid huge Redis values, long serialization times and high memory pressure.
- For cursor pagination, use a safe, deterministic cursor identifier in the key, or cache a compact summary/IDs and fetch details separately.
- If an order can change after being cached, define TTL/invalidation based on freshness requirements; “yesterday” does not automatically mean immutable.
- Consider caching aggregates (e.g. count by pincode/date) instead of full orders when that is what consumers need.
- Never expose another tenant's orders through a shared key or incorrectly scoped cache lookup.

## Key naming conventions
For **new** cache flows, prefer `{domain}:{purpose}:{tenant-or-scope}:{dimensions...}`. Keep existing key conventions unchanged unless a migration is approved. Use normalized dates and filters, avoid credentials or sensitive data in keys, and document ownership and purpose. If renaming a key is approved, plan compatibility/migration explicitly.

## TTL, invalidation and failures
- TTL should reflect freshness needs, data volatility and cost of recomputation. Do not introduce a universal TTL.
- Use write-triggered invalidation for data that must update promptly; avoid relying solely on TTL for strict correctness.
- For concurrent misses on expensive keys, consider single-flight or a bounded distributed lock to prevent a cache stampede.
- Handle Redis timeouts gracefully: where safe, fall back to the source of truth rather than failing the business operation.
- Monitor hit/miss rates, latency, key cardinality, memory usage, evictions, invalidation and errors; do not log sensitive payloads.

## AI implementation instructions
1. Inspect the existing flow and common Redis pool utilities before coding.
2. **PRESERVE** existing working cache keys, clients, TTL behavior and API contracts.
3. **ENHANCE** only missing or incorrect caching behavior; reuse shared utilities and avoid duplicate implementations.
4. For **new** flows, select Redis vs in-memory cache according to consistency, scope, size and TTL requirements.
5. Prefer cache-aside for small datasets and bounded buckets/pages for large results.
6. Report proposed key renames, TTL changes or invalidation changes for approval before applying them to existing flows.
7. Add tests for cache hit, miss, expiry, invalidation, concurrent misses, Redis failure and tenant isolation.

## Acceptance criteria
- Existing projects continue to use the common Redis pool without breaking changes.
- Existing working keys and behavior remain intact.
- New keys are scoped to the correct tenant and query dimensions.
- Cache misses fetch from the source and store with a defined TTL.
- Large cached results are bounded and have an explicit freshness strategy.
- Cache errors, eviction and invalidation are observable without exposing sensitive data.
