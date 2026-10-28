# Documentation examples

[Overview](../README.md) · [Deploy](deployment.md) · [Survey](../automation/aap/SELF-SERVICE.md) · [Remove](../automation/aap/REMOVAL.md)

The guides and diagrams use fictional environment identifiers so you can read and share them without exposing the installation's hostnames or addresses. They describe the workflow; they are not a connection inventory.

## Values used throughout the guides

| Purpose | Documentation example | Where your real value comes from |
| --- | --- | --- |
| Managed clusters | `cluster-a`, `cluster-b` | ACM inventory, selected in the survey |
| Public Keycloak hostname | `auth.example.com` | Your deployment survey |
| Public IPv6 VIP | `PUBLIC_IPV6_VIP` | An available, routed address selected in the survey |
| Namespace | `keycloak` | Your survey; use a distinct namespace for each installation |
| Database secret path | `global/tenant-a/postgresql` | An existing Vault KV v2 entry, relative to mount `kv` |
| Dedicated database | `keycloak_db` | The `database` property in that secret |
| Vault | `https://vault.example.com` | Existing ESO/cert-manager integration |
| OPNsense API | `https://router.example.com:8443` | Shared Vault entry `global/opnsense` |
| AAP | `https://aap.apps.hub.example.com` | Your platform administrator |
| Git repository owner | `platform` | Your SCM configuration |

Public IPv6 values are masked with labels: `PUBLIC_IPV6_VIP` for the service address, `PUBLIC_INTERFACE_IPV6` for the router interface, and `PUBLIC_GATEWAY_IPV6` for its gateway. These labels are not valid addresses; substitute your actual values when configuring a deployment.

Other illustrated IPv6 networks use `2001:db8::/32`; the IPv4 example is `192.0.2.10`. These addresses are reserved for documentation and must be replaced before use. They illustrate relationships between networks, not the production distinction between public and private address space. Names under `example.com` are also examples.

## What is an example, and what is a contract?

- **Replace environment values:** hostnames, addresses, cluster names, issuer/store names, organization labels and tenant-specific paths. Example values are not promises about survey defaults or resources present in your installation.
- **Keep field names:** `host`, `username`, `password`, `database`, `opnsense_url`, `opnsense_key`, `opnsense_secret` and `api-token` are consumed by the automation. Port `5432` is the current database-port contract.
- **Use installed workflow names:** **Keycloak Active Active Self Service** and **Keycloak Active Active Removal**. AAP assigns numeric IDs locally; do not copy IDs from an audit into another controller.
- **Use actual removal confirmations:** `REMOVE <public-hostname>` and, when choosing no backup, `DELETE <database-name> WITHOUT BACKUP`. Substitute the values from the selected deployment record and database secret.

## Public distribution scope

Examples apply throughout this public copy: documentation, diagrams, JSON, YAML, scripts and tests. Operational reports have been replaced with configuration worksheets; private Git history is not imported. The public GitHub URL and copyright notices are retained as project attribution.

Use the [configuration guide](CONFIGURATION.md) to prepare a private deployment fork. Deployment and removal are disabled by default. Explicit `REPLACE_ME_*` markers are either platform settings to fill privately or template fields resolved by the automatic workflow; the guide distinguishes them.

The neutral `keycloak.example.com` ownership prefix is a stable resource contract, not an endpoint. Do not replace it merely because you use a different domain. See [publication and synchronization](PUBLICATION.md) for maintaining this separation in future updates.
