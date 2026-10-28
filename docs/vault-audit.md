# Vault integration and secret schema

[Overview](../README.md) · [Configuration guide](CONFIGURATION.md) · [AAP credentials](aap-credential-audit.md)

The public copy contains no Vault token or populated secret values. Existing ESO and cert-manager integrations authenticate to Vault using their configured platform identities. Provision secret values through your approved secret-management process, not through Git.

## Database entry

Create one dedicated database per independent installation, then store its connection data in KV v2 mount `kv`. The survey takes the entry path relative to that mount, for example `global/tenant-a/postgresql`.

| Property | Meaning |
| --- | --- |
| `host` | PostgreSQL hostname reachable from the managed clusters and execution environment |
| `port` | `5432`; the current automation assumes this port |
| `username` | Account with the required access to the dedicated database |
| `password` | Its secret password |
| `database` | Existing dedicated database name; use this key, not `dbname` |

Use a different database entry for each installation. Selecting TLS in the survey requires trusted server identity verification; disabling it is an explicit customer choice. A certificate failure must not cause an automatic downgrade.

## Shared edge credentials

| Example path | Required properties |
| --- | --- |
| `global/opnsense` | `opnsense_url`, `opnsense_key`, `opnsense_secret` |
| `global/cloudflare` | `api-token` |

Set the corresponding paths and OPNsense URL in `platform.json`. The URL in Vault must agree with the configured endpoint. These credentials can be shared across applications; removal must never delete or change them.

Grant the managed-cluster ESO identity read access to these paths. Verify the selected ClusterSecretStore is Ready and the resulting ExternalSecrets become Ready. A successful store connection alone does not prove access to every requested path.

## Certificate issuance

The automatic workflow uses Ready **native Vault ClusterIssuers**. It discovers issuer CA material and requests an ECDSA P-256 certificate with the chosen FQDN as both Common Name and DNS SAN. The Vault role must permit the name, algorithm and requested validity. An ACME-only issuer is not a drop-in substitute for the current discovery implementation.

Review [external-secrets.yaml](../gitops/keycloak/prerequisites/external-secrets.yaml) and [certificate.yaml](../gitops/keycloak/prerequisites/certificate.yaml) alongside the [configuration guide](CONFIGURATION.md). Their explicit replacement markers are filled by the automatic workflow. Do not apply raw examples directly.
