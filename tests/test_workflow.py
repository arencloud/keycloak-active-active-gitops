import base64
import importlib.util
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

EXPECTED_CSV = 'keycloak-operator.v26.8.1'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import deploy


class ACMRegistrationTests(unittest.TestCase):
    def fixture(self, proxied=True):
        profile = {'hub_namespace': 'openshift-gitops', 'automatic': True, 'placement_name': 'shared',
                   'sites': [{'name': name, 'server': 'https://api.' + name + '.example:6443'} for name in ('a', 'b')]}
        decisions = {'items': [{'status': {'decisions': [{'clusterName': name} for name in ('a', 'b', 'another-deployment')]}}]}
        secrets = {'items': [{'data': {key: base64.b64encode(value.encode()).decode() for key, value in
                    {'name': site['name'], 'server': ('https://cluster-proxy.svc:9092/' + site['name']) if proxied else site['server'],
                     'config': 'SENSITIVE-CONFIG-MUST-NOT-APPEAR'}.items()}} for site in profile['sites']]}
        hub = Mock()
        hub.get.side_effect = lambda kind, **kwargs: secrets if kind == 'secrets' else decisions
        return hub, profile, decisions, secrets

    def test_registered_names_work_with_proxy_or_direct_urls(self):
        for proxied in (True, False):
            hub, profile, _, _ = self.fixture(proxied)
            self.assertFalse(any(deploy.acm_registration_status(hub, profile).values()))
            deploy.wait_acm_registration(hub, profile, 1)

    def test_missing_named_registration_does_not_pass_on_a_direct_url_match(self):
        hub, profile, _, secrets = self.fixture(False)
        secrets['items'][0]['data']['name'] = base64.b64encode(b'wrong-cluster').decode()
        self.assertEqual(deploy.acm_registration_status(hub, profile)['missing_argo_clusters'], ['a'])

    def test_duplicate_names_are_rejected(self):
        hub, profile, _, secrets = self.fixture()
        secrets['items'].append(secrets['items'][0])
        with self.assertRaisesRegex(deploy.Failure, 'Multiple Argo cluster registrations for a'):
            deploy.wait_acm_registration(hub, profile, 1)

    def test_timeout_identifies_missing_decisions_and_registrations_without_secret_data(self):
        hub, profile, decisions, secrets = self.fixture()
        decisions['items'][0]['status']['decisions'] = [{'clusterName': 'a'}]
        secrets['items'] = secrets['items'][:1]
        with patch.object(deploy.time, 'monotonic', side_effect=[0, 0, 2]), patch.object(deploy.time, 'sleep'):
            with self.assertRaises(deploy.Failure) as caught:
                deploy.wait_acm_registration(hub, profile, 1)
        message = str(caught.exception)
        self.assertIn('"missing_placement_clusters": ["b"]', message)
        self.assertIn('"missing_argo_clusters": ["b"]', message)
        self.assertNotIn('SENSITIVE', message)

    def test_legacy_explicit_server_mode_still_requires_the_configured_url(self):
        hub, profile, decisions, _ = self.fixture()
        profile['automatic'] = False
        decisions['items'][0]['status']['decisions'] = [{'clusterName': n} for n in ('a', 'b')]
        self.assertEqual(deploy.acm_registration_status(hub, profile)['missing_argo_clusters'], ['a', 'b'])


class CertificateReadinessTests(unittest.TestCase):
    def certificate(self, generation, observed, kind, status, reason):
        return {'metadata': {'generation': generation}, 'status': {'conditions': [
            {'type': kind, 'status': status, 'reason': reason, 'observedGeneration': observed,
             'message': 'REMOTE-ISSUER-RESPONSE-MUST-NOT-BE-LOGGED'}]}}

    def test_current_terminal_failure_stops_without_waiting_for_timeout(self):
        kube = Mock();kube.get.return_value = self.certificate(1, 1, 'Issuing', 'False', 'Failed')
        with patch.object(deploy.time, 'sleep') as sleep:
            with self.assertRaises(deploy.Failure) as caught:
                deploy.wait_certificate(kube, 'tls', 'tenant', 900)
        self.assertIn('tenant/tls issuance failed (Failed)', str(caught.exception))
        self.assertNotIn('REMOTE', str(caught.exception))
        sleep.assert_not_called()

    def test_old_failure_does_not_reject_a_corrected_spec(self):
        kube = Mock();kube.get.side_effect = [self.certificate(2, 1, 'Issuing', 'False', 'Failed'),
                                            self.certificate(2, 2, 'Ready', 'True', 'Ready')]
        with patch.object(deploy.time, 'sleep'):
            deploy.wait_certificate(kube, 'tls', 'tenant', 900)
        self.assertEqual(kube.get.call_count, 2)

    def test_old_ready_condition_does_not_accept_a_new_spec(self):
        kube = Mock();kube.get.side_effect = [self.certificate(2, 1, 'Ready', 'True', 'Ready'),
                                            self.certificate(2, 2, 'Ready', 'True', 'Ready')]
        with patch.object(deploy.time, 'sleep'):
            deploy.wait_certificate(kube, 'tls', 'tenant', 900)
        self.assertEqual(kube.get.call_count, 2)

    def test_pending_issuance_can_complete(self):
        kube = Mock();kube.get.side_effect = [self.certificate(1, 1, 'Issuing', 'True', 'DoesNotExist'),
                                            self.certificate(1, 1, 'Ready', 'True', 'Ready')]
        with patch.object(deploy.time, 'sleep'):
            deploy.wait_certificate(kube, 'tls', 'tenant', 900)
        self.assertEqual(kube.get.call_count, 2)


class WorkflowTests(unittest.TestCase):
    def test_admin_probe_accepts_new_and_legacy_invalid_grant_statuses(self):
        run = deploy.Deployment.__new__(deploy.Deployment)
        for status in (400, 401):
            api = Mock();api.request.return_value = (status, {'error': 'invalid_grant'})
            self.assertIsNone(run.login(api, 'user', 'password', allow_invalid=True))
            self.assertIn(status, api.request.call_args.kwargs['expected'])
            with self.assertRaisesRegex(deploy.Failure, 'Administrator token request rejected'):
                run.login(api, 'user', 'password')

    def test_admin_probe_does_not_bootstrap_for_client_or_protocol_errors(self):
        run = deploy.Deployment.__new__(deploy.Deployment)
        for error in ('invalid_client', 'unauthorized_client', 'invalid_request', 'PRIVATE-RESPONSE', None):
            api = Mock();api.request.return_value = (400, {'error': error, 'error_description': 'PRIVATE-DESCRIPTION'})
            with self.assertRaises(deploy.Failure) as caught:
                run.login(api, 'user', 'password', allow_invalid=True)
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_successful_admin_login_requires_access_token(self):
        run = deploy.Deployment.__new__(deploy.Deployment)
        api = Mock();api.request.return_value = (200, {'access_token': 'test-token'})
        self.assertEqual(run.login(api, 'user', 'password'), 'test-token')
        api.request.return_value = (200, {})
        with self.assertRaises(deploy.Failure):
            run.login(api, 'user', 'password', allow_invalid=True)

    def test_keycloak_timeout_reports_container_state_without_embedded_logs(self):
        kube = Mock()
        kube.get.return_value = {'items': [{'metadata': {'name': 'keycloak-0'}, 'status': {
            'containerStatuses': [{'name': 'keycloak', 'restartCount': 7, 'ready': False,
                                   'state': {'waiting': {'reason': 'CrashLoopBackOff', 'message': 'PRIVATE-LOG'}}}]}}]}
        with patch.object(deploy, 'poll', side_effect=deploy.Failure('Timed out waiting for Keycloak Ready')):
            with self.assertRaises(deploy.Failure) as caught:
                deploy.wait_keycloak(kube, 'keycloak', 'tenant', 1)
        self.assertIn('CrashLoopBackOff', str(caught.exception))
        self.assertIn('keycloak-0', str(caught.exception))
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_keycloak_wait_ignores_ready_from_previous_generation(self):
        kube = Mock()
        def obj(observed):
            return {'metadata': {'generation': 2}, 'status': {'conditions': [
                {'type': 'Ready', 'status': 'True', 'observedGeneration': observed}]}}
        kube.get.side_effect = [obj(1), obj(2)]
        with patch.object(deploy.time, 'sleep'):
            deploy.wait_keycloak(kube, 'keycloak', 'tenant', 900)
        self.assertEqual(kube.get.call_count, 2)

    def test_kubernetes_error_preserves_reason_without_exposing_response(self):
        kube = deploy.Kube.__new__(deploy.Kube)
        kube.config = '/fixture/kubeconfig'
        response = Mock(returncode=1, stdout='', stderr='Error from server (Forbidden): private-response-with-secret')
        with patch.object(deploy.subprocess, 'run', return_value=response):
            with self.assertRaises(deploy.Failure) as caught:
                kube.get('nodes')
        self.assertEqual(str(caught.exception), 'Kubernetes command failed (Forbidden): get nodes -o')
        self.assertNotIn('private-response', str(caught.exception))

    def test_installplan_with_unreviewed_dependency_is_never_approved(self):
        kube = Mock()
        def get(kind, name, namespace, **kwargs):
            if kind.startswith('subscriptions'):
                return {'status': {'installPlanRef': {'name': 'install-1', 'uid': 'one'}}}
            if kind == 'csv':
                return None
            return {'metadata': {'uid': 'one'}, 'spec': {'clusterServiceVersionNames': ['keycloak-operator.v26.8.1', 'unexpected.v1']}}
        kube.get.side_effect = get
        with self.assertRaises(deploy.Failure):
            deploy.approve_plan(kube, 'keycloak', EXPECTED_CSV, 1)
        kube.patch.assert_not_called()

    def test_installplan_approval_uses_resource_version_guard(self):
        kube = Mock()
        calls = {'csv': 0}
        def get(kind, name, namespace, **kwargs):
            if kind.startswith('subscriptions'):
                return {'status': {'installPlanRef': {'name': 'install-1', 'uid': 'one'}}}
            if kind == 'csv':
                calls['csv'] += 1
                return None if calls['csv'] == 1 else {'status': {'phase': 'Succeeded'}}
            return {'metadata': {'name': 'install-1', 'uid': 'one', 'resourceVersion': '7'}, 'spec': {'approved': False, 'clusterServiceVersionNames': ['keycloak-operator.v26.8.1']}}
        kube.get.side_effect = get
        deploy.approve_plan(kube, 'keycloak', EXPECTED_CSV, 1)
        changes = kube.patch.call_args.args[3]
        self.assertEqual(changes[0], {'op': 'test', 'path': '/metadata/resourceVersion', 'value': '7'})

    def test_dns_does_not_take_over_unowned_record(self):
        api = Mock()
        desired = json.loads((ROOT / 'automation/cloudflare/record.json').read_text())
        api.request.return_value = (200, {'success': True, 'result': [{**desired, 'id': 'record', 'comment': 'another owner'}]})
        with self.assertRaises(deploy.Failure):
            deploy.reconcile_dns(api, 'zone', desired)
        self.assertEqual(api.request.call_count, 1)

    def test_dns_is_idempotent(self):
        api = Mock()
        desired = json.loads((ROOT / 'automation/cloudflare/record.json').read_text())
        api.request.return_value = (200, {'success': True, 'result': [{**desired, 'id': 'record'}]})
        self.assertFalse(deploy.reconcile_dns(api, 'zone', desired))
        self.assertEqual(api.request.call_count, 1)

    def test_externaldns_ownership_stops_publication(self):
        api = Mock()
        api.request.return_value = (200, {'success': True, 'result': [{'type': 'TXT', 'content': 'heritage=external-dns'}]})
        with self.assertRaises(deploy.Failure):
            deploy.reconcile_dns(api, 'zone', {'name': 'auth.example.com'})
        self.assertEqual(api.request.call_count, 1)

    def test_failed_configtest_never_reloads(self):
        api = Mock()
        api.request.return_value = (200, {'result': 'syntax failed'})
        with self.assertRaises(deploy.Failure):
            deploy.reconcile_opnsense(api, [], {'configtest_success': 'valid', 'reconfigure_success': 'ok'})
        self.assertEqual(api.request.call_count, 1)
        self.assertIn('configtest', api.request.call_args.args[1])

    def test_opnsense_unchanged_object_is_not_updated(self):
        api = Mock()
        api.request.side_effect = [(200, {'rows': [{'name': 'site-a', 'uuid': 'server-1'}]}),
                                   (200, {'server': {'name': 'site-a', 'mode': {'active': {'selected': 1}}}}),
                                   (200, {'result': 'valid'}), (200, {'status': 'ok'})]
        deploy.reconcile_opnsense(api, [{'type': 'server', 'name': 'site-a', 'fields': {'mode': 'active'}}],
                                 {'configtest_success': 'valid', 'reconfigure_success': 'ok'})
        self.assertFalse(any('/set_' in c.args[1] or '/add_' in c.args[1] for c in api.request.call_args_list))

    def test_unknown_reference_fails_before_it_is_used(self):
        with self.assertRaises(deploy.Failure):
            deploy.references('@server/missing', {})

    def test_plan_and_disabled_deploy_make_no_live_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ['scripts', 'gitops', 'automation']:
                shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copyfile(ROOT / '.gitignore', root / '.gitignore')
            gate = root / 'automation/aap/profiles/active-active.json'
            profile = json.loads(gate.read_text());profile['execution_enabled'] = False;profile['removal_enabled'] = False
            gate.write_text(json.dumps(profile))
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
            git('init', '-q');git('config', 'user.name', 'Test');git('config', 'user.email', 'test@example.invalid')
            git('add', '.');git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
            sha = git('rev-parse', 'HEAD')
            command = [sys.executable, str(root / 'scripts/deploy.py'), '--revision', sha, '--request-id', 'TEST-1']
            planned = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
            report = json.loads(planned.stdout)
            self.assertTrue(report['unresolved_inputs'])
            self.assertFalse(report['applied'])
            values = {name: default or 'fixture' for name, _, default in deploy.survey.FIELDS}
            values.update(backend_a='fd00::a', backend_b='fd00::b', proxy_cidr='fd00::1/128',
                          automation_cidr='fd00::2/128', admin_cidrs='fd00::/64',
                          backend_ca_uuid='6adf51e79db85',
                          public_cert_uuid='6adf557c923ca',
                          cloudflare_zone='a' * 32, opnsense_url='https://firewall.example.com',
                          database_ca=ssl.DER_cert_to_PEM_cert(ssl.create_default_context().get_ca_certs(binary_form=True)[0]))
            # Keep runtime survey input outside the immutable checkout.
            with tempfile.TemporaryDirectory() as inputs:
                params = Path(inputs) / 'parameters.json'
                params.write_text(json.dumps(values))
                configured = subprocess.run(command + ['--parameters', str(params)], capture_output=True, text=True)
                self.assertEqual(configured.returncode, 0, configured.stdout + configured.stderr)
                self.assertEqual(json.loads(configured.stdout)['unresolved_inputs'], [])
                self.assertFalse(json.loads(configured.stdout)['applied'])
            rejected = subprocess.run(command + ['--mode', 'deploy'], capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 1)
            self.assertIn('disabled', json.loads(rejected.stdout)['error'])
            removal = subprocess.run(command + ['--mode', 'remove', '--operation', 'remove', '--deployed-revision', sha], capture_output=True, text=True)
            self.assertEqual(removal.returncode, 1)
            self.assertIn('Removal is disabled', json.loads(removal.stdout)['error'])

    def test_failed_vip_smoke_blocks_dns_and_releases_lock(self):
        run = deploy.Deployment.__new__(deploy.Deployment)
        run.args = Mock(revision='a' * 40, request_id='TEST-1', lifecycle='initial-install')
        run.p = json.loads((ROOT / 'automation/aap/profiles/active-active.json').read_text())
        run.ns, run.hubns, run.timeout = 'keycloak', 'openshift-gitops', 1
        run.run_id, run.events = 'test-run', []
        run.manifests = Path('/unused')
        run.journal = Mock()
        run.edge = {'objects': []}
        run.edge_api, run.dns_api = Mock(), Mock()
        run.hub = Mock()
        good = {'metadata': {'generation': 1}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True', 'observedGeneration': 1}]}}
        def hub_get(kind, name=None, namespace=None, **kwargs):
            if kind == 'configmap': return {'metadata': {'uid': 'lock-uid'}}
            if kind == 'managedcluster':
                return {'metadata': {'labels': {'cluster.open-cluster-management.io/clusterset': 'default'}}, 'status': {'conditions': [{'type': t, 'status': 'True'} for t in ['ManagedClusterJoined', 'ManagedClusterConditionAvailable', 'ManagedClusterConditionClockSynced']]}}
            if kind.startswith('placementdecisions'):
                return {'items': [{'status': {'decisions': [{'clusterName': n} for n in ['cluster-a', 'cluster-b']]}}]}
            if kind == 'secrets':
                return {'items': [{'data': {'server': base64.b64encode(('https://api.' + n + '.example.com:6443').encode()).decode()}} for n in ['cluster-a', 'cluster-b']]}
            raise AssertionError(kind)
        run.hub.get.side_effect = hub_get
        def site_get(kind, name=None, namespace=None, **kwargs):
            if kind.startswith('subscriptions'): return None
            if kind == 'secret': return {'data': {k: 'ZHVtbXk=' for k in ['host', 'database', 'username', 'password']}}
            return good
        run.sites = {n: Mock() for n in ['cluster-a', 'cluster-b']}
        for k in run.sites.values(): k.get.side_effect = site_get
        run.sync, run.bootstrap = Mock(), Mock()
        run.load_external_credentials = Mock()
        run.backend = Mock(return_value=Mock())
        run.smoke = Mock(side_effect=[None, deploy.Failure('VIP failed')])
        with patch.object(deploy, 'approve_plan'), patch.object(deploy, 'reconcile_opnsense'), patch.object(deploy, 'reconcile_dns') as dns:
            with self.assertRaises(deploy.Failure): run.run()
            dns.assert_not_called()
        self.assertLess(run.events.index('runtime-cluster-a'), run.events.index('bootstrap-or-verify-permanent-admin'))
        self.assertLess(run.events.index('bootstrap-or-verify-permanent-admin'), run.events.index('runtime-cluster-b'))
        self.assertIn('delete', run.hub.command.call_args.args[0])
        self.assertEqual(run.hub.command.call_args.args[1]['preconditions']['uid'], 'lock-uid')

    def test_survey_contains_no_credential_fields(self):
        survey = json.loads((ROOT / 'automation/aap/controller/survey.json').read_text())
        self.assertEqual({x['variable'] for x in survey['spec']}, {'kc_cluster_a', 'kc_cluster_b', 'kc_public_vip', 'kc_public_host', 'kc_namespace', 'kc_database_path', 'kc_database_tls'})
        self.assertFalse(any(x['type'] == 'password' for x in survey['spec']))
        self.assertEqual(survey['spec'][0]['default'], '')


if __name__ == '__main__':
    unittest.main()
