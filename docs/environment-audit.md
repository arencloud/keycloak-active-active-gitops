# Platform inventory worksheet

[Overview](../README.md) · [Configuration guide](CONFIGURATION.md) · [Readiness](deployment-readiness.md)

Use this worksheet for your own platform. The public repository contains examples, not the private installation's inventory. Store completed inventories in your private deployment repository or operations system.

| Component | Record and verify | Configuration location |
| --- | --- | --- |
| ACM hub | API URL, managed-cluster readiness, hub execution identity and permissions | `platform.json`, hub RBAC |
| OpenShift GitOps | Argo CD in `openshift-gitops`, repository access, ACM registration | Hub resources and private SCM credentials |
| Managed clusters | Two distinct eligible ACM names, supported operator catalog/version | Deployment survey; discovered live |
| MetalLB | IPv6 pools, free capacity and L2 advertisements | Pool preference in `platform.json`; backend allocation discovered |
| cert-manager | Ready native Vault issuer, CA trust, signing role restrictions | Issuer preference; discovered per cluster |
| ESO | Ready ClusterSecretStore, KV v2 `kv` access and policies | Store preference; Vault paths |
| OPNsense | HTTPS API, routes, interfaces, HAProxy plugin contracts, available VIP | `platform.json`, shared Vault secret |
| Cloudflare | Authoritative zone, API token scope, DNS-only AAAA record | Shared Vault secret; zone discovered |
| PostgreSQL | Dedicated database, IPv6 reachability, permissions and TLS choice | Vault database entry; survey |
| AAP | Project, inventory, execution environment, instance group and credentials | Private controller settings |
| Recovery | Persistent storage class, PVC permissions and cross-job mount | Hub execution manifests |

The hub may be dual stack. Keycloak backends and the public VIP use IPv6. This release assumes the existing ESO, cert-manager and MetalLB integrations are already installed; it does not create an entire platform.

Run offline checks before live commissioning. Discovery does not replace testing the routed traffic path, edge API writes, database permissions or persistence. Record outcomes with timestamps and the exact private Git revision, keeping logs free of secrets.
