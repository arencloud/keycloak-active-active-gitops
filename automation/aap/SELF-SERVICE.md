# Self-service Keycloak deployment

> **Documentation examples:** names, domains and IP addresses are anonymized. Use your own environment values; see [example conventions](../../docs/examples.md).

[Deploy](../../docs/deployment.md) · [Remove](REMOVAL.md) · [Readiness](../../docs/deployment-readiness.md)

## Deployment survey

Open **Keycloak Active Active Self Service** in AAP. The survey asks for seven non-secret inputs; all values below are examples, not installed defaults.

| Field | What to enter | Example |
| --- | --- | --- |
| Managed cluster A | First Ready, joined ACM managed cluster | `cluster-a` |
| Managed cluster B | A different managed cluster | `cluster-b` |
| Public FQDN | Hostname only, without `https://`, a port or a path | `auth.example.com` |
| Public IPv6 VIP | An available address routed to OPNsense | `PUBLIC_IPV6_VIP` |
| Keycloak namespace | Dedicated namespace, identical on both clusters | `keycloak` |
| Vault database secret path | Existing path relative to KV v2 mount `kv`; omit `kv/` and `data/` | `global/tenant-a/postgresql` |
| PostgreSQL TLS | `enabled` for verified TLS, or explicitly `disabled` | `enabled` |

The selected hostname is used for certificates, HAProxy and DNS. The Vault issuer must permit it, and the Cloudflare token must have access to its zone. Wildcards are rejected; names are normalized to lowercase and a trailing dot is removed. An existing deployment cannot silently change its hostname, cluster pair, VIP or database path.

Cluster choices are refreshed from Ready, joined ACM managed clusters during `configure-controller.yml` registration. The hub is excluded. AAP surveys store a static choice list: rerun registration after ACM inventory changes; opening the survey alone does not query ACM. Every launch revalidates the selected pair, endpoints, credentials and prerequisites. Names and API DNS patterns are not hard-coded in the runner.

## Database TLS choice

- **`enabled` (default):** require TLS, a trusted server certificate and a matching database hostname. After ESO creates the database Secret and before either Keycloak site starts, the runner checks IPv6 connectivity and performs the PostgreSQL TLS handshake. A server refusing TLS, invalid certificate or hostname mismatch fails deployment; it never retries without TLS. Keycloak uses `db-tls-mode=verify-server` and the discovered CA trust bundle.
- **`disabled`:** explicitly use a non-TLS PostgreSQL connection, with `db-tls-mode=disabled` and JDBC `sslmode=disable`. The runner checks IPv6 TCP connectivity without TLS negotiation; database authentication and schema initialization are still checked by Keycloak startup. Database traffic is unencrypted. The database CA ConfigMap and truststore reference are omitted.

Discover/Plan report this choice; the live connection check follows ESO synchronization during gated deployment because a new deployment's database Secret does not exist yet. This setting changes only the database connection. Public HTTPS, HAProxy backend TLS and Vault/API TLS remain enabled.

The choice is stored in the deployment record. Removal automatically uses `verify-full` or `disable` accordingly, without another survey input. Older records default to verified TLS. Changing an existing recorded choice requires a planned database transport migration; install/reconcile refuses an implicit change. Deployment execution and the edge adapter must be commissioned and enabled for your platform. Runtime checks still enforce the selected TLS mode.

Keycloak's `disabled` option leaves TLS to the JDBC driver, so the explicit `sslmode=disable` property is necessary for a deterministic non-TLS choice. See [Keycloak configuration](https://www.keycloak.org/server/all-config) and [PostgreSQL JDBC SSL modes](https://jdbc.postgresql.org/documentation/ssl/).

## Multiple deployments on the same clusters

Choose a distinct namespace for each independent deployment, for example `keycloak-prod` and `keycloak-test`. The namespace must be new or already owned by this workflow; it is identical on both selected clusters and identifies the deployment across the ACM hub. Reusing that namespace for a separate pair is not supported. System namespaces are rejected.

Each deployment gets distinct Argo Applications/AppProject, hub state and lock names, HAProxy objects and firewall-rule ownership. Names use `kc-<namespace-prefix>-<namespace-hash>`. ACM registration is one shared, additive Placement/GitOpsCluster (`keycloak-aap-sites`), so adding a deployment does not unregister earlier clusters. Each AppProject still restricts destinations to its selected pair and namespace. A shared lock serializes deployment/removal changes to reservations and the firewall; it does not steal a stale lock. Other firewall clients still require coordination.

Each namespace has its own OperatorGroup and Keycloak Operator 26.8.1, watching only that namespace. Discovery rejects an existing Keycloak OperatorGroup in another namespace that also watches the requested namespace. All installations share cluster-scoped CRDs; keep operator versions aligned and coordinate upgrades. See [Keycloak Operator installation](https://www.keycloak.org/operator/installation) and [OLM OperatorGroups](https://olm.operatorframework.io/docs/concepts/crds/operatorgroup/).

Each independent deployment requires its **own database**. Both sites of a deployment use the **same** database. A PostgreSQL server may host several dedicated databases, each referenced by its own Vault secret. The workflow rejects reused FQDNs, public VIPs and Vault database paths. After ESO resolves the secret, it also checks host/resolved IPv6 address plus database-name fingerprints against recorded deployments before starting Keycloak and before removal. These checks cannot establish exclusivity against unmanaged applications or every alternate HA endpoint; database isolation remains a prerequisite.

## What the workflow determines

- **Managed access:** the hub's existing `application-manager` ManagedServiceAccount token and ManagedCluster endpoint/CA. No managed-cluster passwords are requested.
- **Placement:** selected pair's common ClusterSet, additive ACM Placement, namespace-scoped Argo destinations, six manual-sync Applications per deployment and per-site Kustomize patches.
- **MetalLB:** a unique automatic IPv6 pool, preferring `machine-network-last-50`, with L2 advertisement. MetalLB allocates the addresses; the runner reads each Service's assigned status. It never selects a seemingly unused IP from the pool.
- **HAProxy sources:** connected OPNsense IPv6 interfaces and routes. Each backend gets an explicit source address; Keycloak trusts only that source, and its NetworkPolicy admits it plus discovered hub pod/node IPv6 ranges.
- **PKI and Vault:** existing Ready Vault ClusterIssuer/ClusterSecretStore, with platform preferences. Public CA material comes from the issuer trust bundle; runtime TLS verification must succeed. ESO provides shared API and database credentials.
- **Public edge:** VIP interface, missing IP alias, one project-specific IPv6 TCP/443 firewall rule, project certificate imported from cert-manager's TLS Secret, backend trust CA by exact certificate match, HAProxy objects and Cloudflare zone/owned AAAA record.
- **Audit/lifecycle:** AAP job/workflow ID, actual project Git commit, and initial-install versus reconcile. A matching owned record allows an incomplete initial installation to resume with its existing credentials. Unrecorded partial installations, missing runtimes after completion, and version upgrades require recovery/review.

Connected routing does not identify an administrator's authorization. The platform profile restricts the public `/admin` path to an approved management network (illustrated here as `2001:db8:400:6::/64`). Change this centrally when management policy changes. No administrator CIDR or AAP source CIDR is a survey question. The workflow renders backend ingress rules for discovered hub IPv6 ranges; verify the effective policy set, including Operator-generated policies, during acceptance. Actual connectivity is checked during execution.

## Credentials and bootstrap

AAP attaches only the existing hub credential to all three jobs, including the read-only discovery/plan jobs. Existing AAP credential values/types remain unchanged. Project Git authentication remains its existing SCM credential.

| Vault KV v2 path, relative to mount `kv` | Fields consumed through existing ESO |
| --- | --- |
| Survey-selected path (default `global/keycloak/active-active/postgresql`) | `host`, `username`, `password`, `database` (port 5432 is a platform constant) |
| `global/opnsense` | `opnsense_url`, `opnsense_key`, `opnsense_secret` |
| `global/cloudflare` | `api-token` |

Enter the database **secret path**, not its password, Vault URL or Kubernetes Secret name. Omit `kv/` and `data/`; for example, use `global/tenant-a/postgresql`. Existing ESO access must allow that path. ESO creates `keycloak-db` in the selected namespace on each cluster, so the same Kubernetes Secret name can safely exist in different deployments. The workflow does not create the database or write its source credentials to Vault.

There is **no bootstrap Vault path** in this workflow. With `bootstrapAdmin` omitted, the [Keycloak Operator generates `keycloak-initial-admin`](https://www.keycloak.org/operator/basic-deployment). The workflow creates a random permanent administrator password and synthetic client secret, persists identical copies in `<selected-namespace>/keycloak-workflow-admin` on the selected pair, establishes the administrator, verifies its login, and removes the temporary administrator user. Reconcile reuses these credentials and never resets an existing administrator. Protect this Kubernetes Secret with normal cluster RBAC and backups. Neither its values nor the TLS private key appear in reports or Git.

The unused legacy bootstrap/automation ExternalSecret mappings remain in the raw scaffolding for compatibility with the older manual renderer. The automatic Applications explicitly remove the bootstrap resource and unused automation mappings. Never apply the raw scaffolding directly.

## Execution and recovery

`automatic-release.yml` runs `automatic.py` in three successive jobs: **Discover → Plan → Deploy**. Discover and Plan perform reads and local rendering only. The public profile disables Deploy. After commissioning, set `execution_enabled: true` and edge `reviewed: true` in your private deployment repository; either gate can be disabled when commissioning must be repeated. There is no extra action, revision, request-ID or infrastructure question in the deployment survey. Git review controls code and platform configuration; the executed project SHA appears in the report. AAP omits `.git` from execution copies, so the runner uses its injected `PROJECT_REVISION`; local execution requires a clean checkout. Discovery passes its revision through workflow artifacts, and later stages refuse a different revision.

Deployment synchronizes operators and prerequisites, checks database IPv6 connectivity and the selected TLS mode before starting Keycloak, configures source restrictions, starts A and establishes administration, starts B, reads MetalLB assignments, configures the scoped edge, verifies VIP authentication, then publishes and tests DNS. The public VIP is an additional IP alias on the existing routed interface, not a separate routed subnet. [MetalLB handles address allocation](https://metallb.io/usage/); [OPNsense provides scoped VIP and firewall APIs](https://docs.opnsense.org/development/api/core/firewall.html).

The hub ConfigMap `openshift-gitops/kc-<namespace-prefix>-<namespace-hash>-state` records public manifests, selected sites, assigned addresses, revision and owned edge configuration for [removal](REMOVAL.md). It contains no credentials. Removal asks for the namespace to select this record. Legacy fixed-name deployment records require explicit migration; the workflow refuses to adopt them. The dedicated Keycloak execution group mounts persistent recovery storage at `/runner/keycloak-recovery`. A partial failure can require recovery from the journal; no automatic database rollback or cluster relocation is attempted.

Certificate import is reconciled on deployment/reconcile runs. Continuous renewal synchronization to OPNsense is **not yet scheduled**; a certificate-refresh schedule or renewal hook must be commissioned before production. The runner does not change database-server configuration or create its database/login.

See [deployment readiness](../../docs/deployment-readiness.md) for completed checks and remaining acceptance work. Preparing or registering templates does not launch them.

ACM may register clusters in Argo CD through its cluster proxy. Automatic Applications and AppProjects use the selected cluster names, while AAP retains their direct API endpoints for managed-cluster commands. Discovery reports missing Placement decisions and Argo registrations; the execution gate accepts proxy or direct Argo registrations by name.
