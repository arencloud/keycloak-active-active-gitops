# AAP credential requirements

[Overview](../README.md) · [Configuration guide](CONFIGURATION.md) · [Self-service](../automation/aap/SELF-SERVICE.md)

This is a credential contract for a new installation, not an inventory of existing controller credentials. Keep shared credentials used by other applications intact.

| Purpose | Where it belongs | How it is used |
| --- | --- | --- |
| Hub API access | An AAP credential injecting `KUBECONFIG` | The runner connects to the hub; ACM supplies managed-cluster access through the configured integration. |
| Private Git checkout | AAP project SCM credential | AAP reads the private deployment fork. Argo CD needs its own repository access configuration. |
| OPNsense API key/secret | Shared Vault KV entry | Existing managed-cluster ESO projects the fields into a Kubernetes Secret for the runner. |
| Cloudflare API token | Shared Vault KV entry | ESO projects the token; the workflow discovers the authorized DNS zone. |
| PostgreSQL credentials | A dedicated database entry in Vault | ESO supplies credentials for the selected installation. |
| Controller registration | Secure environment/credential for the registration playbook | Installs AAP templates and surveys; not a customer survey field. |
| Image registry access, if needed | Registry pull secret/controller credential | Pulls the chosen execution image. |

No Vault token or separate managed-cluster password belongs in the deployment survey. Existing ESO and cert-manager authentication remain platform-managed. The workflow is not a general mechanism for extracting all managed-cluster secrets from hub credentials: its execution identity still needs the specific API permissions configured by the platform administrator.

Configure IDs in an untracked/private copy of [controller settings](../automation/aap/controller/settings.example.yml). IDs are local to your AAP instance. Use the provided credential-type and RBAC definitions as contracts to review against your actual runner identity.

The bootstrap administrator is created during installation and recovery material is kept in the configured recovery storage/Secret. It is not requested as a Vault bootstrap-admin path. Secure recovery storage and control who can launch deployment or destructive removal workflows.
