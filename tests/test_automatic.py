import base64
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import automatic
import discovery
import deploy
import edge_automatic
import instances


def pool(subnet='500', name='machine-network-last-50'):
    return {'metadata': {'name': name, 'namespace': 'metallb-system'}, 'spec': {'addresses': [f'2001:db8:{subnet}:0:ffff:ffff:ffff:ffce-2001:db8:{subnet}:0:ffff:ffff:ffff:ffff']}}


class DiscoveryTests(unittest.TestCase):
    def test_lifecycle_resumes_only_recorded_incomplete_installations(self):
        partial = [{'existing_runtime': True}, {'existing_runtime': False}]
        with self.assertRaises(deploy.Failure):
            automatic.deployment_lifecycle(partial)
        legacy = {'profile': {'sites': [{'existing_runtime': False}, {'existing_runtime': False}]}}
        self.assertEqual(automatic.deployment_lifecycle(partial, legacy), 'initial-install')
        complete = [{'existing_runtime': True}, {'existing_runtime': True}]
        self.assertEqual(automatic.deployment_lifecycle(complete, {**legacy, 'installation_complete': False}), 'initial-install')
        self.assertEqual(automatic.deployment_lifecycle(complete, {**legacy, 'installation_complete': True}), 'reconcile')
        for state in ({**legacy, 'installation_complete': True}, {'profile': {'sites': complete}}, {'profile': {}}):
            with self.assertRaises(deploy.Failure):
                automatic.deployment_lifecycle(partial, state)
        self.assertEqual(automatic.deployment_lifecycle(legacy['profile']['sites']), 'initial-install')

    def test_destination_guard_accepts_names_and_legacy_urls_without_allowing_relocation(self):
        site = {'name': 'site-a', 'server': 'https://api.site-a.example:6443'}
        for identity in ({'name': 'site-a'}, {'server': site['server']}):
            self.assertTrue(automatic.destination_matches_site({**identity, 'namespace': 'tenant'}, site, 'tenant'))
        for destination in ({'name': 'site-b', 'namespace': 'tenant'},
                            {'name': 'site-a', 'namespace': 'other'},
                            {'name': 'site-a', 'server': site['server'], 'namespace': 'tenant'},
                            {'server': 'https://other.example:6443', 'namespace': 'tenant'},
                            {'namespace': 'tenant'}):
            self.assertFalse(automatic.destination_matches_site(destination, site, 'tenant'))

    def test_pool_must_be_unique_ipv6_and_automatic(self):
        p = pool()
        self.assertEqual(discovery.choose_pool([p], 'other'), p)
        with self.assertRaises(deploy.Failure): discovery.choose_pool([p, pool(name='second')], 'absent')
        with self.assertRaises(deploy.Failure): discovery.choose_pool([{**p, 'spec': {'addresses': ['192.0.2.0/24']}}], 'absent')
        with self.assertRaises(deploy.Failure): discovery.choose_pool([{**p, 'spec': {**p['spec'], 'autoAssign': False}}], p['metadata']['name'])

    def test_ready_cluster_list_excludes_hub_and_unavailable(self):
        def cluster(name): return {'metadata': {'name': name}, 'status': {'conditions': [{'type': c, 'status': 'True'} for c in discovery.CONDITIONS]}}
        bad = cluster('unavailable');bad['status']['conditions'][1]['status'] = 'False'
        self.assertEqual(discovery.eligible_clusters([cluster('local-cluster'), cluster('third-site'), bad, cluster('first-site')]), ['first-site', 'third-site'])

    def test_source_requires_connected_interface_and_route(self):
        interfaces = {'vlan500': {'flags': ['up'], 'ipv6': [{'ipaddr': '2001:db8:500::1', 'subnetbits': 64}]}}
        self.assertEqual(discovery.router_source(interfaces, pool()), ('vlan500', '2001:db8:500::1'))
        with self.assertRaises(deploy.Failure): discovery.router_source(interfaces, pool('600'))
        duplicate = {**interfaces, 'other': interfaces['vlan500']}
        with self.assertRaises(deploy.Failure): discovery.router_source(duplicate, pool())
        discovery.connected_route([{'proto': 'ipv6', 'destination': '2001:db8:500::/64', 'gateway': 'link#1', 'netif': 'vlan500'}], 'vlan500', pool())
        with self.assertRaises(deploy.Failure): discovery.connected_route([{'proto': 'ipv6', 'destination': 'default', 'gateway': '2001:db8::1', 'netif': 'vlan500'}], 'vlan500', pool())

    def test_metallb_assigned_status_is_the_backend_authority(self):
        self.assertIsNone(automatic.service_ip({}, pool()))
        svc = {'status': {'loadBalancer': {'ingress': [{'ip': '2001:db8:500:0:ffff:ffff:ffff:ffce'}]}}}
        self.assertEqual(automatic.service_ip(svc, pool()), '2001:db8:500:0:ffff:ffff:ffff:ffce')
        with self.assertRaises(deploy.Failure): automatic.service_ip(svc, pool('600'))

    def test_edge_inventory_and_interface_mapping_are_fail_closed(self):
        api = Mock();api.request.return_value = (200, {'rows': [], 'total': 2})
        with self.assertRaises(deploy.Failure): edge_automatic.inventory(api, '/search')
        api.request.return_value = (200, {'interface': {'interface': {'opt1': {'if': {'vmx1': {'selected': 1}, 'vmx0': {'selected': 0}}}}}})
        self.assertEqual(edge_automatic.interface_id(api, 'vmx1'), 'opt1')

    def test_ipv6_source_port_syntax_does_not_leak_into_trusted_networks(self):
        run = automatic.AutomaticDeployment.__new__(automatic.AutomaticDeployment)
        site = {'overlay': 'cluster-a', 'pool': pool()}
        run.p = {'resource_prefix': 'tenant', 'public_vip': '2001:db8:10:7::70', 'sites': [site]}
        run.ns, run.hubns = 'tenant', 'openshift-gitops'
        run.hub, run.save_state, run.edge_api = Mock(), Mock(), Mock()
        run.edge_api.request.return_value = (200, {})
        run.edge = {'objects': [{'name': 'tenant-site-a', 'fields': {}}]}
        run.found = {'hub_sources': ['2001:db8:500::/48']}
        policy = {'kind': 'NetworkPolicy', 'spec': {'ingress': [{'from': []}]}}
        keycloak = {'kind': 'Keycloak', 'spec': {'additionalOptions': [{'name': 'proxy-trusted-addresses', 'value': 'unset'}]}}
        run.documents = {'cluster-a': [policy, keycloak], 'applications': [{'metadata': {'name': 'tenant-keycloak-cluster-a'}, 'spec': {'source': {}}}]}
        run.originals = {'cluster-a': []}
        with tempfile.TemporaryDirectory() as tmp, patch.object(discovery, 'public_interface', return_value='wan'), patch.object(discovery, 'router_source', return_value=('vlan500', '2001:db8:500::1')), patch.object(discovery, 'connected_route'), patch.object(edge_automatic, 'check_listener_collision'):
            run.manifests = Path(tmp)
            run.prepare_network()
        self.assertEqual(run.edge['objects'][0]['fields']['source'], '2001:db8:500::1:')
        self.assertEqual(site['proxy_source'], '2001:db8:500::1')
        self.assertEqual(keycloak['spec']['additionalOptions'][0]['value'], '2001:db8:500::1')
        self.assertEqual(policy['spec']['ingress'][0]['from'][0]['ipBlock']['cidr'], '2001:db8:500::1/128')

    def test_vip_reuse_never_modifies_other_interface(self):
        api = Mock();api.request.side_effect = [(200, {'interface': {'interface': {'opt1': {'if': {'vmx1': {'selected': 1}}}}}}), (200, {'rows': [{'uuid': 'existing', 'subnet': '2001:db8:10:7::70'}]}), (200, {'vip': {'interface': 'opt9', 'mode': 'ipalias'}})]
        with self.assertRaises(deploy.Failure): edge_automatic.ensure_vip(api, '2001:db8:10:7::70', 'vmx1', Mock())
        self.assertTrue(all(x.args[0] == 'GET' for x in api.request.call_args_list))


class RenderingTests(unittest.TestCase):
    def test_issuer_common_name_limit_fails_before_rendering(self):
        with patch.object(automatic.subprocess, 'run') as render:
            with self.assertRaisesRegex(deploy.Failure, 'Common Name of at most 64'):
                automatic.artifacts({'public_host': 'a' * 60 + '.example.com'}, {}, {}, 'a' * 40, Path('/unused'))
            render.assert_not_called()

    def test_arbitrary_pair_and_per_site_patches_match_all_six_argo_outputs(self):
        self.check_render('enabled')

    def test_disabled_database_tls_matches_all_six_argo_outputs(self):
        self.check_render('disabled')

    def check_render(self, tls):
        platform = json.loads((ROOT / 'automation/aap/profiles/platform.json').read_text())
        profile = json.loads((ROOT / 'automation/aap/profiles/active-active.json').read_text())
        platform['repository_url'] = 'https://git.custom.example/team/keycloak.git'
        profile['public_host'] = 'login.custom.example'
        profile['database_tls'] = tls
        instances.configure(profile, platform, 'tenant-prerequisites', 'databases/tenant-a')
        sites = [{'name': 'third-site', 'server': 'https://api.third.example:6443', 'overlay': 'cluster-a', 'pool': pool(), 'issuer': 'issuer-third', 'secret_store': 'store-third'}, {'name': 'first-site', 'server': 'https://api.first.example:6443', 'overlay': 'cluster-b', 'pool': pool('600'), 'issuer': 'issuer-first', 'secret_store': 'store-first'}]
        found = {'sites': sites, 'hub_sources': ['2001:db8:500::/48', '2001:db8:100::2/128'], 'ca': 'PUBLIC CA ONLY', 'cluster_set': 'new-set'}
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            original, documents, edge = automatic.artifacts(profile, found, platform, 'a' * 40, folder)
            rendered = yaml.safe_dump(documents) + json.dumps(edge)
            self.assertNotIn('auth.example.com', rendered)
            for token in ('REPLACE_ME_PUBLIC_FQDN', 'REPLACE_ME_PUBLIC_IPV6', 'REPLACE_ME_CLUSTER_ISSUER', 'REPLACE_ME_CLUSTER_SECRET_STORE'):
                self.assertNotIn(token, rendered)
            self.assertEqual(documents['hub-project'][0]['spec']['destinations'],
                             [{'name': site['name'], 'namespace': profile['namespace']} for site in sites])
            self.assertEqual(documents['hub-project'][0]['spec']['sourceRepos'], [platform['repository_url']])
            for app in documents['applications']:
                self.assertEqual(app['spec']['source']['repoURL'], platform['repository_url'])
                site = next(site for site in sites if app['metadata']['name'].endswith(site['overlay']))
                self.assertEqual(app['spec']['destination'], {'name': site['name'], 'namespace': profile['namespace']})
            self.assertEqual(deploy.dns_record(profile)['name'], 'login.custom.example')
            for site in sites:
                runtime = documents[site['overlay']]
                svc = next(x for x in runtime if x['kind'] == 'Service')
                self.assertNotIn('metallb.io/loadBalancerIPs', svc['metadata']['annotations'])
                kc = next(x for x in runtime if x['kind'] == 'Keycloak')
                self.assertNotIn('bootstrapAdmin', kc['spec'])
                java = next(e['value'] for e in kc['spec']['env'] if e['name'] == 'JAVA_OPTS_APPEND')
                self.assertIn('-Djava.net.preferIPv4Stack=false', java)
                self.assertIn('-Djava.net.preferIPv6Addresses=true', java)
                options = {x['name']: x.get('value') for x in kc['spec']['additionalOptions']}
                self.assertEqual(options['db-tls-mode'], 'verify-server' if tls == 'enabled' else 'disabled')
                self.assertEqual(kc['spec']['http'], {'httpEnabled': False, 'tlsSecret': 'keycloak-tls'})
                if tls == 'disabled':
                    self.assertEqual(options['db-url-properties'], '?sslmode=disable')
                    self.assertNotIn('truststores', kc['spec'])
                else:
                    self.assertNotIn('db-url-properties', options)
                    self.assertIn('db', kc['spec']['truststores'])
                self.assertEqual(kc['spec']['hostname']['hostname'], 'https://login.custom.example')
                self.assertEqual(next(x for x in kc['spec']['additionalOptions'] if x['name'] == 'cache-embedded-cluster-name')['value'], site['name'])
                prereq = documents['prerequisites-' + site['overlay']]
                self.assertEqual(any(x['metadata']['name'] == 'keycloak-db-rootcert' for x in prereq), tls == 'enabled')
                self.assertFalse(any(x['metadata']['name'] == 'keycloak-bootstrap' for x in prereq))
                certificate = next(x for x in prereq if x['kind'] == 'Certificate')
                self.assertEqual(certificate['spec']['dnsNames'], ['login.custom.example'])
                self.assertEqual(certificate['spec']['commonName'], 'login.custom.example')
                self.assertEqual(certificate['spec']['privateKey'],
                                 {'algorithm': 'ECDSA', 'size': 256, 'encoding': 'PKCS8', 'rotationPolicy': 'Always'})
                es = next(x for x in prereq if x['metadata']['name'] == 'keycloak-automation')
                self.assertEqual({x['secretKey'] for x in es['spec']['data']}, {'opnsense_url', 'opnsense_key', 'opnsense_secret', 'cloudflare_token'})
            for i, app in enumerate(documents['applications']):
                checkout = folder / str(i)
                shutil.copytree(ROOT / 'gitops', checkout / 'gitops')
                src = app['spec']['source'];directory = checkout / src['path']
                kpath = directory / 'kustomization.yaml';k = yaml.safe_load(kpath.read_text())
                k['namespace'] = src['kustomize']['namespace']
                k.setdefault('patches', []).extend(src['kustomize']['patches']);kpath.write_text(yaml.safe_dump(k))
                actual = list(yaml.safe_load_all(subprocess.check_output(['kubectl', 'kustomize', str(directory)], text=True)))
                overlay = app['metadata']['name'][-9:]
                target = next(n for n, path in automatic.render.TARGETS.items() if path == src['path'])
                if target == 'prerequisites': target += '-' + overlay
                self.assertEqual(actual, documents[target])
                self.assertNotIn('REPLACE_ME_', yaml.safe_dump(actual))
                for obj in actual:
                    self.assertEqual(obj['metadata']['name'] if obj['kind'] == 'Namespace' else obj['metadata']['namespace'], profile['namespace'])
                    if obj['kind'] == 'OperatorGroup': self.assertEqual(obj['spec']['targetNamespaces'], [profile['namespace']])
                    if obj['kind'] == 'ExternalSecret' and obj['metadata']['name'] == 'keycloak-db':
                        self.assertEqual({v['remoteRef']['key'] for v in obj['spec']['data']}, {'databases/tenant-a'})

class DatabaseTransportTests(unittest.TestCase):
    def runner(self, mode):
        run = automatic.AutomaticDeployment.__new__(automatic.AutomaticDeployment)
        run.p = {'database_tls': mode, 'sites': [{'name': 'a'}]}
        run.ns = 'tenant'; run.ca = '/fixture/ca.pem'; run.journal = Mock()
        run.sites = {'a': Mock()}
        run.sites['a'].get.return_value = {'data': {'host': base64.b64encode(b'db.example.com').decode()}}
        return run

    def probe(self, run, reply=b'S', failure=None):
        raw = MagicMock(); raw.__enter__.return_value = raw; raw.recv.return_value = reply
        context = MagicMock(); context.wrap_socket.side_effect = failure
        with patch.object(automatic.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('::1', 5432, 0, 0))]), patch.object(automatic.socket, 'socket', return_value=raw), patch.object(automatic.ssl, 'create_default_context', return_value=context) as tls_context:
            if failure or (run.p['database_tls'] == 'enabled' and reply != b'S'):
                with self.assertRaises(deploy.Failure): run.verify_database_connection()
                run.journal.assert_not_called()
            else:
                run.verify_database_connection()
            if run.p['database_tls'] == 'disabled':
                tls_context.assert_not_called();raw.sendall.assert_not_called()
                self.assertEqual(run.journal.call_args.args[0]['operation'], 'verified-ipv6-tcp')
            else:
                tls_context.assert_called_once_with(cafile=run.ca)
                if reply == b'S': context.wrap_socket.assert_called_once_with(raw, server_hostname='db.example.com')
            raw.connect.assert_called_once()

    def test_enabled_tls_checks_certificate_and_hostname(self):
        self.probe(self.runner('enabled'))

    def test_enabled_tls_fails_if_server_refuses_tls(self):
        self.probe(self.runner('enabled'), reply=b'N')

    def test_certificate_failure_never_retries_without_tls(self):
        self.probe(self.runner('enabled'), failure=automatic.ssl.SSLCertVerificationError('fixture'))

    def test_disabled_tls_checks_ipv6_without_tls_negotiation(self):
        self.probe(self.runner('disabled'))

    def test_old_records_require_tls_and_invalid_modes_fail(self):
        self.assertEqual(deploy.database_tls({}), 'enabled')
        with self.assertRaises(deploy.Failure): deploy.database_tls({'database_tls': 'prefer'})
        with self.assertRaisesRegex(deploy.Failure, 'TLS mode changed'):
            discovery.deployment_identity({}, {'database_tls': 'disabled'})


class OwnershipTests(unittest.TestCase):
    def test_l2_selectors_do_not_advertise_an_unrelated_pool(self):
        p = pool();p['metadata']['labels'] = {'site': 'a'}
        self.assertFalse(discovery.advertises_pool({'spec': {'ipAddressPoolSelectors': [{'matchLabels': {'site': 'b'}}]}}, p))
        self.assertTrue(discovery.advertises_pool({'spec': {'ipAddressPoolSelectors': [{'matchLabels': {'site': 'a'}}]}}, p))

    def test_changed_firewall_rule_is_never_deleted(self):
        api = Mock();api.request.return_value = (200, {'rule': {'description': 'other-app'}})
        with self.assertRaises(deploy.Failure):
            edge_automatic.remove_public_rule(api, {'uuid': 'owned', 'fields': {'description': 'keycloak-active-active/auth.example'}}, Mock())
        self.assertEqual(api.request.call_count, 1)
        self.assertEqual(api.request.call_args.args[0], 'GET')

    def test_scoped_firewall_removal_deletes_only_recorded_uuid(self):
        api = Mock();fields = {'description': 'keycloak-active-active/auth.example', 'destination_net': '2001:db8:10:7::70', 'destination_port': '443'}
        api.request.side_effect = [(200, {'rule': fields}), (200, {'result': 'deleted'}), (200, {'status': 'OK\n'})]
        edge_automatic.remove_public_rule(api, {'uuid': 'owned', 'fields': fields}, Mock())
        self.assertEqual([(x.args[0], x.args[1]) for x in api.request.call_args_list], [('GET', '/api/firewall/filter/get_rule/owned'), ('POST', '/api/firewall/filter/del_rule/owned'), ('POST', '/api/firewall/filter/apply')])

    def test_removal_requires_a_record_and_matching_environment(self):
        import automatic_remove
        hub = Mock();hub.get.return_value = None
        platform = {'state_configmap': 'keycloak-state', 'environment': 'active-active'}
        with self.assertRaises(deploy.Failure): automatic_remove.load_state(hub, platform)
        hub.get.return_value = {'data': {'state.json': json.dumps({'profile': {'automatic': True, 'environment': 'another-project', 'public_host': 'foreign.example'}})}}
        with self.assertRaises(deploy.Failure): automatic_remove.load_state(hub, platform)

    def test_owned_admin_inventory_never_contains_secret_data(self):
        run = automatic.AutomaticDeployment.__new__(automatic.AutomaticDeployment)
        run.platform = {'recovery_secret': 'keycloak-workflow-admin'};run.ns = 'keycloak';run.p = {'public_host': 'auth.example'}
        kube = Mock();run.sites = {'site': kube}
        kube.get.return_value = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'keycloak-workflow-admin', 'labels': {'app.kubernetes.io/managed-by': 'keycloak-aap'}, 'annotations': {'keycloak.example.com/hostname': 'auth.example'}}, 'data': {'password': 'NEVER-RECORD'}}
        result = run.owned_automation_secrets()
        self.assertNotIn('data', result[0][1])
        kube.get.return_value['metadata']['annotations']['keycloak.example.com/hostname'] = 'another.example'
        with self.assertRaises(deploy.Failure): run.owned_automation_secrets()

    def test_ca_selection_matches_certificate_not_its_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp);cert = folder / 'root.pem';key = folder / 'root.key'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=fixture-root', '-keyout', str(key), '-out', str(cert)], capture_output=True, check=True)
            api = Mock();api.request.side_effect = [(200, {'rows': [{'uuid': 'ca-one'}]}), (200, {'ca': {'crt_payload': cert.read_text(), 'refid': 'trusted-reference', 'descr': 'irrelevant'}})]
            self.assertEqual(edge_automatic.discover_ca(api, str(cert), folder), 'trusted-reference')
            self.assertTrue(all(c.args[0] == 'GET' for c in api.request.call_args_list))

    def test_other_apps_wildcard_listener_is_not_overwritten(self):
        api = Mock();api.request.side_effect = [(200, {'rows': [{'uuid': 'other', 'name': 'other-application'}]}), (200, {'frontend': {'enabled': '1', 'bind': '[::]:443'}})]
        with self.assertRaises(deploy.Failure): edge_automatic.check_listener_collision(api, '2001:db8:10:7::70')
        self.assertTrue(all(c.args[0] == 'GET' for c in api.request.call_args_list))

    def test_aap_revision_works_without_git_and_requires_a_full_sha(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(deploy, 'ROOT', Path(tmp)):
            with patch.dict('os.environ', {'PROJECT_REVISION': 'b' * 40}):
                self.assertEqual(deploy.execution_revision(), 'b' * 40)
            with patch.dict('os.environ', {'PROJECT_REVISION': 'main'}):
                with self.assertRaises(deploy.Failure): deploy.execution_revision()
            with patch.dict('os.environ', {}, clear=True):
                with self.assertRaises(deploy.Failure): deploy.execution_revision()


class HostnameTests(unittest.TestCase):
    def test_normalization_and_unsafe_input_rejection(self):
        self.assertEqual(discovery.public_hostname('Login.Example.com.'), 'login.example.com')
        for host in ('https://login.example', '*.example.com', 'a..example', 'login.example:443', 'login.example/path', 'login.example\nacl bad', '-bad.example', 'bad-.example', 'a' * 64 + '.example', '127.0.0.1', 'localhost', 'a.example..'):
            with self.subTest(host=host), self.assertRaises(deploy.Failure): discovery.public_hostname(host)

    def test_reconcile_cannot_rename_existing_deployment(self):
        old = {'environment': 'active-active', 'public_host': 'login.example.com', 'public_vip': '2001:db8:10:7::70', 'sites': [{'name': 'one'}, {'name': 'two'}]}
        discovery.deployment_identity(old, copy.deepcopy(old))
        with self.assertRaises(deploy.Failure): discovery.deployment_identity(old, {**old, 'public_host': 'other.example.com'})

    def test_removal_uses_recorded_custom_hostname(self):
        import automatic_remove
        state = {'profile': {'automatic': True, 'environment': 'active-active', 'namespace': 'keycloak', 'resource_prefix': instances.prefix('keycloak'), 'lock_name': instances.prefix('keycloak') + '-lock', 'public_host': 'login.custom.example', 'sites': [{'name': 'a'}, {'name': 'b'}]}, 'documents': {'applications': []}, 'revision': 'a' * 40, 'platform': {'state_configmap': 'state'}}
        hub = Mock();hub.get.return_value = {'metadata': {'labels': {instances.OWNER_LABEL: instances.prefix('keycloak')}}, 'data': {'state.json': json.dumps(state)}}
        _, loaded = automatic_remove.load_state(hub, {'state_configmap': 'state', 'environment': 'active-active', 'default_public_host': 'auth.example.com'})
        self.assertEqual(loaded['profile']['public_host'], 'login.custom.example')
