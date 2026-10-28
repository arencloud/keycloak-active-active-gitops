#!/usr/bin/env python3
"""AAP deployment runner. Plan is offline; deploy is explicitly gated by Git profile."""
import argparse
import base64
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import uuid
import sys
sys.modules.setdefault("deploy", sys.modules[__name__])

import render
import survey

ROOT = Path(__file__).resolve().parents[1]


class Failure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def environment(name):
    value = os.environ.get(name, '')
    require(value, 'Missing injected credential or file: ' + name)
    return value


def database_tls(profile):
    # Older deployment records always required verified TLS.
    mode = profile.get('database_tls', 'enabled')
    require(mode in ('enabled', 'disabled'), 'Database TLS must be enabled or disabled')
    return mode


def checked_revision(revision):
    require(re.fullmatch(r'[0-9a-f]{40}', revision), 'A full reviewed commit is required')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    require(head == revision, 'AAP project checkout does not match release_revision')
    changes = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=ROOT, text=True)
    require(not changes.strip(), 'Release execution requires a clean committed checkout')


def execution_revision():
    """AAP strips .git when copying projects; its injected revision is authoritative.

    Local executions still require a clean Git checkout. No survey revision is used.
    """
    if (ROOT / '.git').exists():
        revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        checked_revision(revision)
        return revision
    revision = environment('PROJECT_REVISION')
    require(re.fullmatch(r'[0-9a-f]{40}', revision), 'AAP did not supply a full project revision')
    return revision


class Kube:
    def __init__(self, config, expected_server):
        self.config = config
        server = self.command(['config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}'])
        require(server == expected_server, 'Injected kubeconfig targets an unexpected API')

    def command(self, args, obj=None, missing=False):
        proc = subprocess.run(['oc', '--kubeconfig', self.config, '--request-timeout=30s'] + args,
                              input=json.dumps(obj) if obj is not None else None,
                              capture_output=True, text=True)
        if proc.returncode:
            if missing and '(NotFound)' in proc.stderr:
                return ''
            # Never echo API bodies, kubeconfig contents, or Secret values.
            reason = next((r for r in ('Forbidden', 'Unauthorized', 'NotFound', 'Conflict', 'AlreadyExists', 'Timeout', 'ServiceUnavailable') if '(' + r + ')' in proc.stderr), None)
            context = ' (' + reason + ')' if reason else ''
            raise Failure('Kubernetes command failed' + context + ': ' + ' '.join(args[:3]))
        return proc.stdout.strip()

    def get(self, kind, name=None, namespace=None, missing=False, selector=None, all_namespaces=False):
        args = ['get', kind]
        if name:
            args.append(name)
        if namespace:
            args += ['-n', namespace]
        if all_namespaces:
            args += ['--all-namespaces']
        if selector:
            args += ['-l', selector]
        result = self.command(args + ['-o', 'json'], missing=missing)
        return json.loads(result) if result else None

    def apply(self, path):
        self.command(['apply', '-f', str(path)])

    def patch(self, kind, name, namespace, patch, patch_type='merge'):
        # Patches contain control-plane state only, never credentials.
        self.command(['patch', kind, name, '-n', namespace, '--type', patch_type,
                      '-p', json.dumps(patch)])


def ready(obj, condition='Ready'):
    return any(c.get('type') == condition and c.get('status') == 'True'
               for c in (obj or {}).get('status', {}).get('conditions', []))


def poll(check, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(5)
    raise Failure('Timed out waiting for ' + label)


def wait_keycloak(kube, name, namespace, timeout):
    def current_ready():
        obj = kube.get('keycloak', name, namespace)
        generation = obj['metadata']['generation']
        return any(c.get('type') == 'Ready' and c.get('status') == 'True'
                   and c.get('observedGeneration', obj.get('status', {}).get('observedGeneration')) == generation
                   for c in obj.get('status', {}).get('conditions', []))
    try:
        return poll(current_ready, timeout, 'Keycloak Ready')
    except Failure as exc:
        if str(exc) != 'Timed out waiting for Keycloak Ready':
            raise
        # Operator condition messages embed container logs. Report structured
        # container states instead, keeping application logs and credentials out.
        pods = kube.get('pods', namespace=namespace,
                        selector='app=keycloak,app.kubernetes.io/instance=' + name)['items']
        summary = []
        for pod in pods:
            for container in pod.get('status', {}).get('containerStatuses', []):
                state = container.get('state', {})
                reason = state.get('waiting', {}).get('reason') or state.get('terminated', {}).get('reason')
                summary.append({'pod': pod['metadata']['name'], 'container': container['name'],
                                'ready': container.get('ready', False), 'restarts': container.get('restartCount', 0),
                                'reason': reason or pod.get('status', {}).get('phase', 'Unknown')})
        raise Failure('Timed out waiting for Keycloak Ready in ' + namespace + '; containers=' + json.dumps(summary)) from None


class HTTPS:
    def __init__(self, base, ca_file=None, headers=None, connect_ip=None):
        url = urllib.parse.urlsplit(base)
        require(url.scheme == 'https' and url.hostname and not url.username and not url.password,
                'Only credential-free HTTPS API URLs are allowed')
        self.host = url.hostname
        self.port = url.port or 443
        self.prefix = url.path.rstrip('/')
        self.context = ssl.create_default_context(cafile=ca_file)
        self.headers = headers or {}
        self.connect_ip = connect_ip

    def request(self, method, path, body=None, expected=(200,), headers=None, form=False, parse_json=True):
        conn = http.client.HTTPSConnection(self.host, self.port, context=self.context, timeout=30)
        if self.connect_ip:
            # Connect to the selected IPv6 site/VIP while verifying the public DNS SAN.
            raw = socket.create_connection((self.connect_ip, self.port), timeout=30)
            conn.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        payload = None
        if body is not None:
            payload = urllib.parse.urlencode(body) if form else json.dumps(body)
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        try:
            conn.request(method, self.prefix + path, payload, request_headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException):
            raise Failure('HTTPS transport or certificate verification failed for ' + self.host) from None
        finally:
            conn.close()
        require(status in expected, 'HTTPS request failed for ' + self.host + ' (status ' + str(status) + ')')
        if not parse_json:
            return status, {}
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            raise Failure('Expected a JSON API response from ' + self.host) from None


def approve_plan(kube, namespace, expected, timeout):
    sub = poll(lambda: kube.get('subscriptions.operators.coreos.com', 'keycloak-operator', namespace), timeout, 'Subscription')
    installed = sub.get('status', {}).get('installedCSV')
    require(installed in (None, '', expected), 'Operator version differs; use a separate coordinated upgrade workflow')
    csv = kube.get('csv', expected, namespace, missing=True)
    if csv and csv.get('status', {}).get('phase') == 'Succeeded':
        return
    def pending():
        current = kube.get('subscriptions.operators.coreos.com', 'keycloak-operator', namespace)
        ref = current.get('status', {}).get('installPlanRef')
        if not ref:
            return None
        plan = kube.get('installplans.operators.coreos.com', ref['name'], namespace)
        require(not ref.get('uid') or ref['uid'] == plan['metadata']['uid'], 'InstallPlan UID mismatch')
        require(plan['spec'].get('clusterServiceVersionNames') == [expected], 'InstallPlan contains an unreviewed CSV or dependency')
        return plan
    plan = poll(pending, timeout, 'reviewed InstallPlan')
    if not plan['spec'].get('approved'):
        kube.patch('installplan', plan['metadata']['name'], namespace, [
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': plan['metadata']['resourceVersion']},
            {'op': 'test', 'path': '/spec/approved', 'value': False},
            {'op': 'replace', 'path': '/spec/approved', 'value': True},
        ], 'json')
    poll(lambda: (kube.get('csv', expected, namespace, missing=True) or {}).get('status', {}).get('phase') == 'Succeeded', timeout, 'operator CSV Succeeded')


def wait_certificate(kube, name, namespace, timeout):
    """Wait for the current spec; terminal issuance errors need intervention."""
    def issued():
        certificate = kube.get('certificate', name, namespace)
        generation = certificate.get('metadata', {}).get('generation')
        conditions = [c for c in certificate.get('status', {}).get('conditions', [])
                      if c.get('observedGeneration', generation) == generation]
        for condition in conditions:
            if (condition.get('type') == 'Issuing' and condition.get('status') == 'False'
                    and condition.get('reason') in ('Failed', 'Denied', 'InvalidRequest')):
                # Issuer messages can contain arbitrary remote API responses.
                raise Failure('Certificate ' + namespace + '/' + name + ' issuance failed ('
                              + condition['reason'] + '); inspect its CertificateRequest conditions')
        return any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in conditions)
    poll(issued, timeout, 'certificate ' + namespace + '/' + name)


def acm_registration_status(hub, profile):
    """Inspect Placement and Argo identities without exposing Secret configuration."""
    namespace = profile['hub_namespace']
    decisions = hub.get('placementdecisions.cluster.open-cluster-management.io', namespace=namespace,
                        selector='cluster.open-cluster-management.io/placement=' + profile.get('placement_name', 'keycloak-sites'))['items']
    selected = {site['name'] for site in profile['sites']}
    scheduled = {c['clusterName'] for d in decisions for c in d.get('status', {}).get('decisions', [])}
    secrets = hub.get('secrets', namespace=namespace, selector='argocd.argoproj.io/secret-type=cluster')['items']
    identities = [{key: base64.b64decode(secret.get('data', {}).get(key, ''), validate=True).decode()
                   for key in ('name', 'server')} for secret in secrets]
    missing = []
    for site in profile['sites']:
        if profile.get('automatic') is True:
            matches = [identity for identity in identities if identity['name'] == site['name']]
            require(len(matches) <= 1, 'Multiple Argo cluster registrations for ' + site['name'])
            present = matches and matches[0]['server']
        else:
            server = site.get('server')
            require(server, 'Legacy site profile requires an explicit API server')
            present = any(identity['server'] == server for identity in identities)
        if not present:
            missing.append(site['name'])
    return {'missing_placement_clusters': sorted(selected - scheduled),
            'unexpected_placement_clusters': sorted(scheduled - selected) if not profile.get('automatic') else [],
            'missing_argo_clusters': sorted(missing)}


def wait_acm_registration(hub, profile, timeout):
    status = {}
    def registered():
        status.update(acm_registration_status(hub, profile))
        return not any(status.values())
    try:
        poll(registered, timeout, 'ACM registration')
    except Failure as exc:
        if str(exc) != 'Timed out waiting for ACM registration':
            raise
        raise Failure(str(exc) + ': ' + json.dumps(status, sort_keys=True)) from None


def normalize_field(value):
    if isinstance(value, dict) and all(isinstance(v, dict) and 'selected' in v for v in value.values()):
        return ','.join(k for k, v in value.items() if v['selected'] in (1, '1', True))
    return str(value) if isinstance(value, (int, float)) else value


def references(value, ids):
    if isinstance(value, str) and value.startswith('@'):
        require(value[1:] in ids, 'Unresolved OPNsense object reference: ' + value)
        return ids[value[1:]]
    if isinstance(value, list):
        return ','.join(references(v, ids) for v in value)
    return value


def reconcile_opnsense(api, objects, config, journal=lambda event: None):
    ids = {}
    # No full-config replacement, deletion, or restart. Update only reviewed fields.
    for obj in objects:
        kind, name = obj['type'], obj['name']
        require(kind in ('healthcheck', 'server', 'backend', 'acl', 'action', 'frontend'), 'Unsupported OPNsense object kind')
        _, search = api.request('POST', '/api/haproxy/settings/search_' + kind + 's',
                                {'current': 1, 'rowCount': -1, 'searchPhrase': name})
        matches = [r for r in search.get('rows', []) if r.get('name') == name]
        require(len(matches) <= 1, 'Ambiguous OPNsense object name: ' + name)
        desired = {k: references(v, ids) for k, v in obj['fields'].items()}
        desired['name'] = name
        if matches:
            uid = matches[0]['uuid']
            _, existing = api.request('GET', '/api/haproxy/settings/get_' + kind + '/' + uid)
            current = existing[kind]
            if any(normalize_field(current.get(k)) != normalize_field(v) for k, v in desired.items()):
                journal({'system': 'opnsense', 'operation': 'update', 'type': kind, 'name': name, 'uuid': uid, 'previous_fields': {k: normalize_field(current.get(k, '')) for k in desired}})
                _, result = api.request('POST', '/api/haproxy/settings/set_' + kind + '/' + uid, {kind: desired})
                require(result.get('result') == 'saved', 'OPNsense rejected an object update')
        else:
            journal({'system': 'opnsense', 'operation': 'create-intent', 'type': kind, 'name': name})
            _, result = api.request('POST', '/api/haproxy/settings/add_' + kind, {kind: desired})
            require(result.get('result') == 'saved' and result.get('uuid'), 'OPNsense rejected object creation')
            uid = result['uuid']
            journal({'system': 'opnsense', 'operation': 'created', 'type': kind, 'name': name, 'uuid': uid})
        ids[kind + '/' + name] = uid
    if config.get('enable_service'):
        _, current = api.request('GET', '/api/haproxy/settings/get')
        enabled = current.get('haproxy', {}).get('general', {}).get('enabled')
        require(enabled in ('0', '1', 0, 1), 'Unexpected HAProxy enable state')
        if str(enabled) != '1':
            journal({'system': 'opnsense', 'operation': 'enable-service', 'previous_enabled': enabled})
            _, result = api.request('POST', '/api/haproxy/settings/set', {'haproxy': {'general': {'enabled': '1'}}})
            require(result.get('result') == 'saved', 'Could not enable HAProxy')
    _, result = api.request('GET', '/api/haproxy/service/configtest')
    require(result.get('result') == config['configtest_success'], 'HAProxy configtest did not match the reviewed success response; no reload attempted')
    _, result = api.request('POST', '/api/haproxy/service/reconfigure', {})
    require(result.get('status') == config['reconfigure_success'], 'HAProxy reconfigure did not match the reviewed success response')


def reconcile_dns(api, zone, desired, journal=lambda event: None):
    query = urllib.parse.urlencode({'name': desired['name'], 'per_page': 100})
    _, result = api.request('GET', '/zones/' + zone + '/dns_records?' + query)
    require(result.get('success') is True, 'Cloudflare record lookup failed')
    require(result.get('result_info', {}).get('total_pages', 1) <= 1, 'Unexpected DNS pagination; review records manually')
    records = result['result']
    require(not any(r['type'] == 'TXT' and 'external-dns' in r.get('content', '') for r in records), 'ExternalDNS ownership exists at the public hostname')
    require(not any(r['type'] in ('A', 'CNAME') for r in records), 'Conflicting A/CNAME at the public hostname')
    records = [r for r in records if r['type'] == 'AAAA']
    require(len(records) <= 1, 'Multiple AAAA records require an explicit migration plan')
    if records:
        old = records[0]
        # Never silently take ownership of another deployment's record.
        require(old.get('comment') == desired['comment'], 'Existing DNS record is not owned by this workflow')
        if all(old.get(k) == v for k, v in desired.items()):
            return False
        journal({'system': 'cloudflare', 'operation': 'update', 'zone': zone, 'previous': old})
        _, changed = api.request('PATCH', '/zones/' + zone + '/dns_records/' + old['id'], desired)
    else:
        journal({'system': 'cloudflare', 'operation': 'create-intent', 'zone': zone, 'name': desired['name']})
        _, changed = api.request('POST', '/zones/' + zone + '/dns_records', desired)
    require(changed.get('success') is True, 'Cloudflare rejected DNS reconciliation')
    journal({'system': 'cloudflare', 'operation': 'applied', 'zone': zone, 'id': changed.get('result', {}).get('id')})
    return True


def validate_release(profile, edge, folder):
    # The execution environment installs the pinned requirements.txt.
    import yaml
    require(profile['namespace'] == 'keycloak' and profile['hub_namespace'] == 'openshift-gitops', 'Unexpected deployment namespace')
    desired_dns = dns_record(profile)
    require(profile['public_host'] == desired_dns['name'] and profile['public_vip'] == desired_dns['content'], 'DNS and deployment profile disagree')
    require(1 <= profile['timeout_seconds'] <= 3600, 'Timeout must be between 1 and 3600 seconds')
    require(profile['expected_csv'] == 'keycloak-operator.v26.8.1', 'Version changes require the upgrade workflow')
    prerequisites = list(yaml.safe_load_all((folder / 'prerequisites.yaml').read_text()))
    ca = next(x for x in prerequisites if x['kind'] == 'ConfigMap')['data']['db-ca.pem']
    ssl.create_default_context(cadata=ca)
    cert = next(x for x in prerequisites if x['kind'] == 'Certificate')
    require(profile['public_host'] in cert['spec']['dnsNames'], 'Certificate SAN differs from public hostname')
    objects = {(o['type'], o['name']): o['fields'] for o in edge['objects']}
    require(len(objects) == len(edge['objects']), 'Duplicate edge object identity')
    for site in profile['sites']:
        docs = list(yaml.safe_load_all((folder / (site['overlay'] + '.yaml')).read_text()))
        svc = next(x for x in docs if x['kind'] == 'Service')
        require(svc['metadata']['annotations']['metallb.io/loadBalancerIPs'] == site['backend_ipv6'], 'MetalLB allocation differs from edge backend')
        kc = next(x for x in docs if x['kind'] == 'Keycloak')
        require(kc['spec']['hostname']['hostname'] == 'https://' + profile['public_host'], 'Keycloak public hostname mismatch')
        require(kc['spec']['image'] == 'quay.io/keycloak/keycloak:26.8.1', 'Runtime version differs from reviewed operator')
        policies = [x for x in docs if x['kind'] == 'NetworkPolicy']
        require(policies, 'Runtime ingress policy is required')
        for policy in policies:
            for rule in policy['spec'].get('ingress', []):
                for peer in rule.get('from', []):
                    if 'ipBlock' in peer:
                        network = ipaddress.ip_network(peer['ipBlock']['cidr'])
                        require(network.version == 6, 'Ingress policy requires IPv6 CIDRs')
        suffix = site['overlay'].removeprefix('cluster-')
        server = objects[('server', 'keycloak-site-' + suffix)]
        require(server['address'] == site['backend_ipv6'] and str(server['port']) == '443', 'OPNsense backend differs from profile')
        require(server.get('ssl') == '1' and server.get('sslVerify') == '1' and server.get('sslCA'), 'Backend TLS verification is required')
        require(server.get('sslSNI') == profile['public_host'] and server.get('advanced') == 'verifyhost ' + profile['public_host'], 'Backend SNI/hostname verification mismatch')
    frontend = objects[('frontend', 'keycloak-public')]
    require(frontend['bind'] == '[' + profile['public_vip'] + ']:443' and frontend['ssl_enabled'] == '1', 'Public VIP TLS listener mismatch')


def private_file(folder, name, content):
    path = folder / name
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(content)
    return str(path)


def managed_access(hub, site, folder):
    cluster = hub.get('managedcluster', site)
    clients = cluster['spec']['managedClusterClientConfigs']
    require(len(clients) == 1, 'Expected one managed API endpoint for ' + site)
    client = clients[0]
    from discovery import api_endpoint
    expected = api_endpoint(cluster)['url']
    account = hub.get('managedserviceaccount', 'application-manager', site)
    ref = account.get('status', {}).get('tokenSecretRef', {}).get('name')
    require(ref, 'ACM application-manager token is unavailable for ' + site)
    secret = hub.get('secret', ref, site)
    token = base64.b64decode(secret['data']['token'], validate=True).decode()
    require(token, 'ACM managed token is empty')
    config = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': site,
              'clusters': [{'name': site, 'cluster': {'server': expected, 'certificate-authority-data': client['caBundle']}}],
              'users': [{'name': site, 'user': {'token': token}}],
              'contexts': [{'name': site, 'context': {'cluster': site, 'user': site}}]}
    return Kube(private_file(folder, site + '.json', json.dumps(config)), expected)


def dns_record(profile):
    desired = json.loads((ROOT / 'automation/cloudflare/record.json').read_text())
    desired.update(name=profile['public_host'], content=profile['public_vip'])
    return desired


class Deployment:
    def __init__(self, args, profile, manifests, edge):
        self.args, self.p, self.manifests, self.edge = args, profile, manifests, edge
        self.stage = 'credentials'
        self.events = []
        self.run_id = str(uuid.uuid4())
        self.recovery = []
        self.report_dir = Path(environment('KEYCLOAK_REPORT_DIR')).resolve()
        require(self.report_dir != ROOT and ROOT not in self.report_dir.parents, 'Recovery directory must be outside the repository')
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.journal({'system': 'workflow', 'operation': 'start', 'request_id': args.request_id, 'revision': args.revision})
        platform = json.loads((ROOT / 'automation/aap/profiles/platform.json').read_text())
        self.hub = Kube(environment('KUBECONFIG'), platform['hub_api'])
        credentials_dir = manifests / 'credentials'
        credentials_dir.mkdir(mode=0o700)
        self.sites = {s['name']: managed_access(self.hub, s['name'], credentials_dir) for s in profile['sites']}
        self.credentials_dir = credentials_dir
        self.timeout = profile['timeout_seconds']
        self.ns = profile['namespace']
        self.hubns = profile['hub_namespace']

    def load_external_credentials(self):
        ref = self.p['credential_ref']
        require(ref == {'namespace': 'keycloak', 'name': 'keycloak-automation'}, 'Unexpected automation Secret reference')
        kube = self.sites[self.p['sites'][0]['name']]
        poll(lambda: ready(kube.get('externalsecret', ref['name'], ref['namespace'])), self.timeout, 'automation ExternalSecret')
        data = kube.get('secret', ref['name'], ref['namespace'])['data']
        def credential(key):
            require(data.get(key), 'Managed-cluster automation Secret is missing key: ' + key)
            return base64.b64decode(data[key], validate=True).decode()
        require(credential('opnsense_url') == self.p['opnsense']['url'], 'Vault and survey OPNsense endpoints differ')
        self.ca = private_file(self.credentials_dir, 'keycloak-ca.pem', credential('keycloak_ca'))
        edge_ca = private_file(self.credentials_dir, 'opnsense-ca.pem', credential('opnsense_ca'))
        self.admin_user = credential('permanent_admin_username')
        self.admin_password = credential('permanent_admin_password')
        self.smoke_secret = credential('smoke_client_secret')
        basic = base64.b64encode((credential('opnsense_key') + ':' + credential('opnsense_secret')).encode()).decode()
        self.edge_api = HTTPS(self.p['opnsense']['url'], edge_ca, {'Authorization': 'Basic ' + basic})
        self.dns_api = HTTPS('https://api.cloudflare.com/client/v4', headers={'Authorization': 'Bearer ' + credential('cloudflare_token')})

    def journal(self, event):
        self.recovery.append(event)
        path = self.report_dir / (self.run_id + '.json')
        temporary = path.with_suffix('.tmp')
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(self.recovery, output, indent=2)
        os.replace(temporary, path)

    def step(self, name):
        self.stage = name
        self.events.append(name)
        self.journal({'system': 'workflow', 'stage': name})
        print(json.dumps({'stage': name, 'request_id': self.args.request_id}), flush=True)

    def application_name(self, component, overlay):
        return self.p.get('resource_prefix', 'keycloak') + '-' + component + '-' + overlay

    def sync(self, app):
        current = self.hub.get('applications.argoproj.io', app, self.hubns)
        require(current['spec']['source']['targetRevision'] == self.args.revision, 'Application revision mismatch')
        require('automated' not in current['spec'].get('syncPolicy', {}), 'Unexpected automated sync policy')
        require(not current.get('operation') and current.get('status', {}).get('operationState', {}).get('phase') not in ('Running', 'Terminating'), 'An Application operation is already active')
        info = [{'name': 'keycloak-workflow-run', 'value': self.run_id}]
        self.hub.patch('applications.argoproj.io', app, self.hubns,
                       {'operation': {'sync': {'revision': self.args.revision, 'prune': False}, 'info': info}})
        def done():
            status = self.hub.get('applications.argoproj.io', app, self.hubns).get('status', {}).get('operationState', {})
            if status.get('operation', {}).get('info') != info:
                return False
            require(status.get('phase') not in ('Failed', 'Error'), 'Argo CD sync failed for ' + app)
            return status.get('phase') == 'Succeeded'
        poll(done, self.timeout, app + ' sync')

    def backend(self, ip):
        return HTTPS('https://' + self.p['public_host'], self.ca, connect_ip=ip)

    def login(self, api, user, password, allow_invalid=False):
        status, result = api.request('POST', '/realms/master/protocol/openid-connect/token',
                                    {'client_id': 'admin-cli', 'grant_type': 'password', 'username': user, 'password': password},
                                    expected=(200, 400, 401), form=True)
        if status == 200:
            require(result.get('access_token'), 'Administrator token response is missing an access token')
            return result['access_token']
        # Since Keycloak 26.6, invalid_grant uses HTTP 400 (previously 401).
        # Only this OAuth error permits the initial-install bootstrap fallback.
        if allow_invalid and result.get('error') == 'invalid_grant':
            return None
        error = result.get('error')
        safe_error = error if error in ('invalid_grant', 'invalid_client', 'unauthorized_client',
                                        'invalid_request', 'unsupported_grant_type') else 'unrecognized_error'
        raise Failure('Administrator token request rejected (HTTP ' + str(status) + '; ' + safe_error + ')')

    def bootstrap(self, kube, api):
        token = self.login(api, self.admin_user, self.admin_password, allow_invalid=True)
        temporary_id = None
        if not token:
            require(self.args.lifecycle == 'initial-install', 'Permanent admin login failed; reconcile cannot bootstrap or reset credentials')
            secret = kube.get('secret', 'keycloak-initial-admin' if self.p.get('automatic') is True else 'keycloak-bootstrap', self.ns)['data']
            user, password = (base64.b64decode(secret[k]).decode() for k in ('username', 'password'))
            require(user != self.admin_user, 'Permanent and temporary administrator names must differ')
            token = self.login(api, user, password)
            auth = {'Authorization': 'Bearer ' + token}
            _, existing = api.request('GET', '/admin/realms/master/users?' + urllib.parse.urlencode({'username': self.admin_user, 'exact': 'true'}), headers=auth)
            require(not existing, 'Permanent administrator already exists but login failed; manual recovery required')
            api.request('POST', '/admin/realms/master/users', {'username': self.admin_user, 'enabled': True, 'credentials': [{'type': 'password', 'value': self.admin_password, 'temporary': False}]}, expected=(201,), headers=auth)
            _, created = api.request('GET', '/admin/realms/master/users?' + urllib.parse.urlencode({'username': self.admin_user, 'exact': 'true'}), headers=auth)
            require(len(created) == 1, 'Cannot uniquely identify the permanent administrator')
            _, role = api.request('GET', '/admin/realms/master/roles/admin', headers=auth)
            api.request('POST', '/admin/realms/master/users/' + created[0]['id'] + '/role-mappings/realm', [role], expected=(204,), headers=auth)
            _, temporary = api.request('GET', '/admin/realms/master/users?' + urllib.parse.urlencode({'username': user, 'exact': 'true'}), headers=auth)
            require(len(temporary) == 1, 'Cannot uniquely identify the temporary administrator')
            temporary_id = temporary[0]['id']
            token = self.login(api, self.admin_user, self.admin_password)
        auth = {'Authorization': 'Bearer ' + token}
        api.request('GET', '/admin/realms/master', headers=auth)
        if temporary_id:
            api.request('DELETE', '/admin/realms/master/users/' + temporary_id, expected=(204,), headers=auth)
        client_id = self.p['smoke_client_id']
        _, clients = api.request('GET', '/admin/realms/master/clients?' + urllib.parse.urlencode({'clientId': client_id}), headers=auth)
        require(len(clients) <= 1, 'Ambiguous synthetic client identity')
        if not clients:
            require(self.args.lifecycle == 'initial-install', 'Synthetic client missing; reconcile cannot create bootstrap configuration')
            api.request('POST', '/admin/realms/master/clients', {'clientId': client_id, 'enabled': True, 'protocol': 'openid-connect', 'publicClient': False, 'serviceAccountsEnabled': True, 'standardFlowEnabled': False, 'directAccessGrantsEnabled': False, 'secret': self.smoke_secret}, expected=(201,), headers=auth)
        # No credential resets and no administrative roles for this synthetic client.
        self.smoke(api)

    def smoke(self, api):
        _, discovery = api.request('GET', '/realms/master/.well-known/openid-configuration')
        require(discovery.get('issuer') == 'https://' + self.p['public_host'] + '/realms/master', 'OIDC issuer mismatch')
        _, result = api.request('POST', '/realms/master/protocol/openid-connect/token', {'grant_type': 'client_credentials', 'client_id': self.p['smoke_client_id'], 'client_secret': self.smoke_secret}, form=True)
        require(result.get('access_token') and result.get('token_type', '').lower() == 'bearer', 'Synthetic authentication failed')

    def run(self):
        lock_name = self.p.get('lock_name', 'keycloak-active-active-workflow-lock')
        lock = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': lock_name, 'namespace': self.hubns}, 'data': {'owner': self.run_id, 'request_id': self.args.request_id, 'revision': self.args.revision}}
        self.step('acquire-environment-lock')
        self.hub.command(['create', '-f', '-'], lock)
        lock_uid = self.hub.get('configmap', lock_name, self.hubns)['metadata']['uid']
        try:
            self.step('verify-managed-clusters')
            for site in self.p['sites']:
                mc = self.hub.get('managedcluster', site['name'])
                require(mc['metadata']['labels'].get('cluster.open-cluster-management.io/clusterset') == self.p.get('cluster_set', 'default'), 'Unexpected cluster-set membership')
                for condition in ('ManagedClusterJoined', 'ManagedClusterConditionAvailable', 'ManagedClusterConditionClockSynced'):
                    require(ready(mc, condition), 'Managed cluster is not ready: ' + site['name'])
                kube = self.sites[site['name']]
                sub = kube.get('subscriptions.operators.coreos.com', 'keycloak-operator', self.ns, missing=True)
                require(not sub or sub.get('status', {}).get('installedCSV') in (None, '', self.p['expected_csv']), 'Operator upgrade/downgrade requires a separate workflow')
                if self.args.lifecycle == 'reconcile':
                    require(sub and sub.get('status', {}).get('installedCSV') == self.p['expected_csv'], 'Reconcile requires an existing operator; select initial-install for first deployment')
                if sub and sub.get('status', {}).get('installedCSV'):
                    kc = kube.get('keycloaks.k8s.keycloak.org', 'keycloak', self.ns, missing=True)
                    require(self.args.lifecycle != 'reconcile' or kc is not None, 'Reconcile requires an existing Keycloak deployment on both sites')
                    require(not kc or kc['spec'].get('image') == 'quay.io/keycloak/keycloak:26.8.1', 'Runtime version change requires a separate workflow')
                require(ready(kube.get('clusterissuer', site.get('issuer', self.p.get('issuer', 'vault-example')))), 'Vault issuer not ready')
                require(ready(kube.get('clustersecretstore', site.get('secret_store', self.p.get('secret_store', 'example-vault')))), 'Vault store not ready')
            self.step('register-clusters-and-applications')
            for artifact in ('hub-registration', 'hub-project', 'applications'):
                self.hub.apply(self.manifests / (artifact + '.yaml'))
            wait_acm_registration(self.hub, self.p, self.timeout)
            shared_database = None
            for site in self.p['sites']:
                kube, suffix = self.sites[site['name']], site['overlay']
                self.step('operator-' + site['name'])
                self.sync(self.application_name('operator', suffix))
                approve_plan(kube, self.ns, self.p['expected_csv'], self.timeout)
                self.step('prerequisites-' + site['name'])
                self.sync(self.application_name('prerequisites', suffix))
                wait_certificate(kube, 'keycloak-tls', self.ns, self.timeout)
                for name in (('keycloak-db',) if self.p.get('automatic') is True else ('keycloak-db', 'keycloak-bootstrap')):
                    poll(lambda: ready(kube.get('externalsecret', name, self.ns)), self.timeout, name)
                    values = kube.get('secret', name, self.ns)['data']
                    keys = ('host', 'database', 'username', 'password') if name == 'keycloak-db' else ('username', 'password')
                    require(all(values.get(k) for k in keys), 'A required Secret key is empty or missing')
                    if name == 'keycloak-db':
                        actual = {k: values[k] for k in keys}
                        require(shared_database is None or actual == shared_database, 'Sites resolve different shared-database credentials')
                        shared_database = actual
            self.step('load-managed-automation-credentials')
            self.load_external_credentials()
            for i, site in enumerate(self.p['sites']):
                self.step('runtime-' + site['name'])
                self.sync(self.application_name('keycloak', site['overlay']))
                kube = self.sites[site['name']]
                wait_keycloak(kube, 'keycloak', self.ns, self.timeout)
                api = self.backend(site['backend_ipv6'])
                api.request('GET', '/lb-check', parse_json=False)
                if i == 0:
                    self.step('bootstrap-or-verify-permanent-admin')
                    self.bootstrap(kube, api)
                else:
                    self.smoke(api)
            if self.p.get('automatic') is True:
                self.prepare_edge()
            self.step('reconcile-opnsense')
            reconcile_opnsense(self.edge_api, self.edge['objects'], self.p['opnsense'], self.journal)
            self.step('verify-public-vip-before-dns')
            self.smoke(self.backend(self.p['public_vip']))
            self.step('reconcile-cloudflare')
            desired = dns_record(self.p)
            reconcile_dns(self.dns_api, self.p['cloudflare']['zone_id'], desired, self.journal)
            self.step('verify-public-dns-and-authentication')
            def public_ready():
                try:
                    addresses = {r[4][0] for r in socket.getaddrinfo(self.p['public_host'], 443, socket.AF_INET6)}
                    return str(ipaddress.IPv6Address(self.p['public_vip'])) in {str(ipaddress.IPv6Address(a)) for a in addresses}
                except OSError:
                    return False
            poll(public_ready, self.timeout, 'public AAAA resolution')
            self.smoke(HTTPS('https://' + self.p['public_host'], self.ca))
            self.step('complete')
        finally:
            # UID precondition prevents deletion of a replacement lock.
            self.hub.command(['delete', '--raw', '/api/v1/namespaces/' + self.hubns + '/configmaps/' + lock_name, '-f', '-'],
                             {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': lock_uid}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['plan', 'deploy', 'remove'], default='plan')
    parser.add_argument('--lifecycle', choices=['initial-install', 'reconcile'], default='reconcile')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--parameters', type=Path, help='Non-secret survey JSON file')
    parser.add_argument('--operation', choices=['deploy', 'remove'], default='deploy')
    parser.add_argument('--confirm-removal', default='')
    parser.add_argument('--database-name', default='')
    parser.add_argument('--deployed-revision', default='')
    parser.add_argument('--backup-reference', default='')
    parser.add_argument('--backup-policy', choices=['verified-backup', 'no-backup'], default='verified-backup')
    parser.add_argument('--no-backup-confirmation', default='')
    parser.add_argument('--haproxy-removal-action', choices=['reload', 'stop-start'], default='reload')
    args = parser.parse_args()
    deployment = None
    try:
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,79}', args.request_id), 'Use a 3–80 character change ID containing letters, digits, dot, underscore, or dash')
        checked_revision(args.revision)
        if args.operation == 'remove':
            require(re.fullmatch(r'[0-9a-f]{40}', args.deployed_revision), 'Removal requires the deployed Application Git revision')
        profile = json.loads((ROOT / 'automation/aap/profiles/active-active.json').read_text())
        edge = json.loads((ROOT / profile['opnsense']['objects_file']).read_text())
        with tempfile.TemporaryDirectory(prefix='keycloak-release-') as tmp:
            folder = Path(tmp)
            mapping = {}
            if args.parameters:
                values = survey.parameters(args.parameters)
                mapping = survey.replacements(values)
                profile = survey.substitute(profile, mapping)
                edge = survey.substitute(edge, mapping)
                profile.update(issuer=values['issuer'], secret_store=values['secret_store'],
                               credential_ref={'namespace': values['credential_namespace'], 'name': values['credential_secret']})
            for name, path in render.TARGETS.items():
                result = subprocess.run(['kubectl', 'kustomize', str(ROOT / path)], capture_output=True, text=True)
                require(result.returncode == 0, 'Kustomize render failed for ' + name)
                manifest = result.stdout.replace('REPLACE_ME_REVIEWED_REVISION', args.deployed_revision if args.operation == 'remove' else args.revision)
                (folder / (name + '.yaml')).write_text(manifest)
            if mapping:
                survey.configure_manifests(folder, mapping, render.TARGETS)
            text = json.dumps(profile) + json.dumps(edge) + ''.join((folder / (n + '.yaml')).read_text() for n in render.TARGETS)
            unresolved = sorted(set(re.findall(r'REPLACE_ME_[A-Z0-9_]+', text)))
            report = {'mode': args.mode, 'operation': args.operation, 'revision': args.revision, 'request_id': args.request_id,
                      'unresolved_inputs': unresolved, 'edge_objects_reviewed': edge.get('reviewed') is True,
                      'execution_enabled': profile.get('execution_enabled') is True, 'applied': False,
                      'parameters': values if args.parameters else {},
                      'removal_enabled': profile.get('removal_enabled') is True,
                      'deployed_revision': args.deployed_revision,
                      'haproxy_removal_action': args.haproxy_removal_action if args.operation == 'remove' else None,
                      'preserved_on_removal': ['all Vault credentials, secret versions, tokens, policies and PKI', 'other applications and firewall objects', 'HAProxy global settings and service enablement', 'shared certificates, CAs and VIP'] if args.operation == 'remove' else [],
                      'removal_stages': ['validate ownership and database', 'detach Applications', 'delete owned DNS', 'delete owned HAProxy objects', 'stop both sites', 'reset dedicated public schema', 'remove namespaced resources and operator CSV'] if args.operation == 'remove' else [],
                      'configuration_source': 'Git revision plus survey-derived Application Kustomize patches'}
            if args.mode == 'plan':
                print(json.dumps(report, indent=2))
                return 0
            if args.mode == 'remove':
                require(args.operation == 'remove' and profile.get('removal_enabled') is True, 'Removal is disabled in the reviewed Git profile')
                from remove import validate_removal_confirmation
                validate_removal_confirmation(profile, args)
            else:
                require(args.operation == 'deploy', 'Removal workflow cannot deploy')
                require(profile.get('execution_enabled') is True, 'Deployment is disabled in the reviewed Git profile')
            require(not unresolved, 'Deployment inputs remain unresolved; run Plan for the list')
            require(edge.get('reviewed') is True and edge.get('objects'), 'Reviewed OPNsense objects are required')
            require({o['type'] for o in edge['objects']} >= {'server', 'backend', 'frontend', 'healthcheck'}, 'Incomplete OPNsense topology')
            require(len(profile['sites']) == 2 and len({s['name'] for s in profile['sites']}) == 2, 'Two distinct configured sites are required')
            for site in profile['sites']:
                ipaddress.IPv6Address(site['backend_ipv6'])
            require(profile['sites'][0]['backend_ipv6'] != profile['sites'][1]['backend_ipv6'], 'Backend IPs must differ')
            validate_release(profile, edge, folder)
            require(args.parameters is not None, 'Deployment requires survey parameters')
            deployment = Deployment(args, profile, folder, edge)
            if args.mode == 'remove':
                from remove import remove
                remove(deployment)
            else:
                deployment.run()
            print(json.dumps({'result': 'succeeded', 'revision': args.revision, 'request_id': args.request_id, 'stages': deployment.events}))
            return 0
    except (Failure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # Only deliberate Failure messages are safe to expose; API/parser bodies may be sensitive.
        message = str(exc) if isinstance(exc, Failure) else 'Input, transport, or response-format failure; inspect the failed stage without exposing credentials'
        print(json.dumps({'result': 'failed', 'stage': deployment.stage if deployment else 'validation', 'error': message}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
