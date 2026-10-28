# Execution identity and persistent recovery storage

[Configuration guide](../../../docs/CONFIGURATION.md) · [Execution image](../../../automation/aap/execution-environment/README.md)

These hub-only resources prepare a namespace-scoped Role/RoleBinding and a durable recovery PVC. The example runner is `aap/keycloak-runner`; replace the ServiceAccount subject with the identity actually used by your AAP hub credential, and review the discovery RBAC as well. Do not change shared identities or credentials used by other applications.

Replace `REPLACE_ME_STORAGE_CLASS` with a suitable existing storage class. Review PVC access modes and the execution pod's UID/mount permissions. After reviewing these values, a hub administrator can apply:

```sh
oc apply -k gitops/hub/execution
oc -n aap get pvc keycloak-recovery
```

Create a dedicated AAP container group using [container-group-pod.yaml](../../../automation/aap/controller/container-group-pod.yaml). Set its worker image to the same immutable digest as the dedicated execution environment; the pod mounts recovery storage at `/runner/keycloak-recovery`. Set `controller_instance_group_ids` in your private controller settings to the assigned group ID. Registration associates only the Keycloak templates and disables fallback.

Create a standalone commissioning template using `automation/aap/playbooks/commission-execution.yml`, the project, inventory, execution environment, hub credential and container group. Run it in two separate jobs and verify the second sees the first job's marker. It checks `psql`, OpenSSL hostname verification, API/network access and durable storage. No database login or deployment is performed.

AAP and Argo CD need access to the same deployment repository. Configure the AAP project SCM credential and a repository-scoped Argo CD credential separately; configure trust for your Git host without overwriting unrelated CA entries. Do not store access tokens, SSH private keys or repository Secret values in this checkout.

Complete [readiness](../../../docs/deployment-readiness.md) before enabling the profile gates. No controller ID, image reference, storage class or repository credential from a different installation is portable by default.
