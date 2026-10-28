#!/usr/bin/env python3
"""Offline validation of public scaffold or a configured private deployment fork."""
import argparse
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit
import yaml

ROOT = Path(__file__).resolve().parents[1]


def validate(root, allow_examples=False):
    errors = []

    def require(condition, field, message):
        if not condition:
            errors.append(f'{field}: {message}')

    def read(relative, yaml_file=False):
        try:
            content = (root / relative).read_text()
            return list(yaml.safe_load_all(content)) if yaml_file else json.loads(content)
        except (OSError, ValueError, yaml.YAMLError):
            errors.append(f'{relative}: cannot read or parse configuration')
            return {}

    platform = read('automation/aap/profiles/platform.json')
    profile = read('automation/aap/profiles/active-active.json')
    edge = read('automation/opnsense/objects.json')
    if not all(isinstance(x, dict) for x in (platform, profile, edge)):
        return ['profiles: expected JSON objects']
    for field in ('hub_api', 'opnsense_url', 'repository_url'):
        value = platform.get(field, '')
        try:
            url = urlsplit(value)
            valid = url.scheme == 'https' and bool(url.hostname) and not (url.username or url.password or url.query or url.fragment)
            # Also reject malformed ports without echoing their values.
            url.port
        except (ValueError, TypeError):
            valid = False
            url = urlsplit('')
        require(valid, field, 'use an HTTPS URL without embedded credentials, query or fragment')
        if not allow_examples:
            host = url.hostname or ''
            require(not (host == 'example.com' or host.endswith(('.example.com', '.example', '.invalid')) or 'REPLACE_ME' in str(value)), field, 'replace the example endpoint')
    for field in ('metallb_pool_preference', 'issuer_preference', 'secret_store_preference',
                  'opnsense_path', 'cloudflare_path', 'opnsense_configtest_success',
                  'opnsense_reconfigure_success', 'recovery_secret', 'environment',
                  'commissioning_cluster', 'commissioning_database_host'):
        value = platform.get(field)
        require(isinstance(value, str) and len(value) > 0, field, 'must be a nonempty string')
        if not allow_examples and isinstance(value, str):
            require('REPLACE_ME' not in value and value not in ('vault-example', 'example-vault') and not value.endswith(('.example.com', '.example')), field, 'replace the example value')
    networks = platform.get('administrator_networks')
    require(isinstance(networks, list) and bool(networks), 'administrator_networks', 'supply at least one IPv6 management CIDR')
    for item in networks if isinstance(networks, list) else []:
        try:
            network = ipaddress.ip_network(item, strict=True)
            require(network.version == 6, 'administrator_networks', 'IPv6 networks are required')
            if network.version == 6 and not allow_examples:
                require(not network.overlaps(ipaddress.ip_network('2001:db8::/32')), 'administrator_networks', 'replace documentation networks')
        except (ValueError, TypeError):
            errors.append('administrator_networks: invalid network CIDR')
    for field, expected in (('database_ca_configmap', 'keycloak-db-rootcert'), ('database_ca_key', 'db-ca.pem')):
        require(platform.get(field) == expected, field, 'must match the manifest contract')
    require(platform.get('environment') == profile.get('environment'), 'environment', 'platform and deployment profile must agree')
    require(profile.get('hub_namespace') == 'openshift-gitops', 'hub_namespace', 'this release requires openshift-gitops')
    for field in ('execution_enabled', 'removal_enabled'):
        require(type(profile.get(field)) is bool, field, 'must be a JSON boolean')
        if allow_examples:
            require(profile.get(field) is False, field, 'public scaffolding must keep execution disabled')
    require(type(edge.get('reviewed')) is bool, 'objects.reviewed', 'must be a JSON boolean')
    if allow_examples:
        require(edge.get('reviewed') is False, 'objects.reviewed', 'public scaffolding must remain unreviewed')
    sites = profile.get('sites', [])
    require(isinstance(sites, list) and len(sites) == 2 and all(isinstance(x, dict) for x in sites), 'sites', 'two site templates are required')
    if not allow_examples:
        for relative in ('automation/aap/execution-environment/buildconfig.yaml',
                         'automation/aap/controller/container-group-pod.yaml',
                         'gitops/hub/execution/recovery-pvc.yaml'):
            data = read(relative, yaml_file=True)
            require('REPLACE_ME' not in json.dumps(data), relative, 'configure the image or storage placeholder')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-examples', action='store_true', help='validate the disabled public scaffold; not a readiness check')
    args = parser.parse_args()
    errors = validate(ROOT, args.allow_examples)
    for error in errors:
        print(error)
    if errors:
        return 1
    print('Offline scaffold validation passed.' if args.allow_examples else 'Offline configuration validation passed; live commissioning is still required.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
