## . Structured Logging

Every changed flow must produce structured logs in a consistent `jsonPayload` format.

The log must be searchable using common fields such as:

- `message`
- `eventName`
- `apiPath`
- `accountSlug`
- `clientID`
- `channelID`
- `statusCode`
- `errorCode`
- `traceId`
- `environment`

Do not store all searchable information only inside a text message.

### Required JSON structure

Every structured log should follow this structure wherever the corresponding value is available:

```
{
  "jsonPayload": {
    "accountSlug": "vi-john-group",
    "apiPath": "api/v2/order",
    "channelID": "CH6724",
    "clientID": "9e7f4cf27fca40fdc5ee8f3723f9be67",
    "context": {
      "customerOrderNumber": "63051d4c-270d-470f-9e71-7924605818d0",
      "durationMs": 1010.12,
      "eventName": "ORDER_CREATE_FAIL",
      "exceptionType": "Illuminate\\Database\\UniqueConstraintViolationException",
      "requestBody": {},
      "responseBody": {}
    },
    "datetime": "2026-07-24T16:15:47.029133+05:30",
    "environment": "production",
    "errorCode": "ORDER_DUPLICATE_QUERY_EXCEPTION",
    "extra": {},
    "httpMethod": "POST",
    "level": 400,
    "level_name": "ERROR",
    "message": "ORDER_CREATE_FAIL",
    "statusCode": 422,
    "traceId": "9d0f859c3d2536d507940eaec4364be7"
  }
}
```

### Mandatory top-level fields

Include the following fields in every structured log where applicable:

| **Field**     | **Required**          | **Description**                                               |
| ------------- | --------------------- | ------------------------------------------------------------- |
| `message`     | ✅                     | Stable searchable event key, for example `ORDER_CREATE_FAIL`  |
| `level_name`  | ✅                     | `INFO`, `WARNING`, or `ERROR`                                 |
| `level`       | ✅                     | Numeric or project-standard log level                         |
| `datetime`    | ✅                     | ISO-8601 timestamp with timezone                              |
| `environment` | ✅                     | `production`, `staging`, `development`, etc.                  |
| `traceId`     | ✅                     | Trace identifier shared across all logs for the request       |
| `apiPath`     | API flows             | API route without host name                                   |
| `httpMethod`  | API flows             | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, etc.                 |
| `statusCode`  | API/integration flows | Actual HTTP or final business response code                   |
| `accountSlug` | Account flows         | Workspace or account slug                                     |
| `clientID`    | Client flows          | Client identifier                                             |
| `channelID`   | Channel flows         | Channel identifier                                            |
| `errorCode`   | Failure logs          | Stable internal error code                                    |
| `extra`       | ✅                     | Additional optional metadata; use an empty object when unused |

### Mandatory `context` fields

The `context` object must contain flow-specific diagnostic details.

Include these fields wherever applicable:

| **Field**             | **Required**   | **Description**                              |
| --------------------- | -------------- | -------------------------------------------- |
| `eventName`           | ✅              | Same stable event key used for the operation |
| `durationMs`          | ✅              | Total processing or downstream-call duration |
| `requestBody`         | Request flows  | Sanitized request data                       |
| `responseBody`        | Response flows | Sanitized response data                      |
| `exceptionType`       | Failure logs   | Fully qualified exception class/type         |
| `customerOrderNumber` | Order flows    | Customer order identifier                    |
| `orderId`             | Order flows    | Internal order identifier                    |
| `shipmentId`          | Shipment flows | Internal shipment identifier                 |
| `externalShipmentId`  | Shipment flows | External shipment identifier                 |
| `batchId`             | Batch flows    | Batch or job identifier                      |
| `accountId`           | Account flows  | Internal account ID                          |
| `requestId`           | Request flows  | Request-level unique identifier              |

Additional business-specific fields may be added without changing the common structure.

### Request and response rules

- &#x20;Every relevant log contains both `requestBody` and `responseBody`.
- &#x20;Request data is captured before processing or before the downstream call.
- &#x20;Response data is captured after processing or after the downstream call.
- &#x20;Failure logs include the downstream or application error response.
- &#x20;Fallback responses are logged.
- &#x20;Empty request or response values use `{}`, `[]`, or `null` consistently.
- &#x20;Prefer JSON objects instead of escaped JSON strings.
- &#x20;Do not double-encode request or response JSON.
- &#x20;Large payloads are truncated or summarized when required.
- &#x20;Binary data, PDFs, files, and base64 content are never written directly to logs.

Preferred:

```
"requestBody": {
  "externalChannelID": "CH6724",
  "customerOrderNumber": "63051d4c-270d-470f-9e71-7924605818d0"
}
```

Avoid:

```
"requestBody": "{\"externalChannelID\":\"CH6724\"}"
```

### Sensitive-data masking

Before logging `requestBody` or `responseBody`, mask or remove sensitive and personal information.

Never log:

- Passwords
- Access tokens
- Refresh tokens
- Authorization headers
- API keys
- Client secrets
- Cookies or session IDs
- OTP values
- Card details
- Bank details
- Full authentication payloads

Mask personal information where it is not required for debugging:

- Customer phone number
- Email address
- Full delivery or billing address
- Customer name

Example:

```
{
  "contactPhone": "******9999",
  "email": "x***@gmail.com",
  "addressLine1": "REDACTED",
  "addressLine2": "REDACTED"
}
```

### Success logging

- &#x20;Successful operations use `level_name: "INFO"`.
- `message` and `context.eventName` contain a stable success event key.
- `requestBody` and `responseBody` are included.
- `statusCode` contains the final response code.
- `durationMs` is included.
- &#x20;Important account, order, shipment, channel, client, and batch identifiers are included.

Example:

```
{
  "message": "ORDER_CREATE_SUCCESS",
  "level_name": "INFO",
  "statusCode": 200,
  "context": {
    "eventName": "ORDER_CREATE_SUCCESS",
    "durationMs": 740.45,
    "requestBody": {},
    "responseBody": {}
  }
}
```

### Failure logging

- &#x20;Failure operations use `level_name: "ERROR"`.
- `message` contains a stable failure event key such as `ORDER_CREATE_FAIL`.
- `errorCode` contains a stable internal error code.
- `statusCode` contains the actual final error response code.
- `exceptionType` contains the exception class/type.
- &#x20;The exception object and stack trace are logged using the existing logging framework.
- `requestBody` and `responseBody` are included.
- &#x20;The error is not logged only as free-form text.
- &#x20;Handled and unhandled exception paths are identified.

Example:

```
{
  "message": "ORDER_CREATE_FAIL",
  "level_name": "ERROR",
  "errorCode": "ORDER_DUPLICATE_QUERY_EXCEPTION",
  "statusCode": 422,
  "context": {
    "eventName": "ORDER_CREATE_FAIL",
    "exceptionType": "Illuminate\\Database\\UniqueConstraintViolationException",
    "durationMs": 1010.12,
    "requestBody": {},
    "responseBody": {
      "customerOrderNumber": [
        "The customerOrderNumber has already been taken."
      ]
    }
  }
}
```

### Logging style

Use the logging style already present in the class.

If the class uses:

```
com.inventoryallocation.util.StructuredLogger
```

use:

```
StructuredLogger.logSuccess(...);
StructuredLogger.logError(...);
```

**This repo does not have that class.** For structured `message: "ERROR"` logs covering 4xx/5xx responses, use the shared utility added 2026-07-27 instead of hand-building the `jsonPayload` map inline:

```
com.shipping.util.StructuredLogUtil
```

- If you already have a resolved `ServiceException` (e.g. from `ExceptionHandler.handleException(ex)`), call:
  ```
  StructuredLogUtil.logError(logger, apiPath, httpMethod, resolvedEx, requestBody, accountId, accountSlug);
  ```
  `resolvedEx.getStatusCode()` becomes the logged `statusCode`, so it always matches what the caller actually receives instead of being guessed from the raw exception.
- If you're rejecting a request directly with a status code and no `ServiceException` (e.g. a servlet filter), call the status-code overload:
  ```
  StructuredLogUtil.logError(logger, apiPath, httpMethod, statusCode, errorCode, message, cause, requestBody, accountId, accountSlug);
  ```
- If you're a servlet filter that also needs to write the JSON error body to the client, call:
  ```
  StructuredLogUtil.sendJsonErrorResponse(logger, request, response, statusCode, reason, message);
  ```
  which logs and writes the response in one call — see `MyCustomFilter.sendUnAuthorisedResponse`/ `sendForbiddenResponse` for the reference usage.

Do not hand-roll a new `jsonPayload`/`HashMap` structure for a 4xx/5xx log in a new class — extend `StructuredLogUtil` instead so every endpoint/filter produces the same shape.

Otherwise (plain INFO/WARNING logs, not a structured 4xx/5xx error), use the existing `java.util.logging` style already present in the class:

```
logger.log(Level.INFO, "Operation completed");
logger.log(Level.SEVERE, "Operation failed", ex);
```

Do not introduce a third logging framework only for a new change.

### Logging review checklist

- &#x20;Log follows the common `jsonPayload` structure.
- `message` is a stable, searchable event key.
- `context.eventName` is present.
- `requestBody` is present and sanitized.
- `responseBody` is present and sanitized.
- `traceId` is present.
- `durationMs` is present.
- `environment` is present.
- `apiPath` and `httpMethod` are present for API calls.
- `statusCode` is present.
- `errorCode` and `exceptionType` are present for failures.
- &#x20;Account, client, channel, order, shipment, or batch identifiers are included where applicable.
- &#x20;No token, secret, or sensitive customer data is exposed.
- &#x20;Request and response JSON are not double-encoded.
- &#x20;Every catch block logs the exception object.
- &#x20;Exceptions are not silently swallowed.

Use this format in the class change log:

```
**Structured logging**

| Code path | Request logged? | Response logged? | Handled / Unhandled | Event / Error code | Status |
|---|---|---|---|---|---|
| Success path | ✅ | ✅ | Handled | `ORDER_CREATE_SUCCESS` | ✅ |
| Duplicate-order exception | ✅ | ✅ | Handled | `ORDER_CREATE_FAIL` / `ORDER_DUPLICATE_QUERY_EXCEPTION` | ✅ |
| Unexpected cast failure | ❌ | ❌ | Unhandled | Not available | ⚠️ Open gap |
```

## Distributed Trace and Message Identification (PRESERVE + ENHANCE)

### Objective
Every new distributed flow must support end-to-end correlation across HTTP services, queues, workers, and scheduled jobs. Existing flows should be enhanced when tracing is missing, without breaking their current IDs or dashboards.

### Identifier semantics
- `traceId`: Stable identifier for one end-to-end logical operation. Propagate it unchanged across downstream calls and asynchronous handoffs. Use an existing trace ID if present; otherwise generate one at the entry boundary. Prefer the project's established distributed tracing standard (such as W3C Trace Context) when available.
- `messageId`: Unique identifier for each individual published message/event. Generate a new one for each distinct message; retain the original message ID when redelivering the same message. Do not use a new message ID for every log line.
- `requestId`: Identifier for an individual incoming HTTP request or processing attempt, where the project already uses one.
- `parentMessageId`: Optional reference to the message that caused a subsequent message to be published.

### PRESERVE — existing flows
1. Reuse existing `traceId`, correlation headers, middleware, logger fields, and propagation mechanisms.
2. Do not rename existing IDs or replace working tracing instrumentation.
3. Recommend adding missing propagation at service boundaries and async producer/consumer boundaries; implement compatibly with existing conventions.
4. If a downstream service cannot receive tracing metadata, log the boundary and available correlation IDs without changing the API contract unexpectedly.

### ENHANCE — new flows
1. At ingress, accept and validate the trusted tracing context; otherwise generate a unique trace ID.
2. Attach the same `traceId` to structured logs and propagate it through internal HTTP headers or messaging metadata.
3. On publishing a new event, generate a `messageId`, preserve `traceId`, and set `parentMessageId` where applicable.
4. On consuming an event, log its `traceId` and `messageId`; preserve the message ID across redeliveries, and use a separate attempt identifier if needed.
5. Ensure background jobs have an identifiable root trace even when no incoming HTTP request exists.
6. Keep identifiers non-sensitive and avoid using tokens, personal data, or secrets as IDs.

### Example
```json
{
  "jsonPayload": {
    "message": "SHIPMENT_SYNC_FAIL",
    "level_name": "ERROR",
    "traceId": "trace-123",
    "messageId": "msg-003",
    "context": {
      "eventName": "SHIPMENT_SYNC_FAIL",
      "parentMessageId": "msg-002",
      "requestId": "attempt-456",
      "durationMs": 1250,
      "requestBody": {},
      "responseBody": {}
    }
  }
}
```

### Acceptance checks
- Logs from all participating services can be searched by a shared `traceId`.
- Distinct published messages have distinct `messageId` values; redelivery preserves message identity.
- Existing trace/correlation conventions remain compatible.
- Missing trace metadata is created at the entry boundary and propagated when supported.
- Tracing metadata does not expose credentials or personal information.
