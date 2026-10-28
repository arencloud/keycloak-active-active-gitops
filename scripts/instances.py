"""Names, ownership and shared-resource coordination for independent deployments."""
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import re
import socket

from deploy import require

STATE_LABEL = 'keycloak.example.com/state'
OWNER_LABEL = 'keycloak.example.com/deployment'


def namespace(value):
    require(isinstance(value, str) and len(value) <= 63 and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', value), 'Namespace must be a lowercase DNS label of at most 63 characters')
    require(value not in ('default', 'openshift', 'kube') and not value.startswith(('openshift-', 'kube-')), 'System namespaces cannot host a self-service deployment')
    return value


def database_path(value):
    require(isinstance(value, str) and 1 <= len(value) <= 512 and
            all(re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', p) for p in value.split('/')),
            'Supply a Vault KV secret path relative to mount kv, without a URL or data/ prefix')
    require(not value.startswith(('kv/', 'data/', 'metadata/')), 'Vault secret path must be relative to mount kv')
    return value


def prefix(ns):
    namespace(ns)
    return 'kc-' + ns[:18].rstrip('-') + '-' + hashlib.sha256(ns.encode()).hexdigest()[:10]


def configure(profile, platform, ns, db_path=None):
    ns = namespace(ns);name = prefix(ns)
    platform.update(state_configmap=name + '-state', environment=profile['environment'])
    profile.update(namespace=ns, resource_prefix=name, placement_name='keycloak-aap-sites', lock_name=name + '-lock')
    if db_path is not None: profile['database_path'] = database_path(db_path)


def edge_names(name):
    return {'keycloak-site-health': name + '-health', 'keycloak-site-a': name + '-site-a',
            'keycloak-site-b': name + '-site-b', 'keycloak-active-active': name + '-backend',
            'keycloak-public': name + '-public'}


def namespace_documents(documents, ns, owner):
    for obj in documents:
        meta = obj['metadata']
        if obj['kind'] == 'Namespace':
            meta['name'] = ns
            meta.setdefault('labels', {})[OWNER_LABEL] = owner
        else:
            meta['namespace'] = ns
        if obj['kind'] == 'OperatorGroup': obj['spec']['targetNamespaces'] = [ns]


def records(hub, hubns):
    return hub.get('configmaps', namespace=hubns, selector=STATE_LABEL + '=true')['items']


def check_claims(items, profile, state_name):
    for obj in items:
        other = json.loads(obj['data']['state.json'])['profile']
        if obj['metadata']['name'] == state_name:
            from discovery import deployment_identity
            deployment_identity(other, profile)
            continue
        require(other['public_host'] != profile['public_host'], 'FQDN is reserved by another deployment')
        require(ipaddress.IPv6Address(other['public_vip']) != ipaddress.IPv6Address(profile['public_vip']), 'Public VIP is reserved by another deployment')
        require(other['database_path'] != profile['database_path'], 'Vault database secret is already used by another deployment')
        require(not (set(other.get('database_signatures', [])) & set(profile.get('database_signatures', []))), 'Database endpoint/name is already used by another deployment')


def database_signatures(host, database):
    addresses = socket.getaddrinfo(host, 5432, socket.AF_INET6, socket.SOCK_STREAM)
    require(addresses, 'Database has no IPv6 endpoint')
    identities = {host.lower().rstrip('.')} | {str(ipaddress.IPv6Address(a[4][0])) for a in addresses}
    return sorted(hashlib.sha256(json.dumps([h, 5432, database]).encode()).hexdigest() for h in identities)


@contextmanager
def shared_lock(hub, hubns, run_id):
    # Namespace locks protect an instance; this shared lock serializes claims and
    # shared firewall reloads across *different* workflow instances.
    name = 'keycloak-shared-resources-lock'
    hub.command(['create', '-f', '-'], {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': name, 'namespace': hubns}, 'data': {'owner': run_id}})
    uid = hub.get('configmap', name, hubns)['metadata']['uid']
    try:
        yield
    finally:
        hub.command(['delete', '--raw', '/api/v1/namespaces/' + hubns + '/configmaps/' + name, '-f', '-'], {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})


def merge_registration(hub, hubns, documents, placement_name):
    """One additive ACM registration bridge, shared by all selected deployments."""
    bridge = next(x for x in documents if x['kind'] == 'GitOpsCluster')
    existing_bridge = hub.get('gitopscluster', placement_name, hubns, missing=True)
    if existing_bridge:
        require(existing_bridge['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'keycloak-aap', 'Shared GitOpsCluster is not owned by this workflow')
        require(all(existing_bridge['spec'].get(key) == bridge['spec'].get(key) for key in ('argoServer', 'placementRef')), 'Shared GitOpsCluster destination changed')
    bridge['metadata'].setdefault('labels', {})['app.kubernetes.io/managed-by'] = 'keycloak-aap'
    placement = next(x for x in documents if x['kind'] == 'Placement')
    current = hub.get('placement', placement_name, hubns, missing=True)
    expression = placement['spec']['predicates'][0]['requiredClusterSelector']['labelSelector']['matchExpressions'][0]
    if current:
        require(current['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'keycloak-aap', 'Shared ACM Placement is not owned by this workflow')
        previous = current['spec']['predicates'][0]['requiredClusterSelector']['labelSelector']['matchExpressions'][0]
        require(previous['key'] == 'name' and previous['operator'] == 'In', 'Shared Placement selector changed')
        expression['values'] = sorted(set(expression['values']) | set(previous['values']))
        placement['spec']['clusterSets'] = sorted(set(placement['spec']['clusterSets']) | set(current['spec']['clusterSets']))
    placement['metadata'].setdefault('labels', {})['app.kubernetes.io/managed-by'] = 'keycloak-aap'
    placement['spec']['numberOfClusters'] = len(expression['values'])


def operator_scope(groups, namespace):
    for group in groups:
        if group['metadata'].get('namespace') == namespace: continue
        provided = group['metadata'].get('annotations', {}).get('olm.providedAPIs', '')
        if 'Keycloak.' not in provided: continue
        targets = group.get('status', {}).get('namespaces', group.get('spec', {}).get('targetNamespaces', []))
        require(targets and '' not in targets and namespace not in targets, 'Another Keycloak OperatorGroup watches this namespace; review operator ownership first')
