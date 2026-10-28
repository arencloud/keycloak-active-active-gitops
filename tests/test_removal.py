import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy
import remove


class RemovalTests(unittest.TestCase):
    def args(self, **changes):
        values = dict(database_name='keycloak', confirm_removal='REMOVE auth.example.com',
                      backup_policy='verified-backup', backup_reference='backup-1', no_backup_confirmation='')
        values.update(changes)
        return SimpleNamespace(**values)

    def test_backup_is_required_by_default_and_blank_reference_is_rejected(self):
        legacy = self.args();del legacy.backup_policy
        self.assertEqual(remove.backup_attestation(legacy)['backup_policy'], 'verified-backup')
        for reference in ('', '   '):
            with self.assertRaises(deploy.Failure):
                remove.backup_attestation(self.args(backup_reference=reference))

    def test_no_backup_requires_explicit_policy_and_exact_database_confirmation(self):
        valid = self.args(backup_policy='no-backup', backup_reference='',
                          no_backup_confirmation='DELETE keycloak WITHOUT BACKUP')
        self.assertEqual(remove.backup_attestation(valid), {
            'backup_policy': 'no-backup', 'backup_reference': '', 'data_loss_acknowledged': True})
        for changes in ({'no_backup_confirmation': ''}, {'no_backup_confirmation': 'yes'},
                        {'no_backup_confirmation': 'DELETE another-db WITHOUT BACKUP'},
                        {'backup_policy': 'verified-backup'}, {'backup_policy': 'skip'},
                        {'backup_reference': 'backup-1'}, {'database_name': ''}):
            with self.subTest(changes=changes), self.assertRaises(deploy.Failure):
                remove.backup_attestation(SimpleNamespace(**{**vars(valid), **changes}))

    def test_no_backup_cannot_bypass_hostname_or_execution_gate(self):
        for enabled, confirmation in ((False, 'REMOVE auth.example.com'), (True, 'REMOVE other.example.com')):
            run = Mock(p={'removal_enabled': enabled, 'public_host': 'auth.example.com'})
            run.args = self.args(backup_policy='no-backup', backup_reference='',
                                 no_backup_confirmation='DELETE keycloak WITHOUT BACKUP', confirm_removal=confirmation)
            with self.assertRaises(deploy.Failure):remove.remove(run)
            run.load_external_credentials.assert_not_called()
            run.hub.command.assert_not_called()

    def test_invalid_backup_choice_stops_before_credentials_or_external_actions(self):
        run = Mock(p={'removal_enabled': True, 'public_host': 'auth.example.com'})
        run.args = self.args(backup_policy='no-backup', backup_reference='')
        with self.assertRaises(deploy.Failure):remove.remove(run)
        run.load_external_credentials.assert_not_called()
        run.hub.command.assert_not_called()
        run.journal.assert_not_called()

    def test_database_reset_rechecks_confirmation_and_records_no_backup_decision(self):
        run = Mock(p={'database_tls': 'disabled'})
        run.args = self.args(backup_policy='no-backup', backup_reference='',
                             no_backup_confirmation='DELETE keycloak WITHOUT BACKUP')
        data = {'host': 'db.example.com', 'database': 'keycloak', 'username': 'test', 'password': 'fixture-secret'}
        with patch.object(remove.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            remove.empty_database(run, data, None)
            self.assertIn('DROP SCHEMA public CASCADE', command.call_args.kwargs['input'])
        self.assertEqual(run.journal.call_args.args[0], {'system': 'database', 'operation': 'public-schema-reset',
            'backup_policy': 'no-backup', 'backup_reference': '', 'data_loss_acknowledged': True})
        for changes in ({'no_backup_confirmation': ''}, {'database_name': 'other', 'no_backup_confirmation': 'DELETE other WITHOUT BACKUP'}):
            run.args = self.args(backup_policy='no-backup', backup_reference='', **changes)
            with patch.object(remove.subprocess, 'run') as command, self.assertRaises(deploy.Failure):
                remove.empty_database(run, data, None)
            command.assert_not_called()

    def test_removal_survey_keeps_backup_default_and_allows_empty_conditional_fields(self):
        spec = json.loads((Path(__file__).resolve().parents[1] / 'automation/aap/controller/removal-survey.json').read_text())['spec']
        fields = {x['variable']: x for x in spec}
        self.assertEqual(fields['backup_policy']['default'], 'verified-backup')
        self.assertEqual(fields['backup_policy']['choices'], 'verified-backup\nno-backup')
        for name in ('backup_reference', 'no_backup_confirmation'):
            self.assertFalse(fields[name]['required'])
            self.assertEqual(fields[name]['min'], 0)
            self.assertEqual(fields[name]['default'], '')

    def test_disabled_removal_never_loads_credentials(self):
        run = Mock(p={'removal_enabled': False})
        with self.assertRaises(deploy.Failure): remove.remove(run)
        run.load_external_credentials.assert_not_called()

    def test_confirmation_mismatch_never_reads_secrets(self):
        run = Mock(p={'removal_enabled': True, 'public_host': 'auth.example.com'})
        run.args.confirm_removal = 'REMOVE another.example.com'
        with self.assertRaises(deploy.Failure): remove.remove(run)
        run.load_external_credentials.assert_not_called()

    def test_dns_same_name_different_vip_cannot_be_deleted(self):
        desired = {'type': 'AAAA', 'name': 'auth.example.com', 'content': 'fd00::70', 'proxied': False, 'comment': 'owner'}
        api = Mock()
        api.request.return_value = (200, {'success': True, 'result': [{**desired, 'content': 'fd00::71'}]})
        with self.assertRaises(deploy.Failure): remove.dns_candidate(api, 'zone', desired)
        self.assertEqual(api.request.call_count, 1)

    def test_shared_haproxy_reference_blocks_deletion(self):
        api = Mock()
        api.request.side_effect = [(200, {'rows': [{'name': 'keycloak-site-a', 'uuid': 'server-id'}]}),
                                   (200, {'server': {'name': 'keycloak-site-a', 'address': 'fd00::a'}}),
                                   (200, {'haproxy': {'servers': {'server': {'server-id': {'name': 'keycloak-site-a'}}},
                                                     'backends': {'backend': {'other-id': {'name': 'shared', 'linkedServers': 'server-id'}}}}})]
        with self.assertRaises(deploy.Failure):
            remove.edge_candidates(api, [{'type': 'server', 'name': 'keycloak-site-a', 'fields': {'address': 'fd00::a'}}])
        self.assertFalse(any('/del_' in c.args[1] for c in api.request.call_args_list))

    def test_delete_uses_uid_and_resource_version_preconditions(self):
        kube = Mock()
        kube.get.return_value = None
        obj = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'keycloak-external', 'namespace': 'keycloak', 'uid': 'identity', 'resourceVersion': '10'}}
        remove.delete_object(kube, obj, 1)
        self.assertEqual(kube.command.call_args.args[1]['preconditions'], {'uid': 'identity', 'resourceVersion': '10'})

    def test_database_dry_check_contains_no_destructive_sql(self):
        run = Mock(p={})
        data = {'host': 'db.example.com', 'database': 'keycloak', 'username': 'test', 'password': 'fixture-secret'}
        with patch.object(remove.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            remove.empty_database(run, data, '/ca.pem', check_only=True)
            sql = command.call_args.kwargs['input']
            self.assertNotIn('DROP SCHEMA', sql)
            self.assertNotIn('CREATE SCHEMA', sql)
            self.assertNotIn('fixture-secret', sql)
            self.assertNotIn('fixture-secret', str(command.call_args.args))
            self.assertEqual(command.call_args.kwargs['env']['PGSSLMODE'], 'verify-full')
        run.journal.assert_not_called()

    def test_database_failure_never_reports_reset(self):
        run = Mock(p={})
        run.args = self.args()
        data = {'host': 'db.example.com', 'database': 'keycloak', 'username': 'test', 'password': 'fixture-secret'}
        with patch.object(remove.subprocess, 'run', return_value=Mock(returncode=1)), self.assertRaises(deploy.Failure):
            remove.empty_database(run, data, '/ca.pem')
        run.journal.assert_not_called()

    def test_database_removal_uses_recorded_disabled_tls_and_ignores_environment(self):
        run = Mock(p={'database_tls': 'disabled'})
        data = {'host': 'db.example.com', 'database': 'keycloak', 'username': 'test', 'password': 'fixture-secret'}
        with patch.dict(remove.os.environ, {'PGSSLMODE': 'prefer', 'PGSSLROOTCERT': '/unrelated/ca', 'PGSERVICE': 'other'}), patch.object(remove.subprocess, 'run', return_value=Mock(returncode=0)) as command:
            remove.empty_database(run, data, None, check_only=True)
            env = command.call_args.kwargs['env']
            self.assertEqual(env['PGSSLMODE'], 'disable')
            self.assertNotIn('PGSSLROOTCERT', env)
            self.assertNotIn('PGSERVICE', env)
            self.assertNotIn('DROP SCHEMA', command.call_args.kwargs['input'])
        run.journal.assert_not_called()

    def test_runtime_stop_failure_prevents_database_reset_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'applications.yaml').write_text('')
            run = Mock()
            run.p = {'removal_enabled': True, 'public_host': 'auth.example.com', 'public_vip': 'fd00::70',
                     'sites': [{'name': 'cluster-a'}, {'name': 'cluster-b'}], 'cloudflare': {'zone_id': 'zone'},
                     'expected_csv': 'keycloak-operator.v26.8.1', 'opnsense': {'configtest_success': 'valid', 'reconfigure_success': 'ok'}}
            run.args.confirm_removal = 'REMOVE auth.example.com'
            run.args.backup_reference, run.args.database_name = 'backup-1', 'keycloak'
            run.args.backup_policy = 'verified-backup'
            run.args.haproxy_removal_action = 'reload'
            run.run_id, run.hubns, run.ns, run.timeout = 'run-1', 'openshift-gitops', 'keycloak', 0.01
            run.manifests = run.credentials_dir = folder
            run.hub.get.return_value = {'metadata': {'uid': 'lock-1'}}
            def get(kind, *args, **kwargs):
                if kind == 'configmap': return {'data': {'db-ca.pem': 'test-ca'}}
                if kind == 'csv': return None
                if kind == 'pods': return {'items': [{'metadata': {'name': 'still-running'}}]}
                return {'items': []}
            run.sites = {name: Mock() for name in ('cluster-a', 'cluster-b')}
            for kube in run.sites.values(): kube.get.side_effect = get
            run.edge = {'objects': []}
            run.edge_api.request.side_effect = [(200, {'result': 'valid'}), (200, {'status': 'ok'})]
            with patch.object(remove, 'database_values', return_value={}), patch.object(remove, 'dns_candidate', return_value=None), patch.object(remove, 'edge_candidates', return_value=[]), patch.object(remove, 'empty_database') as database, patch.object(remove, 'finalize_haproxy'), patch.object(remove, 'protected_haproxy_config', return_value={}), patch.object(remove, 'shared_service_state', return_value='running'), patch.object(deploy.time, 'sleep'):
                with self.assertRaises(deploy.Failure): remove.remove(run)
                self.assertEqual(database.call_count, 1)
                self.assertTrue(database.call_args.kwargs['check_only'])
            self.assertEqual(run.hub.command.call_args.args[1]['preconditions']['uid'], 'lock-1')

    def test_last_frontend_of_running_service_blocks_before_writes(self):
        run = Mock()
        run.edge_api.request.side_effect = [(200, {'rows': [{'uuid': 'ours', 'enabled': '1'}], 'total': 1}), (200, {'status': 'running'})]
        with self.assertRaises(deploy.Failure):
            remove.shared_service_state(run, [('frontend', 'ours', {})])
        self.assertFalse(any(c.args[1].endswith(('/settings/set', '/service/reconfigure', '/service/stop')) for c in run.edge_api.request.call_args_list))

    def test_other_enabled_frontend_allows_project_removal(self):
        run = Mock()
        run.edge_api.request.side_effect = [(200, {'rows': [{'uuid': 'ours', 'enabled': '1'}, {'uuid': 'other', 'enabled': '1'}], 'total': 2}), (200, {'status': 'running'})]
        self.assertEqual(remove.shared_service_state(run, [('frontend', 'ours', {})]), 'running')

    def test_stopped_service_is_never_started_or_reconfigured(self):
        for status in ('stopped', 'disabled'):
            run = Mock()
            run.args.haproxy_removal_action = 'stop-start'
            with patch.object(remove, 'protected_haproxy_config', return_value={}), patch.object(remove, 'shared_service_state', return_value=status):
                remove.finalize_haproxy(run, {}, [('frontend', 'project-id', {})], status)
            run.edge_api.request.assert_not_called()

    def test_unrelated_config_drift_blocks_reload(self):
        run = Mock()
        with patch.object(remove, 'protected_haproxy_config', return_value={'unrelated': 'changed'}), self.assertRaises(deploy.Failure):
            remove.finalize_haproxy(run, {'unrelated': 'original'}, [], 'running')
        run.edge_api.request.assert_not_called()

    def test_running_shared_service_only_validates_and_reloads(self):
        api = Mock()
        run = Mock(p={'opnsense': {'configtest_success': 'valid', 'reconfigure_success': 'ok'}})
        run.edge_api = remove.RemovalEdgeAPI(api, [])
        run.args.haproxy_removal_action = 'reload'
        run.timeout = 1
        api.request.side_effect = [(200, {'result': 'valid'}), (200, {'status': 'ok'}), (200, {'status': 'running'})]
        with patch.object(remove, 'protected_haproxy_config', return_value={}), patch.object(remove, 'shared_service_state', return_value='running'):
            remove.finalize_haproxy(run, {}, [('frontend', 'project-id', {})], 'running')
        self.assertEqual([(c.args[0], c.args[1]) for c in api.request.call_args_list], [('GET', '/api/haproxy/service/configtest'), ('POST', '/api/haproxy/service/reconfigure'), ('GET', '/api/haproxy/service/status')])

    def test_stop_start_restores_service_on_success_and_failures(self):
        for failure in (None, 'stop-response-lost', 'reconfigure-failed', 'start-response-lost', 'restart-failed', 'invalid-config'):
            with self.subTest(failure=failure):
                api = Mock()
                run = Mock(p={'opnsense': {'configtest_success': 'valid', 'reconfigure_success': 'ok'}})
                run.args.haproxy_removal_action = 'stop-start'
                run.timeout = 0.001
                run.edge_api = remove.RemovalEdgeAPI(api, [], 'stop-start')
                state = {'status': 'running'}
                def request(method, path, *args):
                    action = path.rsplit('/', 1)[-1]
                    if action == 'configtest':
                        return 200, {'result': 'invalid' if failure == 'invalid-config' else 'valid'}
                    if action == 'status': return 200, dict(state)
                    if action == 'stop':
                        state['status'] = 'stopped'
                        if failure == 'stop-response-lost': raise OSError('connection lost')
                    if action == 'reconfigure':
                        return 200, {'status': 'failed' if failure == 'reconfigure-failed' else 'ok'}
                    if action == 'start':
                        if failure != 'restart-failed': state['status'] = 'running'
                        if failure == 'start-response-lost': raise OSError('connection lost')
                    return 200, {'response': 'OK'}
                api.request.side_effect = request
                with patch.object(remove, 'protected_haproxy_config', return_value={}), patch.object(remove, 'shared_service_state', return_value='running'), patch.object(deploy.time, 'sleep'):
                    if failure in ('stop-response-lost', 'reconfigure-failed', 'restart-failed', 'invalid-config'):
                        with self.assertRaises((deploy.Failure, OSError)):
                            remove.finalize_haproxy(run, {}, [('frontend', 'project-id', {})], 'running')
                    else:
                        remove.finalize_haproxy(run, {}, [('frontend', 'project-id', {})], 'running')
                paths = [c.args[1] for c in api.request.call_args_list]
                if failure == 'invalid-config':
                    self.assertNotIn('/api/haproxy/service/stop', paths)
                else:
                    self.assertIn('/api/haproxy/service/start', paths)
                self.assertEqual(state['status'], 'stopped' if failure == 'restart-failed' else 'running')
                self.assertNotIn('/api/haproxy/settings/set', paths)

    def test_stop_start_guard_still_blocks_unrelated_writes(self):
        api = Mock()
        guarded = remove.RemovalEdgeAPI(api, [], 'stop-start')
        for path in ('/api/haproxy/service/stop', '/api/haproxy/service/start'):
            guarded.request('POST', path, {})
        for path in ('/api/haproxy/settings/set', '/api/haproxy/service/restart', '/api/haproxy/settings/del_frontend/other', '/v1/kv/destroy/global/opnsense'):
            with self.assertRaises(deploy.Failure): guarded.request('POST', path, {})
        self.assertEqual(api.request.call_count, 2)

    def test_api_guard_blocks_global_changes_other_app_deletion_and_vault_writes(self):
        api = Mock()
        guarded = remove.RemovalEdgeAPI(api, [('server', 'our-id', {})])
        for method, path in [('POST', '/api/haproxy/settings/set'), ('POST', '/api/haproxy/service/stop'),
                             ('POST', '/api/haproxy/service/start'), ('POST', '/api/haproxy/service/restart'),
                             ('POST', '/api/haproxy/settings/del_server/other-id'),
                             ('DELETE', '/v1/kv/data/global/opnsense'), ('POST', '/v1/kv/destroy/global/opnsense')]:
            with self.subTest(path=path), self.assertRaises(deploy.Failure): guarded.request(method, path, {})
        api.request.assert_not_called()
        guarded.request('POST', '/api/haproxy/settings/del_server/our-id', {})
        api.request.assert_called_once()

    def test_unknown_project_object_is_rejected_before_api_lookup(self):
        api = Mock()
        with self.assertRaises(deploy.Failure):
            remove.edge_candidates(api, [{'type': 'frontend', 'name': 'another-app', 'fields': {}}])
        api.request.assert_not_called()

    def test_protected_snapshot_preserves_other_apps_and_ignores_unused_options(self):
        api = Mock()
        common = {'general': {'enabled': '1'}, 'frontends': {'frontend': {'other': {'name': 'other-app', 'defaultBackend': {'backend-ours': {'selected': 0}, 'backend-other': {'selected': 1}}}}}}
        before = {**common, 'backends': {'backend': {'backend-ours': {'name': 'keycloak-active-active'}, 'backend-other': {'name': 'other-backend'}}}}
        after = {**common, 'backends': {'backend': {'backend-other': {'name': 'other-backend'}}}}
        api.request.side_effect = [(200, {'haproxy': before}), (200, {'haproxy': after})]
        candidates = [('backend', 'backend-ours', {})]
        self.assertEqual(remove.protected_haproxy_config(api, candidates), remove.protected_haproxy_config(api, candidates))

    def test_unowned_generated_secret_blocks_removal(self):
        kube = Mock()
        kube.get.return_value = {'metadata': {'uid': 'unrelated'}, 'data': {'password': 'hidden'}}
        es = {'kind': 'ExternalSecret', 'metadata': {'uid': 'es-owner'}, 'spec': {'target': {'name': 'keycloak-db'}}}
        with self.assertRaises(deploy.Failure): remove.generated_secrets([(kube, es)], 'keycloak')

    def test_generated_secret_inventory_excludes_values(self):
        kube = Mock()
        kube.get.return_value = {'metadata': {'uid': 'secret-id', 'ownerReferences': [{'uid': 'es-owner'}]}, 'data': {'password': 'hidden'}}
        es = {'kind': 'ExternalSecret', 'metadata': {'uid': 'es-owner'}, 'spec': {'target': {'name': 'keycloak-db'}}}
        result = remove.generated_secrets([(kube, es)], 'keycloak')
        self.assertNotIn('data', result[0][1])

    def test_selected_uuid_and_custom_option_references_block_removal(self):
        for other_fields in ({'linkedServers': {'server-id': {'selected': 1, 'value': 'Keycloak'}}},
                             {'advanced': 'use-server keycloak-site-a if needed'}):
            with self.subTest(fields=other_fields):
                api = Mock()
                api.request.side_effect = [(200, {'rows': [{'name': 'keycloak-site-a', 'uuid': 'server-id'}]}),
                    (200, {'server': {'name': 'keycloak-site-a', 'address': 'fd00::a'}}),
                    (200, {'haproxy': {'servers': {'server': {'server-id': {'name': 'keycloak-site-a'}}},
                                      'backends': {'backend': {'other-id': {'name': 'another-app', **other_fields}}}}})]
                with self.assertRaises(deploy.Failure):
                    remove.edge_candidates(api, [{'type': 'server', 'name': 'keycloak-site-a', 'fields': {'address': 'fd00::a'}}])
                self.assertFalse(any('/del_' in c.args[1] for c in api.request.call_args_list))

    def test_absent_project_objects_never_trigger_shared_reload(self):
        run = Mock()
        with patch.object(remove, 'protected_haproxy_config', return_value={}), patch.object(remove, 'shared_service_state', return_value='running'):
            remove.finalize_haproxy(run, {}, [], 'running')
        run.edge_api.request.assert_not_called()
