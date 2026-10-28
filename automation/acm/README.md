# ACM integration design

> **Documentation examples:** names, domains and IP addresses are anonymized. Use your own environment values; see [example conventions](../../docs/examples.md).

AAP starts the self-service workflow. ACM supplies managed-cluster inventory, placement, registration with OpenShift GitOps, and fleet visibility. Argo CD reconciles Git-defined Kubernetes resources. AAP coordinates deployment stages and configures OPNsense and Cloudflare.

The existing ACM hub runs the hub-based OpenShift GitOps push model. The live inventory recorded ACM 2.17.3, OpenShift GitOps 1.22.2 and AAP 2.7. ACM registration is now configured through the shared `keycloak-aap-sites` Placement/GitOpsCluster. Automatic Applications target the registered cluster names, including registrations backed by ACM cluster-proxy URLs. The [readiness report](../../docs/deployment-readiness.md) supersedes the earlier unconfigured state in the historical environment audit.

## Responsibility boundaries

| Owner | Responsibility |
| --- | --- |
| Git | Reviewed desired configuration, cluster-to-overlay mapping, exact release revision |
| AAP | Request validation, workflow sequencing, approved InstallPlan actions, edge/DNS APIs, acceptance report |
| ACM | ManagedCluster inventory, explicit target selection, GitOps registration, prerequisite compliance visibility |
| Argo CD | Namespace, OperatorGroup, Subscription, and Keycloak workload manifests |
| OLM | Operator installation and lifecycle, CSV, operator RBAC, CRDs |
| Keycloak Operator | Keycloak runtime resources |
| cert-manager / ESO | Certificate and secret materialization from existing integrations |

Use ACM policies in inform mode for overlapping application checks. Do not use enforcing policies or ManifestWorks to apply the same resources owned by Argo CD. Platform teams can retain enforcement for separate platform resources with explicitly defined ownership. OLM Subscriptions are distinct from ACM application Subscriptions; this design uses the former for the operator and Argo CD Applications for workloads.

## Hub integration

1. Reuse the ManagedClusterSet containing the two existing managed clusters. Bind that set to the authorized hub namespace through ManagedClusterSetBinding, subject to existing RBAC.
2. Define a Placement selecting the two explicitly approved ManagedCluster identities. A generic selector with `numberOfClusters: 2` is insufficient: it could select an unintended replacement. Check the resulting PlacementDecision identities, not merely the count.
3. Reference that Placement from a GitOpsCluster in the same namespace. This registers selected managed clusters with the intended Argo CD instance. Reuse existing registration if already configured; do not create competing registration owners.
4. Map stable managed-cluster names to `cluster-a` and `cluster-b` overlays. Record the survey-selected order in the deployment state and generated Application patches; never derive site identity from PlacementDecision order.
5. The automatic runner renders six Applications using the selected registered cluster names. Direct managed API URLs are retained for AAP access, not used as automatic Argo destinations. This supports both direct and cluster-proxy registrations.

The generated explicit Applications are suitable for each selected pair. An ApplicationSet can generate them later, but is not required to involve ACM. If adopted, use an explicit mapping and reviewed deletion behavior; do not allow a transient PlacementDecision change to prune a site's application or namespace. Validate registration/placement tolerations for temporary cluster unavailability. Freeze destructive operations while a site is unavailable, then reconcile its state when it returns.

The hub-based push model requires Argo CD connectivity through the registered managed-cluster endpoint, which may be ACM’s cluster proxy. AAP needs access to the hub and Argo CD, and to the external APIs used for OPNsense and Cloudflare. Direct managed-cluster queries and InstallPlan approval require narrowly scoped credentials or a validated ACM managed-service-account/proxy integration. ACM inventory access alone does not grant access to managed-cluster resources.

## Readiness and availability

Before initial deployment, require the expected two clusters to be joined, available, and selected, with API access and required platform components healthy. Verify the catalog, issuer/store references, IPv6 pools, database connectivity, and certificate challenge path per cluster. During an established site's outage, preserve existing traffic on the healthy site and prohibit destructive remediation based solely on placement changes.

Argo CD Synced alone does not prove service readiness. The workflow must also inspect CSV Succeeded, Certificate Ready, ExternalSecret readiness and output keys, Keycloak status, pod readiness, and backend `/lb-check`. Set bounded timeouts and record the exact failed condition. ACM compliance reports supplement these checks; they do not replace them.

ACM, AAP, and hub GitOps are management-plane components. Their temporary loss should prevent new deployments while the already-running authentication data path continues independently. Test this operational expectation and avoid runtime authentication dependencies on the automation platform. Keep a break-glass AAP administrative path independent of the Keycloak instance it is deploying.

## Operational reference

The hub resources and AAP templates are installed. Keep cluster inventory, ClusterSet membership, registration permissions and availability tolerations current. The [automatic survey contract](../aap/SELF-SERVICE.md) is the current interface; older manual implementation notes are historical. The [hub manifests](../../gitops/hub/README.md) describe the registration resources, and [deployment readiness](../../docs/deployment-readiness.md) records live validation and remaining acceptance.

## References

- [Red Hat ACM GitOps integration](https://docs.redhat.com/en/documentation/red_hat_advanced_cluster_management_for_kubernetes/2.14/html-single/gitops) describes Placement and GitOpsCluster registration and push/pull models. Check the matching documentation for the installed version before using API examples.
- [Red Hat multicluster authentication with AAP](https://developers.redhat.com/articles/2025/09/08/multicluster-authentication-ansible-automation-platform) describes a managed-service-account and cluster-proxy approach; validate its prerequisites and permissions before adoption.
