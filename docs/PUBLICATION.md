# Public releases and private configuration

[Overview](../README.md) · [Configuration guide](CONFIGURATION.md)

Maintain separate repositories for private development and public releases. The public repository contains sanitized source and examples, with its own Git history. Publishing an anonymized current tree on top of private history would still expose old values.

## Using this public project

1. Create a private deployment fork or private source repository.
2. Follow [configuration](CONFIGURATION.md) to replace platform examples. Keep credentials in Vault/AAP and local controller settings outside the checkout.
3. Configure AAP and Argo CD to read that same private repository and revision.
4. Commission your platform before enabling deployment or removal.

`.gitignore` excludes common local credentials, kubeconfigs, IDE files and generated artifacts. It is a convenience, not a secret scanner. It cannot remove a file or secret already committed. The runtime does not automatically load `.local` overrides; configure the documented files in the private fork.

## Synchronizing a future public release

Export reviewed **tracked source files** from private development into a separate staging directory. Do not copy `.git`, local credentials, recovery state, rendered manifests, logs, caches or IDE configuration. Preserve the public repository's own `.git` directory, license and notices.

Reapply and review public-specific changes before updating the public working tree:

- Replace operational domains, IPs, cluster names, tenant/account identifiers, Vault paths, controller IDs, private registries and image digests with examples or explicit placeholders.
- Preserve configurable repository/commissioning endpoints and the public template markers. Do not restore private literals in Python, tests, YAML, JSON, diagrams or generated assets.
- Keep deployment/removal disabled and edge templates unreviewed. FQDN/VIP survey defaults must remain blank.
- Replace operational audit logs with reusable setup guidance. Do not import private job references or commit hashes as public evidence.
- Keep the public ownership prefix stable. Changes to it require an explicit migration for existing installations.
- Preserve the public configuration guide, validation script and regression coverage.

Do not run a blind directory mirror over the public repository: public configuration and documentation deliberately differ from internal development. Do not merge private Git history into the public branch. A reviewed content export produces an ordinary public commit without importing private ancestry.

## Release checks

```sh
python3 scripts/check_configuration.py --allow-examples
python3 -m unittest discover -s tests -q
kubectl kustomize gitops/operator
kubectl kustomize gitops/keycloak/prerequisites
kubectl kustomize gitops/keycloak/overlays/cluster-a
kubectl kustomize gitops/keycloak/overlays/cluster-b
```

Install Python dependencies and the command-line tools described in the project guides first. Scan both the candidate file tree and the complete **public** Git history with a secret scanner. Review any finding without printing its value; test fixtures can produce false positives, but broad test-directory exclusions can hide real leaks. Also search for private environment identifiers that a generic secret scanner cannot recognize.

Review the staged diff and file list, including SVG metadata, examples and author metadata. Confirm `.gitignore` exclusions and verify that private-source content/history have not leaked. Commit and publish only the reviewed public content. If a real credential was ever published, removing it from the current file does not invalidate it or erase history.

The public GitHub repository address and retained copyright attribution identify the public project; they are not runtime connection settings for the private environment.
