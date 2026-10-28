# Deployment readiness

[Overview](../README.md) · [Configure your platform](CONFIGURATION.md) · [Deploy](deployment.md) · [Remove](../automation/aap/REMOVAL.md)

This public repository is a configurable starting point. Its sample profile has deployment and removal disabled, and its OPNsense objects are unreviewed. No public checkout is connected to or commissioned against the reference environment.

## Reference implementation status

| Area | Status | Scope |
| --- | --- | --- |
| Deployment | **Verified** | Successful deployment was recorded in the private reference environment. |
| Removal | **Verified** | The operator of the reference environment confirmed removal of one deployed installation. |
| HA acceptance | **Runbook provided** | [Failure scenarios and pass criteria](ha-acceptance.md) are available; no completed HA acceptance is claimed. |
| Certificate renewal | **Commissioning needed** | Deployment imports edge certificates; continuous renewal delivery still needs scheduling and validation. |
| Your installation | **Not commissioned** | Configure and test your own platform before enabling execution. |

Reference results describe implementation experience, not a guarantee for another platform. Private job IDs, infrastructure inventories, image digests and operational history are intentionally excluded from this distribution.

## Before enabling deployment

Work through [the configuration guide](CONFIGURATION.md), then record evidence privately for each item:

- **Source:** AAP and Argo CD can read the same private deployment fork and exact commit. The project contains no credentials or generated artifacts.
- **Hub:** the execution identity can use the required ACM, GitOps and deployment-state APIs. Both selected managed clusters are Ready and registered with Argo CD.
- **Execution:** the chosen immutable execution image includes `oc`, `kubectl`, Python/PyYAML, OpenSSL and `psql`. The execution pod can reach the hub, managed APIs, router and database.
- **Recovery:** the recovery PVC mounts successfully; two separate commissioning jobs verify persistence across pods.
- **Secrets:** existing managed-cluster ESO integration reads the database and shared edge credentials from the intended Vault paths. No new Vault credential is required in the survey.
- **Certificates:** the selected Ready native Vault issuers can sign the requested FQDN with ECDSA P-256 and a matching Common Name. Clients and backend checks trust the chain.
- **Database:** a dedicated database already exists, is reachable over IPv6 and has the required permissions. When TLS is selected, server identity and CA validation succeed.
- **Networking:** MetalLB has eligible IPv6 capacity and L2 advertisements; OPNsense has the required connected routes. The public VIP is routed and available. Management CIDRs permit the intended administrators.
- **Edge and DNS:** API permissions, plugin response contracts, listener compatibility, DNS zone access and rollback behavior have been commissioned. Only owned resources are changed.

After reviewing the results, set `execution_enabled` to `true` in the private profile and `reviewed` to `true` in the edge template. Keep `removal_enabled` independent. Commit, sync AAP and launch a new workflow so every stage uses that revision.

## Before enabling removal

Read [the removal runbook](../automation/aap/REMOVAL.md). Select a recorded installation, review its plan and choose a verified backup or explicitly acknowledge deletion without one. Removal must preserve unrelated HAProxy objects, shared infrastructure and all Vault data. The workflow's ownership checks and confirmations remain mandatory when `removal_enabled` is set to `true`.

## After deployment

Verify sign-in and token operations through the public hostname, then perform [HA acceptance](ha-acceptance.md) in an agreed maintenance window. Test certificate renewal, recovery and monitoring before claiming production readiness. Database and edge availability remain dependencies even when both Keycloak sites are active.
