import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import automatic
import automatic_remove
import deploy
import instances
import remove


def profile(ns, vip='2001:db8:10:7::71'):
    p = json.loads((ROOT / 'automation/aap/profiles/active-active.json').read_text())
    platform = json.loads((ROOT / 'automation/aap/profiles/platform.json').read_text())
    instances.configure(p, platform, ns, 'databases/' + ns)
    p.update(automatic=True, public_host=ns + '.example.com', public_vip=vip)
    return p, platform


def record(p, platform):
    return {'metadata': {'name': platform['state_configmap'], 'labels': {instances.OWNER_LABEL: p['resource_prefix']}}, 'data': {'state.json': json.dumps({'profile': p, 'platform': platform, 'documents': {'applications': []}, 'revision': 'a'*40})}}


class InstanceTests(unittest.TestCase):
    def test_namespace_and_vault_path_are_validated(self):
        for ns in ('default', 'openshift-gitops', 'kube-system', 'Upper', '../tenant', 'a'*64, 'bad.name', '-bad'):
            with self.subTest(ns=ns), self.assertRaises(deploy.Failure): instances.namespace(ns)
        for path in ('kv/global/db', 'data/global/db', '../db', '/global/db', 'global//db', 'https://vault/secret', 'db\nvalue'):
            with self.subTest(path=path), self.assertRaises(deploy.Failure): instances.database_path(path)
        self.assertEqual(instances.database_path('global/tenant-a/postgresql'), 'global/tenant-a/postgresql')

    def test_two_namespaces_have_disjoint_application_and_edge_names(self):
        a, pa = profile('tenant-a');b, pb = profile('tenant-b', '2001:db8:10:7::72')
        self.assertNotEqual(pa['state_configmap'], pb['state_configmap'])
        self.assertNotEqual(a['lock_name'], b['lock_name'])
        self.assertFalse(set(instances.edge_names(a['resource_prefix']).values()) & set(instances.edge_names(b['resource_prefix']).values()))
        def apps(p):
            run = deploy.Deployment.__new__(deploy.Deployment);run.p=p
            return {run.application_name(c, o) for c in ('operator', 'prerequisites', 'keycloak') for o in ('cluster-a', 'cluster-b')}
        self.assertFalse(apps(a) & apps(b))
        long, _ = profile('x'*63)
        self.assertTrue(all(len(n) <= 63 for n in apps(long)))
        self.assertNotEqual(instances.prefix('x'*62+'a'), instances.prefix('x'*62+'b'))

    def test_claims_reject_reused_vip_hostname_vault_path_and_database(self):
        a, pa = profile('one');b, pb = profile('two', '2001:db8:10:7::72')
        existing = [record(a, pa)]
        instances.check_claims(existing, b, pb['state_configmap'])
        for key in ('public_host', 'public_vip', 'database_path'):
            with self.subTest(key=key), self.assertRaises(deploy.Failure):
                instances.check_claims(existing, {**b, key: a[key]}, pb['state_configmap'])
        a['database_signatures'] = ['shared'];b['database_signatures'] = ['shared']
        with self.assertRaises(deploy.Failure): instances.check_claims([record(a, pa)], b, pb['state_configmap'])

    def test_database_aliases_and_separate_database_names(self):
        addr = [(None, None, None, None, ('fd00::10', 5432, 0, 0))]
        with patch.object(instances.socket, 'getaddrinfo', return_value=addr):
            a = instances.database_signatures('db-one.example', 'keycloak_one')
            alias = instances.database_signatures('db-alias.example', 'keycloak_one')
            b = instances.database_signatures('db-one.example', 'keycloak_two')
        self.assertTrue(set(a) & set(alias))
        self.assertFalse(set(a) & set(b))
        self.assertNotIn('keycloak_one', json.dumps(a))

    def test_removal_cannot_select_another_namespaces_record(self):
        a, pa = profile('one');hub = Mock();hub.get.return_value = record(a, pa)
        automatic_remove.load_state(hub, pa, 'one')
        with self.assertRaises(deploy.Failure): automatic_remove.load_state(hub, pa, 'two')

    def test_removal_rejects_another_instances_haproxy_objects_before_api_calls(self):
        a, _ = profile('one');b, _ = profile('two')
        api = Mock()
        with self.assertRaises(deploy.Failure):
            remove.edge_candidates(api, [{'type': 'server', 'name': b['resource_prefix']+'-site-a', 'fields': {}}], a['resource_prefix'])
        api.request.assert_not_called()

    def test_shared_lock_is_uid_scoped_and_released_on_failure(self):
        hub = Mock();hub.get.return_value={'metadata': {'uid': 'lock-identity'}}
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with instances.shared_lock(hub, 'openshift-gitops', 'run'):
                raise RuntimeError('fixture')
        self.assertEqual(hub.command.call_args.args[1]['preconditions'], {'uid': 'lock-identity'})

    def test_shared_acm_registration_is_additive(self):
        p, _ = profile('one');selector={'key': 'name', 'operator': 'In', 'values': ['old-a','old-b']}
        current={'metadata': {'labels': {'app.kubernetes.io/managed-by': 'keycloak-aap'}}, 'spec': {'clusterSets': ['old-set'], 'predicates': [{'requiredClusterSelector': {'labelSelector': {'matchExpressions': [selector]}}}]}}
        desired=copy.deepcopy(current);desired['kind']='Placement'
        desired['spec']['clusterSets']=['new-set'];desired['spec']['predicates'][0]['requiredClusterSelector']['labelSelector']['matchExpressions'][0]['values']=['old-b','new-c']
        bridge={'kind':'GitOpsCluster','metadata':{},'spec':{'argoServer':{'argoNamespace':'openshift-gitops'},'placementRef':{'name':p['placement_name']}}}
        hub=Mock();hub.get.side_effect=[None,current]
        instances.merge_registration(hub,'openshift-gitops',[desired,bridge],p['placement_name'])
        self.assertEqual(desired['spec']['numberOfClusters'],3)
        self.assertEqual(desired['spec']['clusterSets'],['new-set','old-set'])
        self.assertEqual(desired['spec']['predicates'][0]['requiredClusterSelector']['labelSelector']['matchExpressions'][0]['values'], ['new-c','old-a','old-b'])
        hub.command.assert_not_called()

        foreign=copy.deepcopy(bridge);foreign['metadata']['labels']={}
        hub.get.side_effect=[foreign]
        with self.assertRaises(deploy.Failure):
            instances.merge_registration(hub,'openshift-gitops',[desired,bridge],p['placement_name'])
        moved=copy.deepcopy(bridge);moved['spec']['argoServer']['argoNamespace']='other'
        hub.get.side_effect=[moved]
        with self.assertRaises(deploy.Failure):
            instances.merge_registration(hub,'openshift-gitops',[desired,bridge],p['placement_name'])

    def test_overlapping_operator_scope_fails_before_install(self):
        group={'metadata': {'namespace':'other','annotations':{'olm.providedAPIs':'Keycloak.v2beta1.k8s.keycloak.org'}}, 'spec':{'targetNamespaces':['other']}}
        instances.operator_scope([group],'one')
        group['spec']['targetNamespaces']=['one']
        with self.assertRaises(deploy.Failure):instances.operator_scope([group],'one')
        group['spec']={}
        with self.assertRaises(deploy.Failure):instances.operator_scope([group],'one')
