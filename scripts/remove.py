"""Explicit, independently gated decommissioning of the reviewed environment."""
import base64
import copy
import re
import json
import os
import subprocess
import urllib.parse
import yaml
import render

# Imported lazily by deploy.main so importing this module never executes a workflow.
from deploy import Failure, require, poll, dns_record, normalize_field, references, private_file, database_tls


def dns_candidate(api, zone, desired):
    _, result = api.request('GET', '/zones/' + zone + '/dns_records?' + urllib.parse.urlencode({'name': desired['name'], 'per_page': 100}))
    require(result.get('success') is True and result.get('result_info', {}).get('total_pages', 1) <= 1, 'DNS lookup incomplete')
    records = result['result']
    require(not any(r['type'] in ('A', 'CNAME') or (r['type'] == 'TXT' and 'external-dns' in r.get('content', '')) for r in records), 'Conflicting DNS ownership')
    records = [r for r in records if r['type'] == 'AAAA']
    require(len(records) <= 1, 'Ambiguous DNS record')
    if not records:
        return None
    current = records[0]
    require(all(current.get(k) == desired[k] for k in ('name', 'type', 'content', 'proxied', 'comment')), 'DNS record differs from the requested deployment')
    return current


PROJECT_EDGE_OBJECTS = {
    ('healthcheck', 'keycloak-site-health'), ('server', 'keycloak-site-a'),
    ('server', 'keycloak-site-b'), ('backend', 'keycloak-active-active'),
    ('frontend', 'keycloak-public'),
}


class RemovalEdgeAPI:
    """Allow scoped deletion and the selected service lifecycle, never Vault writes."""
    def __init__(self, api, candidates, service_action='reload'):
        require(service_action in ('reload', 'stop-start'), 'Unknown HAProxy removal action')
        self.api = api
        self.deletions = {'/api/haproxy/settings/del_' + kind + '/' + uid for kind, uid, _ in candidates}
        self.service_paths = {'/api/haproxy/service/reconfigure'}
        if service_action == 'stop-start':
            self.service_paths.update({'/api/haproxy/service/stop', '/api/haproxy/service/start'})

    def request(self, method, path, *args, **kwargs):
        read = method == 'GET' and path.startswith('/api/haproxy/')
        search = method == 'POST' and re.fullmatch(r'/api/haproxy/settings/search_[a-z]+', path)
        write = method == 'POST' and (path in self.deletions or path in self.service_paths)
        require(read or search or write, 'API operation is outside project removal scope')
        return self.api.request(method, path, *args, **kwargs)


def protected_haproxy_config(api, candidates):
    _, response = api.request('GET', '/api/haproxy/settings/get')
    config = copy.deepcopy(response.get('haproxy'))
    require(isinstance(config, dict) and config, 'Cannot inspect shared HAProxy configuration')
    for kind, uid, _ in candidates:
        section = config.get(kind + 's', {}).get(kind, {})
        require(isinstance(section, dict), 'Unexpected HAProxy configuration structure')
        section.pop(uid, None)
    def canonical(value):
        if isinstance(value, dict):
            if value and all(isinstance(v, dict) and 'selected' in v for v in value.values()):
                return normalize_field(value)
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            return [canonical(v) for v in value]
        return value
    return canonical(config)


def shared_service_state(run, candidates):
    _, response = run.edge_api.request('POST', '/api/haproxy/settings/search_frontends', {'current': 1, 'rowCount': -1, 'searchPhrase': ''})
    require(isinstance(response.get('rows'), list) and int(response.get('total', -1)) == len(response['rows']), 'Incomplete remaining frontend inventory')
    own = {uid for kind, uid, _ in candidates if kind == 'frontend'}
    remaining = [row for row in response['rows'] if row.get('uuid') not in own]
    _, result = run.edge_api.request('GET', '/api/haproxy/service/status')
    status = result.get('status')
    require(status in ('running', 'stopped', 'disabled'), 'Unknown shared HAProxy service state')
    if status == 'running':
        require(any(str(row.get('enabled')) == '1' for row in remaining),
                'Removal would leave running HAProxy without another enabled frontend; cannot ensure a running service afterward. Retain a valid frontend before removal.')
    return status


def edge_candidates(api, objects, resource_prefix=None):
    identities = [(obj['type'], obj['name']) for obj in objects]
    allowed = PROJECT_EDGE_OBJECTS
    if resource_prefix:
        from instances import edge_names
        names = edge_names(resource_prefix)
        allowed = {(kind, names[name]) for kind, name in PROJECT_EDGE_OBJECTS}
    require(set(identities) <= allowed and len(set(identities)) == len(identities), 'HAProxy removal includes an object outside this project')
    ids, found = {}, []
    for obj in objects:
        kind, name = obj['type'], obj['name']
        _, response = api.request('POST', '/api/haproxy/settings/search_' + kind + 's', {'current': 1, 'rowCount': -1, 'searchPhrase': name})
        rows = [r for r in response.get('rows', []) if r.get('name') == name]
        require(len(rows) <= 1, 'Ambiguous HAProxy object')
        if not rows:
            continue
        uid = rows[0]['uuid']
        _, response = api.request('GET', '/api/haproxy/settings/get_' + kind + '/' + uid)
        current = response[kind]
        expected = {k: references(v, ids) for k, v in obj['fields'].items()}
        expected['name'] = name
        require(all(normalize_field(current.get(k)) == normalize_field(v) for k, v in expected.items()), 'HAProxy object differs from reviewed configuration: ' + name)
        ids[kind + '/' + name] = uid
        found.append((kind, uid, expected))
    # Inspect ALL object types that can reference servers/backends, including unrelated objects.
    _, response = api.request('GET', '/api/haproxy/settings/get')
    config = response.get('haproxy', {})
    require(isinstance(config, dict) and config, 'Cannot inspect HAProxy references')
    own = {uid for _, uid, _ in found}
    tokens = own | {fields['name'] for _, _, fields in found}
    pattern = r'(?<![A-Za-z0-9_-])(?:' + '|'.join(re.escape(t) for t in tokens) + r')(?![A-Za-z0-9_-])' if tokens else None
    def contains_reference(value):
        if isinstance(value, dict):
            if value and all(isinstance(v, dict) and 'selected' in v for v in value.values()):
                return contains_reference(normalize_field(value))
            return any(contains_reference(v) for v in value.values())
        if isinstance(value, list):
            return any(contains_reference(v) for v in value)
        return isinstance(value, str) and pattern is not None and re.search(pattern, value) is not None
    own_names = {uid: fields['name'] for _, uid, fields in found}
    def inspect(value):
        if not isinstance(value, dict):
            require(not contains_reference(value), 'Shared HAProxy settings reference this deployment')
            return
        if value and all(isinstance(v, dict) and 'selected' in v for v in value.values()):
            require(not contains_reference(value), 'Shared HAProxy settings reference this deployment')
            return
        for key, child in value.items():
            if key in own and isinstance(child, dict) and child.get('name') == own_names[key]:
                continue
            if isinstance(child, dict) and 'name' in child:
                require(not contains_reference(child), 'Another HAProxy object references this deployment')
            else:
                inspect(child)
    inspect(config)
    return found


def ensure_haproxy_running(run):
    """Recover even if the stop/start response was lost after the action succeeded."""
    try:
        _, state = run.edge_api.request('GET', '/api/haproxy/service/status')
    except Exception:
        state = {}
    if state.get('status') != 'running':
        try:
            run.edge_api.request('POST', '/api/haproxy/service/start', {})
        except Exception:
            pass  # Status polling decides whether the service actually recovered.
    def running():
        try:
            return run.edge_api.request('GET', '/api/haproxy/service/status')[1].get('status') == 'running'
        except Exception:
            return False  # A transient API failure must not abandon recovery polling.
    try:
        poll(running, run.timeout, 'HAProxy running after removal')
    except Exception as exc:
        raise Failure('HAProxy restart could not be verified; restore the shared service before resuming removal') from exc
    run.journal({'system': 'opnsense', 'operation': 'service-running-verified'})


def finalize_haproxy(run, protected, candidates, initial_status):
    require(protected_haproxy_config(run.edge_api, candidates) == protected,
            'Shared HAProxy configuration changed during removal; no reload attempted')
    status = shared_service_state(run, [])
    require(status == initial_status, 'Shared HAProxy service state changed; no reload attempted')
    if not candidates:
        run.journal({'system': 'opnsense', 'operation': 'no-project-objects-no-reload'})
        return
    if status != 'running':
        # Stored project objects have been removed; do not start a stopped service.
        run.journal({'system': 'opnsense', 'operation': 'preserve-service-state', 'status': status})
        return
    _, result = run.edge_api.request('GET', '/api/haproxy/service/configtest')
    require(result.get('result') == run.p['opnsense']['configtest_success'], 'HAProxy configtest failed; no reload attempted')
    if run.args.haproxy_removal_action == 'stop-start':
        # Delete saved objects and validate first, keeping shared downtime short.
        # Record recovery intent BEFORE stop: its response may be lost.
        run.journal({'system': 'opnsense', 'operation': 'temporary-stop-start', 'restore_status': 'running'})
        try:
            run.edge_api.request('POST', '/api/haproxy/service/stop', {})
            poll(lambda: run.edge_api.request('GET', '/api/haproxy/service/status')[1].get('status') == 'stopped',
                 run.timeout, 'HAProxy temporary stop')
            _, result = run.edge_api.request('POST', '/api/haproxy/service/reconfigure', {})
            require(result.get('status') == run.p['opnsense']['reconfigure_success'], 'HAProxy reconfigure failed')
        finally:
            # Reconfigure normally starts an enabled service; explicitly start if needed.
            ensure_haproxy_running(run)
    else:
        _, result = run.edge_api.request('POST', '/api/haproxy/service/reconfigure', {})
        require(result.get('status') == run.p['opnsense']['reconfigure_success'], 'HAProxy reconfigure failed')
        poll(lambda: run.edge_api.request('GET', '/api/haproxy/service/status')[1].get('status') == 'running',
             run.timeout, 'HAProxy running after reload')


PLURALS = {'Application': 'applications', 'Keycloak': 'keycloaks', 'Service': 'services',
           'PodDisruptionBudget': 'poddisruptionbudgets', 'NetworkPolicy': 'networkpolicies',
           'Certificate': 'certificates', 'ExternalSecret': 'externalsecrets', 'ConfigMap': 'configmaps', 'Secret': 'secrets',
           'Subscription': 'subscriptions', 'OperatorGroup': 'operatorgroups',
           'ClusterServiceVersion': 'clusterserviceversions'}


def delete_object(kube, obj, timeout):
    api, kind, meta = obj['apiVersion'], obj['kind'], obj['metadata']
    require(kind in PLURALS, 'Deletion kind is not allowlisted')
    prefix = '/api/v1' if api == 'v1' else '/apis/' + api
    path = prefix + '/namespaces/' + meta['namespace'] + '/' + PLURALS[kind] + '/' + meta['name']
    kube.command(['delete', '--raw', path, '-f', '-'], {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                 'propagationPolicy': 'Foreground', 'preconditions': {'uid': meta['uid'], 'resourceVersion': meta['resourceVersion']}})
    resource = PLURALS[kind] + ('.' + api.split('/')[0] if '/' in api else '')
    poll(lambda: kube.get(resource, meta['name'], meta['namespace'], missing=True) is None, timeout, kind + ' deletion')


def resource_name(obj):
    return PLURALS[obj['kind']] + ('.' + obj['apiVersion'].split('/')[0] if '/' in obj['apiVersion'] else '')


def generated_secrets(resources, namespace):
    found = []
    for kube, obj in resources:
        if obj['kind'] not in ('ExternalSecret', 'Certificate'):
            continue
        name = obj['spec']['target']['name'] if obj['kind'] == 'ExternalSecret' else obj['spec']['secretName']
        secret = kube.get('secret', name, namespace, missing=True)
        if not secret:
            continue
        owned = any(ref.get('uid') == obj['metadata']['uid'] for ref in secret['metadata'].get('ownerReferences', []))
        if obj['kind'] == 'Certificate' and not owned:
            # cert-manager commonly does not enable owner refs on TLS Secrets.
            annotations = secret['metadata'].get('annotations', {})
            issuer = obj['spec']['issuerRef']
            owned = secret.get('type') == 'kubernetes.io/tls' and annotations.get('cert-manager.io/issuer-name') == issuer['name'] and annotations.get('cert-manager.io/issuer-kind') == issuer.get('kind', 'Issuer')
        require(owned, 'Generated Secret ownership cannot be established: ' + name)
        # Keep only metadata in the deletion inventory, never secret data.
        found.append((kube, {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': secret['metadata']}))
    return found


def database_values(run):
    records = []
    for site in run.p['sites']:
        data = run.sites[site['name']].get('secret', 'keycloak-db', run.ns)['data']
        records.append({k: base64.b64decode(data[k], validate=True).decode() for k in ('host', 'database', 'username', 'password')})
    require(records[0] == records[1], 'The two sites point at different databases')
    require(records[0]['database'] == run.args.database_name, 'Database confirmation does not match ESO credentials')
    require(run.args.database_name not in ('postgres', 'template0', 'template1'), 'System databases cannot be emptied')
    return records[0]


def backup_attestation(args):
    policy = getattr(args, 'backup_policy', 'verified-backup')
    require(policy in ('verified-backup', 'no-backup'), 'Unknown backup policy')
    require(isinstance(args.database_name, str) and args.database_name.strip(), 'Dedicated database name required')
    reference = args.backup_reference.strip()
    if policy == 'verified-backup':
        require(reference, 'Verified-backup policy requires a verified backup reference')
    else:
        require(not reference, 'Leave backup reference empty when selecting no-backup')
        require(getattr(args, 'no_backup_confirmation', '') == 'DELETE ' + args.database_name + ' WITHOUT BACKUP',
                'No-backup requires the exact confirmation: DELETE <database-name> WITHOUT BACKUP')
    return {'backup_policy': policy, 'backup_reference': reference,
            'data_loss_acknowledged': policy == 'no-backup'}


def validate_removal_confirmation(profile, args):
    require(args.confirm_removal == 'REMOVE ' + profile['public_host'], 'Exact removal confirmation required')
    return backup_attestation(args)


def empty_database(run, data, ca_file, check_only=False):
    if not check_only:
        attestation = backup_attestation(run.args)
        require(run.args.database_name == data['database'], 'Database confirmation does not match reset target')
    # No secrets in argv, SQL text, or captured error output. No shell interpolation.
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('PG'):
            del env[key]
    env.update(PGHOST=data['host'], PGPORT='5432', PGDATABASE=data['database'], PGUSER=data['username'],
               PGPASSWORD=data['password'], PGSSLMODE='verify-full' if database_tls(run.p) == 'enabled' else 'disable', PGCONNECT_TIMEOUT='15', PGGSSENCMODE='disable')
    if database_tls(run.p) == 'enabled':
        require(ca_file, 'Database TLS requires a CA file')
        env['PGSSLROOTCERT'] = ca_file
    sql = """\nBEGIN;
SET LOCAL lock_timeout = '15s';
SET LOCAL statement_timeout = '120s';
DO $$ BEGIN
  IF NOT (SELECT pg_get_userbyid(datdba) = current_user FROM pg_database WHERE datname = current_database()) THEN
    RAISE EXCEPTION 'Dedicated database owner required';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()) THEN
    RAISE EXCEPTION 'Other database connections exist';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname NOT IN ('public','information_schema')) THEN
    RAISE EXCEPTION 'Unexpected user schemas';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname<>'plpgsql') OR EXISTS (SELECT 1 FROM pg_largeobject_metadata) THEN
    RAISE EXCEPTION 'Extensions or large objects require manual review';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public')
     AND (to_regclass('public.realm') IS NULL OR to_regclass('public.databasechangelog') IS NULL) THEN
    RAISE EXCEPTION 'Database is not recognizable as Keycloak';
  END IF;
END $$;
DROP SCHEMA public CASCADE;
CREATE SCHEMA public AUTHORIZATION CURRENT_USER;
COMMIT;
"""
    if check_only:
        sql = sql.replace("  IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()) THEN\n    RAISE EXCEPTION 'Other database connections exist';\n  END IF;\n", '')
        sql = sql.replace('DROP SCHEMA public CASCADE;\nCREATE SCHEMA public AUTHORIZATION CURRENT_USER;', '')
    result = subprocess.run(['psql', '-X', '--no-password', '--set=ON_ERROR_STOP=1'], input=sql, env=env, text=True, capture_output=True)
    require(result.returncode == 0, 'Database reset failed; transaction rolled back or connection outcome requires review. No SQL/error text is logged.')
    if not check_only:
        run.journal({'system': 'database', 'operation': 'public-schema-reset', **attestation})


def remove(run):
    require(run.p.get('removal_enabled') is True, 'Removal disabled in reviewed profile')
    attestation = validate_removal_confirmation(run.p, run.args)
    run.journal({'system': 'workflow', 'operation': 'removal-backup-decision', **attestation})
    run.load_external_credentials()
    lock_name = run.p.get('lock_name', 'keycloak-active-active-workflow-lock')
    run.hub.command(['create', '-f', '-'], {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': lock_name, 'namespace': run.hubns}, 'data': {'owner': run.run_id, 'action': 'remove'}})
    lock_uid = run.hub.get('configmap', lock_name, run.hubns)['metadata']['uid']
    try:
        run.step('validate-removal-ownership')
        db = database_values(run)
        ca_file = None
        if database_tls(run.p) == 'enabled':
            ca = run.sites[run.p['sites'][0]['name']].get('configmap', 'keycloak-db-rootcert', run.ns)['data']['db-ca.pem']
            ca_file = private_file(run.credentials_dir, 'database-ca.pem', ca)
        dns = dns_candidate(run.dns_api, run.p['cloudflare']['zone_id'], dns_record(run.p))
        edge = edge_candidates(run.edge_api, run.edge['objects'], run.p.get('resource_prefix'))
        run.edge_api = RemovalEdgeAPI(run.edge_api, edge, run.args.haproxy_removal_action)
        protected = protected_haproxy_config(run.edge_api, edge)
        initial_status = shared_service_state(run, edge)
        desired_apps = list(yaml.safe_load_all((run.manifests / 'applications.yaml').read_text()))
        apps, resources, csvs = [], [], []
        for expected in desired_apps:
            name = expected['metadata']['name']
            actual = run.hub.get('applications.argoproj.io', name, run.hubns)
            require(all(actual['spec'].get(k) == expected['spec'].get(k) for k in ('source', 'destination', 'project')), 'Application differs from removal request: ' + name)
            require(not actual['metadata'].get('finalizers') and not actual.get('operation') and actual.get('status', {}).get('operationState', {}).get('phase') not in ('Running', 'Terminating'), 'Application is busy or has cascading finalizers')
            require('automated' not in actual['spec'].get('syncPolicy', {}), 'Automated sync must be disabled before removal')
            apps.append(actual)
            site = next(s for s in run.p['sites'] if name.endswith(s['overlay']))
            target = next(key for key, path in render.TARGETS.items() if path == expected['spec']['source']['path'])
            if target == 'prerequisites' and run.p.get('automatic') is True:
                target += '-' + site['overlay']
            kube = run.sites[site['name']]
            for obj in yaml.safe_load_all((run.manifests / (target + '.yaml')).read_text()):
                if obj['kind'] == 'Namespace':
                    continue
                existing = kube.get(resource_name(obj), obj['metadata']['name'], run.ns, missing=True)
                if not existing:
                    continue
                tracked = actual.get('status', {}).get('resources', [])
                group = obj['apiVersion'].split('/')[0] if '/' in obj['apiVersion'] else ''
                require(any(r.get('name') == obj['metadata']['name'] and r.get('kind') == obj['kind'] and r.get('namespace') == run.ns and r.get('group', '') == group for r in tracked), 'Resource not tracked by expected Application')
                resources.append((kube, existing))
        for site in run.p['sites']:
            kube = run.sites[site['name']]
            all_kc = kube.get('keycloaks.k8s.keycloak.org', namespace=run.ns)['items']
            require(all(x['metadata']['name'] == 'keycloak' for x in all_kc), 'Other Keycloak instances share the operator namespace')
            csv = kube.get('csv', run.p['expected_csv'], run.ns, missing=True)
            if csv:
                csvs.append((kube, csv))
        secrets = generated_secrets(resources, run.ns)
        if run.p.get('automatic') is True:
            secrets.extend(run.owned_automation_secrets())
            if run.p.get('firewall_rule'):
                from edge_automatic import verify_public_rule
                verify_public_rule(run.edge_api.api, run.p['firewall_rule'])
        empty_database(run, db, ca_file, check_only=True)
        require(protected_haproxy_config(run.edge_api, edge) == protected, 'Shared HAProxy configuration changed before removal')
        require(shared_service_state(run, edge) == initial_status, 'Shared HAProxy service state changed before removal')
        run.step('detach-argo-applications')
        for obj in apps:
            current = run.hub.get('applications.argoproj.io', obj['metadata']['name'], run.hubns)
            require(current['metadata']['uid'] == obj['metadata']['uid'] and current['spec'] == obj['spec'], 'Application changed during removal')
            run.journal({'system': 'argocd', 'operation': 'delete-intent', 'application': obj['metadata']['name'], 'spec': obj['spec']})
            delete_object(run.hub, current, run.timeout)
        run.step('remove-public-dns')
        if dns:
            current = dns_candidate(run.dns_api, run.p['cloudflare']['zone_id'], dns_record(run.p))
            require(current and current['id'] == dns['id'], 'DNS identity changed')
            run.journal({'system': 'cloudflare', 'operation': 'delete-intent', 'previous': dns})
            _, result = run.dns_api.request('DELETE', '/zones/' + run.p['cloudflare']['zone_id'] + '/dns_records/' + dns['id'])
            require(result.get('success') is True, 'DNS deletion failed')
        run.step('remove-haproxy-objects')
        require(edge_candidates(run.edge_api, run.edge['objects'], run.p.get('resource_prefix')) == edge, 'HAProxy ownership or references changed before deletion')
        require(protected_haproxy_config(run.edge_api, edge) == protected, 'Shared HAProxy configuration changed before deletion')
        require(shared_service_state(run, edge) == initial_status, 'Shared HAProxy service state changed before deletion')
        # Remove references before targets; shared CA/certificate objects are never deleted.
        for kind, uid, expected in reversed(edge):
            _, current = run.edge_api.request('GET', '/api/haproxy/settings/get_' + kind + '/' + uid)
            require(all(normalize_field(current[kind].get(k)) == normalize_field(v) for k, v in expected.items()), 'HAProxy object changed during removal')
            run.journal({'system': 'opnsense', 'operation': 'delete-intent', 'type': kind, 'uuid': uid, 'previous_fields': expected})
            _, result = run.edge_api.request('POST', '/api/haproxy/settings/del_' + kind + '/' + uid, {})
            require(result.get('result') == 'deleted', 'HAProxy object deletion failed')
        finalize_haproxy(run, protected, edge, initial_status)
        if run.p.get('automatic') is True and run.p.get('firewall_rule'):
            from edge_automatic import remove_public_rule
            remove_public_rule(run.edge_api.api, run.p['firewall_rule'], run.journal)
        run.step('stop-keycloak-on-both-sites')
        for kube, obj in resources:
            if obj['kind'] == 'Keycloak':
                delete_object(kube, obj, run.timeout)
        for site in run.p['sites']:
            kube = run.sites[site['name']]
            poll(lambda: not kube.get('pods', namespace=run.ns, selector='app=keycloak')['items'], run.timeout, 'all Keycloak pods stopped')
        run.step('empty-dedicated-keycloak-database')
        empty_database(run, db, ca_file)
        run.step('remove-application-resources')
        for kube, obj in resources:
            if obj['kind'] != 'Keycloak':
                # Re-read because controllers legitimately update resourceVersion during the run.
                current = kube.get(resource_name(obj), obj['metadata']['name'], run.ns, missing=True)
                if current:
                    require(current['metadata']['uid'] == obj['metadata']['uid'], 'Resource was replaced during removal')
                    delete_object(kube, current, run.timeout)
        for kube, obj in secrets:
            current = kube.get('secret', obj['metadata']['name'], run.ns, missing=True)
            if current:
                require(current['metadata']['uid'] == obj['metadata']['uid'], 'Generated Secret was replaced')
                delete_object(kube, {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': current['metadata']}, run.timeout)
        for kube, obj in csvs:
            current = kube.get('csv', obj['metadata']['name'], run.ns, missing=True)
            if current:
                require(current['metadata']['uid'] == obj['metadata']['uid'], 'Operator CSV was replaced')
                delete_object(kube, current, run.timeout)
        run.step('removal-complete')
    finally:
        run.hub.command(['delete', '--raw', '/api/v1/namespaces/' + run.hubns + '/configmaps/' + lock_name, '-f', '-'],
                        {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': lock_uid}})
