# Hub registration and application project

> **Documentation examples:** names, domains and IP addresses are anonymized. Use your own environment values; see [example conventions](../../docs/examples.md).

The automatic AAP workflow creates additive registration and per-installation projects from discovered values. The static files here are scaffolding for review or the legacy manual path; they contain installation-specific values.

These manifests target the ACM hub and its `openshift-gitops` namespace. They are separate from resources destined for the two managed clusters. No root Application automatically installs them.

## Prepared resources

- `registration/`: a `default` ManagedClusterSetBinding, a `keycloak-sites` Placement with two configured cluster selectors (illustrated as `cluster-a` and `cluster-b`), and a GitOpsCluster referencing that Placement and the existing Argo CD instance.
- `project/`: AppProject `keycloak-active-active`, restricted to this repository, the two API endpoints, the `keycloak` destination namespace, and an explicit resource-kind list.
- `../applications/`: six manual-sync Applications using fixed API server destinations. Registration is still necessary; using a server URL avoids depending on the generated cluster-secret name.

Placement tolerates unavailable and unreachable taints without expiry to retain site registration during transient failures. The AAP preflight separately requires availability before initial deployment. Static Applications have no automated sync, prune, or deletion finalizer. A failed site is never replaced with the hub or an arbitrary third cluster.

## Local review

```sh
kubectl kustomize gitops/hub/registration
kubectl kustomize gitops/hub/project
kubectl kustomize gitops/applications
```

Resolve `REPLACE_ME_REVIEWED_REVISION` to the full reviewed commit before installing Applications. Repository credentials and the Git server CA must already be available to the hub Argo CD repo-server. This repository does not create or export those credentials.

## Manual setup reference

1. Run the AAP preflight with a scoped hub credential. Review existing bindings/registrations for ownership conflicts and verify the installed CRD schemas.
2. Install the reviewed registration manifests on the hub. Wait for both expected PlacementDecision identities and generated Argo CD cluster registrations. Confirm each connection is successful in Argo CD; a Secret's existence alone is insufficient.
3. Install the reviewed AppProject on the hub. Verify Git access, certificate trust, and Argo CD access to both managed-cluster APIs.
4. Install the Applications at the reviewed Git revision. Their creation does not enable automatic synchronization. Follow the operator/prerequisite/bootstrap sequence in the deployment plan.

This manual sequence is not required when using the commissioned automatic workflow. The Ansible preflight can check registration with `require_registration=true` after step 2; it does not itself perform registration or contact the Argo CD API for connection-health validation.

## AAP discovery permissions

`discovery-rbac/` adds read permissions for an example hub identity, `system:serviceaccount:aap:keycloak-runner`: list nodes, get `networks.config.openshift.io/cluster`, and get Argo Applications in `openshift-gitops`. Replace the subject with your actual execution identity and review its existing permissions. These additive resources grant no deployment writes or additional Secret access. Validate and apply them on your own hub; this public copy has not configured your cluster.

For another hub, review the bound ServiceAccount before applying:

```sh
kubectl kustomize gitops/hub/discovery-rbac
oc --context=<hub-context> apply -k gitops/hub/discovery-rbac
```

Keep these bootstrap permissions separate from workload registration: discovery needs them before a deployment can run. The runner uses `applications.argoproj.io` explicitly because the unqualified `application` name resolves to ACM's `app.k8s.io` API on this hub. Commission deployment/sync permissions for each new hub before enabling its execution gate.

## Permissions and ownership

GitOpsCluster defaults to the existing ACM application-manager service accounts. This reuses credentials under ACM's lifecycle, without committing tokens. AppProject restrictions govern applications in this project; they do not reduce those service accounts' underlying Kubernetes permissions, constrain other projects, or revoke hub administrator permissions. Keep creation/update of Applications and AppProjects restricted to trusted automation and reviewers.

The project allows Namespace as its only cluster-scoped kind because the operator application creates `keycloak`. This kind-level rule is not a namespace-name restriction for cluster-scoped objects. Use admission policy and/or a dedicated managed service account with scoped Kubernetes RBAC if stronger enforcement is required. OLM creates its own operator RBAC and CRDs; neither is directly allowed in application source here. Cert-manager and ESO own generated Secrets; direct Secret manifests are deliberately absent from the project allow-list.

This project does not grant AAP an Argo CD role or issue API tokens. Supply a separately reviewed role with only the required application read/sync permissions. OLM approval uses a separately scoped managed-cluster permission. Do not use the permissive default AppProject for these Applications.

## References

- [ACM GitOps integration](https://docs.redhat.com/en/documentation/red_hat_advanced_cluster_management_for_kubernetes/2.14/html-single/gitops)
- [Placement tolerations](https://open-cluster-management.io/docs/scenarios/migrate-workload-with-placement/)
- [Argo CD project specification](https://argo-cd.readthedocs.io/en/stable/operator-manual/project-specification/)

The reference implementation used ACM 2.17.3. Validate API fields and server-side admission against the version installed on your hub.
