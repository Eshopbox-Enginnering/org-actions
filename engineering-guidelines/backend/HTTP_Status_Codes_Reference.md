# HTTP Response Status Codes

A complete reference for 2xx, 3xx, 4xx, and 5xx status codes

HTTP status codes are three-digit numbers returned by a server in response to a client's request. They are grouped into five classes based on the first digit. This document covers the four most commonly encountered families.

## ✅  2xx — Success

The server received, understood, and accepted the request. The specific code tells you what happened.

| Code | Name | Meaning | Server Stance | Example Scenario |
| --- | --- | --- | --- | --- |
| 200 | OK | Request succeeded. Response body contains the requested data. | "Here's exactly what you asked for." | GET /users/1 returns the user object. |
| 201 | Created | Resource successfully created. Location header often points to the new resource. | "Done — I made what you asked for." | POST /articles creates a new article. |
| 202 | Accepted | Request received but processing isn't complete yet. Common for async jobs. | "Got it — I'll handle this in the background." | POST /reports/generate kicks off a background job. |
| 204 | No Content | Request succeeded but there is no body to return. | "Done — nothing to send back." | DELETE /sessions/42 logs the user out. |
| 206 | Partial Content | Server is delivering only part of the resource (Range header used by client). | "Here's the chunk you asked for." | Streaming video byte-range request. |

## ↩️  3xx — Redirection

The client must take an additional step. Whether the redirect is permanent or temporary, and whether the HTTP method is preserved, varies by code.

| Code | Name | Meaning | Server Stance | Example Scenario |
| --- | --- | --- | --- | --- |
| 301 | Moved Permanently | Resource has been permanently moved to a new URL. | "It lives somewhere else now — forever." | Old blog URL redirects to new domain. |
| 302 | Found (Temp Redirect) | Resource temporarily lives at a different URL. Keep using the original. | "Go here for now, but check back later." | Login redirect before the user authenticates. |
| 304 | Not Modified | Client's cached version is still fresh. No body is sent. | "Your cache is still good — use it." | Browser sends If-None-Match; asset hasn't changed. |
| 307 | Temporary Redirect | Redirect to new URL, preserving the original HTTP method. | "Go there temporarily, same method please." | POST form redirected; must stay POST. |
| 308 | Permanent Redirect | Permanent redirect that preserves HTTP method (like 301 but strict). | "It moved forever — same method required." | API endpoint permanently migrated, POST must stay POST. |

## 🚫  4xx — Client Error

The request itself is the problem. The server is fine; the client sent something wrong, missing, or unauthorized.

| Code | Name | Meaning | Server Stance | Example Scenario |
| --- | --- | --- | --- | --- |
| 400 | Bad Request | Server can't process the request due to malformed syntax or invalid parameters. | "I can't read what you sent — check your request." | JSON body has a syntax error. |
| 401 | Unauthorized | Authentication is required and either missing or failed. | "Show me your ID." | Accessing a private profile while logged out. |
| 403 | Forbidden | Server understood the request but refuses to authorize it. | "Your ID works, but you can't enter this room." | A regular user trying to access the admin panel. |
| 404 | Not Found | The requested resource does not exist on the server. | "Nothing here at that address." | GET /users/9999 when that user doesn't exist. |
| 405 | Method Not Allowed | The HTTP method used is not supported on this endpoint. | "You can't do that here — wrong action." | PUT on a read-only endpoint. |
| 408 | Request Timeout | Client took too long to send the request. | "You took too long — I'm closing the connection." | Slow upload times out before completing. |
| 409 | Conflict | Request conflicts with the current state of the resource. | "That would cause a collision — sort it out first." | Trying to create a username already taken. |
| 410 | Gone | Resource existed but has been permanently deleted. | "It was here — it's gone for good." | A deleted blog post with a permanent removal. |
| 422 | Unprocessable Entity | Syntax is correct but the data fails semantic validation. | "Come in, but your paperwork has mistakes." | A logged-in user submitting a form with a negative age. |
| 429 | Too Many Requests | Client has exceeded the rate limit. Retry-After header often included. | "Slow down — you're sending too many requests." | API key exceeds 100 requests per minute. |

## 💥  5xx — Server Error

The request was valid, but something on the server side failed. Usually transient (503, 504) or a bug (500). Clients should retry with backoff where appropriate.

| Code | Name | Meaning | Server Stance | Example Scenario |
| --- | --- | --- | --- | --- |
| 500 | Internal Server Error | Generic server-side failure. Unexpected condition encountered. | "Something broke on our end — not your fault." | Unhandled exception in application code. |
| 501 | Not Implemented | Server doesn't support the functionality required to fulfill the request. | "I don't know how to do that." | An unsupported HTTP method like PATCH on a basic server. |
| 502 | Bad Gateway | Upstream server returned an invalid response to the proxy or gateway. | "The service behind me is misbehaving." | Nginx gets a garbled response from the app server. |
| 503 | Service Unavailable | Server is temporarily unable to handle requests (overload or maintenance). | "I'm temporarily down — try again soon." | Server under high load during a traffic spike. |
| 504 | Gateway Timeout | Gateway or proxy timed out waiting for an upstream response. | "I waited too long for the backend — it's unresponsive." | Load balancer times out waiting for a slow database query. |
| 507 | Insufficient Storage | Server cannot store the representation needed to complete the request. | "No room left on the server." | Uploading a large file when disk is full. |

## Quick Reference Summary

| Family | Meaning | Who is at fault? | Retry? |
| --- | --- | --- | --- |
| 2xx Success | The request was understood and accepted. | Nobody — it worked. | N/A |
| 3xx Redirect | The client must follow a redirect to complete the request. | Nobody — follow the Location header. | Follow redirect |
| 4xx Client Error | The request itself is broken or unauthorized. | Client — fix the request. | Only after fixing (except 429) |
| 5xx Server Error | The request was valid but the server failed. | Server — not the client's fault. | Yes, with exponential backoff. |

Note: 401 vs 403 — Despite the name "Unauthorized", 401 is about authentication (who are you?), while 403 is about authorization (you're known, but you can't do this). 422 vs 400 — 400 means the syntax is broken; 422 means the syntax is fine but the data is semantically invalid.
