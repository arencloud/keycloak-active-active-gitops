#!/usr/bin/env python3
"""Self-service ACM deployment. Discovery/Plan are read-only; Execute is Git-gated."""
import argparse
import base64
import copy
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import socket
import struct
import subprocess
import tempfile
import uuid
import yaml

import deploy
import discovery
import instances
import render
import survey
from deploy import Failure, require, poll, ready, normalize_field

ROOT = Path(__file__).resolve().parents[1]


def discover(hub, names, directory, platform, namespace='keycloak'):
    require(len(names) == 2 and len(set(names)) == 2, 'Select two distinct managed clusters')
    all_clusters = hub.get('managedclusters')['items']
    eligible = discovery.eligible_clusters(all_clusters)
    require(set(names) <= set(eligible), 'Selected cluster is absent, local, unavailable or not joined')
    sites, clients, bundles, sets = [], {}, [], set()
    for index, name in enumerate(names):
        mc = next(x for x in all_clusters if x['metadata']['name'] == name)
        sets.add(mc['metadata'].get('labels', {}).get('cluster.open-cluster-management.io/clusterset'))
        client = discovery.api_endpoint(mc)
        kube = deploy.managed_access(hub, name, directory)
        clients[name] = kube
        instances.operator_scope(kube.get('operatorgroups.operators.coreos.com', all_namespaces=True)['items'], namespace)
        existing_ns = kube.get('namespace', namespace, missing=True)
        require(not existing_ns or existing_ns['metadata'].get('labels', {}).get(instances.OWNER_LABEL) == instances.prefix(namespace), 'Namespace already exists without this deployment ownership label')
        pool = discovery.choose_pool(kube.get('ipaddresspools.metallb.io', all_namespaces=True)['items'], platform['metallb_pool_preference'])
        ads = kube.get('l2advertisements.metallb.io', namespace=pool['metadata']['namespace'])['items']
        require(any(discovery.advertises_pool(a, pool) for a in ads), 'Pool has no L2 advertisement')
        issuer = discovery.ready_vault_resource(kube.get('clusterissuers')['items'], platform['issuer_preference'], 'issuer')
        store = discovery.ready_vault_resource(kube.get('clustersecretstores')['items'], platform['secret_store_preference'], 'store')
        require(store['spec']['provider']['vault'].get('path') == 'kv', 'Platform secret paths require the kv mount')
        bundle = base64.b64decode(issuer['spec']['vault']['caBundle'], validate=True).decode()
        ssl.create_default_context(cadata=bundle)
        bundles.append(bundle)
        svc = kube.get('service', 'keycloak-external', namespace, missing=True)
        address = service_ip(svc, pool) if svc else None
        sub = kube.get('subscriptions.operators.coreos.com', 'keycloak-operator', namespace, missing=True)
        kc = kube.get('keycloaks.k8s.keycloak.org', 'keycloak', namespace, missing=True) if sub and sub.get('status', {}).get('installedCSV') else None
        sites.append({'name': name, 'server': client['url'], 'overlay': 'cluster-' + 'ab'[index],
                      'issuer': issuer['metadata']['name'], 'secret_store': store['metadata']['name'],
                      'pool': pool, 'backend_ipv6': address, 'existing_runtime': kc is not None, 'existing_hostname': (kc or {}).get('spec', {}).get('hostname', {}).get('hostname')})
    require(len(sets) == 1 and None not in sets, 'Selected clusters must belong to one ACM ClusterSet')
    existing = [s['existing_runtime'] for s in sites]
    return {'sites': sites, 'clients': clients, 'cluster_set': sets.pop(),
            'ca': '\n'.join(dict.fromkeys(bundles)), 'hub_sources': discovery.hub_sources(hub),
            'lifecycle': 'reconcile' if all(existing) else 'initial-install', 'eligible_clusters': eligible}


def deployment_lifecycle(sites, state=None):
    """Resume an owned, incomplete initial install without resetting credentials.

    Call only after validating the saved deployment identity and ownership.
    Legacy records captured the initial presence of each runtime before applying it.
    Treat ambiguous legacy records as existing deployments, never as bootstrap runs.
    """
    existing = [site['existing_runtime'] for site in sites]
    if state:
        completed = state.get('installation_complete')
        if completed is None:
            prior = state['profile'].get('sites', [])
            completed = not (len(prior) == 2 and all(s.get('existing_runtime') is False for s in prior))
        if not completed:
            return 'initial-install'
        require(all(existing), 'A previously installed runtime is missing; recover it before reconciling')
    require(all(existing) or not any(existing), 'Partial deployment has no matching incomplete installation record; manual recovery required')
    return 'reconcile' if all(existing) else 'initial-install'


def service_ip(service, pool):
    addresses = [x['ip'] for x in (service or {}).get('status', {}).get('loadBalancer', {}).get('ingress', []) if x.get('ip')]
    require(len(addresses) <= 1, 'Unexpected multiple backend addresses')
    if not addresses: return None
    require(ipaddress.ip_address(addresses[0]).version == 6 and discovery.in_pool(addresses[0], pool), 'Assigned backend IP is outside selected IPv6 pool')
    return str(ipaddress.IPv6Address(addresses[0]))


def patches(original, desired):
    result = []
    for before in original:
        after = next((x for x in desired if x['kind'] == before['kind'] and (x['metadata']['name'] == before['metadata']['name'] or before['kind'] == 'Namespace')), None)
        target = {'kind': before['kind'], 'name': before['metadata']['name']}
        if after is None:
            result.append({'target': target, 'patch': yaml.safe_dump({'$patch': 'delete', 'apiVersion': before['apiVersion'], 'kind': before['kind'], 'metadata': {'name': before['metadata']['name']}})})
        else:
            changes = [{'op': 'replace', 'path': '/' + key, 'value': value} for key, value in after.items() if before.get(key) != value]
            changes += [{'op': 'remove', 'path': '/' + key} for key in before if key not in after]
            if changes: result.append({'target': target, 'patch': json.dumps(changes)})
    return result


def artifacts(profile, found, platform, revision, directory):
    require(len(profile['public_host']) <= 64,
            'The issuer requires a certificate Common Name of at most 64 characters; use a shorter public FQDN')
    tls = deploy.database_tls(profile)
    original = {}
    for name, path in render.TARGETS.items():
        proc = subprocess.run(['kubectl', 'kustomize', str(ROOT / path)], capture_output=True, text=True)
        require(proc.returncode == 0, 'Kustomize failed for ' + name)
        original[name] = list(yaml.safe_load_all(proc.stdout.replace('REPLACE_ME_REVIEWED_REVISION', revision)))
    values = {name: default for name, _, default in survey.FIELDS}
    values.update(public_vip=profile['public_vip'], public_host=profile['public_host'], backend_a='::', backend_b='::',
                  proxy_cidr='::1/128', automation_cidr=found['hub_sources'][0],
                  admin_cidrs=' '.join(platform['administrator_networks']), database_ca=found['ca'],
                  bootstrap_path='unused-operator-generated', opnsense_url=platform['opnsense_url'],
                  database_path=profile['database_path'], opnsense_path=platform['opnsense_path'], cloudflare_path=platform['cloudflare_path'],
                  backend_ca_uuid='pending', public_cert_uuid='pending',
                  configtest_success=platform['opnsense_configtest_success'],
                  reconfigure_success=platform['opnsense_reconfigure_success'], cloudflare_zone='pending')
    desired = {name: survey.substitute(docs, survey.replacements(values)) for name, docs in original.items()}
    for site in found['sites']:
        key = site['overlay']
        for doc in desired[key]:
            if doc['kind'] == 'Service':
                doc['metadata']['annotations'].pop('metallb.io/loadBalancerIPs', None)
                doc['metadata']['annotations']['metallb.io/address-pool'] = site['pool']['metadata']['name']
            if doc['kind'] == 'Keycloak':
                doc['spec'].pop('bootstrapAdmin', None)
                if tls == 'disabled':
                    next(x for x in doc['spec']['additionalOptions'] if x['name'] == 'db-tls-mode')['value'] = 'disabled'
                    doc['spec']['additionalOptions'].append({'name': 'db-url-properties', 'value': '?sslmode=disable'})
                    doc['spec']['truststores'].pop('db', None)
                    if not doc['spec']['truststores']: doc['spec'].pop('truststores')
                next(x for x in doc['spec']['additionalOptions'] if x['name'] == 'cache-embedded-cluster-name')['value'] = site['name']
            if doc['kind'] == 'NetworkPolicy':
                doc['spec']['ingress'][0]['from'] = [{'ipBlock': {'cidr': x}} for x in found['hub_sources']]
        prereq = copy.deepcopy(desired['prerequisites'])
        prereq = [x for x in prereq if x['metadata']['name'] != 'keycloak-bootstrap']
        if tls == 'disabled':
            prereq = [x for x in prereq if not (x['kind'] == 'ConfigMap' and x['metadata']['name'] == 'keycloak-db-rootcert')]
        for doc in prereq:
            if doc['kind'] == 'Certificate': doc['spec']['issuerRef']['name'] = site['issuer']
            if doc['kind'] == 'ExternalSecret':
                doc['spec']['secretStoreRef']['name'] = site['secret_store']
                if doc['metadata']['name'] == 'keycloak-automation':
                    doc['spec']['data'] = [x for x in doc['spec']['data'] if x['secretKey'] in ('opnsense_url', 'opnsense_key', 'opnsense_secret', 'cloudflare_token')]
        desired['prerequisites-' + key] = prereq
    desired['prerequisites'] = desired['prerequisites-cluster-a']
    for target in ('operator', 'prerequisites-cluster-a', 'prerequisites-cluster-b', 'cluster-a', 'cluster-b'):
        instances.namespace_documents(desired[target], profile['namespace'], profile['resource_prefix'])
    for doc in desired['hub-registration']:
        if doc['kind'] == 'ManagedClusterSetBinding':
            doc['metadata']['name'] = found['cluster_set'];doc['spec']['clusterSet'] = found['cluster_set']
        if doc['kind'] in ('Placement', 'GitOpsCluster'):
            doc['metadata']['name'] = profile['placement_name']
        if doc['kind'] == 'GitOpsCluster':
            doc['spec']['placementRef']['name'] = profile['placement_name']
        if doc['kind'] == 'Placement':
            doc['spec']['clusterSets'] = [found['cluster_set']]
            doc['spec']['predicates'][0]['requiredClusterSelector']['labelSelector']['matchExpressions'][0]['values'] = [x['name'] for x in found['sites']]
    for doc in desired['hub-project']:
        doc['metadata']['name'] = profile['resource_prefix']
        doc['spec']['description'] = 'Active/active Keycloak on the selected ACM pair'
        doc['spec']['sourceRepos'] = [platform['repository_url']]
        doc['spec']['destinations'] = [{'name': s['name'], 'namespace': profile['namespace']} for s in found['sites']]
    for app in desired['applications']:
        site = next(x for x in found['sites'] if app['metadata']['name'].endswith(x['overlay']))
        app['metadata']['name'] = app['metadata']['name'].replace('keycloak-', profile['resource_prefix'] + '-', 1)
        app['spec']['project'] = profile['resource_prefix']
        app['spec']['source']['repoURL'] = platform['repository_url']
        # ACM can register a cluster-proxy URL instead of the managed API URL.
        # Argo resolves the registered name; direct API access remains in site.server.
        app['spec']['destination'] = {'name': site['name'], 'namespace': profile['namespace']}
        target = next(name for name, path in render.TARGETS.items() if path == app['spec']['source']['path'])
        resource_set = 'prerequisites-' + site['overlay'] if target == 'prerequisites' else target
        app['spec']['source']['kustomize'] = {'namespace': profile['namespace'], 'patches': patches(original[target], desired[resource_set])}
    write_artifacts(directory, desired)
    edge = survey.substitute(json.loads((ROOT / 'automation/opnsense/objects.json').read_text()), survey.replacements(values))
    edge = survey.substitute(edge, instances.edge_names(profile['resource_prefix']))
    profile['opnsense'].update(url=platform['opnsense_url'],
                              configtest_success=platform['opnsense_configtest_success'],
                              reconfigure_success=platform['opnsense_reconfigure_success'])
    profile['credential_ref'] = {'namespace': profile['namespace'], 'name': 'keycloak-automation'}
    return original, desired, edge


def write_artifacts(directory, documents):
    for name, docs in documents.items():
        (directory / (name + '.yaml')).write_text(yaml.safe_dump_all(docs, sort_keys=False))


def destination_matches_site(destination, site, namespace):
    """Accept registered names and legacy direct endpoints, but never relocation."""
    if destination.get('namespace') != namespace:
        return False
    name, server = destination.get('name'), destination.get('server')
    if name:
        return not server and name == site['name']
    return server == site['server']


class AutomaticDeployment(deploy.Deployment):
    def __init__(self, args, profile, folder, edge, found, platform, originals, documents):
        # Reuse discovery clients; do not create a second set of private kubeconfigs.
        self.args, self.p, self.manifests, self.edge = args, profile, folder, edge
        self.found, self.platform, self.originals, self.documents = found, platform, originals, documents
        self.installation_complete = args.lifecycle == 'reconcile'
        self.stage, self.events, self.recovery = 'credentials', [], []
        self.run_id = str(uuid.uuid4())
        self.hub, self.sites = found['hub'], found['clients']
        self.credentials_dir = folder / 'credentials';self.credentials_dir.mkdir(mode=0o700)
        self.ns, self.hubns, self.timeout = profile['namespace'], profile['hub_namespace'], profile['timeout_seconds']
        self.report_dir = Path(deploy.environment('KEYCLOAK_REPORT_DIR')).resolve()
        require(self.report_dir != ROOT and ROOT not in self.report_dir.parents, 'Recovery storage must be outside Git')
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.journal({'system': 'workflow', 'operation': 'start', 'request_id': args.request_id, 'revision': args.revision})

    def run(self):
        with instances.shared_lock(self.hub, self.hubns, self.run_id):
            instances.check_claims(instances.records(self.hub, self.hubns), self.p, self.platform['state_configmap'])
            instances.merge_registration(self.hub, self.hubns, self.documents['hub-registration'], self.p['placement_name'])
            write_artifacts(self.manifests, self.documents)
            self.save_state()
            super().run()
            self.installation_complete = True
            self.save_state()

    def assert_database_isolation(self, persist=False):
        kube = self.sites[self.p['sites'][0]['name']]
        data = kube.get('secret', 'keycloak-db', self.ns)['data']
        host, database = (base64.b64decode(data[k], validate=True).decode() for k in ('host', 'database'))
        signatures = instances.database_signatures(host, database)
        previous = self.p.get('database_signatures', [])
        require(not previous or set(previous) & set(signatures), 'Database identity changed since deployment')
        if not persist: require(previous, 'No recorded database identity; recover partial deployment before removal')
        candidate = {**self.p, 'database_signatures': signatures}
        instances.check_claims(instances.records(self.hub, self.hubns), candidate, self.platform['state_configmap'])
        if persist:
            self.p['database_signatures'] = signatures
            self.save_state()

    def load_external_credentials(self):
        kube = self.sites[self.p['sites'][0]['name']]
        poll(lambda: ready(kube.get('externalsecret', 'keycloak-automation', self.ns)), self.timeout, 'shared API credentials')
        data = kube.get('secret', 'keycloak-automation', self.ns)['data']
        def value(key): return base64.b64decode(data[key], validate=True).decode()
        require(value('opnsense_url') == self.platform['opnsense_url'], 'Unexpected Vault firewall URL')
        self.ca = deploy.private_file(self.credentials_dir, 'trust.pem', self.found['ca'])
        self.edge_api = deploy.HTTPS(value('opnsense_url'), self.ca, {'Authorization': 'Basic ' + base64.b64encode((value('opnsense_key') + ':' + value('opnsense_secret')).encode()).decode()})
        self.dns_api = deploy.HTTPS('https://api.cloudflare.com/client/v4', headers={'Authorization': 'Bearer ' + value('cloudflare_token')})
        if getattr(self.args, 'operation', 'deploy') == 'deploy':
            self.verify_database_connection()
            self.assert_database_isolation(persist=True)
            self.prepare_network()
            self.ensure_admin_credentials()
        _, zones = self.dns_api.request('GET', '/zones?per_page=50&status=active')
        require(zones.get('success') is True and zones.get('result_info', {}).get('total_pages', 1) <= 1, 'Cloudflare zone inventory is incomplete')
        matches = [x for x in zones['result'] if self.p['public_host'].endswith('.' + x['name']) or self.p['public_host'] == x['name']]
        require(matches, 'No accessible Cloudflare zone for public hostname')
        zone = max(matches, key=lambda x: len(x['name']))
        if getattr(self.args, 'operation', 'deploy') == 'remove':
            require(self.p['cloudflare']['zone_id'] == zone['id'], 'Recorded Cloudflare zone changed')
        self.p['cloudflare']['zone_id'] = zone['id']

    def verify_database_connection(self):
        tls = deploy.database_tls(self.p)
        kube = self.sites[self.p['sites'][0]['name']]
        data = kube.get('secret', 'keycloak-db', self.ns)['data']
        host = base64.b64decode(data['host'], validate=True).decode()
        addresses = socket.getaddrinfo(host, 5432, socket.AF_INET6, socket.SOCK_STREAM)
        require(addresses, 'Database has no IPv6 endpoint')
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as raw:
            raw.settimeout(15)
            raw.connect(addresses[0][4])
            if tls == 'enabled':
                context = ssl.create_default_context(cafile=self.ca)
                raw.sendall(struct.pack('!II', 8, 80877103))
                require(raw.recv(1) == b'S', 'Database TLS is enabled in the survey, but PostgreSQL is not offering TLS')
                try:
                    with context.wrap_socket(raw, server_hostname=host):
                        pass
                except ssl.SSLCertVerificationError:
                    raise Failure('PostgreSQL TLS certificate or hostname verification failed') from None
                except ssl.SSLError:
                    raise Failure('PostgreSQL TLS handshake failed') from None
        self.journal({'system': 'database', 'operation': 'verified-ipv6-tls' if tls == 'enabled' else 'verified-ipv6-tcp', 'database_tls': tls})

    def ensure_admin_credentials(self):
        name = self.platform['recovery_secret']
        existing = [k.get('secret', name, self.ns, missing=True) for k in self.sites.values()]
        present = [x for x in existing if x]
        for obj in present:
            require(obj['metadata'].get('annotations', {}).get('keycloak.example.com/hostname') == self.p['public_host'] and obj['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'keycloak-aap', 'Workflow admin Secret ownership mismatch')
        if present:
            data = present[0]['data']
            require(all(data.get(k) for k in ('username', 'password', 'smoke_client_secret')), 'Incomplete workflow credentials')
            require(all(x['data'] == data for x in present), 'Workflow credentials disagree between sites')
        else:
            require(self.args.lifecycle == 'initial-install', 'Permanent workflow credentials are missing; do not reset an existing administrator')
            data = {k: base64.b64encode(v.encode()).decode() for k, v in {'username': 'keycloak-platform-admin', 'password': secrets.token_urlsafe(48), 'smoke_client_secret': secrets.token_urlsafe(48)}.items()}
        for kube, obj in zip(self.sites.values(), existing):
            if not obj:
                kube.command(['create', '-f', '-'], {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': self.ns, 'annotations': {'keycloak.example.com/hostname': self.p['public_host']}, 'labels': {'app.kubernetes.io/managed-by': 'keycloak-aap'}}, 'type': 'Opaque', 'data': data})
        self.admin_user, self.admin_password, self.smoke_secret = (base64.b64decode(data[k]).decode() for k in ('username', 'password', 'smoke_client_secret'))

    def prepare_network(self):
        _, interfaces = self.edge_api.request('GET', '/api/diagnostics/interface/getInterfaceConfig')
        _, routes = self.edge_api.request('GET', '/api/diagnostics/interface/getRoutes')
        self.public_device = discovery.public_interface(interfaces, self.p['public_vip'])
        from edge_automatic import check_listener_collision
        check_listener_collision(self.edge_api, self.p['public_vip'], self.p['resource_prefix'] + '-public')
        for site in self.p['sites']:
            device, source = discovery.router_source(interfaces, site['pool'])
            discovery.connected_route(routes, device, site['pool'])
            site['proxy_source'] = source
            for doc in self.documents[site['overlay']]:
                if doc['kind'] == 'NetworkPolicy':
                    doc['spec']['ingress'][0]['from'] = [{'ipBlock': {'cidr': source + '/128'}}] + [{'ipBlock': {'cidr': x}} for x in self.found['hub_sources']]
                if doc['kind'] == 'Keycloak':
                    next(x for x in doc['spec']['additionalOptions'] if x['name'] == 'proxy-trusted-addresses')['value'] = source
            obj = next(x for x in self.edge['objects'] if x['name'] == self.p['resource_prefix'] + '-site-' + site['overlay'][-1])
            # os-haproxy rejects brackets in this field. HAProxy accepts a
            # trailing colon as an explicit empty port after the IPv6 address.
            obj['fields']['source'] = str(ipaddress.IPv6Address(source)) + ':'
            app = next(x for x in self.documents['applications'] if x['metadata']['name'] == self.application_name('keycloak', site['overlay']))
            app['spec']['source']['kustomize'] = {'namespace': self.ns, 'patches': patches(self.originals[site['overlay']], self.documents[site['overlay']])}
            self.hub.patch('applications.argoproj.io', app['metadata']['name'], self.hubns, {'spec': {'source': app['spec']['source']}})
        write_artifacts(self.manifests, self.documents)
        self.save_state()

    def sync(self, app):
        super().sync(app)
        if app.startswith(self.p['resource_prefix'] + '-keycloak-'):
            site = next(x for x in self.p['sites'] if app.endswith(x['overlay']))
            kube = self.sites[site['name']]
            site['backend_ipv6'] = poll(lambda: service_ip(kube.get('service', 'keycloak-external', self.ns), site['pool']), self.timeout, 'MetalLB IPv6 assignment')
            next(x for x in self.edge['objects'] if x['name'] == self.p['resource_prefix'] + '-site-' + site['overlay'][-1])['fields']['address'] = site['backend_ipv6']
            self.journal({'system': 'metallb', 'site': site['name'], 'assigned_ipv6': site['backend_ipv6']})

    def prepare_edge(self):
        from edge_automatic import ensure_vip, ensure_certificate, discover_ca, ensure_public_rule
        ensure_vip(self.edge_api, self.p['public_vip'], self.public_device, self.journal)
        kube = self.sites[self.p['sites'][0]['name']]
        tls = kube.get('secret', 'keycloak-tls', self.ns)['data']
        cert = ensure_certificate(self.edge_api, self.p['public_host'], tls, self.journal, self.credentials_dir, self.ca, self.p['resource_prefix'] + '-public', self.p['resource_prefix'])
        ca = discover_ca(self.edge_api, self.ca, self.credentials_dir)
        for obj in self.edge['objects']:
            if obj['type'] == 'server': obj['fields']['sslCA'] = ca
            if obj['type'] == 'frontend':
                obj['fields']['ssl_certificates'] = obj['fields']['ssl_default_certificate'] = cert
        self.p['firewall_rule'] = ensure_public_rule(self.edge_api, self.p['public_host'], self.p['public_vip'], self.public_device, self.journal, self.p['resource_prefix'])
        self.save_state()

    def owned_automation_secrets(self):
        result = []
        for kube in self.sites.values():
            obj = kube.get('secret', self.platform['recovery_secret'], self.ns, missing=True)
            if obj:
                require(obj['metadata'].get('annotations', {}).get('keycloak.example.com/hostname') == self.p['public_host'] and obj['metadata'].get('labels', {}).get('app.kubernetes.io/managed-by') == 'keycloak-aap', 'Workflow Secret ownership changed')
                result.append((kube, {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': obj['metadata']}))
        return result

    def save_state(self):
        # Recovery record has public manifests and object IDs only, never secret values.
        state = {'profile': self.p, 'edge': self.edge, 'documents': self.documents, 'revision': self.args.revision,
                 'platform': self.platform, 'ca': self.found['ca'], 'hub_sources': self.found['hub_sources'],
                 'installation_complete': self.installation_complete}
        self.hub.command(['apply', '-f', '-'], {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': self.platform['state_configmap'], 'namespace': self.hubns, 'labels': {instances.STATE_LABEL: 'true', instances.OWNER_LABEL: self.p['resource_prefix']}}, 'data': {'state.json': json.dumps(state)}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['discover', 'plan', 'deploy'], default='plan')
    parser.add_argument('--cluster-a', required=True);parser.add_argument('--cluster-b', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--database-path', required=True)
    parser.add_argument('--database-tls', choices=['enabled', 'disabled'], default='enabled')
    parser.add_argument('--public-host', required=True)
    parser.add_argument('--public-vip', required=True);parser.add_argument('--request-id', default='')
    args = parser.parse_args()
    run = None
    try:
        args.namespace = instances.namespace(args.namespace)
        args.database_path = instances.database_path(args.database_path)
        args.public_host = discovery.public_hostname(args.public_host)
        require(ipaddress.IPv6Address(args.public_vip).is_global, 'Public VIP must be globally routable IPv6')
        args.revision = deploy.execution_revision()
        expected_revision = os.environ.get('KEYCLOAK_EXPECTED_REVISION', '')
        require(not expected_revision or expected_revision == args.revision, 'Project revision changed between workflow stages; restart discovery')
        if args.mode == 'deploy' and os.environ.get('PROJECT_REVISION'):
            require(expected_revision, 'Deployment requires the discovery revision from its workflow')
        args.request_id = args.request_id or 'AAP-' + str(uuid.uuid4())
        platform = json.loads((ROOT / 'automation/aap/profiles/platform.json').read_text())
        profile = json.loads((ROOT / 'automation/aap/profiles/active-active.json').read_text())
        profile['database_tls'] = args.database_tls
        instances.configure(profile, platform, args.namespace, args.database_path)
        with tempfile.TemporaryDirectory(prefix='keycloak-discovery-') as tmp:
            folder = Path(tmp)
            hub = deploy.Kube(deploy.environment('KUBECONFIG'), platform['hub_api'])
            require(not hub.get('configmap', 'keycloak-active-active-state', 'openshift-gitops', missing=True), 'Legacy deployment record exists; migrate its ownership before using namespace-scoped workflows')
            found = discover(hub, [args.cluster_a, args.cluster_b], folder, platform, args.namespace)
            found['hub'] = hub
            args.lifecycle = found['lifecycle']
            profile.update(automatic=True, public_vip=str(ipaddress.IPv6Address(args.public_vip)), public_host=args.public_host, sites=found['sites'], cluster_set=found['cluster_set'])
            previous = hub.get('configmap', platform['state_configmap'], 'openshift-gitops', missing=True)
            state = None
            if previous:
                require(previous['metadata'].get('labels', {}).get(instances.OWNER_LABEL) == profile['resource_prefix'], 'Deployment record ownership mismatch')
                state = json.loads(previous['data']['state.json'])
                discovery.deployment_identity(state['profile'], profile)
                profile['database_signatures'] = state['profile'].get('database_signatures', [])
            args.lifecycle = deployment_lifecycle(found['sites'], state)
            # Existing identity is immutable through the install/reconcile workflow.
            for site in profile['sites']:
                require(not site['existing_runtime'] or site['existing_hostname'] == 'https://' + profile['public_host'], 'Existing Keycloak uses another FQDN; hostname migration requires a separate operation')
                current = hub.get('applications.argoproj.io', profile['resource_prefix'] + '-keycloak-' + site['overlay'], 'openshift-gitops', missing=True)
                require(not current or destination_matches_site(current['spec']['destination'], site, profile['namespace']), 'Existing deployment targets another pair or namespace; remove it before relocation')
            instances.check_claims(instances.records(hub, 'openshift-gitops'), profile, platform['state_configmap'])
            originals, documents, edge = artifacts(profile, found, platform, args.revision, folder)
            report = {'mode': args.mode, 'revision': args.revision, 'request_id': args.request_id, 'lifecycle': args.lifecycle,
                      'public_host': profile['public_host'], 'public_vip': profile['public_vip'], 'namespace': profile['namespace'], 'database_path': profile['database_path'],
                      'database_tls': profile['database_tls'],
                      'sites': [{'name': s['name'], 'server': s['server'], 'pool': s['pool']['metadata']['name'], 'backend_ipv6': s['backend_ipv6'] or 'allocated by MetalLB during deployment', 'issuer': s['issuer'], 'secret_store': s['secret_store']} for s in profile['sites']],
                      'hub_sources': found['hub_sources'], 'applied': False,
                      'argo_registration': deploy.acm_registration_status(hub, profile),
                      'execution_enabled': profile.get('execution_enabled') is True,
                      'deferred_checks': ['OPNsense interfaces/VIP/certificate and Cloudflare zone after ESO synchronization', 'PostgreSQL connection using selected TLS mode after ESO synchronization, before Keycloak startup', 'Backend and public token issuance']}
            if args.mode != 'deploy': print(json.dumps(report, indent=2));return 0
            require(profile.get('execution_enabled') is True, 'Deployment is disabled in the reviewed Git profile; resolve the platform readiness report first')
            require(edge.get('reviewed') is True, 'OPNsense adapter requires commissioning review')
            run = AutomaticDeployment(args, profile, folder, edge, found, platform, originals, documents)
            run.run()
            print(json.dumps({'result': 'succeeded', 'revision': args.revision, 'stages': run.events}))
        return 0
    except (Failure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({'result': 'failed', 'error': str(exc) if isinstance(exc, Failure) else type(exc).__name__, 'stage': run.stage if run else 'discovery'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
