# HA acceptance: prove continuity under failure

[Overview](../README.md) · [Architecture](architecture.md) · [Deployment guide](deployment.md) · [Readiness evidence](deployment-readiness.md)

> **Plan, not a completed test.** This runbook defines proposed acceptance tests. No outage was triggered while preparing it. Names and addresses follow the [anonymized example conventions](examples.md); public addresses remain masked.

HA acceptance answers a practical question: **can users still sign in, refresh sessions and obtain valid tokens when a component fails, at the load we expect to serve?** A successful deployment or a green health endpoint does not answer that question.

## 1. Decide what we are accepting

| Scope | What must be demonstrated | Current evidence gap |
| --- | --- | --- |
| Keycloak application HA | Either site serves the full agreed workload; pod, node and site failures recover correctly | Controlled failure and capacity tests have not been recorded |
| Database HA | A surviving writable database, correct fencing and preserved data after writer failure | External PostgreSQL replication, failover mechanism and failure-domain placement need verification |
| Edge HA | Public IPv6 VIP, routing, firewall and TLS service survive appliance/uplink loss | Redundant OPNsense appliances and VIP failover have not been established by this repository |
| Operational recovery | Operators can detect incidents, recover service and restore a backup | Alerting, restore exercises and continuous certificate delivery need acceptance |

If PostgreSQL has one writer with no proven failover, or the VIP depends on one router, record **application HA passed; end-to-end HA incomplete**. Do not turn the general HA badge green solely because one OpenShift site can be stopped.

For this pinned release, Keycloak requires synchronous database replication, low database latency and synchronized clocks. Its guidance requires commit latency below 10 ms and recommends below 5 ms. Validate these from both sites under load; network ping is not a database commit measurement. See the [26.8.1 infrastructure requirements](https://github.com/keycloak/keycloak/blob/26.8.1/docs/guides/high-availability/multi-cluster-v2/introduction.adoc).

## 2. Prepare a controlled test

Start with an isolated acceptance deployment: its own namespace, FQDN, VIP, dedicated database and test identities. It may reuse the two managed clusters, but node, router and database infrastructure faults can still affect other tenants. Those fault scenarios require an agreed maintenance window with the infrastructure owners.

Before injecting any fault:

- Record the deployed Git revision, Keycloak/operator versions, selected clusters, namespace, runtime resources, image digests, database topology and HAProxy configuration. Use the saved deployment state to identify owned objects.
- Verify both sites are healthy, both backends receive real traffic, certificates validate, clocks are synchronized, and monitoring is collecting data.
- Inspect the actual pod-to-node placement, readiness probes, PodDisruptionBudgets and scheduling rules. The source CR requests two instances, but the repository does not explicitly configure anti-affinity or a disruption budget; inspect what the Operator generates before drawing conclusions.
- Confirm that either site has capacity for **100% of the agreed peak load**, including database connections. Do not increase load beyond an approved test budget on shared infrastructure.
- Create a dedicated acceptance realm, clients and users through a separately reviewed setup. Do not reuse `master` or the deployment administrator for browser/load tests. Keep client secrets, passwords, cookies and tokens out of logs and reports.
- Verify a backup and recovery procedure before database failover/restore exercises. Restore only into a separate database for the restore test. Removal's `no-backup` option is unrelated to HA acceptance.
- Assign an owner, exact scope, maximum duration and rollback procedure to each fault. Preserve the original configuration and arrange a recovery channel independent of the component being failed.

Run traffic generators and the recovery watchdog outside both managed sites. For a hub outage test, they must also run outside the ACM/AAP hub. Record generator-side CPU, network errors and clock accuracy so a failing generator is not mistaken for a Keycloak outage.

## 3. Keep meaningful traffic running

Use several concurrent probes throughout baseline, fault, recovery and failback:

| Probe | What it establishes | Important detail |
| --- | --- | --- |
| Public DNS + TLS + discovery | The real FQDN, AAAA answer, VIP and certificate path work | Run over IPv6 from outside the cluster networks; validate the issuer, not only HTTP 200 |
| Fresh browser sign-in | Redirect, credentials/MFA, authorization code, PKCE exchange and application callback work | Use a real test application; validate state, nonce, issuer, audience, signature and expiry |
| Existing session + refresh | A session established before failure survives service failover | Establish cohorts while served by each site; serialize refreshes per session and retain rotated refresh tokens |
| Client credentials | New machine-to-machine tokens can be issued | Validate tokens through the test application; a previously issued JWT alone does not prove Keycloak availability |
| Logout / revocation | Session invalidation works across sites | Check subsequent refresh and configured logout behavior; offline JWT validation may accept an existing access token until expiry |
| Configuration consistency | Changes reach a previously warmed cache on the other site | Change a test client's enabled state or test role mapping on one site; verify the expected next token/auth behavior on the other and after recovery |

Include an in-progress browser journey: begin authentication while served by site A, fail that path, and complete the callback/token exchange through B. Also exchange a still-valid code issued before the fault exactly once through the surviving path. Repeat in reverse; record expected expiry separately from continuity failures.

Use the public hostname for end-user tests. For site-specific diagnostics, a controlled test client may pin the same hostname/SNI to a discovered backend address from an allowed source. Preserve certificate and hostname verification and the public issuer; do not use `-k` or expose backend health endpoints publicly.

Warm caches on both sites before the consistency test. After a temporary database-path failure, change test-only configuration through the healthy site, restore the failed site's connection, and verify it no longer serves the old configuration. This exercises the database-backed invalidation design described in the [26.8.1 concepts](https://github.com/keycloak/keycloak/blob/26.8.1/docs/guides/high-availability/multi-cluster-v2/concepts.adoc).

For load, use a release-compatible [Keycloak Benchmark](https://www.keycloak.org/keycloak-benchmark/) configuration plus browser journeys for the actual application. Pin the tool version and scenario parameters; do not assume its nightly build matches Keycloak 26.8.1. Dataset generation belongs only in the acceptance realm/database.

## 4. Agree measurable pass criteria

These are **proposed starting thresholds**, not product guarantees or an established SLA. Approve or replace them before execution and record the agreed values in the report.

| Measure | Proposed acceptance target |
| --- | --- |
| Baseline | 15 minutes at the approved peak request rate and realistic authentication/refresh mix |
| Single-site recovery | Public authentication returns to the agreed success/latency envelope within 120 seconds of fault injection |
| Database/edge recovery | Explicit owner-approved RTO before the test; no automatic pass without a defined target |
| Recovery confirmation | At least 60 seconds of sustained healthy probes after recovery, followed by 15 minutes at full target load on the surviving path |
| Post-recovery success | At least 99.9% for valid scheduled authentication operations in each measured phase; report each flow separately |
| Latency | Pre-agreed absolute p95/p99 budgets per flow; compare to baseline and exclude human/MFA interaction time from server processing measurements |
| Data correctness | No loss of acknowledged durable test writes; define and test session-state RPO separately against the database/release commit configuration |
| Consistency | Suggested test-only configuration convergence budget: 5 seconds while healthy; after reconnection, no stale authorization once a site is returned to service |
| Repetition | Three clean runs in each site direction, restoring full health between faults |
| Failback | Both sites return to healthy traffic distribution without a second service outage or repeated flapping |

Measure recovery from the fault timestamp to the beginning of a subsequently confirmed healthy window. Report total failed requests, timeouts, longest failure streak and the recovery confirmation time as separate values. Include failures from the detection window; do not hide them inside a long average or retry-success rate.

Use an open arrival-rate workload, or report scheduled versus actually started requests and dropped iterations. A closed loop that slows down during an outage can undercount failures. Report raw first-attempt outcomes and any user-visible retry outcomes separately. Do not blindly replay authorization-code exchanges or rotating refresh-token requests.

The repository requests HAProxy checks every `3s`, with `fall=3`, `rise=2`, connect timeout `5s` and server timeout `60s` in [the adapter template](../automation/opnsense/objects.json). **This is not a nine-second failover guarantee.** Inspect the generated live configuration and measure scheduling, connection timeouts, Keycloak readiness propagation and in-flight request failures. Upstream documents that some failure cases can take minutes; see the [release limitations](https://github.com/keycloak/keycloak/blob/26.8.1/docs/guides/high-availability/multi-cluster-v2/introduction.adoc).

## 5. Run the failure matrix in order

For every row: collect baseline → apply one bounded fault → observe recovery under load → restore the exact original state → verify both sites and traffic distribution → collect the report. Stop on a failed invariant; do not stack another fault onto a degraded system.

| ID | Scenario and controlled method | What must pass | Recovery / scope |
| --- | --- | --- | --- |
| HA-01 | Baseline through the public endpoint with both sites healthy | All probe types pass; HAProxy shows traffic reaching both sites; load and latency budgets hold | No injected fault |
| HA-02 | Drain only this installation's site-A HAProxy server, then repeat for B | The other site handles the full workload and existing sessions | Restore the original server state; this proves alternate capacity, **not automatic failure detection** |
| HA-03 | Terminate one owned Keycloak pod during traffic, then repeat at the other site | Requests recover within the agreed budget; the Operator replaces the pod; session checks pass | Scope by namespace and verified owner/UID; never delete all replicas together |
| HA-04 | Controlled worker disruption, including the node advertising the MetalLB address | Remaining endpoints serve traffic and IPv6 reachability recovers | Infrastructure owner restores/uncordons the node; verify placement and IPv6 neighbor/advertisement convergence |
| HA-05 | Blackhole only the selected site's backend IPv6:443 path from the edge; repeat in reverse | HAProxy detects the unreachable backend and removes it automatically; the surviving site meets full load and session criteria | Use a reviewed, narrowly scoped network rule with independent timed rollback; keep the hub and surviving site's paths intact |
| HA-06 | Deny database connectivity only for this installation at site A, then B | The affected site becomes unsuitable for traffic; the healthy site issues valid tokens; recovered caches reflect intervening changes | Verify the actual packet path first: an added deny NetworkPolicy does not override other allow policies; do not modify shared DB rules blindly |
| HA-07 | Real site loss using the infrastructure owner's controlled outage procedure | Application/data/network failure domains behave as claimed, beyond a simulated backend block | Only after HA-05 succeeds; restore the site and validate automatic failback. Record shared dependencies affected by the outage |
| HA-08 | Database-owner-controlled writer failover, then a separately scoped abrupt writer-loss test if required | Fencing prevents two writers; the connection endpoint recovers; durable writes and sessions meet their agreed RPO/RTO | Use the database product's tested procedure; do not stop a database process without an available, verified recovery path |
| HA-09 | Fail the active OPNsense appliance or uplink, **only if redundancy is configured** | VIP/routing/TLS recover through the alternate edge and unrelated tenants meet their agreed impact budget | Edge owner restores the original topology; with one appliance, mark end-to-end edge HA incomplete |
| HA-10 | Hub-management outage or isolation of this installation's management paths | Existing public login/refresh/token flows continue without ACM/AAP/Argo access | Keep observers and rollback outside the hub; a full hub outage affects other managed services |
| HA-11 | Controlled certificate renewal and delivery exercise | Both backends and public TLS present valid renewed certificates with successful authentication | Coordinate the issuer/edge owners; scheduling continuous delivery remains a prerequisite for operational acceptance |
| HA-12 | Restore a verified backup into an isolated database and recovery deployment | Recovery meets the agreed restore time and data point; restored identities/configuration work | Never overwrite the serving database; use an isolated hostname and block outbound email/webhooks from restored test data |

A voluntary node drain exercises eviction and rescheduling; it does not prove behavior during abrupt node loss. Record which method HA-04 used and include a separately approved abrupt-loss test if that failure is in scope.

A successful HA-05 proves loss of the backend path, not every consequence of losing a data center. HA-07 must account for where PostgreSQL, the router, DNS resolution, Vault and shared storage actually live. Likewise, a planned DB switchover alone does not establish behavior during an abrupt writer failure.

A successful public `/lb-check` is **not** a pass criterion: this repository deliberately blocks that path on the public frontend. Observe site health privately and exercise authentication publicly. Individual pod readiness and site-level load-balancer health have different roles in [Keycloak 26.8.1](https://github.com/keycloak/keycloak/blob/26.8.1/docs/guides/high-availability/multi-cluster-v2/concepts.adoc).

## 6. Capture evidence and recover safely

Collect synchronized timestamps and sanitized evidence from:

- **Client generators:** DNS/TLS results, operation type, status/error category, latency, scheduled and started operations, retries, and session-cohort continuity. Never capture response tokens, credentials, cookies or authorization headers.
- **OPNsense/HAProxy:** owned frontend/backend/server identifiers, health transitions, session counts, connection errors and relevant generated timeouts. Redact actual addresses in shareable reports.
- **Both clusters:** Keycloak conditions, pod restarts/readiness, node placement, EndpointSlices, Service address, relevant events and internal metrics. Observe Operator-generated policy and resource changes too.
- **PostgreSQL:** writer identity, replication mode/lag, fencing and promotion events, connection failures, commit latency and test-write acknowledgements. Match database monitoring to client timestamps.
- **Test control:** exact fault/rollback operations, original state, fault deadline, operator actions and final restored-state verification.

Abort and restore the fault when the surviving site fails, an unrelated application is affected outside the agreed scope, a data-integrity check fails, the recovery deadline is exceeded, or the controller loses visibility. Preserve evidence before cleanup. A `finally`/Ansible `always` handler is useful but cannot run if its execution node is lost; use an independent watchdog and a documented manual recovery channel.

Do not use the removal workflow to simulate a site outage. It removes DNS/edge resources and empties the dedicated database; that tests a different lifecycle and destroys the session state HA acceptance needs to observe.

## 7. Record the result before changing the badge

Use one report per scenario and repetition:

| Field | Record |
| --- | --- |
| Run identity | UTC date, request/job ID, operator, Git revision, deployment identity |
| Scope | Scenario ID, site direction, dependencies included/excluded, approved maintenance window |
| Workload | Tool/version, realm/client fixture identifiers, peak rate, concurrency and flow mix; no secrets |
| Objectives | Approved RTO, durable/session RPO, p95/p99 limits and allowed impact on other tenants |
| Timeline | Baseline, fault, backend exclusion, first recovered operation, stable-window start/end, rollback, failback |
| Results | Per-flow attempts/failures/timeouts, dropped iterations, latency, data/session/consistency checks |
| Restoration | Original settings restored, both sites healthy, unrelated resources unchanged |
| Conclusion | Pass / fail / not tested, evidence locations, limitations and follow-up owner |

The overall outcome is the weakest required dependency result. Keep **HA acceptance: Runbook provided** while required database, edge or site tests are missing; this label indicates documented procedures, not a passed test campaign. If only application-site tests pass, report that narrower outcome explicitly. Document the tested traffic level and fault model; a test campaign does not prove an unlimited uptime guarantee.

## 8. Proposed AAP automation

A separate **Keycloak HA Acceptance** workflow would make repeated tests reproducible. It is **not implemented or registered by this documentation change**.

Suggested sequence: **Read-only discovery → Baseline → Approve scoped fault → Inject → Observe → Restore → Verify recovery → Report**. Ensure restoration runs on success, failure and ordinary cancellation, with an independent watchdog for loss of AAP itself. Manual infrastructure steps must not be mislabeled as automated tests.

Keep its survey small: deployment namespace, test scope, approved load profile and maintenance-window reference. Discover cluster/backend identities from the saved deployment record. Read-only observation should be the default; fixture creation, load and fault injection are explicit modes. Use dedicated acceptance credentials and project ownership checks. Hub access can identify managed clusters, but it does not by itself authorize database promotion or hypervisor/router failover.

Begin with HA-01 through HA-05 and configuration/session checks. Add database, edge and real-site fault adapters only after their actual redundancy mechanisms and owner-approved recovery procedures are known. Deletion/removal and acceptance workflows must remain separate.
