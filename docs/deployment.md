# Deployment runbook

> **Documentation examples:** names, domains and IP addresses are anonymized. Use your own environment values; see [example conventions](examples.md).

[Overview](../README.md) · [Architecture](architecture.md) · [Survey reference](../automation/aap/SELF-SERVICE.md) · [Verified readiness](deployment-readiness.md)

Use the existing AAP workflow **Keycloak Active Active Self Service**. It coordinates ACM registration, Argo CD synchronization and the external edge/DNS configuration. This runbook describes the automatic workflow; raw Kubernetes templates and older manual playbooks are not the self-service entry point.

## Before launching

| Confirm | Why it matters |
| --- | --- |
| Two Ready, joined ACM managed clusters | Discovery checks availability, a common ClusterSet, managed API access and compatible operator scope. |
| Existing MetalLB, cert-manager and ESO/Vault integrations | Discovery selects the IPv6 pool, ready issuer and store; it does not install these platform services. |
| A dedicated PostgreSQL database and its Vault secret | Both sites share this database. The runner does not provision the database/login or populate Vault. |
| An allowed FQDN, available or already-owned public IPv6 VIP, and dedicated namespace | Identity and ownership checks prevent silent reuse or relocation of another deployment. |
| Database TLS choice | Select `enabled` only with working, trusted database TLS. Check your own database; the survey never falls back from TLS automatically. |
| Client IPv6 reachability and Vault CA trust | DNS-only publication sends clients directly to OPNsense; client certificate trust must already exist. |

First complete the [platform configuration](CONFIGURATION.md), including an execution environment with `psql` and persistent recovery storage. The configured hub credential supplies managed-cluster access through ACM. Shared OPNsense and Cloudflare credentials arrive through ESO from Vault. Cluster dropdowns are refreshed during controller registration; rerun registration when ACM inventory changes.

## Launch a deployment

1. Open **Keycloak Active Active Self Service** and launch a new workflow job.
2. Choose cluster A and cluster B, public FQDN, public IPv6 VIP, namespace, Vault database secret path and database TLS mode. The [survey reference](../automation/aap/SELF-SERVICE.md) documents every field.
3. Follow **Discover → Plan → Deploy**. Discovery and Plan are read-only and render the intended configuration. They pass the project revision to Deploy, which rejects a revision mismatch.
4. In the Execute job, inspect **Publish sanitized discovery and execution results**. The last `stage` and final `error` identify the failed dependency; the final Ansible assertion only reports that failure.
5. Accept the installation after both backends and the public endpoint pass the workflow checks, then complete the operational acceptance exercises below.

No manually reserved MetalLB backend addresses are needed: MetalLB allocates them and AAP reads each Service status. The public VIP is the survey input; backend addresses are distinct allocations inside the selected sites.

If you are preparing a different platform, complete [AAP platform setup](../automation/aap/README.md#platform-setup) before launching.

## What Deploy does

| Order | Stage | Completion check |
| --- | --- | --- |
| 1 | Lock and validate ownership | Instance/shared locks, cluster identity and recorded deployment claims |
| 2 | ACM registration and Argo Applications | Selected Placement decisions and unique named Argo registrations |
| 3 | Operator on each site | Expected `keycloak-operator.v26.8.1` InstallPlan only; CSV Succeeded |
| 4 | Prerequisites on each site | Certificate Ready; ESO output keys present; matching shared database credentials |
| 5 | Database and network preparation | Selected TLS mode, IPv6 reachability, database isolation, proxy sources and retained workflow credentials |
| 6 | Site A runtime and administration | Keycloak Ready, MetalLB allocation, backend TLS/health, permanent administrator and token issuance |
| 7 | Site B runtime | Keycloak Ready, MetalLB allocation and token issuance against the shared database |
| 8 | OPNsense | Owned VIP, firewall rule, certificate and HAProxy objects; configuration test and reload |
| 9 | Public endpoint and DNS | VIP token issuance before owned Cloudflare AAAA publication; DNS resolution and public token issuance afterward |

Argo Applications use manual synchronization without automatic pruning so AAP can enforce this sequence. OLM owns operator Deployments, RBAC and cluster-scoped CRDs; the Keycloak Operator owns its runtime resources. The workflow approves only the exact expected CSV and reviewed dependencies. `startingCSV` selects an initial version, but it does not prevent future upgrades on its own.

## Retry a failed installation

After a correction is pushed and the Keycloak AAP project is synchronized, **start a new job from Keycloak Active Active Self Service with the same survey values**. Start at Discover, not by relaunching only the failed Execute job: the workflow carries the reviewed revision between stages.

A matching owned recovery record allows an incomplete installation to resume. Existing resources and permanent administrator credentials are reused. A completed installation follows reconciliation rules. A partial deployment without a matching record, missing runtimes from a completed installation, or a changed cluster pair/namespace/FQDN/VIP/database identity requires separate recovery. The workflow does not reset an existing administrator or roll back the database.

| Failure location | Inspect first |
| --- | --- |
| Registration | Placement decisions, named Argo cluster registrations and Application destination errors |
| Operator | Referenced InstallPlan, expected CSV and conflicting OperatorGroup scope |
| Certificate / secrets | Current-generation certificate conditions, issuer signing policy and ExternalSecret status |
| Runtime | Keycloak conditions, pod states/logs, IPv6 database connectivity and database permissions |
| Administrator bootstrap | Sanitized token error, presence of the permanent account and temporary-account validity |
| Edge / DNS | Scoped recovery journal, HAProxy configuration test, endpoint certificate/health and DNS ownership |

The [readiness report](deployment-readiness.md) records fixes already validated for ACM cluster-proxy registration, Vault ECDSA policy, JVM IPv6 preference and the Keycloak HTTP 400 bootstrap response. For a StatefulSet stuck on an older failed pod after a corrected template, inspect ownership and rollout revisions before a controlled pod restart; repeated workflow launches are not a substitute for that recovery.

## Acceptance beyond installation

Follow the [HA acceptance runbook](ha-acceptance.md) for test scope, fault scenarios, proposed thresholds, evidence collection and restoration. It separates Keycloak site resilience from database and edge HA.

- Exercise browser login, token refresh, logout and client-credentials access through the public FQDN. Confirm a common issuer on both sites.
- Verify realm/client cache invalidation between sites and behavior during pod/site loss. Measure surviving capacity and recovery time.
- Test database writer failover, fencing and backup restoration; verify commit latency against the HA design.
- Test effective NetworkPolicies, direct-backend restrictions and spoofed forwarded headers. Verify public denial of health/metrics paths and restricted administration.
- Test certificate and credential rotation. Commission continuous certificate delivery/reload on OPNsense; it is not scheduled by this repository yet.
- Confirm replica placement, monitoring and alerting. Validate VIP failover if a redundant OPNsense pair exists.

The latest completed checks and remaining gaps are in [deployment readiness](deployment-readiness.md). A successful workflow does not replace failover, restore, load and renewal acceptance.

## Upgrades and removal

Both sites share a schema, so upgrades need one coordinated procedure and a database backup. The current workflow pins version 26.8.1 and rejects runtime/operator version changes. Review the release’s [multi-cluster upgrade procedure](https://www.keycloak.org/high-availability/multi-cluster-v2/upgrades) before promotion; reverting an image alone is not a database recovery plan.

Removal uses **Keycloak Active Active Removal**, a separate workflow enabled for user-initiated runs. Removal is verified following the user’s confirmation of one successful removal; the gate does not replace its runtime checks. It uses the saved namespace-scoped ownership record and explicit confirmations. It removes only this deployment’s resources and edge/DNS objects, and resets its dedicated database according to the removal contract. Vault credentials and unrelated applications remain untouched. See [removal and recovery](../automation/aap/REMOVAL.md).
