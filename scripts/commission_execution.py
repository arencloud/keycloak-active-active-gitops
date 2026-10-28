#!/usr/bin/env python3
"""Check the Keycloak execution pod without deploying workloads or changing APIs."""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

import deploy
import discovery


def main():
    required = ('oc', 'kubectl', 'openssl', 'python3', 'psql')
    missing = [name for name in required if not shutil.which(name)]
    deploy.require(not missing, 'Missing execution tools: ' + ', '.join(missing))
    verify_help = subprocess.run(['openssl', 'verify', '-help'], capture_output=True, text=True)
    deploy.require('-verify_hostname' in verify_help.stdout + verify_help.stderr,
                   'OpenSSL must support certificate hostname verification')
    psql_version = subprocess.run(['psql', '--version'], capture_output=True, text=True)
    deploy.require(psql_version.returncode == 0, 'PostgreSQL client cannot execute')
    directory = Path(deploy.environment('KEYCLOAK_REPORT_DIR')).resolve()
    deploy.require(os.path.ismount(directory), 'Recovery directory must be a mounted volume')
    directory.mkdir(exist_ok=True)
    marker = directory / 'commissioning-marker.json'
    previous = json.loads(marker.read_text()) if marker.exists() else None
    current = {'job': os.environ.get('COMMISSION_JOB_ID', 'manual')}
    temporary = directory / 'commissioning-marker.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(current, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker)
    deploy.require(json.loads(marker.read_text()) == current, 'Recovery read-back failed')
    platform = json.loads((Path(__file__).resolve().parents[1] / 'automation/aap/profiles/platform.json').read_text())
    hub = deploy.Kube(deploy.environment('KUBECONFIG'), platform['hub_api'])
    with tempfile.TemporaryDirectory(prefix='keycloak-ee-check-') as tmp:
        folder = Path(tmp)
        managed = deploy.managed_access(hub, platform['commissioning_cluster'], folder)
        issuer = discovery.ready_vault_resource(managed.get('clusterissuers')['items'], platform['issuer_preference'], 'issuer')
        ca = deploy.private_file(folder, 'public-ca.pem', base64.b64decode(issuer['spec']['vault']['caBundle']).decode())
        # Login redirects and authentication failures still prove network/TLS access.
        # Authenticated adapter behavior is commissioned separately.
        deploy.HTTPS(platform['opnsense_url'], ca).request('GET', '/api/haproxy/service/status', expected=(200, 302, 401, 403), parse_json=False)
        addresses = socket.getaddrinfo(platform['commissioning_database_host'], 5432, socket.AF_INET6, socket.SOCK_STREAM)
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as connection:
            connection.settimeout(15)
            connection.connect(addresses[0][4])
    print(json.dumps({'result': 'succeeded', 'job': current['job'], 'prior_persisted_job': (previous or {}).get('job'),
                      'recovery_mount': str(directory), 'required_tools': list(required),
                      'psql_available': True, 'psql_version': psql_version.stdout.strip(),
                      'managed_api': 'reachable', 'opnsense_https': 'verified', 'postgresql_ipv6_tcp': 'reachable',
                      'application_deployed': False}))


if __name__ == '__main__':
    try:
        main()
    except (deploy.Failure, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'result': 'failed', 'error': str(exc) if isinstance(exc, deploy.Failure) else type(exc).__name__}))
        raise SystemExit(1)
