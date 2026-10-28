#!/usr/bin/env python3
"""Remove the recorded automatic deployment, never reconstruct targets from guesses."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import automatic
import deploy
import discovery
import instances
import remove
from deploy import require, Failure

ROOT = Path(__file__).resolve().parents[1]


def load_state(hub, platform, namespace='keycloak'):
    obj = hub.get('configmap', platform['state_configmap'], 'openshift-gitops', missing=True)
    require(obj, 'No recorded automatic deployment exists; removal has nothing to target')
    state = json.loads(obj['data']['state.json'])
    state['profile']['database_tls'] = deploy.database_tls(state['profile'])
    require(state['profile'].get('automatic') is True and state['profile'].get('environment') == platform['environment'], 'Deployment state belongs to another environment')
    require(state['profile'].get('namespace') == namespace and state['profile'].get('resource_prefix') == instances.prefix(namespace), 'Deployment namespace/identity mismatch')
    require(obj['metadata'].get('labels', {}).get(instances.OWNER_LABEL) == instances.prefix(namespace), 'Deployment record ownership mismatch')
    require(state['platform']['state_configmap'] == platform['state_configmap'], 'Recorded state location differs from selected deployment')
    require(state['profile'].get('lock_name') == instances.prefix(namespace) + '-lock', 'Recorded deployment lock differs')
    require(discovery.public_hostname(state['profile']['public_host']) == state['profile']['public_host'], 'Recorded public hostname is not canonical')
    require(len(state['profile']['sites']) == 2, 'Unexpected deployment topology')
    for app in state['documents']['applications']:
        require(app['spec']['source']['targetRevision'] == state['revision'], 'Recorded Application revision mismatch')
    return obj, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--mode', choices=['plan', 'remove'], default='plan')
    parser.add_argument('--confirm-removal', default='')
    parser.add_argument('--database-name', default='')
    parser.add_argument('--backup-reference', default='')
    parser.add_argument('--backup-policy', choices=['verified-backup', 'no-backup'], default='verified-backup')
    parser.add_argument('--no-backup-confirmation', default='')
    parser.add_argument('--haproxy-removal-action', choices=['reload', 'stop-start'], default='reload')
    parser.add_argument('--request-id', default='AAP-removal')
    args = parser.parse_args();run = None
    try:
        args.namespace = instances.namespace(args.namespace)
        args.revision = deploy.execution_revision()
        platform = json.loads((ROOT / 'automation/aap/profiles/platform.json').read_text())
        gates = json.loads((ROOT / 'automation/aap/profiles/active-active.json').read_text())
        instances.configure(gates, platform, args.namespace)
        hub = deploy.Kube(deploy.environment('KUBECONFIG'), platform['hub_api'])
        obj, state = load_state(hub, platform, args.namespace)
        profile = state['profile'];profile['removal_enabled'] = gates.get('removal_enabled') is True
        if args.mode == 'plan':
            print(json.dumps({'mode': 'plan', 'applied': False, 'deployed_revision': state['revision'], 'namespace': profile['namespace'], 'database_path': profile['database_path'], 'database_tls': profile['database_tls'], 'public_host': profile['public_host'], 'public_vip': profile['public_vip'], 'clusters': [s['name'] for s in profile['sites']], 'removal_enabled': profile['removal_enabled'], 'preserves': ['Vault', 'shared HAProxy applications/settings', 'certificates', 'public VIP'], 'backup_policy': args.backup_policy, 'backup_confirmation_checks': 'enforced before removal; plan does not attest a backup', 'database_checks': 'deferred to gated execution'}))
            return 0
        require(profile['removal_enabled'], 'Removal is disabled in the reviewed Git profile')
        remove.validate_removal_confirmation(profile, args)
        args.operation = 'remove';args.lifecycle = 'reconcile'
        with tempfile.TemporaryDirectory(prefix='keycloak-removal-') as tmp:
            folder = Path(tmp)
            clients = {s['name']: deploy.managed_access(hub, s['name'], folder) for s in profile['sites']}
            for site in profile['sites']:
                require(discovery.api_endpoint(hub.get('managedcluster', site['name']))['url'] == site['server'], 'Managed API changed since deployment')
            automatic.write_artifacts(folder, state['documents'])
            found = {'hub': hub, 'clients': clients, 'ca': state['ca'], 'hub_sources': state['hub_sources']}
            run = automatic.AutomaticDeployment(args, profile, folder, state['edge'], found, state['platform'], {}, state['documents'])
            with instances.shared_lock(hub, profile['hub_namespace'], run.run_id):
                current = hub.get('configmap', platform['state_configmap'], profile['hub_namespace'])
                require(current['metadata']['uid'] == obj['metadata']['uid'] and current['data'] == obj['data'], 'Deployment state changed before removal')
                run.assert_database_isolation()
                remove.remove(run)
                current = hub.get('configmap', platform['state_configmap'], profile['hub_namespace'])
                require(current['metadata']['uid'] == obj['metadata']['uid'] and current['data'] == obj['data'], 'Deployment state changed during removal')
                remove.delete_object(hub, current, profile['timeout_seconds'])
        print(json.dumps({'result': 'succeeded', 'stages': run.events}))
        return 0
    except (Failure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({'result': 'failed', 'error': str(exc) if isinstance(exc, Failure) else type(exc).__name__, 'stage': run.stage if run else 'state-validation'}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
