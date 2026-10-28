import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import deploy
import render
import survey


class SurveyTests(unittest.TestCase):
    def test_acm_discovery_uses_hub_token_secret_and_private_file(self):
        hub = Mock()
        hub.get.side_effect = [
            {'spec': {'managedClusterClientConfigs': [{'url': 'https://api.cluster-a.example.com:6443', 'caBundle': 'Y2E='}]}},
            {'status': {'tokenSecretRef': {'name': 'rotating-token'}}},
            {'data': {'token': base64.b64encode(b'not-a-real-token').decode()}},
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.object(deploy, 'Kube') as kube:
            deploy.managed_access(hub, 'cluster-a', Path(tmp))
            path = Path(kube.call_args.args[0])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            config = json.loads(path.read_text())
            self.assertEqual(config['users'][0]['user']['token'], 'not-a-real-token')
            self.assertEqual(config['clusters'][0]['cluster']['certificate-authority-data'], 'Y2E=')
        hub.get.assert_any_call('secret', 'rotating-token', 'cluster-a')

    def test_insecure_managed_endpoint_never_reads_token(self):
        hub = Mock()
        hub.get.return_value = {'spec': {'managedClusterClientConfigs': [{'url': 'http://wrong.invalid', 'caBundle': 'Y2E='}]}}
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(deploy.Failure):
            deploy.managed_access(hub, 'cluster-a', Path(tmp))
        self.assertEqual(hub.get.call_count, 1)

    def test_argo_patches_render_exactly_the_survey_plan(self):
        mapping = {'REPLACE_ME_CLUSTER_A_METALLB_IPV6': 'fd00::a', 'REPLACE_ME_CLUSTER_B_METALLB_IPV6': 'fd00::b',
                   'REPLACE_ME_DATABASE_CA_PEM': 'example public CA\nsecond line',
                   'REPLACE_ME_DATABASE_VAULT_PATH': 'apps/db',
                   'auth.example.com': 'login.example.com',
                   'REPLACE_ME_OBSERVED_PROXY_IPV6_CIDRS': 'fd00::1/128',
                   'REPLACE_ME_OBSERVED_PROXY_IPV6_CIDR': 'fd00::1/128'}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'gitops', root / 'gitops')
            for name, path in render.TARGETS.items():
                output = subprocess.check_output(['kubectl', 'kustomize', str(root / path)], text=True)
                (root / (name + '.yaml')).write_text(output)
            survey.configure_manifests(root, mapping, render.TARGETS)
            apps = list(yaml.safe_load_all((root / 'applications.yaml').read_text()))
            # Each Application gets an independent checkout, just as Argo does.
            for index, app in enumerate(apps):
                checkout = root / str(index)
                shutil.copytree(ROOT / 'gitops', checkout / 'gitops')
                source = app['spec']['source']
                directory = checkout / source['path']
                kpath = directory / 'kustomization.yaml'
                k = yaml.safe_load(kpath.read_text())
                k.setdefault('patches', []).extend(source.get('kustomize', {}).get('patches', []))
                kpath.write_text(yaml.safe_dump(k))
                actual = list(yaml.safe_load_all(subprocess.check_output(['kubectl', 'kustomize', str(directory)], text=True)))
                name = next(n for n, p in render.TARGETS.items() if p == source['path'])
                expected = list(yaml.safe_load_all((root / (name + '.yaml')).read_text()))
                self.assertEqual(actual, expected, app['metadata']['name'])

    def test_replacement_text_is_not_interpreted_as_another_parameter(self):
        self.assertEqual(survey.substitute('ONE', {'ONE': 'TWO', 'TWO': 'wrong'}), 'TWO')

    def test_dns_uses_same_survey_hostname_and_vip(self):
        record = deploy.dns_record({'public_host': 'login.example.com', 'public_vip': 'fd00::70'})
        self.assertEqual(record['name'], 'login.example.com')
        self.assertEqual(record['content'], 'fd00::70')
        self.assertFalse(record['proxied'])

class ManagedCredentialTests(unittest.TestCase):
    def test_credentials_are_read_from_managed_site_not_hub(self):
        run = deploy.Deployment.__new__(deploy.Deployment)
        run.p = {'credential_ref': {'namespace': 'keycloak', 'name': 'keycloak-automation'},
                 'sites': [{'name': 'cluster-a'}], 'opnsense': {'url': 'https://router.example.com'}}
        run.hub = Mock()
        kube = Mock()
        run.sites = {'cluster-a': kube}
        run.timeout = 1
        values = {k: 'fixture' for k in ('keycloak_ca', 'opnsense_ca', 'permanent_admin_username', 'permanent_admin_password', 'smoke_client_secret', 'opnsense_key', 'opnsense_secret', 'cloudflare_token')}
        values['opnsense_url'] = 'https://router.example.com'
        kube.get.side_effect = [{'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
                                {'data': {k: base64.b64encode(v.encode()).decode() for k,v in values.items()}}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(deploy, 'HTTPS'):
            run.credentials_dir = Path(tmp)
            run.load_external_credentials()
        kube.get.assert_any_call('secret', 'keycloak-automation', 'keycloak')
        run.hub.get.assert_not_called()
