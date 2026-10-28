"""Non-secret survey parameters and matching Argo CD Kustomize patches."""
import ipaddress
import json
import re
import ssl
import urllib.parse
import yaml

# The cluster pair/namespaces are an approved platform boundary, not free-form targets.
FIELDS = [
    ('public_host', 'Public DNS hostname', 'auth.example.com'),
    ('public_vip', 'OPNsense IPv6 VIP (already assigned)', '2001:db8:10:7::70'),
    ('backend_a', 'cluster-a MetalLB IPv6 address', ''),
    ('backend_b', 'cluster-b MetalLB IPv6 address', ''),
    ('proxy_cidr', 'Observed HAProxy source IPv6 CIDR', ''),
    ('automation_cidr', 'AAP execution node IPv6 CIDR', ''),
    ('admin_cidrs', 'Administrator IPv6 CIDRs (space separated; include AAP source)', ''),
    ('database_path', 'Vault KV database path (relative to configured store)', 'global/keycloak/active-active/postgresql'),
    ('bootstrap_path', 'Vault KV bootstrap administrator path', ''),
    ('database_ca', 'PostgreSQL trusted CA PEM (public certificate only)', ''),
    ('issuer', 'Existing ClusterIssuer', 'vault-example'),
    ('secret_store', 'Existing ClusterSecretStore', 'example-vault'),
    ('metallb_pool', 'Existing MetalLB address pool', 'machine-network-last-50'),
    ('opnsense_url', 'OPNsense HTTPS API URL', 'https://router.example.com:8443'),
    ('backend_ca_uuid', 'OPNsense backend trust CA UUID', ''),
    ('public_cert_uuid', 'OPNsense public TLS certificate UUID', ''),
    ('configtest_success', 'Reviewed HAProxy configtest success result', ''),
    ('reconfigure_success', 'Reviewed HAProxy reconfigure success status', ''),
    ('cloudflare_zone', 'Cloudflare zone ID', '00000000000000000000000000000000'),
    ('credential_namespace', 'Managed-cluster automation Secret namespace (fixed)', 'keycloak'),
    ('credential_secret', 'Managed-cluster automation Secret (fixed)', 'keycloak-automation'),
    ('automation_path', 'Vault KV automation passwords and CA bundles path', 'global/keycloak/active-active/automation'),
    ('opnsense_path', 'Shared Vault KV OPNsense path', 'global/opnsense'),
    ('cloudflare_path', 'Shared Vault KV Cloudflare path', 'global/cloudflare'),
]


def parameters(path):
    values = json.loads(path.read_text())
    allowed = {f[0] for f in FIELDS}
    if set(values) - allowed:
        raise ValueError('Unknown survey field')
    for name, _, default in FIELDS:
        values.setdefault(name, default)
        if not isinstance(values[name], str) or not values[name].strip():
            raise ValueError('Required survey value: ' + name)
        if name != 'database_ca' and any(c in values[name] for c in '\n\r\x00'):
            raise ValueError('Multiline input is not allowed: ' + name)
    url = urllib.parse.urlsplit(values['opnsense_url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Credential-free HTTPS OPNsense URL required')
    for name in ('public_vip', 'backend_a', 'backend_b'):
        ipaddress.IPv6Address(values[name])
    if ipaddress.IPv6Address(values['backend_a']) == ipaddress.IPv6Address(values['backend_b']):
        raise ValueError('Backend addresses must differ')
    for name in ('proxy_cidr', 'automation_cidr', 'admin_cidrs'):
        parts = values[name].split()
        if name != 'admin_cidrs' and len(parts) != 1:
            raise ValueError('One IPv6 CIDR is required')
        for part in parts:
            if ipaddress.ip_network(part).version != 6:
                raise ValueError('IPv6 CIDRs required')
    if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9]+(?:[-.][a-z0-9]+)*\.[a-z]{2,63}', values['public_host']):
        raise ValueError('Invalid public hostname')
    for name in ('backend_ca_uuid', 'public_cert_uuid'):
        if not re.fullmatch(r'(?:[a-fA-F0-9]{13}|[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12})', values[name]):
            raise ValueError('OPNsense certificate reference ID required')
    for name in ('issuer', 'secret_store', 'metallb_pool', 'credential_namespace', 'credential_secret'):
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]', values[name]):
            raise ValueError('Invalid Kubernetes object name')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', values['cloudflare_zone']):
        raise ValueError('Cloudflare zone ID required')
    if values['credential_namespace'] != 'keycloak' or values['credential_secret'] != 'keycloak-automation':
        raise ValueError('Use the fixed managed-cluster automation Secret')
    ssl.create_default_context(cadata=values['database_ca'])
    return values


def replacements(v):
    mapping = {
        'PUBLIC_FQDN': 'public_host', 'PUBLIC_IPV6': 'public_vip',
        'CLUSTER_ISSUER': 'issuer', 'CLUSTER_SECRET_STORE': 'secret_store',
        'CLUSTER_A_METALLB_IPV6': 'backend_a', 'CLUSTER_B_METALLB_IPV6': 'backend_b',
        'OBSERVED_PROXY_IPV6_CIDRS': 'proxy_cidr', 'OBSERVED_PROXY_IPV6_CIDR': 'proxy_cidr',
        'AUTOMATION_IPV6_CIDR': 'automation_cidr', 'ADMIN_IPV6_CIDRS': 'admin_cidrs',
        'DATABASE_VAULT_PATH': 'database_path', 'BOOTSTRAP_VAULT_PATH': 'bootstrap_path',
        'AUTOMATION_VAULT_PATH': 'automation_path', 'OPNSENSE_VAULT_PATH': 'opnsense_path',
        'CLOUDFLARE_VAULT_PATH': 'cloudflare_path', 'DATABASE_CA_PEM': 'database_ca', 'OPNSENSE_BACKEND_CA_UUID': 'backend_ca_uuid',
        'OPNSENSE_PUBLIC_CERT_UUID': 'public_cert_uuid',
        'EXACT_CONFIGTEST_SUCCESS_RESULT': 'configtest_success',
        'EXACT_RECONFIGURE_SUCCESS_STATUS': 'reconfigure_success', 'CLOUDFLARE_ZONE_ID': 'cloudflare_zone',
    }
    result = {'REPLACE_ME_' + key: v[field] for key, field in mapping.items()}
    result.update({'auth.example.com': v['public_host'], '2001:db8:10:7::70': v['public_vip'],
                   'vault-example': v['issuer'], 'example-vault': v['secret_store'],
                   'machine-network-last-50': v['metallb_pool'],
                   'https://REPLACE_ME_OPNSENSE_API_HOST': v['opnsense_url']})
    return result


def substitute(obj, mapping):
    if isinstance(obj, dict):
        return {k: substitute(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, mapping) for v in obj]
    if isinstance(obj, str):
        # One pass: replacement text can never introduce a second substitution.
        pattern = '|'.join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
        return re.sub(pattern, lambda m: mapping[m.group()], obj)
    return obj


def configure_manifests(folder, mapping, targets):
    patches = {}
    for name, path in targets.items():
        file = folder / (name + '.yaml')
        original = list(yaml.safe_load_all(file.read_text()))
        desired = substitute(original, mapping)
        patches[path] = []
        for before, after in zip(original, desired):
            operations = [{'op': 'replace', 'path': '/' + key, 'value': after[key]}
                          for key in before if before[key] != after[key]]
            if operations:
                patches[path].append({'target': {'kind': before['kind'], 'name': before['metadata']['name']},
                                      'patch': json.dumps(operations)})
        file.write_text(yaml.safe_dump_all(desired, sort_keys=False))
    file = folder / 'applications.yaml'
    apps = list(yaml.safe_load_all(file.read_text()))
    for app in apps:
        source = app['spec']['source']
        if patches.get(source['path']):
            source['kustomize'] = {'patches': patches[source['path']]}
    file.write_text(yaml.safe_dump_all(apps, sort_keys=False))
