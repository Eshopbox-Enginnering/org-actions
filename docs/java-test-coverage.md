# Java coverage test infrastructure

The shared workflow keeps failed builds, failed tests and zero-test runs as
gate failures. Coverage from these runs is partial: the report displays
`Not compared` rather than treating a difference as a valid coverage regression.
Successful, nonempty runs still fail if PR line coverage decreases.

AJIO integration needs local MySQL and Redis to create its Spring context.
For `Eshopbox-Enginnering/esb-ajio-integration` only, the workflow starts healthy
MySQL 8 and Redis 7 services on dynamically allocated host ports and generates
the same test-only harness in both checkouts. The bundled fixtures also work
on older PR/base revisions which do not yet contain the test infrastructure.
They mock only the Google Cloud Tasks client factory, keep eager Spring bean
initialization, and do not change production resources, existing tests or POMs.
They do not grant access to real cloud services or production databases.

The fixture defaults and `CloudTasksTestExecutionListener` match the AJIO
repository's `src/test` infrastructure. Update both copies when changing the
harness. The AJIO branch must also restore its commented-out `contextLoads`
test; updating the shared workflow alone cannot restore a removed test.

Validate workflow changes with:

```sh
python3 -m unittest discover -s tests -v
```

The regression tests execute the actual embedded setup/report scripts against
isolated fixtures. A complete AJIO smoke run additionally requires running both
revisions' Maven/JaCoCo commands and the final gate with the service containers.
