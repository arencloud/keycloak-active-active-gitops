"""Exercise the actual provisioning playbook against a local HTTPS API fixture."""
import http.server
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('ansible-playbook') and shutil.which('openssl'), 'Requires ansible-playbook and openssl')
class ControllerTests(unittest.TestCase):
    def test_graph_and_survey_are_idempotent_and_never_launched(self):
        state = {'job_templates': {}, 'workflow_job_templates': {}, 'workflow_job_template_nodes': {}}
        requests = []
        counter = [1]
        class API(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def respond(self, code, body=None):
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                if body is not None: self.wfile.write(json.dumps(body).encode())
            def handle_request(self):
                requests.append((self.command, self.path))
                url = urllib.parse.urlsplit(self.path)
                path = url.path.removeprefix('/api/controller/v2/').strip('/').split('/')
                query = urllib.parse.parse_qs(url.query)
                collection = path[0]
                store = state[collection]
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                # Association endpoints require a JSON integer, unlike serializer FK fields.
                if 'id' in body and type(body['id']) is not int:
                    self.respond(400, {'id': 'Expected a JSON integer'});return
                for field in ['id', 'organization', 'project', 'inventory', 'execution_environment', 'unified_job_template']:
                    if field in body: body[field] = int(body[field])
                if len(path) == 1:
                    if self.command == 'GET':
                        rows = [v for v in store.values() if all(str(v.get(k)) == values[0] for k, values in query.items() if k != 'page_size')]
                        self.respond(200, {'count': len(rows), 'results': rows, 'next': None});return
                    number = counter[0];counter[0] += 1
                    store[number] = {'id': number, **body}
                    self.respond(201, store[number]);return
                obj = store[int(path[1])]
                if len(path) == 2:
                    obj.update(body);self.respond(200, obj);return
                relation = path[2]
                if relation in ('credentials', 'instance_groups'):
                    ids = obj.setdefault(relation, [])
                    if self.command == 'GET':
                        self.respond(200, {'count': len(ids), 'results': [{'id': n} for n in ids], 'next': None})
                    else:
                        if body['id'] in ids:
                            self.respond(400, {'error': 'Cannot assign multiple credentials of the same type.'});return
                        ids.append(body['id'])
                        self.respond(204)
                elif relation == 'survey_spec':
                    obj['survey'] = body;self.respond(200, {})
                elif relation == 'workflow_nodes':
                    if self.command == 'GET':
                        rows = [n for n in state['workflow_job_template_nodes'].values() if n['workflow_job_template'] == obj['id'] and all(str(n.get(k)) == v[0] for k, v in query.items() if k != 'page_size')]
                        self.respond(200, {'count': len(rows), 'results': rows, 'next': None})
                    else:
                        number = counter[0];counter[0] += 1
                        node = {'id': number, 'workflow_job_template': obj['id'], 'success_nodes': [], 'failure_nodes': [], 'always_nodes': [], **body}
                        state['workflow_job_template_nodes'][number] = node
                        self.respond(201, node)
                elif relation == 'success_nodes':
                    if body['id'] not in obj['success_nodes']: obj['success_nodes'].append(body['id'])
                    self.respond(204)
                else: self.respond(404, {})
            do_GET = handle_request
            do_POST = handle_request
            do_PATCH = handle_request
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cert, key = tmp / 'cert.pem', tmp / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost', '-keyout', str(key), '-out', str(cert)], capture_output=True, check=True)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), API)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            fixture = tmp / 'repository' / 'automation' / 'aap'
            shutil.copytree(ROOT / 'automation/aap', fixture)
            choice_script = tmp / 'repository' / 'scripts' / 'survey_choices.py'
            choice_script.parent.mkdir()
            choice_script.write_text('import json; print(json.dumps(["cluster-a", "cluster-b", "new-cluster"]))')
            for playbook in (fixture / 'playbooks').rglob('*.yml'):
                playbook.write_text(playbook.read_text().replace('no_log: true', 'no_log: false'))
            settings = {'configure_controller': True, 'controller_refresh_cluster_choices': True, 'controller_ca_file': str(cert), 'controller_api_base': 'https://localhost:' + str(server.server_port) + '/api/controller/v2', 'controller_organization_id': 1, 'controller_project_id': 2, 'controller_inventory_id': 3, 'controller_execution_environment_id': 4, 'controller_instance_group_ids': [22], 'controller_preflight_credential_ids': [11], 'controller_deploy_credential_ids': [12]}
            variables = tmp / 'settings.json';variables.write_text(json.dumps(settings))
            env = os.environ.copy();env['CONTROLLER_OAUTH_TOKEN'] = 'local-fixture-only';env['SSL_CERT_FILE'] = str(cert)
            env['ANSIBLE_LOCAL_TEMP'] = str(tmp / 'ansible-tmp')
            try:
                for play in ('configure-controller.yml', 'configure-removal-controller.yml'):
                    for _ in range(2):
                        result = subprocess.run(['ansible-playbook', '-i', 'localhost,', str(fixture / 'playbooks' / play), '-e', '@' + str(variables)], env=env, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.shutdown();server.server_close();thread.join()
        self.assertEqual(len(state['job_templates']), 6)
        for job in state['job_templates'].values():
            self.assertEqual(job['instance_groups'], [22])
            self.assertTrue(job['prevent_instance_group_fallback'])
        self.assertEqual(len(state['workflow_job_templates']), 2)
        nodes = {n['identifier']: n for n in state['workflow_job_template_nodes'].values()}
        self.assertEqual(nodes['preflight']['success_nodes'], [nodes['plan']['id']])
        self.assertEqual(nodes['plan']['success_nodes'], [nodes['execute']['id']])
        self.assertEqual(nodes['execute']['success_nodes'], [])
        self.assertFalse(any('/launch/' in path for _, path in requests))
        workflow = next(iter(state['workflow_job_templates'].values()))
        self.assertTrue(workflow['survey_enabled'])
        self.assertFalse(workflow['allow_simultaneous'])
        self.assertEqual([x['variable'] for x in workflow['survey']['spec']], ['kc_cluster_a', 'kc_cluster_b', 'kc_public_host', 'kc_public_vip', 'kc_namespace', 'kc_database_path', 'kc_database_tls'])

        self.assertIn('new-cluster', workflow['survey']['spec'][0]['choices'])
        self.assertIn('new-cluster', workflow['survey']['spec'][1]['choices'])
        self.assertEqual(workflow['survey']['spec'][2]['default'], '')
