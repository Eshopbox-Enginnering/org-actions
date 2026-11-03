## Engineering standards (organization-managed)

Engineering standards are maintained in `docs/engineering-guidelines/` within this repository.

Before changing related application code, inspect the relevant guideline documents that are present there. Treat them as mandatory engineering requirements, subject to more specific repo-level constraints and the task requirements. In particular:

- Authentication or 401 responses: read `docs/engineering-guidelines/eshopbox-401-error-handling-preserve-enhance.md` if present.
- Caching, cache keys, eviction, or expiry: read `docs/engineering-guidelines/eshopbox-cache-usage-guidelines.md` if present.
- Logging or error instrumentation: read `docs/engineering-guidelines/Eshopbox_Structured_Logging_Guidelines_v2.md` if present.
- Outbound API throttling or HTTP 429 handling: read `docs/engineering-guidelines/Eshopbox_429_Outbound_API_Guidelines_v2.md` if present.
- Response status selection or error handling: read `docs/engineering-guidelines/HTTP_Status_Codes_Reference.md` if present.

When reviewing or implementing changes, check the applicable standards and flag deviations. Do not claim that an absent document was reviewed. Do not remove or override repository-specific instructions. If a guideline conflicts with the actual repository contract or an explicit user requirement, flag the conflict rather than silently changing behavior.
