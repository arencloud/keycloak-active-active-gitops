# Build the Keycloak execution image

[Platform configuration](../../../docs/CONFIGURATION.md) · [Recovery storage](../../../gitops/hub/execution/README.md)

The Containerfile adds the RHEL PostgreSQL client package (`postgresql`, providing `psql`) to an **operator-supplied** AAP execution image. It does not contain a private registry reference or base-image digest.

Choose a RHEL-based image that already provides Ansible Runner, Python/PyYAML, `oc`, `kubectl`, OpenSSL with hostname verification and the CA trust required by your APIs. The build installs the client as root, retains the base entrypoint and runs as UID 1000 afterward. Check that this user is compatible with your base image and container-group permissions.

1. Replace `REPLACE_ME_APPROVED_BASE_EE_IMAGE_AT_DIGEST` in `buildconfig.yaml` with your approved immutable base reference, including `@sha256:…`.
2. Review the example `aap` namespace, registry permissions and resource requests.
3. From the repository root, build using your authenticated hub context:

```sh
oc apply -f automation/aap/execution-environment/buildconfig.yaml
oc -n aap start-build keycloak-ee \
  --from-dir=automation/aap/execution-environment --follow --wait
oc -n aap get istag keycloak-ee:1.0.0 \
  -o jsonpath='{.image.dockerImageReference}{"\n"}'
```

The binary upload includes only this directory. It needs no Git token. Package installation verifies repository signatures; rebuilds may resolve newer package versions. Use the resulting immutable image digest, not a mutable tag, in the dedicated AAP execution environment and the worker image of `controller/container-group-pod.yaml`.

For a local container build, pass the same approved reference through `--build-arg BASE_EE_IMAGE=...`; the Containerfile intentionally has no environment-specific default.

Run `playbooks/commission-execution.yml` as a standalone AAP job using the actual hub credential, image and container group. It verifies tools, managed API access, router TLS, database IPv6 TCP reachability and recovery storage. Run it twice to prove the marker survives separate execution pods. The commissioning cluster and database host come from `platform.json`. It does not authenticate to or mutate the database, deploy Keycloak, configure HAProxy or validate every plugin API operation.

Keep execution disabled until the complete [readiness checklist](../../../docs/deployment-readiness.md) is reviewed. Building an image does not enable deployment or destructive removal.
