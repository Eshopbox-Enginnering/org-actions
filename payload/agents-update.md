## Cloud SQL standards

Apply these checks whenever you write **or review** code that touches the database.

### Query optimization

Flag any of the following as bugs, not style issues:

- **Possible missing index** — `WHERE` or `JOIN` on a column that is not an obvious primary key or foreign key. Leave a comment: *"Verify that `<column>` is indexed — a full table scan here will be slow at production volume."* Let the reviewer confirm.
- **N+1 pattern** — a query runs inside a loop (`for`, `forEach`, `map`, `while`). Extract it and batch.
- **`SELECT *`** where only specific columns are consumed. Replace with an explicit column list.
- **Unbounded query** — no `LIMIT` on a table that can grow (e.g. orders, events, inventory). Add a limit or cursor.

### Session / connection hygiene

Every opened connection or session must be closed on **all** exit paths — success and failure.

- Wrap database work in `try/finally` (or equivalent) so `release()` / `close()` / `session.close()` is guaranteed to run even on exceptions.
- Check every early `return`, `throw`, and error branch inside a database block — none may exit without closing the session first.
- On review: trace all code paths from `getConnection()` / `createSession()` and confirm each one reaches a close call.
