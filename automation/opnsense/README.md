# OPNsense HAProxy desired configuration

> **Documentation examples:** names, domains and IP addresses are anonymized. Use your own environment values; see [example conventions](../../docs/examples.md).

The automatic deployment workflow configures OPNsense through its APIs. It discovers backend addresses and routing, imports the certificate issued by cert-manager, and reconciles only the selected installation's HAProxy objects. The adjacent `objects.json` is a payload template, not a file to apply directly.

The adapter performs name/UUID checks, a configuration test and reconfiguration. It does not overwrite the generated HAProxy file. Platform maintainers must validate the installed plugin schema and exact API response expectations when commissioning or upgrading the adapter.

## Traffic settings

| Object | Desired settings |
| --- | --- |
| Public service | HTTP mode, TLS listener `[PUBLIC_IPV6_VIP]:443` |
| Certificate | cert-manager/Vault certificate for `auth.example.com`, imported by the workflow |
| Backend pool | Both cluster MetalLB IPv6 addresses on 443, equal weights, round-robin initially |
| Backend TLS | Verify CA and hostname; SNI `auth.example.com`; never disable verification |
| Health check | HTTPS GET `/lb-check`, Host and SNI set to the public hostname, expect HTTP 200 |
| Initial check timing | 3 seconds, fall 3, rise 2; tune using failure tests |
| Access | Restrict administrative paths to approved management sources; block public `/lb-check` |
| Forwarded headers | Remove incoming Forwarded and X-Forwarded-* identity/routing headers; set X-Forwarded-For to client IPv6, Proto=https, Host=public hostname, Port=443 |
| Backend firewall | Permit only intended edge sources and required operational access |

Health checks originate directly from HAProxy to each backend; blocking the public `/lb-check` route must not block these checks. Do not expose port 9000. Reject unexpected Host values. Choose connection/request/idle timeouts from tested login flows; avoid automatic replay of authentication POST requests.

Before making changes, export configuration and inspect the diff. Resolve existing objects by stable names/UUIDs to make reconciliation idempotent. Run configtest before reconfigure, then verify both backend states and a synthetic login. Use the recovery journal to review and recover a failed change; there is no automatic full-firewall rollback. For HA pairs, include configuration/certificate synchronization and test VIP ownership/failover; VIP allocation alone does not provide redundancy.

## Adapter inputs and recovery

The automatic runner supplies certificate/CA UUIDs and allocated backend addresses. Approved administration networks and expected configtest/reconfigure responses belong in the platform profile, not the user survey. The runner transfers the selected TLS Secret's certificate and private key to OPNsense over verified HTTPS; these values must not appear in logs or Git.

Certificate import is reconciled during deployment/reconcile. Continuous renewal delivery is not scheduled yet; commission a refresh schedule or renewal hook before production use.

API saves precede configtest. If any write or validation fails, DNS publication stops. Touched fields and created object identities are journaled to the protected AAP artifact volume for reviewed recovery; automatic full-firewall rollback is not implemented. Frontend custom options implement hostname filtering, management-path restrictions, and authoritative proxy headers. Test them against the effective generated configuration and actual source addresses.

## Removal on a shared router

[Removal](../aap/REMOVAL.md) checks ownership and cross-application references before deleting the selected installation's objects. Other applications, certificates, trust CAs, the public VIP and Vault credentials remain. The default is `reload`; optional `stop-start` affects the shared service briefly and includes a restart check. Removing the last enabled frontend of a running service is blocked during preflight.
