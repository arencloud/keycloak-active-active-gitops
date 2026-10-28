# Configure your own environment

[Overview](../README.md) · [Deploy](deployment.md) · [Survey](../automation/aap/SELF-SERVICE.md) · [Publication boundary](PUBLICATION.md)

This public repository is a reusable starting point. It contains fictional endpoints and explicit replacement markers, and ships with deployment, removal and edge-review gates **disabled**. It has no access to the reference environment.

## Start here

Here, a private deployment fork means your own private repository containing the source and reviewed configuration; it does not require a GitHub Fork relationship.

1. Create a **private deployment fork** or private Git repository from this public source. Put your non-secret infrastructure configuration there, with normal Git review. Keep passwords, tokens, private keys and kubeconfigs outside Git, in AAP credentials and Vault.
2. Configure `automation/aap/profiles/platform.json` using the field table below. Set `repository_url` to that private repository, and point the AAP Project to the **same repository and branch**. AAP and Argo CD each need their own Git access and CA trust.
3. Configure the execution image, recovery storage and hub identity. Copy `automation/aap/controller/settings.example.yml` outside the checkout and fill in your actual AAP IDs.
4. Check the existing managed-cluster ESO/Vault and cert-manager integrations. Populate the required Vault entries through your approved secret-management process.
5. Validate locally, commission the execution environment and edge API behavior, then register the AAP templates. Enable each execution gate only in the reviewed private configuration when its prerequisites are ready.
6. Launch the deployment survey with the real cluster pair, FQDN, public IPv6 VIP, namespace, database secret path and database TLS mode.

The runner reads configuration from the checked-out Git revision; it does **not** automatically load `.local` files or arbitrary environment overrides. A private deployment fork keeps AAP's revision and Argo's rendered configuration aligned. AAP controller settings are a separate file supplied to registration, not a replacement for the runtime platform JSON.

## Which files do I edit?

| File | Edit for your installation? | Used by |
| --- | --- | --- |
| `automation/aap/profiles/platform.json` | **Yes:** platform endpoints, policy and repository settings | Discovery, deployment, removal lookup, survey-choice refresh and commissioning |
| `automation/aap/profiles/active-active.json` | **Review:** execution gates and approved runtime constants | Automatic deployment/removal; static defaults also serve legacy tools |
| `automation/aap/controller/settings.example.yml` | **Copy outside Git and fill:** AAP URL and assigned object IDs | Controller registration playbooks |
| `automation/aap/controller/survey.json` | Usually **no**; optional non-secret defaults only | Deployment survey registration; cluster choices are refreshed from ACM |
| `automation/aap/controller/removal-survey.json` | Usually **no** | Removal survey registration; actual targets come from saved deployment state |
| `automation/opnsense/objects.json` | **Review the adapter**, not per-deployment hostname/address fields | Runtime rendering and scoped HAProxy API reconciliation |
| `automation/cloudflare/record.json` | Usually **no**; retain DNS-only AAAA behavior | DNS creation/update and removal ownership checks |
| `gitops/keycloak/prerequisites/external-secrets.yaml` | Review field schema; automatic rendering fills store/path values | ESO materializes namespace-local Secrets |
| `gitops/hub/execution/`, `gitops/hub/discovery-rbac/` | **Yes:** storage class and actual AAP hub ServiceAccount | Administrator-reviewed hub bootstrap |
| Execution image files and `controller/container-group-pod.yaml` | **Yes:** approved base image and final immutable image reference | Image build and AAP container group |

Leave runtime payload markers in `objects.json`, `record.json` and the Keycloak templates intact; do not fill them with one customer's values. Image and storage bootstrap markers are different: fill those privately as described below. The automatic renderer replaces them from survey input, platform settings and live discovery. The static `gitops/hub/registration`, `gitops/hub/project`, `gitops/applications`, `vars/environment.yml` and `scripts/survey.py` defaults support the older manual path; the automatic workflow generates cluster destinations, registration and repository patches for you. Use the automatic workflow for new installations.

## `platform.json`: every field

All examples below are fictional. Public IP addresses must be supplied by you; documentation networks are rejected as public VIPs.

| Key | What to supply | Example / constraint |
| --- | --- | --- |
| `hub_api` | Exact ACM hub Kubernetes API URL matching the injected kubeconfig | `https://api.hub.example.com:6443`; no token in the URL |
| `repository_url` | Git repository used by this installation's AAP Project and Argo CD | `https://git.example.com/platform/keycloak-deployment.git`; no embedded credentials |
| `metallb_pool_preference` | Preferred auto-assign IPv6 IPAddressPool name | Prefer your actual name. Discovery falls back only when there is one unambiguous eligible pool with L2 advertisement |
| `issuer_preference` | Preferred Ready native-Vault ClusterIssuer | `vault-example`; both sites must have usable signing integrations |
| `secret_store_preference` | Preferred Ready Vault ClusterSecretStore | `example-vault`; KV v2 mount `kv` is required by the current workflow |
| `opnsense_path` | Shared Vault key relative to `kv` | `global/opnsense`; no leading `kv/` or `data/` |
| `cloudflare_path` | Shared Vault key relative to `kv` | `global/cloudflare` |
| `opnsense_url` | Verified HTTPS API base URL | `https://router.example.com:8443`; must exactly match Vault's `opnsense_url` value |
| `opnsense_configtest_success` | Exact JSON `result` string returned by a successful HAProxy configtest on your plugin version | The sample `"\n\n"` is two newline characters after JSON decoding. Commission it; do not assume it matches your router |
| `opnsense_reconfigure_success` | Exact successful reconfigure `status` string | Sample `"ok"`; verify against the installed plugin |
| `administrator_networks` | Explicitly authorized IPv6 management CIDRs for `/admin` | An array of real IPv6 networks; examples are not an authorization policy |
| `database_ca_configmap` | Generated public database-CA ConfigMap name | Keep `keycloak-db-rootcert`; changing it requires matching the manifests |
| `database_ca_key` | PEM data key in that ConfigMap | Keep `db-ca.pem` |
| `recovery_secret` | Namespace-local permanent workflow administrator Secret name | Keep `keycloak-workflow-admin`; never put its value here or rename it mid-deployment |
| `environment` | Stable deployment-environment identifier | Keep aligned with `active-active.json`; changing it invalidates saved-state identity checks |
| `commissioning_cluster` | One Ready ACM managed cluster used for the execution-environment probe | Replace `cluster-a`; not the pair selector for deployment |
| `commissioning_database_host` | Database hostname used for commissioning's IPv6 TCP probe | Replace `postgresql.example.com`; runtime DB identity still comes from the survey-selected Vault entry |

`state_configmap` is derived from the survey namespace at runtime; do not hard-code it. Commissioning proves tools, access and network/TLS connectivity, not database authentication or HA acceptance.

Current platform assumptions: hub Argo resources use `openshift-gitops`; database connections use PostgreSQL port `5432`; existing Vault/ESO stores use KV v2 mount `kv`; managed application networking is IPv6-only with MetalLB L2. This release is not a turnkey adapter for different namespaces, database ports, Vault mounts or BGP-only pools. Review the corresponding implementation before changing those assumptions.

The automatic workflow currently derives a shared CA bundle from the selected native-Vault issuers and uses it for backend/edge verification and database TLS when selected. Ensure the relevant endpoints chain to that trust bundle and the execution image trusts hub/Git/other API endpoints. Do not disable TLS verification to bypass a trust mismatch.

## `active-active.json`: gates and defaults

The shipped values are `execution_enabled: false` and `removal_enabled: false`. The edge payload separately has `reviewed: false`.

| Setting | Guidance |
| --- | --- |
| `execution_enabled` | Set true only after platform access, execution tools, recovery storage and edge commissioning are reviewed |
| `removal_enabled` | Independent destructive gate; leave false until removal is intentionally commissioned |
| `environment` | Must match the platform profile |
| `expected_csv` | Keep `keycloak-operator.v26.8.1` with the corresponding runtime image; version changes need coordinated upgrade work |
| `timeout_seconds` | Bounded resource-readiness wait; increasing it is not a fix for an unhealthy dependency |
| `hub_namespace` | Keep `openshift-gitops` for this implementation |
| `namespace`, `public_host`, `public_vip`, `sites` | Static/legacy examples. Automatic runs replace identity with survey/ACM values; do not use them to relocate an existing deployment |
| `opnsense.objects_file` | Keep the repository-relative adapter template path |
| `opnsense.enable_service` | Review whether the workflow may enable HAProxy on first deployment; shared-service ownership checks still apply |
| `opnsense.url`, response strings, `cloudflare.zone_id` | Automatic runs use platform settings and zone discovery; the static fields support legacy preparation |
| `smoke_client_id` | Keep the scoped synthetic authentication client identifier unless you review bootstrap/reconcile behavior |

The public `2001:db8::/32` example addresses are non-routable documentation values. The survey intentionally leaves public hostname/VIP defaults empty. Enter a real globally routable IPv6 VIP during launch; do not weaken validation to accept documentation addresses.

## `objects.json`: leave replacement markers intact

The file describes five HAProxy object types: health check, two servers, backend pool and public frontend. `@server/...` and `@healthcheck/...` are references resolved to verified UUIDs, not literal names to send to HAProxy.

| Marker / field | Filled by |
| --- | --- |
| `REPLACE_ME_PUBLIC_FQDN`, `REPLACE_ME_PUBLIC_IPV6` | Deployment survey; reused consistently for TLS/SNI, host ACLs and listener |
| `REPLACE_ME_CLUSTER_A_METALLB_IPV6`, `REPLACE_ME_CLUSTER_B_METALLB_IPV6` | Each runtime Service's actual MetalLB allocation |
| `REPLACE_ME_OPNSENSE_PUBLIC_CERT_UUID` | Imported cert-manager TLS certificate |
| `REPLACE_ME_OPNSENSE_BACKEND_CA_UUID` | Exact matching backend trust CA |
| `REPLACE_ME_ADMIN_IPV6_CIDRS` | `administrator_networks` in the platform profile |
| Object names / references | Namespace-derived deployment prefix |
| Backend `source` | Discovered router interface/source, with IPv6 syntax handled by the adapter |

Keep verified backend TLS, `/lb-check`, DNS hostname checks and the public health/metrics restrictions. Review timeouts and health thresholds against the installed os-haproxy model. After testing the scoped API adapter, set `reviewed: true` in the **private deployment fork**. It is independent of the deployment execution flag.

## `record.json`: DNS template

Keep `type: "AAAA"` and `proxied: false`. `name` and `content` contain replacement markers; `dns_record()` overwrites both from the selected deployment. `ttl` is the desired TTL. The `comment` participates in ownership verification; do not change it on an existing installation without an ownership migration. The zone is discovered using the Vault-backed API token and selected hostname, not supplied in the deployment survey.

## Surveys: registration, not live forms

`survey.json` defines seven fields: cluster A, cluster B, public hostname, public IPv6 VIP, namespace, database secret path and PostgreSQL TLS. Registration refreshes the two cluster-choice lists from ACM. The checked-in `cluster-a`/`cluster-b` entries are offline examples; keep `controller_refresh_cluster_choices: true` to replace them with actual eligible clusters.

`removal-survey.json` defines namespace, Plan/Remove, reload/stop-start, hostname confirmation, database name, backup policy, backup reference and optional no-backup confirmation. Leave destructive confirmation defaults blank. The actual clusters, public endpoint and resource ownership are read from the saved deployment record. Never insert a customer's database password or API token into either survey.

After changing either survey file, sync the AAP project and rerun its controller registration playbook. Editing Git does not immediately update a survey already installed in AAP.

## Vault and `external-secrets.yaml`

Create the values through Vault administration, not through Git or the AAP survey:

| Vault key relative to `kv` | Required properties |
| --- | --- |
| Survey-selected database path, e.g. `global/tenant-a/postgresql` | `host`, `username`, `password`, `database`; port is fixed at `5432` by the runner |
| Platform `opnsense_path` | `opnsense_url`, `opnsense_key`, `opnsense_secret` |
| Platform `cloudflare_path` | `api-token` |

Both managed clusters' existing ESO identities must be allowed to read these entries. A dedicated database and login must already exist. `dbname` is not the consumed property name; store it as `database`. Do not use one database for independent installations.

The automatic renderer replaces `REPLACE_ME_CLUSTER_SECRET_STORE` with each selected site's discovered store, and fills remote paths from the survey/platform. It removes the legacy bootstrap ExternalSecret and unused automation mappings. The Keycloak Operator creates the temporary administrator; the runner establishes and persists its permanent credentials in owned Kubernetes Secrets. There is no bootstrap-admin Vault path in the automatic survey.

`REPLACE_ME_CLUSTER_ISSUER` is replaced in the Certificate as well. The certificate template requests ECDSA P-256 and a Common Name; confirm that your Vault signing role permits those settings and the selected public hostname.

## Execution image, storage and controller settings

1. Supply `BASE_EE_IMAGE` in the BuildConfig's Docker build arguments. Use an approved, digest-pinned RHEL-based Ansible execution image that already contains `oc`, `kubectl`, `ansible-runner`, Python/PyYAML, compatible OpenSSL and your public CA trust. This Containerfile adds `psql`; it does not build an entire AAP image from an empty OS.
2. Build and publish the image using your registry access. Replace `REPLACE_ME_KEYCLOAK_EE_IMAGE_AT_DIGEST` in the worker pod specification with the final image reference. Register that same image in the dedicated AAP execution environment.
3. Replace `REPLACE_ME_STORAGE_CLASS` in `gitops/hub/execution/recovery-pvc.yaml` with a suitable storage class. The recovery claim must mount persistently at `/runner/keycloak-recovery` across execution pods.
4. Review every RBAC subject `aap/keycloak-runner` in the hub bootstrap files. It is an example identity; bind permissions to the ServiceAccount represented by your AAP hub credential. These additive roles are not a full cluster-admin bootstrap.
5. Copy `automation/aap/controller/settings.example.yml` to a private location outside the checkout. Replace all zero IDs and empty ID lists with objects from your AAP controller: organization, project, inventory, EE, dedicated container group and hub credentials. Use the hub credential for discovery/plan and execution; existing managed-cluster credentials come through ACM. Keep fallback disabled and mount recovery storage in the selected group.
6. Inject `CONTROLLER_OAUTH_TOKEN` and `KUBECONFIG` securely, then register. Do not put either value in a command-line literal, Git file or job survey.

Example registration commands, after the settings and dependencies are ready:

```sh
ansible-playbook automation/aap/playbooks/configure-controller.yml \
  -e @/secure/path/controller-settings.yml -e configure_controller=true
ansible-playbook automation/aap/playbooks/configure-removal-controller.yml \
  -e @/secure/path/controller-settings.yml -e configure_controller=true
```

These playbooks register templates; they do not launch deployment/removal. Configure the standalone commissioning template to run `automation/aap/playbooks/commission-execution.yml` with the same hub credential, EE, group and recovery volume. Run it twice to verify marker persistence. Its router probe checks TLS/connectivity; authenticated edge writes need separate commissioning.

## Validate before enabling execution

Install Python dependencies with `python3 -m pip install -r automation/aap/requirements.txt`. Local regression tests also need `kubectl`, `openssl` with hostname verification, and `ansible-playbook` from a supported Ansible installation.

```sh
python3 -m json.tool automation/aap/profiles/platform.json > /dev/null
python3 scripts/check_configuration.py
python3 -m unittest discover -s tests -v
kubectl kustomize gitops/operator
kubectl kustomize gitops/keycloak/prerequisites
kubectl kustomize gitops/keycloak/overlays/cluster-a
kubectl kustomize gitops/keycloak/overlays/cluster-b
```

The configuration checker is offline. It intentionally rejects unconfigured sample endpoints, documentation management networks, missing required platform fields and image/storage placeholders; it does not test credentials, network access or server admission. Use `--allow-examples` only to validate the public scaffolding locally; it is not permission to deploy samples. Raw Kustomize output still includes workflow-supplied markers and must not be applied as-is.

Commit reviewed non-secret changes to the private deployment fork. Sync AAP to that commit, commission access, then enable the deployment and edge-review flags. The automatic workflow revalidates live readiness and passes the exact revision between stages. Keep removal disabled until it is intentionally required.

## Existing deployments and ownership

The public code uses `keycloak.example.com` ownership labels/annotations. This neutral identifier is an API contract, not a URL to contact; keep it stable after deployment. Existing private deployments with another ownership prefix are **not automatically adopted**. Do not point this public runner at an existing installation or replace its source without a separately reviewed ownership/state migration.

The internal development repository remains authoritative for its own environment. Use a sanitized source export for updates, not a Git merge that imports private history. See [publication and synchronization](PUBLICATION.md).
